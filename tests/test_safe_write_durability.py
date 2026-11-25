"""SafeWriter durability: copies are atomic and fsynced (file, then its
directory), never inherit read-only modes, can refuse to publish over an
existing name, and a failed cleanup never hides the error that caused it."""

from __future__ import annotations

import errno
import os
import stat

import pytest

import echolist.safe_write as safe_write
from echolist.safe_write import SafeWriter, atomic_write_text
from conftest import TINY_FLAC


# ── Atomic, durable copies ──


def _fail_after_first_chunk(copied, total):
    raise OSError(errno.ENOSPC, "No space left on device")


def test_copy_in_failure_leaves_no_partial_file_at_final_name(tmp_path):
    src = tmp_path / "big.flac"
    src.write_bytes(os.urandom(1024 * 1024))
    w = SafeWriter(tmp_path / "ws")
    with pytest.raises(OSError):
        w.copy_in(src, "Mix/01 - Big.flac", progress_cb=_fail_after_first_chunk,
                  compute_hash=True)
    mix = tmp_path / "ws" / "Mix"
    assert not (mix / "01 - Big.flac").exists()
    assert list(mix.iterdir()) == [], "temp file was not cleaned up"


def test_failed_copy_is_not_adopted_by_rescan(manager, source):
    pid = manager.create_playlist("Mix")
    with pytest.raises(OSError):
        manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac",
                          progress_cb=_fail_after_first_chunk)
    manager.rescan_playlist(pid)
    assert manager.store.playlists[pid]["tracks"] == []


def test_stranded_copy_temp_is_not_adopted_by_rescan(manager, source):
    pid = manager.create_playlist("Mix")
    folder = manager.writer.root / manager.store.playlists[pid]["folder"]
    # What a power cut mid-copy leaves behind: only the temp file.
    (folder / ".echolist-copy-abc123.part").write_bytes(TINY_FLAC[:20])
    manager.rescan_playlist(pid)
    assert manager.store.playlists[pid]["tracks"] == []


