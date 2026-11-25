"""GUI sync safety: crash recovery never deletes audio, failed sync steps
stay staged and are reported, staged changes and the journal belong to one
card, reorders combine with adds and removes, and rename / delete / offload /
undo / search / settings keep staged changes consistent.

Each test drives the real App code path; its docstring describes the
behaviour it pins down (and, in the past tense, what used to go wrong).
"""

import errno
import signal
import threading
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pytest

import echolist.gui as gui
from echolist.journal import SyncJournal
from conftest import _make_flac
from test_gui import (app, gui_env, isolated_home, callback_errors,  # noqa: F401  (fixtures)
                      pump, _flush_bg_ops, _flush_tracks)


class Crash(BaseException):
    """Simulates the process dying (card pulled, power loss) mid-operation.

    A BaseException so no ``except Exception`` handler can swallow it, the way
    a real crash skips every handler.
    """


@pytest.fixture(autouse=True)
def _isolate_local_state(tmp_path, monkeypatch):
    monkeypatch.setattr("echolist.journal.JOURNAL_FILE", tmp_path / "sync_journal.json")
    monkeypatch.setattr("echolist.config.DEFAULT_FILE", tmp_path / "default.json")


def _sources(gui_env):
    four = gui_env["src"] / "ArtistD" / "Album4" / "07 Song Four.flac"
    if not four.exists():
        _make_flac(four, "ArtistD", "Song Four")
    # One, Two, Three, Four (sorted by artist directory)
    return sorted(gui_env["src"].rglob("*.flac"))


def _titles(app, pid):
    tracks = sorted(app.mgr.store.playlists[pid]["tracks"], key=lambda t: t["index"])
    return [t["copy_name"].split(" - ", 1)[1].rsplit(".", 1)[0] for t in tracks]


def _disk_names(app, pid):
    folder = app.mgr.writer.root / app.mgr.store.playlists[pid]["folder"]
    return sorted(f.name for f in folder.iterdir() if f.suffix == ".flac")


def _synced_playlist(app, gui_env, n):
    app._create_playlist()
    pid = app.current_pid
    for f in _sources(gui_env)[:n]:
        app.mgr.add_track(pid, f)
    app._refresh_tracks()
    _flush_tracks(app)
    return pid


def _quiet_dialogs():
    return [
        patch("echolist.gui.messagebox.showwarning"),
        patch("echolist.gui.messagebox.showinfo"),
        patch("echolist.gui.messagebox.showerror"),
        patch("echolist.gui.messagebox.askyesno", return_value=False),
    ]


def _open(app, src, dest, **kw):
    """Re-open a workspace the way the setup screen does (after a crash/restart)."""
    if app.mgr is not None:
        app.mgr.release_lock()
    ps = _quiet_dialogs()
    mocks = [p.start() for p in ps]
    try:
        app._open_workspace(str(src), str(dest), **kw)
        _flush_bg_ops(app)
    finally:
        for p in ps:
            p.stop()
    return {"warning": mocks[0], "info": mocks[1], "error": mocks[2]}


def _reopen(app, gui_env):
    return _open(app, gui_env["src"], gui_env["dest"])


def _wait_sync(app):
    pump(app)


# ── Crash recovery never deletes audio ──

def test_crash_mid_reorder_restores_tracks_on_next_open(app, gui_env):
    """A crash between reorder pass 1 and 2 left _echolist_tmp_* files that
    the next start unlinked — permanent loss of committed tracks."""
    pid = _synced_playlist(app, gui_env, 3)
    app.staging.set_reorder(pid, [{"key": "c:3"}, {"key": "c:1"}, {"key": "c:2"}])

    real_rename = app.mgr.writer.rename
    calls = []

    def crash_on_second(old, new):
        calls.append(old)
        if len(calls) == 2:
            raise Crash()
        return real_rename(old, new)

    with patch.object(app.mgr.writer, "rename", side_effect=crash_on_second):
        with pytest.raises(Crash):
            app._do_sync_blocking()

    _reopen(app, gui_env)

    names = _disk_names(app, pid)
    assert len(names) == 3, names
    assert not any(n.startswith("_echolist_tmp_") for n in names)
    assert _titles(app, pid) == ["Song Three", "Song One", "Song Two"]
    assert pid not in app.staging.pending_reorders


