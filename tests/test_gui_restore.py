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
from test_gui_sync_engine import _track


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


# ── Restores stage under the playlist they actually created ──


def test_snapshot_restore_stages_under_the_created_playlist_id(app, gui_env, tmp_path):
    """Snapshot restore staged adds under the snapshot's (legacy) pid
    although create_playlist() returned a different one, so every restored
    track failed to sync."""
    card = tmp_path / "card2"
    card.mkdir()
    snapshot = {
        "config": {"source_root": str(gui_env["src"]),
                   "playlist_folder": gui.DEFAULT_PLAYLIST_FOLDER},
        "store": {"schema": 1, "playlists": {"ac/dc": {
            "name": "AC/DC", "folder": "AC_DC",
            "tracks": [{"index": 1, "copy_name": "01 - Song One.flac",
                        "src_path": "ArtistA/Album1/01 Song One.flac"}]}}},
    }
    app.mgr.release_lock()
    with patch.object(gui.PlaylistManager, "find_snapshot", return_value=snapshot), \
            patch("echolist.gui.messagebox.askyesno", return_value=True), \
            patch("echolist.gui.messagebox.showwarning"), \
            patch("echolist.gui.messagebox.showinfo"):
        app._open_workspace(str(gui_env["src"]), str(card))
    pump(app)

    [pid] = list(app.mgr.store.playlists)
    assert [a["pid"] for a in app.staging.pending_adds] == [pid]
    assert app._do_sync_blocking() == []
    assert _titles(app, pid) == ["Song One"]


def test_restore_deleted_stages_under_the_recreated_playlist_id(app, gui_env):
    """A backup kept under an old pid ('old_mix') recreated the
    playlist as playlist_id(name) but staged its tracks under 'old_mix'."""
    from echolist.config import save_backup
    from echolist.naming import playlist_id
    save_backup(app.mgr.writer.root, "old_mix", "20200101_000000", {
        "playlist_name": "Mix", "folder": "Mix",
        "tracks": [{"copy_name": "01 - Song One.flac", "index": 1,
                    "src_path": "ArtistA/Album1/01 Song One.flac"}]})
    with _quiet():
        app._restore_deleted("old_mix", "20200101_000000")
        pump(app)

    pid = playlist_id("Mix")
    assert [a["pid"] for a in app.staging.pending_adds] == [pid]
    assert app._do_sync_blocking() == []
    assert _titles(app, pid) == ["Song One"]


def test_snapshot_restore_reports_a_playlist_whose_folder_exists(app, gui_env, tmp_path):
    """create_playlist now refuses a folder that already exists on the card;
    the snapshot restore must say which playlist was not restored and why,
    instead of counting its tracks as missing sources."""
    card = tmp_path / "card2"
    (card / gui.DEFAULT_PLAYLIST_FOLDER / "AC_DC").mkdir(parents=True)
    snapshot = {
        "config": {"source_root": str(gui_env["src"]),
                   "playlist_folder": gui.DEFAULT_PLAYLIST_FOLDER},
        "store": {"schema": 1, "playlists": {"ac_dc": {
            "name": "AC/DC", "folder": "AC_DC",
            "tracks": [{"index": 1, "copy_name": "01 - Song One.flac",
                        "src_path": "ArtistA/Album1/01 Song One.flac"}]}}},
    }
    app.mgr.release_lock()
    with patch.object(gui.PlaylistManager, "find_snapshot", return_value=snapshot), \
            patch("echolist.gui.messagebox.askyesno", return_value=True), \
            patch("echolist.gui.messagebox.showwarning") as warn, \
            patch("echolist.gui.messagebox.showinfo"):
        app._open_workspace(str(gui_env["src"]), str(card))
    pump(app)
    text = " ".join(str(c) for c in warn.call_args_list)
    assert "AC/DC" in text and "not restored" in text
    assert not app.staging.pending_adds


# ── Restore points and staged changes ──