def test_copy_in_fsyncs_before_rename(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(safe_write.os, "fsync",
                        lambda fd: (events.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(safe_write.os, "replace",
                        lambda a, b: (events.append("replace"), real_replace(a, b)))
    src = tmp_path / "a.flac"
    src.write_bytes(b"x" * 1000)
    (tmp_path / "ws").mkdir()
    w = SafeWriter(tmp_path / "ws")
    dest, _ = w.copy_in(src, "a.flac")
    assert dest.read_bytes() == b"x" * 1000
    assert events[:2] == ["fsync", "replace"]


def test_atomic_write_fsyncs_and_handles_short_writes(tmp_path, monkeypatch):
    real_write = os.write
    fsyncs = []
    real_fsync = os.fsync
    # A short write: the kernel accepts at most 7 bytes per call.
    monkeypatch.setattr(safe_write.os, "write", lambda fd, b: real_write(fd, bytes(b[:7])))
    monkeypatch.setattr(safe_write.os, "fsync",
                        lambda fd: (fsyncs.append(fd), real_fsync(fd)))
    w = SafeWriter(tmp_path / "ws")
    payload = '{"schema": 1, "playlists": {}}'
    p = w.write_text(".echolist/playlists.json", payload)
    assert p.read_text(encoding="utf-8") == payload
    assert fsyncs, "atomic write never fsynced"


def test_copy_in_before_commit_runs_on_temp_then_fsyncs(tmp_path, monkeypatch):
    import os
    import echolist.safe_write as safe_write
    from echolist.safe_write import SafeWriter

    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(safe_write.os, "fsync",
                        lambda fd: (events.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(safe_write.os, "replace",
                        lambda a, b: (events.append("replace"), real_replace(a, b)))
    src = tmp_path / "a.flac"
    src.write_bytes(b"x" * 100)

    def edit(tmp):
        events.append("callback")
        assert tmp.name.endswith(".part") and tmp.read_bytes() == b"x" * 100
        tmp.write_bytes(b"y" * 100)

    (tmp_path / "ws").mkdir()
    w = SafeWriter(tmp_path / "ws")
    dest, _ = w.copy_in(src, "a.flac", before_commit=edit)
    assert dest.read_bytes() == b"y" * 100
    # (then the directory is fsynced too, where supported)
    assert events[:3] == ["callback", "fsync", "replace"]


# ── Directory fsync ──


def _spy_fsync_and_replace(monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync-{kind}")
        return real_fsync(fd)

    def replace(a, b):
        events.append("replace")
        return real_replace(a, b)

    monkeypatch.setattr(safe_write.os, "fsync", fsync)
    monkeypatch.setattr(safe_write.os, "replace", replace)
    return events


@pytest.mark.skipif(os.name == "nt", reason="directories cannot be fsynced on Windows")
def test_copy_in_fsyncs_directory_after_replace(tmp_path, monkeypatch):
    events = _spy_fsync_and_replace(monkeypatch)
    src = tmp_path / "a.flac"
    src.write_bytes(b"x" * 100)
    w = SafeWriter(tmp_path / "ws")
    w.makedirs("Mix")
    events.clear()
    w.copy_in(src, "Mix/a.flac")
    assert events == ["fsync-file", "replace", "fsync-dir"]


@pytest.mark.skipif(os.name == "nt", reason="directories cannot be fsynced on Windows")
def test_atomic_write_fsyncs_directory_after_replace(tmp_path, monkeypatch):
    events = _spy_fsync_and_replace(monkeypatch)
    atomic_write_text(tmp_path / "state" / "x.json", "{}")
    assert events == ["fsync-file", "replace", "fsync-dir"]


@pytest.mark.skipif(os.name == "nt", reason="directories cannot be fsynced on Windows")
def test_rename_fsyncs_directory(tmp_path, monkeypatch):
    w = SafeWriter(tmp_path / "ws")
    w.write_text("Mix/a.txt", "a")
    events = _spy_fsync_and_replace(monkeypatch)
    w.rename("Mix/a.txt", "Mix/b.txt")
    assert "fsync-dir" in events


def test_directory_fsync_unsupported_is_tolerated(tmp_path, monkeypatch):
    """Some filesystems refuse fsync on a directory (EINVAL): not an error."""
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_fsync(fd)

    monkeypatch.setattr(safe_write.os, "fsync", fsync)
    atomic_write_text(tmp_path / "x.json", "{}")
    assert (tmp_path / "x.json").read_text() == "{}"


def test_directory_fsync_io_error_propagates(tmp_path, monkeypatch):
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "I/O error")
        return real_fsync(fd)

    monkeypatch.setattr(safe_write.os, "fsync", fsync)
    if os.name == "nt":
        pytest.skip("no directory fsync on Windows")
    with pytest.raises(OSError) as e:
        atomic_write_text(tmp_path / "x.json", "{}")
    assert e.value.errno == errno.EIO


# ── Copies never inherit read-only mode or flags ──


def test_copy_in_of_read_only_source_is_writable(tmp_path):
    src = tmp_path / "ro.flac"
    src.write_bytes(TINY_FLAC)
    os.chmod(src, 0o444)
    try:
        w = SafeWriter(tmp_path / "ws")
        for compute_hash in (False, True):
            dest, _ = w.copy_in(src, f"Mix/{compute_hash}.flac", compute_hash=compute_hash)
            assert os.stat(dest).st_mode & stat.S_IWUSR, "copy inherited read-only mode"
    finally:
        os.chmod(src, 0o644)


def test_add_track_from_read_only_source_tags_copy(manager, source):
    src = source / "ArtistA" / "Album1" / "01 Song One.flac"
    os.chmod(src, 0o444)
    try:
        pid = manager.create_playlist("Mix")
        manager.add_track(pid, src)
    finally:
        os.chmod(src, 0o644)
    assert len(manager.store.playlists[pid]["tracks"]) == 1


def test_copy_in_keeps_source_mtime(tmp_path):
    src = tmp_path / "a.flac"
    src.write_bytes(b"x")
    os.utime(src, (1_600_000_000, 1_600_000_000))
    dest, _ = SafeWriter(tmp_path / "ws").copy_in(src, "a.flac")
    assert int(dest.stat().st_mtime) == 1_600_000_000


# ── Exclusive publish ──


def test_copy_in_exclusive_refuses_existing_name(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    w.makedirs("Mix")
    (w.root / "Mix" / "01 - A.flac").write_bytes(b"old")
    src = tmp_path / "src.flac"
    src.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        w.copy_in(src, "Mix/01 - A.flac", exclusive=True)
    assert (w.root / "Mix" / "01 - A.flac").read_bytes() == b"old"
    assert sorted(p.name for p in (w.root / "Mix").iterdir()) == ["01 - A.flac"]


# ── A failed cleanup never hides the original error ──


def _failing_unlink(monkeypatch):
    def unlink(path, *a, **kw):
        raise OSError(errno.EIO, "card pulled", str(path))
    monkeypatch.setattr(safe_write.os, "unlink", unlink)


def test_copy_in_keeps_original_error_when_cleanup_fails(tmp_path, monkeypatch):
    src = tmp_path / "a.flac"
    src.write_bytes(b"x" * 100)
    w = SafeWriter(tmp_path / "ws")
    w.makedirs("Mix")
    _failing_unlink(monkeypatch)

    class TagError(Exception):
        pass

    def bad_tags(tmp):
        raise TagError("tag write failed")

    with pytest.raises(TagError):
        w.copy_in(src, "Mix/a.flac", before_commit=bad_tags)


def test_atomic_write_keeps_original_error_when_cleanup_fails(tmp_path, monkeypatch):
    def enospc(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(safe_write.os, "write", enospc)
    _failing_unlink(monkeypatch)
    with pytest.raises(OSError) as e:
        atomic_write_text(tmp_path / "x.json", "{}")
    assert e.value.errno == errno.ENOSPC