def test_stranded_temp_file_is_renamed_back_not_deleted(app, gui_env):
    """A leftover _echolist_tmp_* (no reorder plan to finish) is restored
    to its original name instead of being unlinked."""
    pid = _synced_playlist(app, gui_env, 1)
    folder = app.mgr.writer.root / app.mgr.store.playlists[pid]["folder"]
    name = app.mgr.store.playlists[pid]["tracks"][0]["copy_name"]
    (folder / name).rename(folder / f"_echolist_tmp_1_{name}")
    # Journal in the pre-fix format: a reorder still pending, no plan.
    SyncJournal.begin([], [], {pid: [{"key": "c:1"}]})

    _reopen(app, gui_env)

    assert (folder / name).exists()
    assert not list(folder.glob("_echolist_tmp_*"))
    assert len(app.mgr.store.playlists[pid]["tracks"]) == 1


def test_stranded_copy_part_files_are_swept_on_open(app, gui_env):
    """Coordinator (d): copy_in temp files stranded by a hard crash are removed
    on the next open (they are partial copies, never user audio)."""
    pid = _synced_playlist(app, gui_env, 1)
    folder = app.mgr.writer.root / app.mgr.store.playlists[pid]["folder"]
    part = folder / ".echolist-copy-abc123.part"
    part.write_bytes(b"partial")

    _reopen(app, gui_env)

    assert not part.exists()
    assert len(_disk_names(app, pid)) == 1


# ── Failed sync steps stay staged and are reported ──

def _failing_add(app, needle):
    real_add = app.mgr.add_track

    def add(pid, src, progress_cb=None):
        if needle in str(src):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_add(pid, src, progress_cb=progress_cb)
    return patch.object(app.mgr, "add_track", side_effect=add)


def test_blocking_sync_keeps_failed_add_staged_and_returns_errors(app, gui_env):
    """A failed add was marked done, staging cleared, error swallowed."""
    app._create_playlist()
    pid = app.current_pid
    app._stage_add_files(_sources(gui_env)[:2])

    with _failing_add(app, "Song Two"):
        errors = app._do_sync_blocking()

    assert [Path(a["src"]).name for a in app.staging.pending_adds] == ["03 Song Two.flac"]
    assert _titles(app, pid) == ["Song One"]
    assert errors and "No space left" in errors[0]


def test_device_error_stops_remaining_writes(app, gui_env):
    """After ENOSPC the remaining adds are not attempted and stay staged."""
    app._create_playlist()
    pid = app.current_pid
    app._stage_add_files(_sources(gui_env)[:3])
    calls = []
    real_add = app.mgr.add_track

    def add(p, src, progress_cb=None):
        calls.append(Path(src).name)
        if len(calls) == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_add(p, src, progress_cb=progress_cb)

    with patch.object(app.mgr, "add_track", side_effect=add):
        errors = app._do_sync_blocking()

    assert calls == ["01 Song One.flac"]
    assert len(app.staging.pending_adds) == 3
    assert app.mgr.store.playlists[pid]["tracks"] == []
    assert len(errors) == 1


def test_close_with_failed_sync_reports_and_stays_open(app, gui_env):
    """Close -> "Sync now" with a failing add exited silently."""
    app._create_playlist()
    app._stage_add_files(_sources(gui_env)[:1])

    with _failing_add(app, "Song One"), \
            patch("echolist.gui.messagebox.askyesnocancel", return_value=True), \
            patch("echolist.gui.messagebox.showwarning") as warn, \
            patch.object(app.root, "destroy") as destroy:
        app._on_close()

    destroy.assert_not_called()
    warn.assert_called_once()
    assert len(app.staging.pending_adds) == 1


def test_async_sync_keeps_failed_add_staged(app, gui_env):
    """The threaded Sync button path cleared staging after failures."""
    app._create_playlist()
    pid = app.current_pid
    app._stage_add_files(_sources(gui_env)[:2])

    with _failing_add(app, "Song Two"), \
            patch("echolist.gui.messagebox.showwarning") as warn:
        app._do_sync()
        _wait_sync(app)

    warn.assert_called_once()
    assert [Path(a["src"]).name for a in app.staging.pending_adds] == ["03 Song Two.flac"]
    assert _titles(app, pid) == ["Song One"]


