"""PlaylistManager safety on FAT32/macOS: renames never overwrite a name
that differs only in case or Unicode form, playlists never claim existing or
internal folders, adds never publish over a file, removes delete before they
commit, and restore points match tracks by their audio.

Each test injects the failure at the real I/O boundary (SafeWriter, the
filesystem) and checks what is on disk and in the reopened Store.
"""

from __future__ import annotations

import errno
import os
import unicodedata
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from conftest import TINY_FLAC, _make_flac
from echolist.safe_write import SafeWriter
from echolist.store import Store
from echolist.config import save_backup, list_backups, load_backup


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


class FatWriter(SafeWriter):
    """SafeWriter with FAT32 name semantics on any host filesystem.

    FAT32 is case-insensitive: renaming onto "01 - Y.ogg" replaces an
    existing "01 - y.ogg". The host filesystem may be case-sensitive (Linux)
    so the aliasing is emulated here, at the rename boundary.
    """

    def rename(self, old_rel, new_rel):
        old = self._resolve(old_rel)
        new = self._resolve(new_rel)
        for entry in new.parent.iterdir():
            if entry != old and entry.name != new.name and _key(entry.name) == _key(new.name):
                entry.unlink()  # what FAT does: the alias is the same entry
        return super().rename(old_rel, new_rel)


def _reopen_store(mgr) -> dict:
    return Store.load(mgr.writer).playlists


def _payloads(folder: Path) -> list[bytes]:
    return sorted(p.read_bytes() for p in folder.iterdir() if not p.name.startswith("."))


# ── Renumber is case- and normalization-safe ──


def test_renumber_does_not_overwrite_case_alias_on_fat(manager):
    folder = manager.writer.root / "Legacy"
    folder.mkdir()
    (folder / "001 - Y.ogg").write_bytes(b"recording A")
    (folder / "01 - y.ogg").write_bytes(b"recording B")
    manager.writer = FatWriter(manager.writer.root)

    pid = manager.adopt_playlist("Legacy")

    assert _payloads(folder) == [b"recording A", b"recording B"]
    tracks = manager.store.playlists[pid]["tracks"]
    assert sorted((folder / t["copy_name"]).read_bytes() for t in tracks) == [
        b"recording A", b"recording B"]


def test_renumber_does_not_overwrite_nfd_alias(manager):
    """A target equal to another file's name in a different Unicode form."""
    folder = manager.writer.root / "Legacy"
    folder.mkdir()
    nfc = unicodedata.normalize("NFC", "001 - Café.ogg")
    nfd = unicodedata.normalize("NFD", "01 - Café.ogg")  # target is NFC
    (folder / nfc).write_bytes(b"recording A")
    (folder / nfd).write_bytes(b"recording B")
    manager.writer = FatWriter(manager.writer.root)

    manager.adopt_playlist("Legacy")

    assert _payloads(folder) == [b"recording A", b"recording B"]


def test_renumber_parks_under_tmp_prefix_rescan_ignores(manager, monkeypatch):
    """A file parked mid-renumber is not adopted as a track by rescan."""
    folder = manager.writer.root / "Legacy"
    folder.mkdir()
    (folder / "001 - Y.ogg").write_bytes(b"A")
    (folder / "01 - Y.ogg").write_bytes(b"B")

    real = SafeWriter.rename
    calls = []

    def fail_on_publish(self, old, new):
        calls.append((str(old), str(new)))
        if "_echolist_tmp_" in str(old):
            raise OSError(errno.EIO, "card pulled")
        return real(self, old, new)

    monkeypatch.setattr(SafeWriter, "rename", fail_on_publish)
    with pytest.raises(OSError):
        manager.adopt_playlist("Legacy")
    monkeypatch.setattr(SafeWriter, "rename", real)

    parked = [p.name for p in folder.iterdir() if p.name.startswith("_echolist")]
    assert parked and all(n.startswith("_echolist_tmp_") for n in parked)
    pid = next(iter(manager.store.playlists))
    manager.rescan_playlist(pid)
    names = [t["copy_name"] for t in manager.store.playlists[pid]["tracks"]]
    assert not any(n.startswith("_echolist") for n in names)


# ── Playlists never claim existing or internal folders ──


