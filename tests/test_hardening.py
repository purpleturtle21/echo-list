"""Hardening tests — large playlists, cross-playlist sync, corrupt data,
workspace locking, atomic writes, sync journal, and edge cases."""

import builtins
import errno
import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from mutagen.flac import FLAC

from echolist.manager import PlaylistManager, WorkspaceLockError
import echolist.safe_write as safe_write
from echolist.safe_write import SafeWriter, atomic_write_text
from echolist.config import (
    load_backup, list_backups, save_backup, load_playlist_snapshot,
    delete_all_backups, list_all_backup_pids,
)
from echolist.journal import SyncJournal
from conftest import _make_flac, assert_originals_untouched


# ── Large playlist (100+ tracks, 3-digit padding) ──

@pytest.fixture
def large_source(tmp_path):
    """Create 105 FLAC files to test 3-digit padding transition."""
    lib = tmp_path / "library"
    for i in range(1, 106):
        _make_flac(
            lib / f"Artist{i}" / "Album" / f"{i:02d} Track {i}.flac",
            f"Artist{i}", f"Track {i}",
        )
    return lib


def test_large_playlist_three_digit_padding(large_source, tmp_path):
    """Playlists with >99 tracks use 3-digit zero-padded filenames throughout."""
    dest = tmp_path / "card"
    dest.mkdir()
    mgr = PlaylistManager.init(large_source, dest)

    pid = mgr.create_playlist("Big")
    for f in sorted(large_source.rglob("*.flac")):
        mgr.add_track(pid, f)

    tracks = mgr.store.playlists[pid]["tracks"]
    assert len(tracks) == 105
    # Crossing 100 re-pads the earlier tracks so names sort in playlist order
    assert tracks[0]["copy_name"].startswith("001 - ")
    assert tracks[98]["copy_name"].startswith("099 - ")
    assert tracks[99]["copy_name"].startswith("100 - ")
    assert tracks[104]["copy_name"].startswith("105 - ")

    for t in tracks:
        assert (mgr.writer.root / "Big" / t["copy_name"]).exists()

    mgr.release_lock()


def test_large_playlist_remove_renumbers_correctly(large_source, tmp_path):
    """Removing from a large playlist renumbers with correct padding."""
    dest = tmp_path / "card"
    dest.mkdir()
    mgr = PlaylistManager.init(large_source, dest)

    pid = mgr.create_playlist("Big")
    for f in sorted(large_source.rglob("*.flac"))[:105]:
        mgr.add_track(pid, f)

    mgr.remove_track(pid, 1)
    tracks = mgr.store.playlists[pid]["tracks"]
    assert len(tracks) == 104
    assert tracks[0]["copy_name"].startswith("001 - ")
    assert tracks[103]["copy_name"].startswith("104 - ")

    mgr.remove_track(pid, 100)
    tracks = mgr.store.playlists[pid]["tracks"]
    assert len(tracks) == 103
    assert tracks[0]["copy_name"].startswith("001 - ")

    mgr.release_lock()


# ── Cross-playlist sync ──

def test_cross_playlist_operations(manager, source):
    """Add to one playlist and remove from another in the same session."""
    pid1 = manager.create_playlist("Alpha")
    pid2 = manager.create_playlist("Beta")

    src1 = source / "ArtistA" / "Album1" / "01 Song One.flac"
    src2 = source / "ArtistB" / "Album2" / "03 Song Two.flac"
    src3 = source / "ArtistC" / "Album3" / "05 Song Three.flac"

    manager.add_track(pid1, src1)
    manager.add_track(pid1, src2)
    manager.add_track(pid2, src3)

    manager.remove_track(pid1, 1)

    assert len(manager.store.playlists[pid1]["tracks"]) == 1
    assert len(manager.store.playlists[pid2]["tracks"]) == 1
    assert manager.store.playlists[pid1]["tracks"][0]["index"] == 1


def test_same_track_in_multiple_playlists(manager, source):
    """The same source file can be added to multiple playlists."""
    pid1 = manager.create_playlist("Alpha")
    pid2 = manager.create_playlist("Beta")

    src = source / "ArtistA" / "Album1" / "01 Song One.flac"
    rel1 = manager.add_track(pid1, src)
    rel2 = manager.add_track(pid2, src)

    assert (manager.writer.root / rel1).exists()
    assert (manager.writer.root / rel2).exists()
    assert rel1 != rel2

    f1 = FLAC(manager.writer.root / rel1)
    f2 = FLAC(manager.writer.root / rel2)
    assert f1["ALBUM"] == ["Alpha"]
    assert f2["ALBUM"] == ["Beta"]


