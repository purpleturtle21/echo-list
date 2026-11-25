"""Tagging by format: WAV/AIFF carry an ID3 chunk, MP3 sort frames are
stripped on every Mutagen version, one registry lists the formats, formats
EchoList can't tag are copied untouched, and a copy is tagged before it gets
its final name."""

from __future__ import annotations

import os
import struct
import wave
from pathlib import Path

import pytest
from mutagen.aiff import AIFF
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK, TSO2, TXXX
from mutagen.wave import WAVE

from echolist import tags as tagmod
from echolist.manager import AUDIO_EXTS
from echolist.tags import _EASY_STRIP_TAGS, read_playlist_tags, restore_tags, tags_supported


# ── Fixture builders ──


TITLE = "A Fairly Long Song Title"


def _pcm_bytes(n_frames: int = 64) -> bytes:
    return b"".join(struct.pack("<h", (i * 517) % 32768 - 16384) for i in range(n_frames))


def _make_wav(path: Path, with_id3: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(_pcm_bytes())
    if with_id3:
        f = WAVE(path)
        f.add_tags()
        _add_source_frames(f.tags)
        f.save()


def _make_aiff(path: Path, with_id3: bool = True) -> None:
    """Minimal AIFF (big-endian PCM) built by hand; stdlib aifc is gone in 3.13."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = b"".join(struct.pack(">h", (i * 517) % 32768 - 16384) for i in range(64))
    # 44100 Hz as an 80-bit IEEE extended float
    rate = bytes.fromhex("400EAC44000000000000")
    comm = struct.pack(">hIh", 1, 64, 16) + rate
    ssnd = struct.pack(">II", 0, 0) + pcm
    body = b"AIFF"
    body += b"COMM" + struct.pack(">I", len(comm)) + comm
    body += b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)
    if with_id3:
        f = AIFF(path)
        f.add_tags()
        _add_source_frames(f.tags)
        f.save()


def _add_source_frames(tags: ID3) -> None:
    """Frames a typical ripped source WAV carries."""
    tags.add(TIT2(encoding=3, text=[TITLE]))
    tags.add(TPE1(encoding=3, text=["Some Artist"]))
    tags.add(TRCK(encoding=3, text=["1/20"]))
    tags.add(TALB(encoding=3, text=["Original Album"]))
    tags.add(TSO2(encoding=3, text=["Sort Album Artist"]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=["abc"]))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=b"\x89PNG fake"))


def _aiff_ssnd(path: Path) -> bytes:
    data = path.read_bytes()
    pos = 12
    while pos < len(data):
        cid, size = data[pos:pos + 4], struct.unpack(">I", data[pos + 4:pos + 8])[0]
        if cid == b"SSND":
            return data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    raise AssertionError("no SSND chunk")


def _wav_frames(path: Path):
    with wave.open(str(path), "rb") as w:
        return w.getparams(), w.readframes(w.getnframes())


def _adts_aac(path: Path, n_frames: int = 8) -> None:
    """Raw ADTS AAC-LC stream (44.1 kHz mono), as produced by many encoders."""
    payload = bytes.fromhex("2110050020a01933")
    frame_len = 7 + len(payload)
    header = bytes([
        0xFF, 0xF1,
        (1 << 6) | (4 << 2),            # profile LC, 44100 Hz, chan bit 2 = 0
        (1 << 6) | ((frame_len >> 11) & 3),  # mono
        (frame_len >> 3) & 0xFF,
        ((frame_len & 7) << 5) | 0x1F,
        0xFC,
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((header + payload) * n_frames)


@pytest.fixture
def wav_src(source):
    p = source / "Some Artist" / "Original Album" / f"01 - {TITLE}.wav"
    _make_wav(p)
    return p


@pytest.fixture
def aiff_src(source):
    p = source / "Some Artist" / "Original Album" / f"01 - {TITLE}.aiff"
    _make_aiff(p)
    return p


_MP3_FRAME = bytes.fromhex("fffb9064") + b"\x00" * 413


def _make_mp3(path: Path, frames) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_MP3_FRAME * 8)
    t = ID3()
    for f in frames:
        t.add(f)
    t.save(path)


# ── WAV / AIFF ──


@pytest.mark.parametrize("fixture", ["wav_src", "aiff_src"])
def test_add_id3_chunk_file_names_copy_from_tit2(manager, request, fixture):
    src = request.getfixturevalue(fixture)
    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, src)
    assert Path(rel).name == f"01 - {TITLE}{src.suffix}"


@pytest.mark.parametrize("fixture,cls", [("wav_src", WAVE), ("aiff_src", AIFF)])
def test_add_id3_chunk_file_gets_playlist_tags(manager, request, fixture, cls):
    src = request.getfixturevalue(fixture)
    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, src)
    tags = cls(manager.writer.root / rel).tags
    assert tags["TPE2"].text == ["* PLAYLISTS *"]
    assert tags["TALB"].text == ["Mix"]
    assert tags["TRCK"].text == ["1"]
    assert tags["TPOS"].text == ["1"]
    # Title, artist and cover art survive; sort / MusicBrainz frames go.
    assert tags["TIT2"].text == [TITLE]
    assert tags["TPE1"].text == ["Some Artist"]
    assert tags.getall("APIC")
    assert not tags.getall("TSO2")
    assert not tags.getall("TXXX:MusicBrainz Album Id")
    assert manager.audit_playlist_metadata(pid) == []


@pytest.mark.parametrize("make,ext", [(_make_wav, ".wav"), (_make_aiff, ".aiff")])
def test_read_playlist_tags_id3_chunk(tmp_path, make, ext):
    p = tmp_path / f"x{ext}"
    make(p)
    tags = read_playlist_tags(p)
    assert tags["title"] == TITLE
    assert tags["artist"] == "Some Artist"
    assert tags["album"] == "Original Album"
    assert tags["tracknumber"] == "1"


@pytest.mark.parametrize("make,cls,ext", [(_make_wav, WAVE, ".wav"), (_make_aiff, AIFF, ".aiff")])
def test_untagged_id3_chunk_file_gets_new_chunk(manager, source, make, cls, ext):
    src = source / "Bare" / f"Bare Song{ext}"
    make(src, with_id3=False)
    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, src)
    assert Path(rel).name == f"01 - Bare Song{ext}"
    tags = cls(manager.writer.root / rel).tags
    assert tags is not None
    assert tags["TPE2"].text == ["* PLAYLISTS *"]
    assert tags["TRCK"].text == ["1"]


def test_wav_tagging_leaves_pcm_untouched(manager, wav_src):
    params_before, frames_before = _wav_frames(wav_src)
    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, wav_src)
    params_after, frames_after = _wav_frames(manager.writer.root / rel)
    assert params_after == params_before
    assert frames_after == frames_before
    assert WAVE(manager.writer.root / rel).tags["TALB"].text == ["Mix"]


def test_aiff_tagging_leaves_pcm_untouched(manager, aiff_src):
    before = _aiff_ssnd(aiff_src)
    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, aiff_src)
    assert _aiff_ssnd(manager.writer.root / rel) == before
    assert AIFF(manager.writer.root / rel).tags["TALB"].text == ["Mix"]


def test_restore_tags_wav(tmp_path):
    p = tmp_path / "x.wav"
    _make_wav(p)
    restore_tags(p, {"albumartist": "AA", "album": "Back", "tracknumber": "7/9",
                     "title": "New Title", "artist": ""})
    tags = WAVE(p).tags
    assert tags["TPE2"].text == ["AA"]
    assert tags["TALB"].text == ["Back"]
    assert tags["TRCK"].text == ["7"]
    assert tags["TIT2"].text == ["New Title"]
    assert not tags.getall("TPE1")


def test_wav_strip_frames_match_mp3_strip_list(manager, source, tmp_path):
    """Every easy key the MP3 path strips is stripped from WAV ID3 too."""
    scratch = tmp_path / "scratch.mp3"
    easy = EasyID3()
    for key in sorted(_EASY_STRIP_TAGS):
        easy[key] = "1" if key == "compilation" else "x"
    easy.save(scratch)
    strip_frames = ID3(scratch)

    src = source / "Strip" / "Strip.wav"
    _make_wav(src)
    f = WAVE(src)
    for frame in strip_frames.values():
        f.tags.add(frame)
    f.save()

    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, src)
    left = set(WAVE(manager.writer.root / rel).tags.keys()) & set(strip_frames.keys())
    assert left == set()


# ── MP3 sort frames ──


@pytest.mark.parametrize("frame", [
    TSO2(encoding=3, text=["Earlier Artist"]),
    TXXX(encoding=3, desc="ALBUMARTISTSORT", text=["Earlier Artist"]),
])
def test_mp3_album_artist_sort_removed_in_both_representations(manager, source, frame):
    src = source / "Mp3" / "song.mp3"
    _make_mp3(src, [TIT2(encoding=3, text=["Song"]), frame])
    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, src)
    left = ID3(manager.writer.root / rel)
    assert not left.getall("TSO2")
    assert not left.getall("TXXX:ALBUMARTISTSORT")
    assert left["TPE2"].text == ["* PLAYLISTS *"]


# ── One format registry ──


def test_format_registry_covers_every_taggable_extension():
    for ext in (".mp4", ".m4a", ".aif", ".aiff", ".wav", ".flac", ".mp3", ".aac"):
        assert ext in AUDIO_EXTS


def test_mp4_track_survives_rescan(manager, source):
    pid = manager.create_playlist("Mix")
    folder = manager.writer.root / "Mix"
    (folder / "01 - Clip.mp4").write_bytes(b"\x00" * 32)
    manager.store.add_track(pid, {"index": 1, "src_path": "x/Clip.mp4",
                                  "copy_name": "01 - Clip.mp4", "src_hash": ""})
    manager.rescan_playlist(pid)
    assert [t["copy_name"] for t in manager.store.playlists[pid]["tracks"]] == ["01 - Clip.mp4"]


def test_aif_alias_is_tagged(manager, source):
    src = source / "Aif" / "song.aif"
    _make_aiff(src)
    assert tags_supported(src)
    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, src)
    copy = manager.writer.root / rel
    assert copy.suffix == ".aif"
    assert tagmod.read_playlist_tags(copy)["albumartist"] == "* PLAYLISTS *"


# ── Formats EchoList can't tag ──


def test_raw_adts_aac_add_does_not_crash(manager, source):
    src = source / "Raw" / "Raw Song.aac"
    _adts_aac(src)
    pid = manager.create_playlist("Mix")
    rel = manager.add_track(pid, src)
    assert (manager.writer.root / rel).read_bytes() == src.read_bytes()


@pytest.mark.parametrize("ext", [".ogg", ".wma", ".aac", ".dsf"])
def test_untaggable_formats_not_reported_as_fixable(manager, source, ext):
    src = source / "Odd" / f"Odd Song{ext}"
    if ext == ".aac":
        _adts_aac(src)
    else:
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x00" * 256)
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, src)
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    assert manager.audit_playlist_metadata(pid) == []
    assert manager.fix_playlist_metadata(pid) == 0


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions, not root")
def test_tags_supported_is_false_for_unreadable_aac(tmp_path):
    p = tmp_path / "broken.aac"
    p.write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 16)
    p.chmod(0)
    try:
        assert tags_supported(p) is False
    finally:
        p.chmod(0o644)


# ── Tagging happens before the copy gets its final name ──


def test_tag_failure_leaves_no_untagged_copy(manager, source, monkeypatch):
    import mutagen
    import echolist.tags as tags_mod

    def boom(*a, **k):
        raise mutagen.MutagenError("card pulled mid tag write")

    monkeypatch.setattr(tags_mod, "_tag_flac", boom)
    pid = manager.create_playlist("Mix")
    with pytest.raises(mutagen.MutagenError):
        manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    folder = manager.writer.root / "Mix"
    assert list(folder.iterdir()) == []
    manager.rescan_playlist(pid)
    assert manager.store.playlists[pid]["tracks"] == []


@pytest.mark.parametrize("ext,make", [(".flac", None), (".wav", _make_wav)])
def test_copy_is_tagged_when_it_appears(manager, source, monkeypatch, ext, make):
    """The file at the final name is already tagged the moment it exists."""
    import echolist.safe_write as safe_write
    seen = {}
    real_replace = safe_write.os.replace

    def spy(a, b):
        if Path(b).suffix == ext:  # not the store's own playlists.json write
            seen["tmp_album"] = read_playlist_tags(Path(a), suffix=ext)["album"]
        return real_replace(a, b)

    monkeypatch.setattr(safe_write.os, "replace", spy)
    src = source / "ArtistA" / "Album1" / "01 Song One.flac"
    if make:
        src = source / "W" / f"W{ext}"
        make(src)
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, src)
    assert seen["tmp_album"] == "Mix"
