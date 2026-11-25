"""The sync engine: staged removes are matched by a stable identity,
a reorder's commit is recorded rather than inferred, and recovery keeps what
never started and what it could not finish."""

import errno
from unittest.mock import patch

import pytest

import echolist.gui as gui
from conftest import _make_flac
from test_gui import (app, gui_env, isolated_home, callback_errors)  # noqa: F401  (fixtures)
from test_gui_sync_safety import (_sources, _titles, _disk_names,
                                  _synced_playlist, _reopen, Crash)


# ── Staged removes are matched by stable identity ──


def _track(app, pid, index):
    return next(t for t in app.mgr.store.playlists[pid]["tracks"] if t["index"] == index)


def test_rejected_remove_never_retargets_the_replacement_track(app, gui_env):
    """A remove rejected as stale had its copy_name rewritten by rebase to
    whatever now sat at that index, so the next sync deleted that track."""
    import shutil
    pid = _synced_playlist(app, gui_env, 2)
    first = _track(app, pid, 1)
    app._stage_remove_track(pid, 1, first["copy_name"])
    folder = app.mgr.writer.root / app.mgr.store.playlists[pid]["folder"]
    (folder / first["copy_name"]).unlink()  # replaced outside EchoList
    shutil.copy(_sources(gui_env)[2], folder / "01 - Song Three.flac")
    app.mgr.rescan_playlist(pid)

    first_errors = app._do_sync_blocking()
    second_errors = app._do_sync_blocking()

    assert first_errors
    assert second_errors == []
    assert _titles(app, pid) == ["Song Three", "Song Two"]
    assert not app.staging.pending_removes


def test_removing_first_and_last_of_100_tracks_survives_the_pad_change(app, gui_env):
    """Removing #100 re-pads 001.. to 01..; the staged remove of #1 still
    named '001 - ...' and was rejected as stale."""
    app._create_playlist()
    pid = app.current_pid
    for i in range(1, 101):
        src = gui_env["src"] / "Many" / f"{i:03d}.flac"
        _make_flac(src, "Many", f"Tune {i:03d}")
        app.mgr.add_track(pid, src)
    app._stage_remove_track(pid, 1, _track(app, pid, 1)["copy_name"])
    app._stage_remove_track(pid, 100, _track(app, pid, 100)["copy_name"])

    assert app._do_sync_blocking() == []

    titles = _titles(app, pid)
    assert len(titles) == 98
    assert titles[0] == "Tune 002" and titles[-1] == "Tune 099"
    assert _disk_names(app, pid)[0] == "01 - Tune 002.flac"


def test_remove_matches_a_name_stored_in_a_different_unicode_form(app, gui_env):
    """The staged copy_name (NFC) was compared byte-for-byte with the
    store's name, which a rescan on macOS takes from disk in NFD."""
    import unicodedata
    app._create_playlist()
    pid = app.current_pid
    src = gui_env["src"] / "Cafe" / "cafe.flac"
    _make_flac(src, "Cafe", "Café")
    app.mgr.add_track(pid, src)
    nfc = _track(app, pid, 1)["copy_name"]
    app._stage_remove_track(pid, 1, nfc)
    _track(app, pid, 1)["copy_name"] = unicodedata.normalize("NFD", nfc)

    assert app._do_sync_blocking() == []
    assert app.mgr.store.playlists[pid]["tracks"] == []


# ── A reorder's commit is recorded, not inferred ──


def test_reorder_store_save_failure_does_not_double_shuffle_on_retry(app, gui_env):
    """store.save() failing in the reorder commit discarded the journal
    and left the reorder staged, so the retry shuffled the files again."""
    pid = _synced_playlist(app, gui_env, 3)
    app.staging.set_reorder(pid, [{"key": "c:3"}, {"key": "c:1"}, {"key": "c:2"}])
    real_save = app.mgr.store.save
    calls = []

    def full_once():
        calls.append(1)
        if len(calls) == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_save()

    with patch.object(app.mgr.store, "save", side_effect=full_once):
        assert app._do_sync_blocking()
    assert app._do_sync_blocking() == []
    _reopen(app, gui_env)

    assert _titles(app, pid) == ["Song Three", "Song One", "Song Two"]
    assert _disk_names(app, pid) == ["01 - Song Three.flac", "02 - Song One.flac",
                                     "03 - Song Two.flac"]
    assert pid not in app.staging.pending_reorders
    assert not gui.journal_file(app.mgr.writer.root).exists()


def test_reorder_store_save_failure_is_committed_on_next_open(app, gui_env):
    """The same failure followed by a restart (no retry in between)."""
    pid = _synced_playlist(app, gui_env, 3)
    app.staging.set_reorder(pid, [{"key": "c:3"}, {"key": "c:1"}, {"key": "c:2"}])
    with patch.object(app.mgr.store, "save",
                      side_effect=OSError(errno.ENOSPC, "No space left on device")):
        assert app._do_sync_blocking()
    _reopen(app, gui_env)
    assert _titles(app, pid) == ["Song Three", "Song One", "Song Two"]
    assert pid not in app.staging.pending_reorders
    assert not gui.journal_file(app.mgr.writer.root).exists()


def _same_title_pair(app, gui_env):
    a = gui_env["src"] / "X1" / "x.flac"
    b = gui_env["src"] / "X2" / "x.flac"
    _make_flac(a, "Band A", "Same")
    _make_flac(b, "Band B", "Same")
    app._create_playlist()
    pid = app.current_pid
    app.mgr.add_track(pid, a)
    app.mgr.add_track(pid, b)
    return pid, "X1/x.flac", "X2/x.flac"


