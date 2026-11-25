"""Playlist names and ids: ids are safe path segments, sanitize() strips
control characters and never splits a combining accent from its letter."""

import unicodedata

import pytest

from echolist.naming import playlist_id, sanitize


@pytest.mark.parametrize("name", ["AC/DC", "a\\b", "..", "../../etc", "what?", "a:b", "x*y"])
def test_playlist_id_is_a_safe_path_segment(name):
    pid = playlist_id(name)
    assert pid
    assert not any(c in pid for c in '\\/:*?"<>|')
    assert ".." not in pid


def test_sanitize_strips_control_characters():
    assert sanitize("a\x00b\x1fc\x7f") == "a_b_c_"


def test_slash_playlist_backups_are_listed(manager, source):
    from echolist.config import list_all_backup_pids

    pid = manager.create_playlist("AC/DC")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid)
    assert pid in list_all_backup_pids(manager.writer.root)


def test_sanitize_truncation_keeps_accent_on_nfd_input():
    name = "A" * 59 + unicodedata.normalize("NFD", "é")
    assert sanitize(name) == "A" * 59 + "é"