def test_restore_point_replaces_the_playlists_staged_changes(app, gui_env):
    """Restoring rewrote the playlist under staged
    removes/reorders whose indices then pointed at other tracks. The restore
    now discards that playlist's staged changes (and undo) first; other
    playlists keep theirs."""
    pid = _synced_playlist(app, gui_env, 2)
    app.mgr.backup_playlist_metadata(pid, timestamp="20200101_000000")
    app._create_playlist()
    other = app.current_pid
    app._stage_add_files(_sources(gui_env)[2:3])
    app._stage_remove_track(pid, 1, _track(app, pid, 1)["copy_name"])
    app.staging.set_reorder(pid, [{"key": "c:2"}, {"key": "c:1"}])

    with _quiet():
        app._restore_from_point(pid, "20200101_000000")
        pump(app)

    assert not app.staging.pending_removes
    assert pid not in app.staging.pending_reorders
    assert [a["pid"] for a in app.staging.pending_adds] == [other]
    assert not app._undo_stack
    assert _titles(app, pid) == ["Song One", "Song Two"]


def test_restore_point_puts_restaged_tracks_back_in_their_place(app, gui_env):
    """to_stage entries carry the position they had in the
    restore point; staging them as plain adds put them at the end."""
    pid = _synced_playlist(app, gui_env, 3)
    app.mgr.backup_playlist_metadata(pid, timestamp="20200101_000000")
    app._stage_remove_track(pid, 1, _track(app, pid, 1)["copy_name"])
    assert app._do_sync_blocking() == []
    assert _titles(app, pid) == ["Song Two", "Song Three"]

    with _quiet():
        app.current_pid = pid
        app._restore_from_point(pid, "20200101_000000")
        pump(app)
    assert [r["title"] for r in app._track_data] == ["Song One", "Song Two", "Song Three"]
    assert app._do_sync_blocking() == []
    assert _titles(app, pid) == ["Song One", "Song Two", "Song Three"]


def test_restore_point_reports_what_it_could_not_do(app, gui_env):
    """Files the card refused to delete come back in
    "failed"; the summary never mentioned them."""
    pid = _synced_playlist(app, gui_env, 1)
    app.mgr.backup_playlist_metadata(pid, timestamp="20200101_000000")
    app._stage_add_files(_sources(gui_env)[1:2])
    assert app._do_sync_blocking() == []
    extra = _track(app, pid, 2)["copy_name"]

    with patch.object(app.mgr.writer, "delete",
                      side_effect=PermissionError(errno.EACCES, "Permission denied")), \
            patch("echolist.gui.messagebox.askyesno", return_value=True), \
            patch("echolist.gui.messagebox.showinfo") as info, \
            patch("echolist.gui.messagebox.showwarning") as warn:
        app._restore_from_point(pid, "20200101_000000")
        pump(app)
    text = " ".join(str(c) for c in info.call_args_list + warn.call_args_list)
    assert extra in text


def test_undo_after_a_restore_does_not_unstage_its_tracks(app, gui_env):
    """Undo snapshots taken before a structural operation survived
    it; undoing then silently dropped the tracks the restore had staged."""
    pid = _synced_playlist(app, gui_env, 2)
    app.mgr.backup_playlist_metadata(pid, timestamp="20200101_000000")
    app._stage_remove_track(pid, 2, _track(app, pid, 2)["copy_name"])
    assert app._do_sync_blocking() == []
    app._create_playlist()
    other = app.current_pid
    app._stage_add_files(_sources(gui_env)[2:3])   # leaves an undo entry
    app.current_pid = pid

    with _quiet():
        app._restore_from_point(pid, "20200101_000000")
        pump(app)
    assert any(a["pid"] == pid for a in app.staging.pending_adds)
    app._do_undo()
    assert any(a["pid"] == pid for a in app.staging.pending_adds)
    assert any(a["pid"] == other for a in app.staging.pending_adds)
