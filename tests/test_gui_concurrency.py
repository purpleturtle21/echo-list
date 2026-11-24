"""One writer at a time: background playlist operations, sync and the
store-changing actions exclude each other, the Store is mutated on the Tk
thread, and failed preflight / settings / workspace switches leave the UI
usable and the right workspace locked."""

import errno
import threading
from unittest.mock import patch

import pytest

import echolist.gui as gui
from test_gui import (app, gui_env, isolated_home, callback_errors, pump)  # noqa: F401  (fixtures)
from test_gui_sync_safety import _sources, _titles, _synced_playlist


# ── Background operations and sync exclude each other ──


class _Gate:
    """Hold a worker inside ``target`` until release(), so the test can act
    on the Tk thread while the background operation is running."""

    def __init__(self, obj, name):
        self.entered = threading.Event()
        self.go = threading.Event()
        real = getattr(obj, name)

        def held(*a, **kw):
            self.entered.set()
            assert self.go.wait(30), "gate never released"
            return real(*a, **kw)
        self._patch = patch.object(obj, name, side_effect=held)

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self.go.set()
        self._patch.stop()

    def wait_entered(self):
        assert self.entered.wait(30), "worker never reached the gate"


def _quiet():
    return patch.multiple("echolist.gui.messagebox",
                          showinfo=lambda *a, **k: None,
                          showwarning=lambda *a, **k: None,
                          showerror=lambda *a, **k: None,
                          askyesno=lambda *a, **k: True)


def test_sync_is_refused_while_a_playlist_operation_runs(app, gui_env):
    """Sync started while an offload worker was mid-way, so two threads
    rewrote playlists.json from different Store states."""
    one = _synced_playlist(app, gui_env, 1)
    app._create_playlist()
    two = app.current_pid
    app._stage_add_files(_sources(gui_env)[1:2])

    with _quiet(), _Gate(app.mgr, "backup_playlist_metadata") as gate:
        app._offload_playlist(one)
        gate.wait_entered()
        app._do_sync()
        assert not app._syncing
        gate.go.set()
        pump(app)

    assert app.mgr.store.playlists[one]["offloaded"] is True
    assert app.mgr.store.playlists[two]["tracks"] == []
    assert len(app.staging.pending_adds) == 1


def test_store_changing_actions_wait_for_a_running_operation(app, gui_env):
    """Refresh, create, rename, delete, drag-reorder, settings and the
    playlist menus ignored _busy_playlists and changed the Store meanwhile."""
    one = _synced_playlist(app, gui_env, 2)
    app._create_playlist()
    two = app.current_pid
    pids_before = set(app.mgr.store.playlists)

    with _quiet(), _Gate(app.mgr, "backup_playlist_metadata") as gate:
        app._offload_playlist(one)
        gate.wait_entered()
        with patch.object(app.mgr, "rescan_playlist") as rescan:
            app._refresh_all()
            app.current_pid = two
            app._refresh_tracks()
        rescan.assert_not_called()
        app._create_playlist()
        assert not app._rename_playlist(two, "Other")
        app._delete_playlist()
        app._reindex_playlist(one)
        app._create_restore_point(two)
        app._show_settings()
        assert app.playlist_tree.winfo_exists()
        assert set(app.mgr.store.playlists) == pids_before
        assert not app.staging.has_pending
        gate.go.set()
        pump(app)

    assert app.mgr.store.playlists[one]["offloaded"] is True
    assert set(app.mgr.store.playlists) == pids_before
    assert app.mgr.list_metadata_backups(two) == []


def test_playlist_operations_are_refused_during_sync(app, gui_env):
    """Menu and context-menu operations ignored _syncing."""
    one = _synced_playlist(app, gui_env, 1)
    app._stage_add_files(_sources(gui_env)[1:2])

    with _quiet(), _Gate(app.mgr, "add_track") as gate:
        app._do_sync()
        gate.wait_entered()
        app._offload_playlist(one)
        app._create_restore_point(one)
        assert not app._busy_playlists
        gate.go.set()
        pump(app)

    assert not app.mgr.store.playlists[one].get("offloaded")
    assert _titles(app, one) == ["Song One", "Song Two"]


def test_delete_and_offload_change_the_store_on_the_tk_thread(app, gui_env):
    """Workers do the file I/O; the Store mutation and
    store.save() run in on_done on the Tk thread."""
    one = _synced_playlist(app, gui_env, 1)
    two = _synced_playlist(app, gui_env, 1)
    threads = []
    real_save = app.mgr.store.save

    def spy():
        threads.append(threading.current_thread() is threading.main_thread())
        return real_save()

    with _quiet(), patch.object(app.mgr.store, "save", side_effect=spy):
        app._offload_playlist(one)
        pump(app)
        app.current_pid = two
        app._delete_playlist()
        pump(app)

    assert threads and all(threads)
    assert app.mgr.store.playlists[one]["offloaded"] is True
    assert two not in app.mgr.store.playlists