@pytest.mark.parametrize("name", [".echolist", ".ECHOLIST", "._Mix", ".hidden"])
def test_create_playlist_refuses_dot_folders(manager, name):
    with pytest.raises(ValueError):
        manager.create_playlist(name)
    assert (manager.writer.root / ".echolist" / "config.json").exists()


def test_create_playlist_refuses_existing_untracked_folder(manager):
    existing = manager.writer.root / "mix"
    existing.mkdir()
    (existing / "keep.flac").write_bytes(b"user audio")
    with pytest.raises(ValueError):
        manager.create_playlist("Mix")
    assert manager.store.playlists == {}
    assert (existing / "keep.flac").read_bytes() == b"user audio"


def test_create_playlist_rejects_folder_collision(manager, source):
    first = manager.create_playlist("AC/DC")
    manager.add_track(first, source / "ArtistA" / "Album1" / "01 Song One.flac")
    with pytest.raises(ValueError):
        manager.create_playlist("AC_DC")
    with pytest.raises(ValueError):
        manager.create_playlist("ac_dc")


def test_create_playlist_rejects_folder_used_by_legacy_pid(manager):
    # A store written before playlist_id() sanitized names keeps pid "ac/dc".
    manager.store.add_playlist("ac/dc", "AC/DC", "AC_DC")
    with pytest.raises(ValueError):
        manager.create_playlist("ac_dc")


# ── Add never publishes over an existing copy ──


def test_add_after_external_removal_does_not_overwrite(manager, tmp_path):
    lib = Path(manager.config.source_root)
    _make_flac(lib / "x" / "a.flac", "X", "A")
    _make_flac(lib / "x" / "intro1.flac", "First", "Intro")
    _make_flac(lib / "x" / "intro2.flac", "Second", "Intro")
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, lib / "x" / "a.flac")
    manager.add_track(pid, lib / "x" / "intro1.flac")
    folder = manager.writer.root / "Mix"
    (folder / "01 - A.flac").unlink()  # removed outside EchoList
    manager.rescan_playlist(pid)

    manager.add_track(pid, lib / "x" / "intro2.flac")

    tracks = manager.store.playlists[pid]["tracks"]
    assert len({t["copy_name"] for t in tracks}) == 2
    artists = sorted(FLAC(folder / t["copy_name"])["ARTIST"][0] for t in tracks)
    assert artists == ["First", "Second"]
    assert len(list(folder.glob("*.flac"))) == 2


# ── Remove deletes first, commits after ──


def test_failed_delete_leaves_store_untouched(manager, source, monkeypatch):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.add_track(pid, source / "ArtistB" / "Album2" / "03 Song Two.flac")
    before = [dict(t) for t in manager.store.playlists[pid]["tracks"]]

    def deny(self, rel):
        raise PermissionError(errno.EACCES, "locked", str(rel))

    monkeypatch.setattr(SafeWriter, "delete", deny)
    with pytest.raises(PermissionError):
        manager.remove_track(pid, 1)

    assert manager.store.playlists[pid]["tracks"] == before
    assert _reopen_store(manager)[pid]["tracks"] == before
    assert len(list((manager.writer.root / "Mix").glob("*.flac"))) == 2


# ── Rescan: Unicode forms and numeric order ──


def test_rescan_matches_nfd_disk_name_to_nfc_store_entry(manager, source):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    pl = manager.store.playlists[pid]
    folder = manager.writer.root / pl["folder"]
    nfc_name = unicodedata.normalize("NFC", "01 - Café.flac")
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    (folder / pl["tracks"][0]["copy_name"]).rename(folder / nfd_name)
    pl["tracks"][0]["copy_name"] = nfc_name
    assert [f.name for f in folder.iterdir()] == [nfd_name]

    manager.rescan_playlist(pid)

    track = manager.store.playlists[pid]["tracks"][0]
    assert track["src_path"] == "ArtistA/Album1/01 Song One.flac"
    assert track["src_hash"].startswith("blake2b:")
    assert "original_tags" in track


def test_rescan_title_match_survives_nfd(manager, source):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    pl = manager.store.playlists[pid]
    folder = manager.writer.root / pl["folder"]
    (folder / pl["tracks"][0]["copy_name"]).rename(
        folder / unicodedata.normalize("NFD", "07 - Café.flac"))
    pl["tracks"][0]["copy_name"] = unicodedata.normalize("NFC", "01 - Café.flac")

    manager.rescan_playlist(pid)

    assert manager.store.playlists[pid]["tracks"][0]["src_path"] != ""