def _src_order(app, pid):
    tracks = sorted(app.mgr.store.playlists[pid]["tracks"], key=lambda t: t["index"])
    return [t["src_path"] for t in tracks]


def test_same_title_swap_recovered_before_the_store_save_keeps_identities(app, gui_env):
    """Recovery decided 'already committed' because the store's names
    equalled the plan's new names, which is always true for a same-title swap,
    so each entry kept the other file's src_path."""
    pid, a, b = _same_title_pair(app, gui_env)
    app.staging.set_reorder(pid, [{"key": "c:2"}, {"key": "c:1"}])
    with patch.object(app.mgr.store, "save", side_effect=Crash()):
        with pytest.raises(Crash):
            app._do_sync_blocking()
    _reopen(app, gui_env)

    assert _src_order(app, pid) == [b, a]
    folder = app.mgr.writer.root / app.mgr.store.playlists[pid]["folder"]
    from echolist.tags import read_playlist_tags
    assert read_playlist_tags(folder / "01 - Same.flac")["artist"] == "Band B"


def test_recovery_records_its_rename_phase(app, gui_env):
    """Recovery ran the final rename pass without recording phase
    'final'; a second crash then made the next recovery treat parked files as
    unmoved and restore them under a taken name."""
    pid, a, b = _same_title_pair(app, gui_env)
    app.staging.set_reorder(pid, [{"key": "c:2"}, {"key": "c:1"}])
    from echolist.safe_write import SafeWriter
    real_rename = SafeWriter.rename

    def crash_on(n):
        calls = []

        def rename(self, old, new):
            calls.append(old)
            if len(calls) == n:
                raise Crash()
            return real_rename(self, old, new)
        return patch.object(SafeWriter, "rename", rename)

    with crash_on(2), pytest.raises(Crash):
        app._do_sync_blocking()          # one file parked, phase "tmp"
    with crash_on(3), pytest.raises(Crash):
        _reopen(app, gui_env)            # recovery: parks the other, moves one back
    _reopen(app, gui_env)

    assert _disk_names(app, pid) == ["01 - Same.flac", "02 - Same.flac"]
    assert _src_order(app, pid) == [b, a]


# ── Recovery keeps what never started and what it couldn't fix ──


def test_reorder_that_never_started_survives_an_interrupted_sync(app, gui_env):
    """Every pid with an unfinished journal step lost its staged
    reorder, including reorders the crash never reached."""
    one = _synced_playlist(app, gui_env, 2)
    app.staging.set_reorder(one, [{"key": "c:2"}, {"key": "c:1"}])
    app._create_playlist()
    two = app.current_pid
    app._stage_add_files(_sources(gui_env)[2:3])

    with patch.object(app.mgr, "add_track", side_effect=Crash()):
        with pytest.raises(Crash):
            app._do_sync_blocking()
    _reopen(app, gui_env)

    assert app.staging.pending_reorders.get(one) == [{"key": "c:2"}, {"key": "c:1"}]
    assert app._do_sync_blocking() == []
    assert _titles(app, one) == ["Song Two", "Song One"]
    assert _titles(app, two) == ["Song Three"]


def test_journal_is_kept_when_recovery_cannot_finish(app, gui_env):
    """The journal was discarded even when finishing the reorder failed,
    so the half-renamed playlist could never be completed."""
    pid = _synced_playlist(app, gui_env, 3)
    app.staging.set_reorder(pid, [{"key": "c:3"}, {"key": "c:1"}, {"key": "c:2"}])
    real_rename = app.mgr.writer.rename
    calls = []

    def crash_on_fourth(old, new):
        calls.append(old)
        if len(calls) == 4:
            raise Crash()
        return real_rename(old, new)

    with patch.object(app.mgr.writer, "rename", side_effect=crash_on_fourth):
        with pytest.raises(Crash):
            app._do_sync_blocking()
    jpath = gui.journal_file(app.mgr.writer.root)

    from echolist.safe_write import SafeWriter
    with patch.object(SafeWriter, "rename", side_effect=OSError(errno.EIO, "I/O error")):
        warn = _reopen(app, gui_env)["warning"]
    assert warn.called
    assert jpath.exists()

    app._refresh_all()  # must not rescan the parked files out of the store
    _reopen(app, gui_env)
    assert _titles(app, pid) == ["Song Three", "Song One", "Song Two"]
    assert [t["src_path"] for t in sorted(app.mgr.store.playlists[pid]["tracks"],
                                          key=lambda t: t["index"])] == [
        "ArtistC/Album3/05 Song Three.flac", "ArtistA/Album1/01 Song One.flac",
        "ArtistB/Album2/03 Song Two.flac"]
    assert not jpath.exists()


# ── An add published before the store save is adopted ──


def test_add_published_before_store_save_is_not_copied_twice(app, gui_env):
    """A crash after copy_in published the file but before store.save()
    left the add staged; rescan saw an unknown file, and the retry copied the
    track a second time."""
    pid = _synced_playlist(app, gui_env, 1)
    app._stage_add_files(_sources(gui_env)[1:2])
    with patch.object(app.mgr.store, "save", side_effect=Crash()):
        with pytest.raises(Crash):
            app._do_sync_blocking()
    _reopen(app, gui_env)

    assert not app.staging.pending_adds
    assert app._do_sync_blocking() == []
    assert _titles(app, pid) == ["Song One", "Song Two"]
    assert _track(app, pid, 2)["src_path"] == "ArtistB/Album2/03 Song Two.flac"
