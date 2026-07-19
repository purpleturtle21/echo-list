"""Tests for ~/.echolist/default.json persistence (save_defaults/load_defaults)."""

from echolist import config


def test_load_defaults_missing_file_returns_empty():
    assert config.load_defaults() == {}


def test_save_and_load_roundtrip(tmp_path):
    src = tmp_path / "library"
    dest = tmp_path / "card"
    src.mkdir()
    dest.mkdir()

    config.save_defaults(str(src), str(dest), dest_mode="manual", playlist_folder="MyTunes")

    data = config.load_defaults()
    assert data["source"] == str(src.resolve())
    assert data["dest"] == str(dest.resolve())
    assert data["dest_mode"] == "manual"
    assert data["playlist_folder"] == "MyTunes"


def test_save_defaults_partial_update_preserves_other_fields(tmp_path):
    """Regression: a call that only changes one field (e.g. flipping dest_mode
    back to auto) must not drop playlist_folder — this was the root cause of
    an app restart resetting the 'folder' and 'backup every' Settings fields
    to their defaults. The old implementation wrote a brand-new dict on every
    save, so anything not passed to that particular call was silently lost.
    """
    src = tmp_path / "library"
    dest = tmp_path / "card"
    src.mkdir()
    dest.mkdir()

    config.save_defaults(str(src), str(dest), dest_mode="manual", playlist_folder="MyTunes")
    config.save_defaults(dest_mode="auto")  # e.g. "Switch to auto-detect" link

    data = config.load_defaults()
    assert data["dest_mode"] == "auto"
    assert data["playlist_folder"] == "MyTunes"
    assert data["source"] == str(src.resolve())
    assert data["dest"] == str(dest.resolve())
