"""Restores, offload and delete in the GUI: a playlist is never lost
without its restore point, restores stage under the playlist they actually
created, and a restore point replaces the playlist's staged changes."""

import errno
from unittest.mock import patch

import echolist.gui as gui
from test_gui import (app, gui_env, isolated_home, callback_errors, pump)  # noqa: F401  (fixtures)
from test_gui_sync_safety import (_sources, _titles, _disk_names,
                                  _synced_playlist, _reopen)
from test_gui_concurrency import _quiet


# ── Offload and delete never lose the restore point ──


def test_delete_is_aborted_when_the_restore_point_cannot_be_saved(app, gui_env):
    """_delete_playlist swallowed a backup failure and deleted anyway,
    leaving a playlist that 'can be brought back' with no restore point."""
    pid = _synced_playlist(app, gui_env, 2)
    with patch.object(app.mgr, "backup_playlist_metadata",
                      side_effect=OSError(errno.ENOSPC, "No space left")), \
            patch("echolist.gui.messagebox.askyesno", return_value=True), \
            patch("echolist.gui.messagebox.showerror") as err:
        app._delete_playlist()
        pump(app)
    err.assert_called_once()
    assert len(_disk_names(app, pid)) == 2
    _reopen(app, gui_env)
    assert _titles(app, pid) == ["Song One", "Song Two"]


def test_offload_keeps_the_playlist_when_the_device_delete_fails(app, gui_env):
    """Offload swallowed writer.delete errors, then marked the playlist
    offloaded with tracks=[] although the files were still on the card."""
    pid = _synced_playlist(app, gui_env, 2)
    with _quiet(), patch.object(app.mgr.writer, "delete",
                                side_effect=OSError(errno.EIO, "I/O error")):
        app._offload_playlist(pid)
        pump(app)
    _reopen(app, gui_env)
    pl = app.mgr.store.playlists[pid]
    assert not pl.get("offloaded")
    assert _titles(app, pid) == ["Song One", "Song Two"]


def test_onload_uses_the_restore_point_the_offload_saved(app, gui_env):
    """backup_playlist_metadata dedupes to an older identical point, but
    onload took the newest backup, restoring a different track list."""
    pid = _synced_playlist(app, gui_env, 2)
    app.mgr.backup_playlist_metadata(pid, timestamp="20000101_000000")
    app._stage_add_files(_sources(gui_env)[2:3])
    app._do_sync()
    pump(app)
    app.mgr.backup_playlist_metadata(pid, timestamp="20990101_000000")
    app._stage_remove_track(pid, 3, app.mgr.store.playlists[pid]["tracks"][2]["copy_name"])
    app._do_sync()
    pump(app)
    assert _titles(app, pid) == ["Song One", "Song Two"]

    with _quiet():
        app._offload_playlist(pid)
        pump(app)
        _reopen(app, gui_env)
        app._onload_playlist(pid)
        pump(app)
    assert [a["title"] for a in app.staging.pending_adds] == ["Song One", "Song Two"]