# ── Journal and staging belong to one card and survive a crash ──

def test_journal_in_progress_final_action_is_incomplete():
    """A crash during the only/last action (status in_progress) was missed."""
    j = SyncJournal.begin([], [{"pid": "p", "src": "/x.flac"}], {})
    j.mark_current(0)

    loaded = SyncJournal.load_incomplete()
    assert loaded is not None
    assert [a["op"] for a in loaded.pending_actions] == ["add"]


def test_staged_changes_are_scoped_to_their_workspace(app, gui_env, tmp_path):
    """pending.json was global, so card A's staged changes ran against card B."""
    _reopen(app, gui_env)
    app._create_playlist()
    app._stage_add_files(_sources(gui_env)[:1])
    assert len(app.staging.pending_adds) == 1

    other = tmp_path / "card2"
    other.mkdir()
    _open(app, gui_env["src"], other)
    assert app.staging.pending_adds == []

    _reopen(app, gui_env)
    assert len(app.staging.pending_adds) == 1


def test_crash_after_remove_does_not_replay_stale_remove(app, gui_env):
    """Remove #1 done, crash during the add; the stale
    ``remove index=1`` in pending.json then deleted the *new* #1."""
    _reopen(app, gui_env)
    pid = _synced_playlist(app, gui_env, 3)
    first = app.mgr.store.playlists[pid]["tracks"][0]["copy_name"]
    app._stage_remove_track(pid, 1, first)
    app._stage_add_files([_sources(gui_env)[3]])

    with patch.object(app.mgr, "add_track", side_effect=Crash()):
        with pytest.raises(Crash):
            app._do_sync_blocking()

    _reopen(app, gui_env)
    app._do_sync_blocking()

    assert _titles(app, pid) == ["Song Two", "Song Three", "Song Four"]


# ── Reorders combined with adds and removes ──

def test_reorder_of_newly_added_tracks_is_applied(app, gui_env):
    """p:* keys were ignored, so a reorder of staged adds was dropped."""
    app._create_playlist()
    pid = app.current_pid
    app._stage_add_files(_sources(gui_env)[:3])
    app.staging.set_reorder(pid, [{"key": "p:2"}, {"key": "p:1"}, {"key": "p:0"}])

    app._do_sync_blocking()

    assert _titles(app, pid) == ["Song Three", "Song Two", "Song One"]
    assert _disk_names(app, pid) == ["01 - Song Three.flac", "02 - Song Two.flac",
                                     "03 - Song One.flac"]


def test_reorder_with_pending_remove_keeps_intended_order(app, gui_env):
    """c:* keys pointed at shifted tracks after removes ran first."""
    pid = _synced_playlist(app, gui_env, 4)
    first = app.mgr.store.playlists[pid]["tracks"][0]["copy_name"]
    app._stage_remove_track(pid, 1, first)
    app.staging.set_reorder(pid, [{"key": "c:4"}, {"key": "c:2"}, {"key": "c:3"}])

    app._do_sync_blocking()

    assert _titles(app, pid) == ["Song Four", "Song Two", "Song Three"]


def test_failed_remove_keeps_reorder_and_remove_valid(app, gui_env):
    """After a partial sync the remaining staged ops still point at the
    right tracks (keys are rebased onto the new store).

    The failure is injected at the device delete (writer.delete), not by
    replacing the whole manager.remove_track, so the test covers what
    remove_track does to the Store when the delete fails."""
    pid = _synced_playlist(app, gui_env, 3)
    tracks = app.mgr.store.playlists[pid]["tracks"]
    app._stage_remove_track(pid, 1, tracks[0]["copy_name"])
    app._stage_add_files([_sources(gui_env)[3]])
    app.staging.set_reorder(pid, [{"key": "p:0"}, {"key": "c:3"}, {"key": "c:2"}])

    with patch.object(app.mgr.writer, "delete",
                      side_effect=PermissionError(errno.EACCES, "Permission denied")):
        errors = app._do_sync_blocking()
    assert errors
    # The add went through; remove + reorder remain staged.
    assert _titles(app, pid) == ["Song One", "Song Two", "Song Three", "Song Four"]
    assert len(app.staging.pending_removes) == 1
    assert pid in app.staging.pending_reorders

    _reopen(app, gui_env)
    assert app._do_sync_blocking() == []
    assert _titles(app, pid) == ["Song Four", "Song Three", "Song Two"]