# ── Corrupt backup data ──

def test_corrupt_backup_json_returns_none(manager, source):
    """A backup file with invalid JSON returns None instead of crashing."""
    pid = manager.create_playlist("Test")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    manager.backup_playlist_metadata(pid, timestamp="20260101_120000")
    backups = list_backups(manager.writer.root, pid)
    assert len(backups) == 1

    backups[0]["path"].write_text("NOT VALID JSON {{{", encoding="utf-8")
    result = load_backup(manager.writer.root, pid, "20260101_120000")
    assert result is None


def test_backup_missing_tracks_key_returns_none(manager, source):
    """A backup with valid JSON but missing 'tracks' key returns None."""
    pid = manager.create_playlist("Test")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    manager.backup_playlist_metadata(pid, timestamp="20260101_120000")
    backups = list_backups(manager.writer.root, pid)

    backups[0]["path"].write_text('{"playlist_name": "Test"}', encoding="utf-8")
    result = load_backup(manager.writer.root, pid, "20260101_120000")
    assert result is None


def test_backup_tracks_not_list_returns_none(manager, source):
    """A backup where 'tracks' is not a list returns None."""
    pid = manager.create_playlist("Test")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    manager.backup_playlist_metadata(pid, timestamp="20260101_120000")
    backups = list_backups(manager.writer.root, pid)

    backups[0]["path"].write_text('{"tracks": "not a list"}', encoding="utf-8")
    result = load_backup(manager.writer.root, pid, "20260101_120000")
    assert result is None


def test_corrupt_snapshot_returns_none(manager, source, dest):
    """A corrupt snapshot.json returns None."""
    pid = manager.create_playlist("Test")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    snap_path = manager.save_snapshot()

    snap_path.write_text("BROKEN JSON", encoding="utf-8")
    result = load_playlist_snapshot(manager.writer.root)
    assert result is None


def test_snapshot_missing_keys_returns_none(manager, source, dest):
    """A snapshot missing required keys returns None."""
    pid = manager.create_playlist("Test")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    snap_path = manager.save_snapshot()

    snap_path.write_text('{"config": {}}', encoding="utf-8")
    result = load_playlist_snapshot(manager.writer.root)
    assert result is None


# ── Restore robustness ──

def test_restore_to_point_with_corrupt_backup_raises(manager, source):
    """Restoring from a backup that became corrupt raises KeyError."""
    pid = manager.create_playlist("Test")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid, timestamp="20260101_120000")

    backups = list_backups(manager.writer.root, pid)
    backups[0]["path"].write_text("CORRUPT", encoding="utf-8")

    with pytest.raises(KeyError, match="backup.*not found"):
        manager.restore_playlist_to_point(pid, "20260101_120000")


def test_restore_metadata_with_corrupt_backup(manager, source):
    """Restoring metadata when backup is corrupt returns 0."""
    pid = manager.create_playlist("Test")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid, timestamp="20260101_120000")

    backups = list_backups(manager.writer.root, pid)
    backups[0]["path"].write_text("NOPE", encoding="utf-8")

    restored = manager.restore_playlist_metadata(pid, "20260101_120000")
    assert restored == 0


# ── Atomic write safety ──

def test_atomic_write_text_creates_file(tmp_path):
    """atomic_write_text creates the target file atomically."""
    p = tmp_path / "subdir" / "test.json"
    atomic_write_text(p, '{"hello": "world"}')
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {"hello": "world"}


def test_atomic_write_text_overwrites_safely(tmp_path, monkeypatch):
    """If the final rename fails, the old file is untouched and no temp file
    is left behind."""
    p = tmp_path / "data.json"
    atomic_write_text(p, '{"version": 1}')

    def replace_fails(a, b):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(safe_write.os, "replace", replace_fails)
    with pytest.raises(OSError):
        atomic_write_text(p, '{"version": 2}')

    assert json.loads(p.read_text(encoding="utf-8")) == {"version": 1}
    assert sorted(f.name for f in tmp_path.iterdir()) == ["data.json"]


