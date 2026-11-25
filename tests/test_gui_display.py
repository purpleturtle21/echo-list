"""What the GUI shows and checks: WAV/AIFF titles, literal file-name
search, no stale rows or reorders, folder-name checks on rename, and reorder
retagging through the writer."""

from pathlib import Path
from unittest.mock import patch

import echolist.gui as gui
from conftest import _make_flac
from test_gui import (app, gui_env, isolated_home, callback_errors, pump)  # noqa: F401  (fixtures)
from test_gui_sync_safety import _sources, _synced_playlist


# ── WAV / AIFF titles ──


def _make_tagged_wav(path, title, artist):
    import wave
    from mutagen.id3 import TIT2, TPE1
    from mutagen.wave import WAVE
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 800)
    f = WAVE(path)
    f.add_tags()
    f.tags.add(TIT2(encoding=3, text=title))
    f.tags.add(TPE1(encoding=3, text=artist))
    f.save()


def test_wav_titles_are_shown_for_staged_and_synced_tracks(app, gui_env):
    """Titles were read with mutagen easy=True, which has no title key
    for WAV/AIFF ID3 chunks, so these tracks showed blank."""
    wav = gui_env["src"] / "Wav" / "take.wav"
    _make_tagged_wav(wav, "Wave Song", "Wave Band")
    app._create_playlist()
    pid = app.current_pid
    app._stage_add_files([wav])
    assert (app.staging.pending_adds[0]["title"],
            app.staging.pending_adds[0]["artist"]) == ("Wave Song", "Wave Band")
    assert app._do_sync_blocking() == []
    app._tag_cache.clear()
    app._refresh_tracks()
    pump(app)
    rows = [app.track_tree.item(i, "values") for i in app.track_tree.get_children()]
    assert [tuple(r[1:]) for r in rows] == [("Wave Song", "Wave Band")]


def test_untagged_file_falls_back_to_its_name(app, gui_env):
    """An empty title displays the file stem, not a blank row."""
    wav = gui_env["src"] / "Wav" / "untitled take.wav"
    _make_tagged_wav(wav, "", "")
    app._create_playlist()
    app._stage_add_files([wav])
    assert app.staging.pending_adds[0]["title"] == "untitled take"


# ── File-name search treats names literally ──


def test_moved_source_with_brackets_in_its_name_is_found(app, gui_env):
    """The filename fallback used rglob(name) unescaped, so '[Remix]'
    became a character class and the moved file was never found."""
    moved = gui_env["src"] / "Moved" / "Song [Remix].flac"
    _make_flac(moved, "ArtistR", "Song Remix")
    root = Path(gui_env["src"]).resolve()
    assert gui._resolve_source_file("Old/Song [Remix].flac", root) == moved.resolve()


# ── Stale rows and stale reorders ──


def test_remove_during_loading_does_not_use_the_previous_playlists_rows(app, gui_env):
    """_track_data kept the previous playlist's rows while 'Loading...'
    was shown, so Delete staged a remove of the wrong track."""
    app._create_playlist()
    b = app.current_pid
    app.mgr.add_track(b, _sources(gui_env)[2])
    _synced_playlist(app, gui_env, 2)          # A's rows are on screen
    assert len(app._track_data) == 2
    app.current_pid = b
    app._tag_cache.clear()                      # B's tags must load in the background
    app._refresh_tracks()
    assert app.track_tree.get_children() == ("_loading",)
    app.track_tree.selection_set("_loading")
    app._remove_track()
    assert not app.staging.pending_removes
    pump(app)


def test_declining_an_orphan_remove_still_clears_the_shifted_reorder(app, gui_env):
    """Answering No to the 'no source' prompt returned before
    unstage_reorder, leaving a reorder whose p:N keys now named other adds."""
    import shutil
    pid = _synced_playlist(app, gui_env, 1)
    folder = app.mgr.writer.root / app.mgr.store.playlists[pid]["folder"]
    shutil.copy(_sources(gui_env)[3], folder / "02 - Stray.flac")  # no source
    app._refresh_tracks()
    pump(app)
    app._stage_add_files(_sources(gui_env)[1:3])
    app.staging.set_reorder(pid, [{"key": "p:1"}, {"key": "p:0"},
                                  {"key": "c:1"}, {"key": "c:2"}])
    app._refresh_tracks()
    pump(app)
    rows = {r["key"]: i for r, i in zip(app._track_data, app.track_tree.get_children())}
    app.track_tree.selection_set([rows["p:0"], rows["c:2"]])
    with patch("echolist.gui.messagebox.askyesno", return_value=False):
        app._remove_track()

    assert [a["title"] for a in app.staging.pending_adds] == ["Song Three"]
    assert pid not in app.staging.pending_reorders


# ── Rename checks the folder name the way the card does ──


def test_rename_refuses_a_dot_folder(app, gui_env):
    pid = _synced_playlist(app, gui_env, 1)
    folder = app.mgr.store.playlists[pid]["folder"]
    with patch("echolist.gui.messagebox.showerror") as err:
        assert not app._rename_playlist(pid, ".Hidden")
    err.assert_called_once()
    assert app.mgr.store.playlists[pid]["folder"] == folder
    assert (app.mgr.writer.root / folder).is_dir()


def test_rename_refuses_a_folder_differing_only_in_unicode_form(app, gui_env):
    """macOS (and so the card as macOS sees it) treats NFC and NFD names as
    one; the casefold-only compare let two playlists share a folder."""
    app.mgr.create_playlist("Café")
    pid = _synced_playlist(app, gui_env, 1)
    with patch("echolist.gui.messagebox.showerror") as err:
        assert not app._rename_playlist(pid, "CAFÉ")
    err.assert_called_once()


# ── Reorder retags through the writer; one TMP_PREFIX ──


def test_reorder_retags_through_writer_edit_in_place(app, gui_env):
    """Tag writes re-create macOS ._ sidecars on FAT32; edit_in_place drops
    them. _commit_reorder wrote the tags directly."""
    pid = _synced_playlist(app, gui_env, 2)
    app.staging.set_reorder(pid, [{"key": "c:2"}, {"key": "c:1"}])
    real = app.mgr.writer.edit_in_place
    edited = []

    def spy(rel, edit):
        edited.append(Path(rel).name)
        return real(rel, edit)

    with patch.object(app.mgr.writer, "edit_in_place", side_effect=spy):
        assert app._do_sync_blocking() == []
    assert sorted(edited) == ["01 - Song Two.flac", "02 - Song One.flac"]


def test_gui_does_not_redefine_tmp_prefix():
    """The parking prefix is defined once, in manager.py (rescan ignores it,
    recovery restores it); gui.py must import it, not keep a copy."""
    import inspect
    import re
    from echolist import manager
    assert not re.search(r"^TMP_PREFIX\s*=", inspect.getsource(gui), re.M), (
        "gui.py defines TMP_PREFIX; use `from .manager import TMP_PREFIX`")
    assert gui.TMP_PREFIX == manager.TMP_PREFIX