def test_close_during_a_playlist_operation_keeps_the_lock_until_it_ends(app, gui_env):
    """Closing released the workspace lock while a worker still wrote."""
    one = _synced_playlist(app, gui_env, 1)

    with _quiet(), _Gate(app.mgr, "backup_playlist_metadata") as gate, \
            patch.object(app.root, "destroy") as destroy:
        app._offload_playlist(one)
        gate.wait_entered()
        app._on_close()
        destroy.assert_not_called()
        assert app.mgr._lock_fd is not None
        gate.go.set()
        pump(app)
        destroy.assert_called_once()
    assert app.mgr._lock_fd is None
    assert app.mgr.store.playlists[one]["offloaded"] is True


# ── A failed sync preflight must not lock the UI ──


def test_sync_preflight_failure_unlocks_the_ui(app, gui_env):
    """_backup_before_sync raising (read-only card) left _syncing set
    forever; Close then waited for a sync that never ran."""
    app._create_playlist()
    app._stage_add_files(_sources(gui_env)[:1])

    with patch.object(app.mgr.config, "increment_sync",
                      side_effect=OSError(errno.EROFS, "Read-only file system")), \
            patch("echolist.gui.messagebox.showerror") as err:
        app._do_sync()
    pump(app)

    err.assert_called_once()
    assert not app._syncing
    assert app.sync_btn.winfo_manager() == "pack"
    assert len(app.staging.pending_adds) == 1
    with patch.object(app.root, "destroy") as destroy, \
            patch("echolist.gui.messagebox.askyesnocancel", return_value=False):
        app._on_close()
    destroy.assert_called_once()


# ── Settings keep the lock until the new workspace opens ──


def test_failed_workspace_switch_keeps_the_current_lock_and_config(app, gui_env, tmp_path):
    """Settings released the lock (and saved the new backup interval into
    the old card's config) before opening the new workspace; when that failed
    the app kept editing the old card unlocked."""
    from echolist.manager import PlaylistManager, WorkspaceLockError
    other = tmp_path / "card2"
    other.mkdir()
    holder = PlaylistManager.init(gui_env["src"], other)  # another instance
    old_interval = app.mgr.config.backup_interval
    try:
        app._show_settings()
        app._dest_var.set(str(other))
        app._backup_var.set(str(old_interval + 7))
        with patch("echolist.gui.messagebox.showerror") as err, \
                patch("echolist.gui.messagebox.showwarning"):
            app._on_settings_open()
        err.assert_called_once()
        assert app.mgr.writer.root == (gui_env["dest"] / "Playlists").resolve()
        with pytest.raises(WorkspaceLockError):
            PlaylistManager.open(gui_env["dest"])
        import json
        cfg = json.loads((gui_env["dest"] / "Playlists" / ".echolist" / "config.json")
                         .read_text(encoding="utf-8"))
        assert cfg["backup_interval"] == old_interval
    finally:
        holder.release_lock()


def test_workspace_switch_releases_the_old_lock_after_the_new_one_opens(app, gui_env, tmp_path):
    """The successful path still ends with exactly one workspace locked."""
    from echolist.manager import PlaylistManager
    other = tmp_path / "card2"
    other.mkdir()
    old_mgr = app.mgr
    app._show_settings()
    app._dest_var.set(str(other))
    with patch("echolist.gui.messagebox.showwarning"), \
            patch("echolist.gui.messagebox.askyesno", return_value=False):
        app._on_settings_open()
    pump(app)
    assert app.mgr is not old_mgr
    assert old_mgr._lock_fd is None
    assert app.mgr._lock_fd is not None
    PlaylistManager.open(gui_env["dest"]).release_lock()
    app.mgr.release_lock()


# ── An unreadable legacy pending.json does not abort opening ──


def test_unreadable_legacy_pending_file_does_not_abort_open(app, gui_env):
    """_bind_legacy_staging ran outside the StagingLoadError handling, so
    a truncated ~/.echolist/pending.json stopped the workspace from opening."""
    staging_path = gui.pending_file(app.mgr.writer.root)
    staging_path.unlink(missing_ok=True)
    gui.PENDING_FILE.write_text('{"adds": [', encoding="utf-8")

    app.mgr.release_lock()
    with patch("echolist.gui.messagebox.showwarning") as warn:
        app._open_workspace(str(gui_env["src"]), str(gui_env["dest"]))
    pump(app)

    warn.assert_called_once()
    assert app.playlist_tree.winfo_exists()
    assert gui.PENDING_FILE.read_text(encoding="utf-8") == '{"adds": ['