# ── Rename / delete / offload with staged changes ──

def _rename_via_entry(app, pid, new_name):
    with patch.object(app.playlist_tree, "bbox", return_value=(0, 0, 120, 20)):
        app._start_inline_rename(pid)
    entry = app._rename_entry
    entry.delete(0, "end")
    entry.insert(0, new_name)
    _fire_binding(entry, "<Return>")
    app.root.update()


def _fire_binding(widget, sequence):
    """Run a widget's Python binding for ``sequence``.

    Tk does not deliver synthetic key events to a withdrawn window, so call the
    registered Tcl command directly with dummy event fields (%W = widget).
    """
    import re
    cmd = re.search(r"\[(\S+) ", widget.bind(sequence)).group(1)
    fields = ["0"] * 19
    fields[14] = str(widget)
    widget.tk.call(cmd, *fields)


def test_rename_carries_staged_adds_to_new_pid(app, gui_env):
    """Renaming before Sync orphaned staged adds -> KeyError on Sync."""
    app._create_playlist()
    pid = app.current_pid
    app._stage_add_files(_sources(gui_env)[:2])

    with patch("echolist.gui.messagebox.showerror"):
        _rename_via_entry(app, pid, "Gym")
    new_pid = gui.playlist_id("Gym")
    assert new_pid in app.mgr.store.playlists
    assert [a["pid"] for a in app.staging.pending_adds] == [new_pid, new_pid]

    assert app._do_sync_blocking() == []
    assert _titles(app, new_pid) == ["Song One", "Song Two"]


def test_rename_rejects_case_insensitive_folder_collision(app, gui_env):
    """Coordinator (a): rename must not reuse another playlist's folder (FAT32
    is case-insensitive), even when the other entry's pid predates playlist_id()."""
    app.mgr.store.add_playlist("legacy gym", "Gym", "Gym")
    app.mgr.writer.makedirs("Gym")
    app.mgr.store.save()
    app._create_playlist()
    pid = app.current_pid

    with patch("echolist.gui.messagebox.showerror") as err:
        _rename_via_entry(app, pid, "GYM")

    err.assert_called_once()
    assert pid in app.mgr.store.playlists
    assert app.mgr.store.playlists["legacy gym"]["folder"] == "Gym"


def test_delete_playlist_purges_its_staged_changes(app, gui_env):
    """Deleting a playlist left its staged adds behind."""
    app._create_playlist()
    pid = app.current_pid
    app._stage_add_files(_sources(gui_env)[:1])

    with patch("echolist.gui.messagebox.askyesno", return_value=True):
        app._delete_playlist()
    _flush_bg_ops(app)

    assert pid not in app.mgr.store.playlists
    assert not any(a["pid"] == pid for a in app.staging.pending_adds)


def test_offload_refuses_with_staged_changes(app, gui_env):
    """Offloading a playlist with staged removes orphaned them."""
    pid = _synced_playlist(app, gui_env, 1)
    first = app.mgr.store.playlists[pid]["tracks"][0]["copy_name"]
    app._stage_remove_track(pid, 1, first)

    with patch("echolist.gui.messagebox.askyesno", return_value=True), \
            patch("echolist.gui.messagebox.showinfo"):
        app._offload_playlist(pid)
    _flush_bg_ops(app)

    assert not app.mgr.store.playlists[pid].get("offloaded")
    assert len(app.staging.pending_removes) == 1


# ── Undo ──

def test_undo_after_unstaging_pending_add_restores_exact_state(app, gui_env):
    """An "unstage_add" undo was a no-op and later undos popped the wrong add."""
    app._create_playlist()
    one, two = _sources(gui_env)[:2]
    app._stage_add_files([one])
    app._stage_add_files([two])
    _flush_tracks(app)
    first_row = app.track_tree.get_children()[0]
    app.track_tree.selection_set(first_row)
    app._remove_track()

    def staged():
        return [Path(a["src"]).name for a in app.staging.pending_adds]

    assert staged() == ["03 Song Two.flac"]
    app._do_undo()
    assert staged() == ["01 Song One.flac", "03 Song Two.flac"]
    app._do_undo()
    assert staged() == ["01 Song One.flac"]
    app._do_undo()
    assert staged() == []


