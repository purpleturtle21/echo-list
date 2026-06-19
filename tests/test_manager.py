"""M3 tests — operations + originals-untouched assertion."""

import pytest
from mutagen.flac import FLAC

from echolist.manager import PlaylistManager
from echolist.safe_write import UnsafeWriteError
from conftest import assert_originals_untouched


def test_create_playlist(manager):
    pid = manager.create_playlist("Workout")
    assert pid == "workout"
    assert (manager.writer.root / "Workout").is_dir()
    assert "workout" in manager.store.playlists


def test_create_duplicate_raises(manager):
    manager.create_playlist("Workout")
    with pytest.raises(ValueError):
        manager.create_playlist("Workout")


def test_add_track(manager, source):
    pid = manager.create_playlist("Workout")
    src = source / "ArtistA" / "Album1" / "01 Song One.flac"
    rel = manager.add_track(pid, src)

    copy_path = manager.writer.root / rel
    assert copy_path.exists()

    f = FLAC(copy_path)
    assert f["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f["ALBUM"] == ["Workout"]
    assert f["TRACKNUMBER"] == ["1"]
    assert f["ECHOLIST_ROLE"] == ["playlist-copy"]
    assert f["ARTIST"] == ["ArtistA"]
    assert f["TITLE"] == ["Song One"]


def test_add_two_tracks(manager, source):
    pid = manager.create_playlist("Road Trip")
    src1 = source / "ArtistA" / "Album1" / "01 Song One.flac"
    src2 = source / "ArtistB" / "Album2" / "03 Song Two.flac"
    rel1 = manager.add_track(pid, src1)
    rel2 = manager.add_track(pid, src2)

    assert "01 - " in rel1
    assert "02 - " in rel2

    f1 = FLAC(manager.writer.root / rel1)
    f2 = FLAC(manager.writer.root / rel2)
    assert f1["TRACKNUMBER"] == ["1"]
    assert f2["TRACKNUMBER"] == ["2"]


def test_originals_untouched(manager, source, hashes):
    pid = manager.create_playlist("Test")
    for flac in sorted(source.rglob("*.flac")):
        manager.add_track(pid, flac)
    manager.remove_track(pid, 1)
    assert_originals_untouched(source, hashes)


def test_remove_track(manager, source):
    pid = manager.create_playlist("Mix")
    src1 = source / "ArtistA" / "Album1" / "01 Song One.flac"
    src2 = source / "ArtistB" / "Album2" / "03 Song Two.flac"
    rel1 = manager.add_track(pid, src1)
    rel2 = manager.add_track(pid, src2)

    manager.remove_track(pid, 1)

    assert not (manager.writer.root / rel1).exists()
    tracks = manager.store.playlists[pid]["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["index"] == 1
    # Track was renumbered: 02 -> 01
    assert tracks[0]["copy_name"].startswith("01 - ")
    assert (manager.writer.root / "Mix" / tracks[0]["copy_name"]).exists()


def test_stats(manager, source):
    pid = manager.create_playlist("S")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    device_tracks, workspace_bytes = manager.compute_expensive_stats()
    s = manager.stats(cached_device_tracks=device_tracks, cached_workspace_bytes=workspace_bytes)
    assert s["playlists"] == 1
    assert s["tracks"] == 1
    assert s["workspace_bytes"] > 0


def test_overlap_source_inside_workspace(tmp_path):
    """Source inside workspace must be rejected — workspace could overwrite originals."""
    workspace_parent = tmp_path / "card"
    workspace_parent.mkdir()
    src = workspace_parent / "Playlists" / "sneaky"
    src.mkdir(parents=True)
    with pytest.raises(UnsafeWriteError):
        PlaylistManager.init(src, workspace_parent)


def test_same_dir_overlap(tmp_path):
    d = tmp_path / "same"
    d.mkdir()
    with pytest.raises(UnsafeWriteError):
        PlaylistManager.init(d / "Playlists", d)


def test_workspace_inside_source_allowed(tmp_path):
    """Common case: source=/sd_card, dest=/sd_card — workspace is /sd_card/Playlists."""
    src = tmp_path / "sd_card"
    src.mkdir()
    mgr = PlaylistManager.init(src, src)
    assert mgr.writer.root == (src / "Playlists").resolve()
