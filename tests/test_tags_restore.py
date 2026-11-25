"""An adoption restore point brings back every tag field the tagger
changes, exactly: full values, repeated values and absent fields."""

from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TDRC, TIT2, TPE1, TRCK, TSO2
from mutagen.wave import WAVE

from conftest import TINY_FLAC
from test_tag_formats import _make_mp3, _make_wav


def _id3(path: Path):
    if path.suffix == ".mp3":
        return ID3(path)
    return WAVE(path).tags


def _frames(tags) -> dict:
    return {k: [str(x) for x in v.text] for k, v in tags.items() if hasattr(v, "text")}


ORIGINAL_ID3 = [
    TIT2(encoding=3, text=["Song"]),
    TPE1(encoding=3, text=["Artist"]),
    TRCK(encoding=3, text=["4/20"]),
    TDRC(encoding=3, text=["2020-02-03"]),
    TSO2(encoding=3, text=["sort"]),
]


@pytest.mark.parametrize("ext", [".wav", ".mp3"])
def test_adoption_restore_point_restores_id3_exactly(manager, ext):
    folder = manager.writer.root / "Old"
    p = folder / f"Song{ext}"
    if ext == ".wav":
        _make_wav(p, with_id3=False)
        f = WAVE(p)
        f.add_tags()
        for fr in ORIGINAL_ID3:
            f.tags.add(fr)
        f.save()
    else:
        _make_mp3(p, ORIGINAL_ID3)
    before = _frames(_id3(p))

    pid = manager.adopt_playlist("Old")
    copy = folder / manager.store.playlists[pid]["tracks"][0]["copy_name"]
    assert _frames(_id3(copy)) != before  # adoption really did change tags

    assert manager.restore_playlist_metadata(pid, "before_echolist") == 1
    assert _frames(_id3(copy)) == before


def test_adoption_restore_point_restores_flac_exactly(manager):
    folder = manager.writer.root / "Old"
    folder.mkdir()
    p = folder / "Song.flac"
    p.write_bytes(TINY_FLAC)
    f = FLAC(p)
    f["TITLE"] = "Song"
    f["ARTIST"] = ["A", "B"]
    f["TRACKNUMBER"] = "4/20"
    f["DATE"] = "2020-02-03"
    f["ALBUMARTISTSORT"] = "sort"
    f["MUSICBRAINZ_ALBUMID"] = "abc"
    f.save()
    before = dict(FLAC(p).tags.as_dict())

    pid = manager.adopt_playlist("Old")
    copy = folder / manager.store.playlists[pid]["tracks"][0]["copy_name"]
    assert manager.restore_playlist_metadata(pid, "before_echolist") == 1
    assert dict(FLAC(copy).tags.as_dict()) == before
