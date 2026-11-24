"""Workspace locking: the lock is taken before anything on the card is
read, a read-only card is reported as such (not as "another instance"), and
the lock is released when opening fails and retaken when the workspace
folder is recreated."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

fcntl = pytest.importorskip("fcntl")  # lock probes below use flock

from echolist.manager import PlaylistManager, WorkspaceLockError
from echolist.store import Store


def _lock_is_held(root: Path) -> bool:
    fd = os.open(str(root / ".echolist" / "workspace.lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def test_open_locks_before_reading_store(manager, dest, monkeypatch):
    root = manager.writer.root
    manager.release_lock()
    seen = []
    real_load = Store.load.__func__

    def spy(cls, writer):
        seen.append(_lock_is_held(writer.root))
        return real_load(cls, writer)

    monkeypatch.setattr(Store, "load", classmethod(spy))
    mgr = PlaylistManager.open(dest)
    mgr.release_lock()
    assert seen == [True]
    assert root == mgr.writer.root


def test_init_locks_before_reading_store(source, dest, monkeypatch):
    seen = []
    real_load = Store.load.__func__

    def spy(cls, writer):
        seen.append(_lock_is_held(writer.root))
        return real_load(cls, writer)

    monkeypatch.setattr(Store, "load", classmethod(spy))
    mgr = PlaylistManager.init(source, dest)
    mgr.release_lock()
    assert seen == [True]


def test_read_only_card_is_not_reported_as_another_instance(manager, dest, monkeypatch):
    manager.release_lock()
    real_open = os.open

    def erofs(path, flags, *a, **kw):
        if str(path).endswith("workspace.lock"):
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", erofs)
    with pytest.raises(OSError) as exc:
        PlaylistManager.open(dest)
    assert not isinstance(exc.value, WorkspaceLockError)
    assert exc.value.errno == errno.EROFS


def test_init_releases_lock_when_config_save_fails(source, dest, monkeypatch):
    from echolist.config import Config

    def enospc(self, writer):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Config, "save", enospc)
    with pytest.raises(OSError):
        PlaylistManager.init(source, dest)
    assert not _lock_is_held(dest / "Playlists")


def test_recreated_workspace_is_locked_again(manager, dest):
    import shutil
    shutil.rmtree(manager.writer.root)
    manager.create_playlist("Again")
    with pytest.raises(WorkspaceLockError):
        PlaylistManager.open(dest)
