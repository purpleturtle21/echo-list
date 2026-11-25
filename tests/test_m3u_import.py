"""M3U import: BOMs, file:// URIs, stream URLs in any case, and names that
need disambiguating."""

from __future__ import annotations

from echolist.m3u import curate_playlist_name, parse_m3u
from echolist.naming import playlist_id
from conftest import _make_flac


# ── BOMs and file:// URIs ──


def _lib(tmp_path):
    lib = tmp_path / "library"
    _make_flac(lib / "My Artist" / "01 Song.flac", "A", "Song")
    return lib


def test_m3u8_with_bom_parses_header(tmp_path):
    lib = _lib(tmp_path)
    m = tmp_path / "list.m3u8"
    m.write_bytes(b"\xef\xbb\xbf#EXTM3U\n" + str(lib / "My Artist" / "01 Song.flac").encode() + b"\n")
    result = parse_m3u(m)
    assert result["missing"] == []
    assert len(result["tracks"]) == 1


def test_m3u8_with_bom_and_no_header_resolves_first_track(tmp_path):
    lib = _lib(tmp_path)
    m = lib / "list.m3u8"
    m.write_bytes(b"\xef\xbb\xbfMy Artist/01 Song.flac\n")
    assert len(parse_m3u(m)["tracks"]) == 1


def test_m3u_file_uri_is_resolved(tmp_path):
    lib = _lib(tmp_path)
    track = lib / "My Artist" / "01 Song.flac"
    m = tmp_path / "vlc.m3u8"
    m.write_text("#EXTM3U\n" + track.resolve().as_uri() + "\n", encoding="utf-8")
    assert "%20" in m.read_text(encoding="utf-8")
    result = parse_m3u(m)
    assert result["missing"] == []
    assert result["tracks"] == [track.resolve()]


def test_m3u_file_localhost_uri_is_resolved(tmp_path):
    lib = _lib(tmp_path)
    track = (lib / "My Artist" / "01 Song.flac").resolve()
    m = tmp_path / "x.m3u"
    uri = "file://localhost" + track.as_uri()[len("file://"):]
    m.write_text(uri + "\n", encoding="utf-8")
    assert parse_m3u(m)["tracks"] == [track]


def test_file_uri_host_is_case_insensitive(tmp_path):
    track = tmp_path / "lib" / "a b.flac"
    track.parent.mkdir()
    track.write_bytes(b"x")
    m = tmp_path / "x.m3u"
    uri = "file://LOCALHOST" + track.resolve().as_uri()[len("file://"):]
    m.write_text(uri + "\n", encoding="utf-8")
    assert parse_m3u(m)["tracks"] == [track.resolve()]


def test_uppercase_stream_urls_are_skipped(tmp_path):
    m = tmp_path / "x.m3u"
    m.write_text("HTTP://example.com/stream.mp3\nHttps://example.com/b.mp3\n",
                 encoding="utf-8")
    assert parse_m3u(m) == {"name": "x", "tracks": [], "missing": []}


# ── Playlist names ──


def test_long_name_gets_a_distinct_id():
    name = "A" * 60
    existing = {playlist_id(name)}
    curated = curate_playlist_name(name, existing)
    assert playlist_id(curated) not in existing
    existing.add(playlist_id(curated))
    third = curate_playlist_name(name, existing)
    assert playlist_id(third) not in existing


def test_long_name_disambiguation_survives_100_collisions():
    name = "B" * 60
    existing = {playlist_id(name)}
    for _ in range(105):
        c = curate_playlist_name(name, existing)
        assert playlist_id(c) not in existing
        existing.add(playlist_id(c))


def test_dot_playlist_name_is_importable():
    curated = curate_playlist_name(".echolist", set())
    assert not curated.startswith(".")