def test_rescan_sorts_mixed_width_prefixes_numerically(manager):
    pid = manager.create_playlist("Big")
    folder = manager.writer.root / manager.store.playlists[pid]["folder"]
    names = ["01 - a.flac", "09 - b.flac", "10 - c.flac", "11 - d.flac",
             "99 - e.flac", "100 - f.flac", "101 - g.flac"]
    for n in names:
        (folder / n).write_bytes(TINY_FLAC)
    manager.rescan_playlist(pid)
    got = [t["copy_name"] for t in manager.store.playlists[pid]["tracks"]]
    assert got == names


# ── Untracked detection uses FAT/macOS name equality ──


def test_tracked_folder_in_other_unicode_form_is_not_untracked(manager, source):
    pid = manager.create_playlist("Café")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    folder = manager.writer.root / manager.store.playlists[pid]["folder"]
    nfd = unicodedata.normalize("NFD", "Café")
    os.rename(folder, folder.with_name(nfd))
    assert manager.detect_untracked_playlists() == []


def test_tracked_folder_in_other_case_is_not_untracked(manager, source):
    pid = manager.create_playlist("Gym")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    folder = manager.writer.root / "Gym"
    os.rename(folder, folder.with_name("tmp-gym"))
    os.rename(folder.with_name("tmp-gym"), folder.with_name("GYM"))
    assert manager.detect_untracked_playlists() == []


# ── Adoption keeps numeric order ──


def test_adopt_orders_numerically(manager):
    folder = manager.writer.root / "Old"
    folder.mkdir()
    for i in range(1, 11):
        (folder / f"{i} - T{i}.ogg").write_bytes(f"t{i}".encode())
    pid = manager.adopt_playlist("Old")
    names = [t["copy_name"] for t in manager.store.playlists[pid]["tracks"]]
    assert names[1] == "02 - T2.ogg"
    assert names[9] == "10 - T10.ogg"
    assert (folder / "10 - T10.ogg").read_bytes() == b"t10"


# ── Device track count stays on the device ──


def test_expensive_stats_do_not_scan_sibling_volumes(tmp_path, source):
    from echolist.manager import PlaylistManager

    volumes = tmp_path / "Volumes"
    card = volumes / "ECHO MINI"
    card.mkdir(parents=True)
    _make_flac(card / "Music" / "on_device.flac", "A", "On device")
    _make_flac(volumes / "Backup" / "elsewhere.flac", "B", "Other volume")
    mgr = PlaylistManager.init(source, card)
    try:
        device_tracks, _ = mgr.compute_expensive_stats()
    finally:
        mgr.release_lock()
    assert device_tracks == 1


# ── Restore points ──


@pytest.fixture
def lib(manager):
    lib = Path(manager.config.source_root)
    _make_flac(lib / "r" / "a.flac", "AA", "A")
    _make_flac(lib / "r" / "b.flac", "BB", "B")
    _make_flac(lib / "r" / "c.flac", "CC", "C")
    _make_flac(lib / "r" / "x1.flac", "First", "X")
    _make_flac(lib / "r" / "x2.flac", "Second", "X")
    return lib / "r"


def test_restore_rejects_same_name_different_audio(manager, lib):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, lib / "x1.flac")
    manager.backup_playlist_metadata(pid, "t1")
    manager.remove_track(pid, 1)
    manager.add_track(pid, lib / "x2.flac")  # also "01 - X.flac"

    result = manager.restore_playlist_to_point(pid, "t1")

    staged = [s["src_path"] for s in result["to_stage"]]
    assert staged == ["r/x1.flac"]
    folder = manager.writer.root / "Mix"
    for t in manager.store.playlists[pid]["tracks"]:
        audio_src = FLAC(folder / t["copy_name"])["ECHOLIST_SRC"][0]
        assert audio_src == t["src_path"]


