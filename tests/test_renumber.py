"""Renumbering: track numbers are rewritten with the file names, names
without an index prefix are kept, nothing is overwritten, and the whole
playlist shares one pad width across the 99 -> 100 boundary."""

import pytest
from mutagen.flac import FLAC

from conftest import _make_flac
from echolist.manager import PlaylistManager


# ── Renumbering rewrites TRACKNUMBER and keeps names ──


def test_remove_track_rewrites_tracknumber(manager, source):
    pid = manager.create_playlist("Mix")
    for src in sorted(source.rglob("*.flac")):
        manager.add_track(pid, src)
    manager.remove_track(pid, 1)

    folder = manager.writer.root / "Mix"
    for t in manager.store.playlists[pid]["tracks"]:
        f = FLAC(folder / t["copy_name"])
        assert f["TRACKNUMBER"] == [str(t["index"])], t["copy_name"]
        assert f["ECHOLIST_INDEX"] == [str(t["index"])], t["copy_name"]
    assert manager.audit_playlist_metadata(pid) == []


def test_remove_then_add_has_no_duplicate_tracknumbers(manager, source):
    pid = manager.create_playlist("Mix")
    srcs = sorted(source.rglob("*.flac"))
    for src in srcs:
        manager.add_track(pid, src)
    manager.remove_track(pid, 1)
    manager.add_track(pid, srcs[0])

    folder = manager.writer.root / "Mix"
    numbers = [FLAC(folder / t["copy_name"])["TRACKNUMBER"][0]
               for t in manager.store.playlists[pid]["tracks"]]
    assert numbers == ["1", "2", "3"]


def test_adopt_keeps_names_without_index_prefix(manager):
    folder = manager.writer.root / "Old Mix"
    _make_flac(folder / "01. Song.flac", "X", "Song")
    p = folder / "Band - Title.flac"
    _make_flac(p, "Band", "Title")
    f = FLAC(p)
    del f["TITLE"]
    f.save()

    pid = manager.adopt_playlist("Old Mix")
    names = [t["copy_name"] for t in manager.store.playlists[pid]["tracks"]]
    assert names == ["01 - Song.flac", "02 - Band - Title.flac"]


def test_renumber_never_overwrites_another_track(manager):
    """'001 - Y' renamed to '01 - Y' must not clobber the existing '01 - Y'."""
    folder = manager.writer.root / "Legacy"
    _make_flac(folder / "001 - Y.flac", "First", "Y")
    _make_flac(folder / "01 - Y.flac", "Second", "Y")

    pid = manager.adopt_playlist("Legacy")
    tracks = manager.store.playlists[pid]["tracks"]
    artists = [FLAC(folder / t["copy_name"])["ARTIST"][0] for t in tracks]
    assert sorted(artists) == ["First", "Second"]
    assert len(list(folder.glob("*.flac"))) == 2


# ── Consistent padding across the 99 -> 100 boundary ──


@pytest.fixture
def big_source(tmp_path):
    lib = tmp_path / "biglib"
    for i in range(1, 102):
        _make_flac(lib / f"{i:03d}.flac", f"Artist{i}", f"Track {i}")
    return lib


def _assert_consistent(mgr, pid, width):
    tracks = mgr.store.playlists[pid]["tracks"]
    names = [t["copy_name"] for t in tracks]
    assert all(n.split(" - ", 1)[0].isdigit() and len(n.split(" - ", 1)[0]) == width
               for n in names), names
    on_disk = sorted(p.name for p in (mgr.writer.root / mgr.store.playlists[pid]["folder"]).iterdir()
                     if p.suffix == ".flac")
    assert on_disk == names  # lexicographic order on disk == playlist order


def test_crossing_100_tracks_repads_whole_playlist(big_source, tmp_path):
    dest = tmp_path / "card"
    dest.mkdir()
    mgr = PlaylistManager.init(big_source, dest)
    pid = mgr.create_playlist("Big")
    srcs = sorted(big_source.glob("*.flac"))
    for src in srcs[:100]:
        mgr.add_track(pid, src)
    _assert_consistent(mgr, pid, 3)
    assert mgr.rescan_playlist(pid) is False
    assert [t["copy_name"] for t in mgr.store.playlists[pid]["tracks"]][:2] == [
        "001 - Track 1.flac", "002 - Track 2.flac"]

    mgr.remove_track(pid, 50)
    _assert_consistent(mgr, pid, 2)
    assert mgr.rescan_playlist(pid) is False
    mgr.release_lock()