def test_atomic_write_no_partial_on_disk(tmp_path, monkeypatch):
    """A write that stops part-way (disk full) never reaches the target."""
    p = tmp_path / "data.json"
    atomic_write_text(p, '{"original": true}')
    real_write = os.write
    calls = []

    def short_then_full(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(fd, bytes(data[:3]))  # short write
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(safe_write.os, "write", short_then_full)
    with pytest.raises(OSError) as e:
        atomic_write_text(p, '{"updated": true, "pad": "' + "x" * 100 + '"}')

    assert e.value.errno == errno.ENOSPC
    assert len(calls) == 2  # the short write was retried, not taken as done
    assert json.loads(p.read_text(encoding="utf-8")) == {"original": True}
    assert sorted(f.name for f in tmp_path.iterdir()) == ["data.json"]


# ── Workspace lock ──

def test_workspace_lock_prevents_double_open(tmp_path):
    """Opening the same workspace twice raises WorkspaceLockError."""
    src = tmp_path / "lib"
    _make_flac(src / "A" / "B" / "01.flac", "A", "T")
    dest = tmp_path / "card"
    dest.mkdir()

    mgr1 = PlaylistManager.init(src, dest)
    mgr1.create_playlist("Init")
    with pytest.raises(WorkspaceLockError):
        PlaylistManager.open(dest)

    mgr1.release_lock()

    mgr2 = PlaylistManager.open(dest)
    mgr2.release_lock()


def test_workspace_lock_released_on_release(tmp_path):
    """After release_lock, another instance can open."""
    src = tmp_path / "lib"
    _make_flac(src / "A" / "B" / "01.flac", "A", "T")
    dest = tmp_path / "card"
    dest.mkdir()

    mgr = PlaylistManager.init(src, dest)
    mgr.create_playlist("Init")
    mgr.release_lock()

    mgr2 = PlaylistManager.open(dest)
    mgr2.release_lock()


# ── Sync journal ──

def test_journal_begin_and_complete(tmp_path, monkeypatch):
    """Journal records actions and cleans up on complete."""
    monkeypatch.setattr("echolist.journal.JOURNAL_FILE", tmp_path / "journal.json")

    removes = [{"pid": "test", "index": 1, "copy_name": "01 - x.flac"}]
    adds = [{"pid": "test", "src": "/path/to/file.flac", "title": "Song"}]
    journal = SyncJournal.begin(removes, adds, {})

    assert len(journal.actions) == 2
    assert all(a["status"] == "pending" for a in journal.actions)
    assert (tmp_path / "journal.json").exists()

    journal.mark_done(0)
    assert journal.actions[0]["status"] == "done"

    journal.mark_done(1)
    journal.complete()
    assert not (tmp_path / "journal.json").exists()


def test_journal_load_incomplete(tmp_path, monkeypatch):
    """An incomplete journal is detected on next startup."""
    monkeypatch.setattr("echolist.journal.JOURNAL_FILE", tmp_path / "journal.json")

    removes = [{"pid": "test", "index": 1, "copy_name": "01 - x.flac"}]
    journal = SyncJournal.begin(removes, [], {})
    journal.mark_done(0)

    # Simulate a new action that wasn't completed
    journal.actions.append({"op": "add", "pid": "test", "src": "/x.flac", "status": "pending"})
    journal._save()

    loaded = SyncJournal.load_incomplete()
    assert loaded is not None
    assert len(loaded.pending_actions) == 1

    SyncJournal.discard()


def test_journal_load_complete_returns_none(tmp_path, monkeypatch):
    """A fully completed journal returns None on load."""
    monkeypatch.setattr("echolist.journal.JOURNAL_FILE", tmp_path / "journal.json")

    journal = SyncJournal.begin([], [{"pid": "test", "src": "/x.flac"}], {})
    journal.mark_done(0)
    journal._save()

    loaded = SyncJournal.load_incomplete()
    assert loaded is None


def test_journal_corrupt_json_discarded(tmp_path, monkeypatch):
    """A corrupt journal file is discarded gracefully."""
    jf = tmp_path / "journal.json"
    monkeypatch.setattr("echolist.journal.JOURNAL_FILE", jf)

    jf.write_text("NOT JSON", encoding="utf-8")
    loaded = SyncJournal.load_incomplete()
    assert loaded is None
    assert not jf.exists()


def test_journal_no_file_returns_none(tmp_path, monkeypatch):
    """No journal file means no incomplete sync."""
    monkeypatch.setattr("echolist.journal.JOURNAL_FILE", tmp_path / "nonexistent.json")
    assert SyncJournal.load_incomplete() is None


def test_journal_with_reorders(tmp_path, monkeypatch):
    """Journal records reorder operations."""
    monkeypatch.setattr("echolist.journal.JOURNAL_FILE", tmp_path / "journal.json")

    journal = SyncJournal.begin([], [], {"playlist_a": [{"key": "c:1"}]})
    assert len(journal.actions) == 1
    assert journal.actions[0]["op"] == "reorder"
    assert journal.actions[0]["pid"] == "playlist_a"
    journal.complete()


# ── Rescan edge cases ──

def test_rescan_file_without_index_separator(manager, source):
    """Files without ' - ' separator are handled by rescan."""
    pid = manager.create_playlist("Mix")
    folder = manager.writer.root / "Mix"
    folder.mkdir(parents=True, exist_ok=True)

    src = source / "ArtistA" / "Album1" / "01 Song One.flac"
    shutil.copy2(src, folder / "SomeTrack.flac")

    changed = manager.rescan_playlist(pid)
    assert changed
    tracks = manager.store.playlists[pid]["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["copy_name"] == "SomeTrack.flac"


# ── Renumber robustness ──

def test_renumber_preserves_store_on_rename_failure(manager, source, monkeypatch):
    """A rename that fails part-way through a renumber leaves a Store whose
    every name is a file on the card holding that entry's audio."""
    pid = manager.create_playlist("Mix")
    for p in ("ArtistA/Album1/01 Song One.flac", "ArtistB/Album2/03 Song Two.flac",
              "ArtistC/Album3/05 Song Three.flac"):
        manager.add_track(pid, source / p)

    real_rename = SafeWriter.rename
    calls = []

    def flaky_rename(self, old, new):
        calls.append((old, new))
        if len(calls) == 3:  # both tracks parked; the first publish fails
            raise OSError(errno.EIO, "card I/O error")
        return real_rename(self, old, new)

    monkeypatch.setattr(SafeWriter, "rename", flaky_rename)
    with pytest.raises(OSError):
        manager.remove_track(pid, 1)
    assert len(calls) == 3

    stored = json.loads((manager.writer.root / ".echolist" / "playlists.json")
                        .read_text(encoding="utf-8"))["playlists"][pid]["tracks"]
    folder = manager.writer.root / "Mix"
    assert len(stored) == 2
    for t in stored:
        assert (folder / t["copy_name"]).exists(), t["copy_name"]
        assert FLAC(folder / t["copy_name"])["ECHOLIST_SRC"][0] == t["src_path"]
    on_disk = {f.name for f in folder.iterdir() if f.suffix == ".flac"}
    assert on_disk == {t["copy_name"] for t in stored}


# ── Source file disappears before add ──

def test_add_track_missing_source_raises(manager, source):
    """Adding a track whose source file doesn't exist raises FileNotFoundError."""
    pid = manager.create_playlist("Test")
    with pytest.raises(FileNotFoundError):
        manager.add_track(pid, source / "nonexistent.flac")


def test_add_track_source_deleted_after_resolve(manager, source, monkeypatch):
    """The source vanishes after add_track checked it but before SafeWriter
    opens it: nothing is published, no partial copy is left, the Store is
    unchanged."""
    pid = manager.create_playlist("Test")
    src = source / "ArtistA" / "Album1" / "01 Song One.flac"
    real_open = builtins.open

    def open_after_delete(file, mode="r", *a, **kw):
        if Path(file) == src.resolve() and "r" in mode:
            src.unlink()  # deleted between the check and the copy
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(safe_write, "open", open_after_delete, raising=False)
    with pytest.raises(FileNotFoundError):
        manager.add_track(pid, src)

    assert not src.exists()
    assert list((manager.writer.root / "Test").iterdir()) == []
    assert manager.store.playlists[pid]["tracks"] == []


# ── Backup edge cases ──

def test_backup_empty_playlist_returns_none(manager):
    """Backing up a playlist with no tracks returns None."""
    pid = manager.create_playlist("Empty")
    result = manager.backup_playlist_metadata(pid)
    assert result is None


def test_backup_nonexistent_playlist_returns_none(manager):
    """Backing up a nonexistent playlist returns None."""
    result = manager.backup_playlist_metadata("does_not_exist")
    assert result is None


def test_originals_untouched_after_full_cycle(manager, source, hashes):
    """Source files are never modified through create, add, remove, fix, restore cycle."""
    pid = manager.create_playlist("Full")
    for f in sorted(source.rglob("*.flac")):
        manager.add_track(pid, f)

    manager.backup_playlist_metadata(pid, timestamp="checkpoint")

    manager.remove_track(pid, 1)
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    # Corrupt metadata on a copy and fix it
    track = manager.store.playlists[pid]["tracks"][0]
    track_path = manager.writer.root / "Full" / track["copy_name"]
    f = FLAC(track_path)
    f["ALBUMARTIST"] = "Wrong"
    f.save()

    manager.fix_playlist_metadata(pid)
    manager.restore_playlist_to_point(pid, "checkpoint")

    assert_originals_untouched(source, hashes)


# ── Multiple restore points ──

# ── Backup interval ──

def test_backup_interval_respects_config(manager, source):
    """Backups only happen every N syncs based on backup_interval."""
    manager.config.backup_interval = 3
    manager.config._sync_count = 0
    manager.config.save(manager.writer)

    pid = manager.create_playlist("Test")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    assert manager.config.should_backup()  # sync 0: should backup
    manager.config.increment_sync(manager.writer)
    assert not manager.config.should_backup()  # sync 1: skip
    manager.config.increment_sync(manager.writer)
    assert not manager.config.should_backup()  # sync 2: skip
    manager.config.increment_sync(manager.writer)
    assert manager.config.should_backup()  # sync 3: should backup


def test_backup_interval_default_is_five(tmp_path):
    """Default backup interval is 5."""
    src = tmp_path / "lib"
    _make_flac(src / "A" / "B" / "01.flac", "A", "T")
    dest = tmp_path / "card"
    dest.mkdir()
    mgr = PlaylistManager.init(src, dest)
    assert mgr.config.backup_interval == 5
    mgr.release_lock()


def test_custom_playlist_folder(tmp_path):
    """Workspace can use a custom root folder name."""
    src = tmp_path / "lib"
    _make_flac(src / "A" / "B" / "01.flac", "A", "T")
    dest = tmp_path / "card"
    dest.mkdir()
    mgr = PlaylistManager.init(src, dest, playlist_folder="Music")
    assert mgr.writer.root == (dest / "Music").resolve()
    assert mgr.config.playlist_folder == "Music"

    pid = mgr.create_playlist("Test")
    mgr.add_track(pid, src / "A" / "B" / "01.flac")
    assert (dest / "Music" / "Test").is_dir()
    mgr.release_lock()

    mgr2 = PlaylistManager.open(dest, playlist_folder="Music")
    assert "test" in mgr2.store.playlists
    mgr2.release_lock()


def test_multiple_restore_points_independent(manager, source):
    """Multiple restore points can be created and restored independently."""
    pid = manager.create_playlist("Multi")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid, timestamp="point_1")

    manager.add_track(pid, source / "ArtistB" / "Album2" / "03 Song Two.flac")
    manager.backup_playlist_metadata(pid, timestamp="point_2")

    manager.add_track(pid, source / "ArtistC" / "Album3" / "05 Song Three.flac")

    # Restore to point_1 — should have 1 track, remove 2
    result = manager.restore_playlist_to_point(pid, "point_1")
    assert result["restored"] == 1
    assert result["removed"] == 2
    assert len(manager.store.playlists[pid]["tracks"]) == 1


# ── Permanent deletion of backups ──

class TestPermanentDelete:
    """Verify delete_all_backups is safe, complete, and isolated."""

    def test_deletes_all_backups_for_target(self, tmp_path):
        """All backup files for the target pid are removed."""
        src = tmp_path / "lib"
        _make_flac(src / "A" / "B" / "01.flac", "A", "Song")
        dest = tmp_path / "card"
        dest.mkdir()
        mgr = PlaylistManager.init(src, dest)
        mgr.create_playlist("Target")
        mgr.add_track("target", src / "A" / "B" / "01.flac")

        # Use save_backup directly to create distinct backups
        data = {"playlist_name": "Target", "folder": "Target", "tracks": [{"x": 1}]}
        save_backup(mgr.writer.root, "target", "point_1", data)
        save_backup(mgr.writer.root, "target", "point_2", {**data, "tracks": [{"x": 2}]})

        assert len(list_backups(mgr.writer.root, "target")) == 2

        delete_all_backups(mgr.writer.root, "target")

        assert list_backups(mgr.writer.root, "target") == []
        assert "target" not in list_all_backup_pids(mgr.writer.root)
        mgr.release_lock()

    def test_other_playlists_untouched(self, tmp_path):
        """Deleting one playlist's backups must not affect any other playlist."""
        src = tmp_path / "lib"
        _make_flac(src / "A" / "B" / "01.flac", "A", "Song")
        _make_flac(src / "A" / "B" / "02.flac", "A", "Song2")
        dest = tmp_path / "card"
        dest.mkdir()
        mgr = PlaylistManager.init(src, dest)

        # Create distinct backups via save_backup
        keep_data_a = {"playlist_name": "Keep Me", "folder": "Keep Me", "tracks": [{"v": 1}]}
        keep_data_b = {"playlist_name": "Keep Me", "folder": "Keep Me", "tracks": [{"v": 2}]}
        save_backup(mgr.writer.root, "keep_me", "backup_a", keep_data_a)
        save_backup(mgr.writer.root, "keep_me", "backup_b", keep_data_b)

        del_data = {"playlist_name": "Delete Me", "folder": "Delete Me", "tracks": [{"v": 3}]}
        save_backup(mgr.writer.root, "delete_me", "backup_c", del_data)

        delete_all_backups(mgr.writer.root, "delete_me")

        # Target is gone
        assert list_backups(mgr.writer.root, "delete_me") == []
        # Other playlist's backups are intact
        keep_backups = list_backups(mgr.writer.root, "keep_me")
        assert len(keep_backups) == 2
        assert {b["timestamp"] for b in keep_backups} == {"backup_a", "backup_b"}
        # Data is still loadable
        data = load_backup(mgr.writer.root, "keep_me", "backup_a")
        assert data is not None
        assert data["playlist_name"] == "Keep Me"
        mgr.release_lock()

    def test_delete_nonexistent_is_noop(self, tmp_path):
        """Deleting backups for a pid that has none doesn't crash."""
        dest = tmp_path / "card"
        dest.mkdir()
        src = tmp_path / "lib"
        src.mkdir()
        mgr = PlaylistManager.init(src, dest)
        delete_all_backups(mgr.writer.root, "nonexistent_playlist")
        mgr.release_lock()

    def test_rejects_path_traversal(self, tmp_path):
        """Pid with path separators or '..' is rejected."""
        dest = tmp_path / "card"
        dest.mkdir()
        src = tmp_path / "lib"
        src.mkdir()
        mgr = PlaylistManager.init(src, dest)
        with pytest.raises(ValueError):
            delete_all_backups(mgr.writer.root, "../../../etc")
        with pytest.raises(ValueError):
            delete_all_backups(mgr.writer.root, "foo/bar")
        with pytest.raises(ValueError):
            delete_all_backups(mgr.writer.root, "foo\\bar")
        with pytest.raises(ValueError):
            delete_all_backups(mgr.writer.root, "")
        mgr.release_lock()

    def test_partial_delete_leaves_no_orphan_state(self, tmp_path):
        """Simulates interrupted deletion — remaining files are still valid."""
        src = tmp_path / "lib"
        src.mkdir()
        dest = tmp_path / "card"
        dest.mkdir()
        mgr = PlaylistManager.init(src, dest)

        base = {"playlist_name": "Partial", "folder": "Partial"}
        save_backup(mgr.writer.root, "partial", "t1", {**base, "tracks": [{"v": 1}]})
        save_backup(mgr.writer.root, "partial", "t2", {**base, "tracks": [{"v": 2}]})
        save_backup(mgr.writer.root, "partial", "t3", {**base, "tracks": [{"v": 3}]})

        # Manually delete one file to simulate yanked cable mid-delete
        backups = list_backups(mgr.writer.root, "partial")
        assert len(backups) == 3
        backups[0]["path"].unlink()

        # Remaining backups are still valid and loadable
        remaining = list_backups(mgr.writer.root, "partial")
        assert len(remaining) == 2
        for b in remaining:
            data = load_backup(mgr.writer.root, "partial", b["timestamp"])
            assert data is not None
            assert isinstance(data["tracks"], list)

        # Full delete still works after partial state
        delete_all_backups(mgr.writer.root, "partial")
        assert list_backups(mgr.writer.root, "partial") == []
        mgr.release_lock()

    def test_non_json_files_survive_delete(self, tmp_path):
        """Only .json files are removed — any other file type is left alone."""
        src = tmp_path / "lib"
        _make_flac(src / "A" / "B" / "01.flac", "A", "Song")
        dest = tmp_path / "card"
        dest.mkdir()
        mgr = PlaylistManager.init(src, dest)
        mgr.create_playlist("Mixed")
        mgr.add_track("mixed", src / "A" / "B" / "01.flac")
        mgr.backup_playlist_metadata("mixed", "t1")

        # Plant a non-json file in the backup dir
        from echolist.config import BACKUPS_ROOT, _workspace_id
        wid = _workspace_id(mgr.writer.root)
        rogue = BACKUPS_ROOT / wid / "mixed" / "notes.txt"
        rogue.write_text("keep me")

        delete_all_backups(mgr.writer.root, "mixed")

        # JSON is gone
        assert list_backups(mgr.writer.root, "mixed") == []
        # Non-json survived
        assert rogue.exists()
        assert rogue.read_text() == "keep me"
        # Dir still exists because rmdir fails on non-empty
        assert (BACKUPS_ROOT / wid / "mixed").exists()

        # Cleanup
        rogue.unlink()
        (BACKUPS_ROOT / wid / "mixed").rmdir()
        mgr.release_lock()

    def test_delete_also_purges_pending_staging(self, tmp_path):
        """Permanently deleting a playlist also cleans pending.json references."""
        src = tmp_path / "lib"
        _make_flac(src / "A" / "B" / "01.flac", "A", "Song")
        dest = tmp_path / "card"
        dest.mkdir()
        mgr = PlaylistManager.init(src, dest)

        save_backup(mgr.writer.root, "doomed", "t1",
                    {"playlist_name": "Doomed", "folder": "Doomed", "tracks": [{"v": 1}]})

        # Simulate staged adds/removes/reorders for this pid
        from echolist.gui import StagingState, PENDING_FILE
        staging = StagingState.__new__(StagingState)
        staging.pending_adds = [
            {"pid": "doomed", "src": "x.flac", "title": "X", "artist": "A"},
            {"pid": "keeper", "src": "y.flac", "title": "Y", "artist": "B"},
        ]
        staging.pending_removes = [
            {"pid": "doomed", "index": 1, "copy_name": "01 - X.flac"},
        ]
        staging.pending_reorders = {"doomed": [{"key": "c:1"}]}

        # Purge (same logic as _permanently_delete_playlist)
        staging.pending_adds = [a for a in staging.pending_adds if a["pid"] != "doomed"]
        staging.pending_removes = [r for r in staging.pending_removes if r["pid"] != "doomed"]
        staging.pending_reorders.pop("doomed", None)

        assert len(staging.pending_adds) == 1
        assert staging.pending_adds[0]["pid"] == "keeper"
        assert staging.pending_removes == []
        assert "doomed" not in staging.pending_reorders
        mgr.release_lock()

    def test_workspace_dir_survives_playlist_delete(self, tmp_path):
        """The workspace-level backup dir is not removed when one playlist is deleted."""
        src = tmp_path / "lib"
        _make_flac(src / "A" / "B" / "01.flac", "A", "Song")
        dest = tmp_path / "card"
        dest.mkdir()
        mgr = PlaylistManager.init(src, dest)
        mgr.create_playlist("Alpha")
        mgr.add_track("alpha", src / "A" / "B" / "01.flac")
        mgr.backup_playlist_metadata("alpha", "t1")

        from echolist.config import BACKUPS_ROOT, _workspace_id
        wid = _workspace_id(mgr.writer.root)
        wid_dir = BACKUPS_ROOT / wid

        delete_all_backups(mgr.writer.root, "alpha")

        # Playlist dir is gone but workspace dir remains
        assert not (wid_dir / "alpha").exists()
        assert wid_dir.exists()
        mgr.release_lock()