def test_restore_swapped_same_title_tracks(manager, lib):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, lib / "x1.flac")
    manager.add_track(pid, lib / "x2.flac")
    manager.backup_playlist_metadata(pid, "t1")
    folder = manager.writer.root / "Mix"
    # A completed reorder swapped the two files and their store rows.
    os.rename(folder / "01 - X.flac", folder / "tmp")
    os.rename(folder / "02 - X.flac", folder / "01 - X.flac")
    os.rename(folder / "tmp", folder / "02 - X.flac")
    t1, t2 = manager.store.playlists[pid]["tracks"]
    t1["copy_name"], t2["copy_name"] = "02 - X.flac", "01 - X.flac"
    t1["index"], t2["index"] = 2, 1
    manager.store.playlists[pid]["tracks"] = [t2, t1]
    manager.store.save()

    manager.restore_playlist_to_point(pid, "t1")

    # ECHOLIST_SRC is written once at copy time: it identifies the audio.
    assert FLAC(folder / "01 - X.flac")["ECHOLIST_SRC"][0] == "r/x1.flac"
    assert FLAC(folder / "02 - X.flac")["ECHOLIST_SRC"][0] == "r/x2.flac"
    tracks = manager.store.playlists[pid]["tracks"]
    assert [t["src_path"] for t in tracks] == ["r/x1.flac", "r/x2.flac"]


def test_restore_keeps_indices_contiguous_and_positions_missing(manager, lib):
    pid = manager.create_playlist("Mix")
    for n in ("a", "b", "c"):
        manager.add_track(pid, lib / f"{n}.flac")
    manager.backup_playlist_metadata(pid, "t1")
    manager.remove_track(pid, 1)

    result = manager.restore_playlist_to_point(pid, "t1")

    tracks = manager.store.playlists[pid]["tracks"]
    assert [t["index"] for t in tracks] == [1, 2]
    folder = manager.writer.root / "Mix"
    for t in tracks:
        assert t["copy_name"].startswith(f"{t['index']:02d} - ")
        assert (folder / t["copy_name"]).exists()
        assert FLAC(folder / t["copy_name"])["TRACKNUMBER"][0] == str(t["index"])
    assert [(s["src_path"], s["index"]) for s in result["to_stage"]] == [("r/a.flac", 1)]


def test_restore_keeps_existing_track_without_saved_tags(manager, lib):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, lib / "a.flac")
    t = manager.store.playlists[pid]["tracks"][0]
    save_backup(manager.writer.root, pid, "t1", {
        "playlist_name": "Mix", "folder": "Mix",
        "tracks": [{"copy_name": t["copy_name"], "index": 1,
                    "src_path": t["src_path"], "src_hash": t["src_hash"],
                    "original_tags": t["original_tags"]}],  # no "tags": file was missing then
    })
    result = manager.restore_playlist_to_point(pid, "t1")
    assert result["to_stage"] == []
    assert [x["copy_name"] for x in manager.store.playlists[pid]["tracks"]] == [t["copy_name"]]


def test_restore_counts_only_real_removals(manager, lib, monkeypatch):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, lib / "a.flac")
    manager.backup_playlist_metadata(pid, "t1")
    manager.add_track(pid, lib / "b.flac")

    def deny(self, rel):
        raise PermissionError(errno.EACCES, "locked", str(rel))

    monkeypatch.setattr(SafeWriter, "delete", deny)
    result = manager.restore_playlist_to_point(pid, "t1")

    assert result["removed"] == 0
    assert result["failed"]
    folder = manager.writer.root / "Mix"
    names = [t["copy_name"] for t in manager.store.playlists[pid]["tracks"]]
    assert all((folder / n).exists() for n in names)
    assert {p.name for p in folder.glob("*.flac")} == set(names)


# ── Dedupe never makes an older backup look current ──


def test_backup_dedupe_only_against_latest(manager, lib):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, lib / "a.flac")
    manager.backup_playlist_metadata(pid, "20250101_000001")   # state A
    manager.add_track(pid, lib / "b.flac")
    manager.backup_playlist_metadata(pid, "20250101_000002")   # state B
    manager.remove_track(pid, 2)                                # back to A

    path = manager.backup_playlist_metadata(pid, "20250101_000003")

    newest = list_backups(manager.writer.root, pid)[0]
    assert newest["path"] == path
    data = load_backup(manager.writer.root, pid, newest["timestamp"])
    assert [t["src_path"] for t in data["tracks"]] == ["r/a.flac"]