# ── Source search ──

def test_source_search_hides_appledouble_and_workspace_copies(app, gui_env):
    """Search listed ._* sidecars and the workspace's own playlist copies."""
    _synced_playlist(app, gui_env, 1)
    sidecar = gui_env["src"] / "ArtistA" / "Album1" / "._01 Song One.flac"
    sidecar.write_bytes(b"\0" * 4096)
    # Source root above both the library and the card (e.g. source == dest).
    app.mgr.config.source_root = str(gui_env["tmp"])
    app._src_search_placeholder = False
    app._src_search_var.set("song one")
    app._do_source_search()

    hits = [Path(app.source_tree.item(i, "values")[0])
            for i in app.source_tree.get_children()]
    assert hits == [gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"]


def test_source_search_matches_across_unicode_normalization(app, gui_env):
    """Coordinator (c): an NFD file name (macOS) matches an NFC query."""
    nfd = unicodedata.normalize("NFD", "Café Song.flac")
    _make_flac(gui_env["src"] / "ArtistE" / nfd, "ArtistE", "Cafe")
    app._src_search_placeholder = False
    app._src_search_var.set(unicodedata.normalize("NFC", "café"))
    app._do_source_search()

    hits = [Path(app.source_tree.item(i, "values")[0]).name
            for i in app.source_tree.get_children()]
    assert [unicodedata.normalize("NFC", h) for h in hits] == ["Café Song.flac"]


# ── Playlist folder setting ──

def test_custom_playlist_folder_applies_and_is_remembered(app, gui_env):
    """FOLDER changed in Settings was not applied and forgotten on restart."""
    _reopen(app, gui_env)
    app._show_settings()
    app._folder_var.set("MyPlaylists")
    with patch("echolist.gui.messagebox.showwarning"), \
            patch("echolist.gui.messagebox.askyesno", return_value=False):
        app._on_settings_open()
    expected = (gui_env["dest"] / "MyPlaylists").resolve()
    assert app.mgr.writer.root == expected

    # Next launch: the setup screen's [ OPEN ] goes through _open_with_dest.
    app.mgr.release_lock()
    with patch("echolist.gui.messagebox.showwarning"), \
            patch("echolist.gui.messagebox.askyesno", return_value=False):
        app._open_with_dest(str(gui_env["dest"]))
    assert app.mgr.writer.root == expected


def test_playlist_folder_outside_dest_is_rejected(app, gui_env):
    """FOLDER like ../outside escaped the destination."""
    _reopen(app, gui_env)
    root_before = app.mgr.writer.root
    app._show_settings()
    app._folder_var.set("../outside")
    with patch("echolist.gui.messagebox.showerror") as err:
        app._on_settings_open()
    err.assert_called_once()
    assert app.mgr.writer.root == root_before
    assert app.mgr.config.playlist_folder != "../outside"


# ── macOS menus, exit paths, background store mutation ──

def test_macos_context_menu_bindings(app):
    """Aqua Tk sends right-click as Button-2 / Control-click."""
    with patch("echolist.gui.platform.system", return_value="Darwin"):
        app._show_main()
    for tree in (app.playlist_tree, app.track_tree):
        assert tree.bind("<Button-2>")
        assert tree.bind("<Control-Button-1>")
    assert app.root.bind("<Command-z>")


def test_file_exit_goes_through_close_prompt(app, gui_env):
    """File -> Exit called root.quit, skipping the pending-changes prompt."""
    app._create_playlist()
    app._stage_add_files(_sources(gui_env)[:1])
    menubar = app.root.nametowidget(app.root["menu"])
    file_menu = app.root.nametowidget(menubar.entrycget(0, "menu"))
    exit_idx = next(i for i in range(file_menu.index("end") + 1)
                    if file_menu.type(i) == "command"
                    and file_menu.entrycget(i, "label") == "Exit")

    with patch("echolist.gui.messagebox.askyesnocancel", return_value=None) as ask:
        file_menu.invoke(exit_idx)
    ask.assert_called_once()


def test_sigint_shuts_down_gracefully():
    """SIGINT called os._exit(0), skipping lock release and temp cleanup."""
    handlers = {}
    with patch("echolist.gui.signal.signal",
               side_effect=lambda s, h: handlers.__setitem__(s, h)), \
            patch("echolist.gui.App") as FakeApp, \
            patch("echolist.gui.os._exit") as hard_exit:
        gui.run()
        handlers[signal.SIGINT](signal.SIGINT, None)
    hard_exit.assert_not_called()
    FakeApp.return_value._schedule_callback.assert_called_once()


def test_track_load_does_not_mutate_store_off_main_thread(app, gui_env):
    """_load_tracks_bg ran rescan_playlist (store write) on a worker thread."""
    pid = _synced_playlist(app, gui_env, 2)
    threads = []
    real = app.mgr.rescan_playlist

    def spy(p):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(p)

    app._invalidate_caches()
    with patch.object(app.mgr, "rescan_playlist", side_effect=spy):
        app.current_pid = pid
        app._refresh_tracks()
        _flush_tracks(app)
    assert threads and all(threads)


# ── config: legacy backup ids ──

def test_rename_backup_pid_handles_legacy_pid_with_slash(tmp_path):
    """Coordinator (b): stores from before playlist_id() sanitizing can hold
    pids like "rock/pop"; renaming them raised and aborted the rename midway."""
    import json
    from echolist import config
    root = tmp_path / "ws"
    root.mkdir()
    legacy = config.BACKUPS_ROOT / config._workspace_id(root) / "rock" / "pop"
    legacy.mkdir(parents=True)
    (legacy / "20260101_000000.json").write_text(
        json.dumps({"folder": "rock_pop", "tracks": []}), encoding="utf-8")

    config.rename_backup_pid(root, "rock/pop", "rock_pop", "Rock Pop")

    moved = config.load_backup(root, "rock_pop", "20260101_000000")
    assert moved is not None and moved["folder"] == "Rock Pop"
    assert not legacy.exists()


def test_rename_backup_pid_rejects_escape(tmp_path):
    from echolist import config
    with pytest.raises(ValueError):
        config.rename_backup_pid(tmp_path, "../../etc", "x")
    with pytest.raises(ValueError):
        config.rename_backup_pid(tmp_path, "ok", "a/b")


def test_reorder_large_playlist_keeps_pad_width_titles_and_tags(app, gui_env):
    """Reorder names use naming.pad_width (3 digits for 100+
    tracks like add/renumber do), keep titles containing " - ", and rewrite
    TRACKNUMBER to the new position."""
    from echolist.tags import read_playlist_tags
    lib = gui_env["tmp"] / "big"
    for i in range(101):
        title = "Band - Hit" if i == 100 else f"Track {i:03d}"
        _make_flac(lib / f"{i:03d}.flac", "Artist", title)
    app.mgr.config.source_root = str(gui_env["tmp"])
    pid = app.mgr.create_playlist("Big")
    for f in sorted(lib.iterdir()):
        app.mgr.add_track(pid, f)
    n = len(app.mgr.store.playlists[pid]["tracks"])
    assert n == 101
    order = [{"key": f"c:{n}"}] + [{"key": f"c:{i}"} for i in range(1, n)]
    app.staging.set_reorder(pid, order)

    app._do_sync_blocking()

    assert not app.staging.has_pending
    tracks = sorted(app.mgr.store.playlists[pid]["tracks"], key=lambda t: t["index"])
    assert tracks[0]["copy_name"] == "001 - Band - Hit.flac"
    assert tracks[1]["copy_name"] == "002 - Track 000.flac"
    folder = app.mgr.writer.root / app.mgr.store.playlists[pid]["folder"]
    assert sorted(f.name for f in folder.glob("*.flac")) == sorted(t["copy_name"] for t in tracks)
    for t in (tracks[0], tracks[1], tracks[-1]):
        assert read_playlist_tags(folder / t["copy_name"])["tracknumber"] == str(t["index"])
