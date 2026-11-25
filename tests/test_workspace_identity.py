"""Per-card workspace identity: local state is keyed by a UUID stored on
the card, so two cards at one mount path never share it, and state an older
version keyed by mount path is migrated to the card it belongs to."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import echolist.config as config
from echolist.config import list_backups, pending_file, journal_file, workspace_state_dir
from echolist.manager import PlaylistManager


def _legacy_id(root: Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]


def _card(tmp_path, name="card"):
    d = tmp_path / name
    d.mkdir()
    return d


def _swap_card(dest: Path, aside: Path) -> None:
    """Eject the card at dest and insert a blank one at the same mount path."""
    shutil.move(str(dest), str(aside))
    dest.mkdir()


def test_two_cards_at_same_mount_path_get_separate_state(source, tmp_path):
    dest = _card(tmp_path)
    a = PlaylistManager.init(source, dest)
    a_pending = pending_file(a.writer.root)
    a_journal = journal_file(a.writer.root)
    a.release_lock()
    _swap_card(dest, tmp_path / "card-A")

    b = PlaylistManager.init(source, dest)
    try:
        assert pending_file(b.writer.root) != a_pending
        assert journal_file(b.writer.root) != a_journal
    finally:
        b.release_lock()


def test_workspace_uuid_persists_on_card(source, tmp_path):
    dest = _card(tmp_path)
    mgr = PlaylistManager.init(source, dest)
    state = workspace_state_dir(mgr.writer.root)
    mgr.release_lock()
    cfg = json.loads((dest / "Playlists" / ".echolist" / "config.json").read_text())
    assert cfg["workspace_uuid"]

    again = PlaylistManager.open(dest)
    try:
        assert workspace_state_dir(again.writer.root) == state
        assert again.config.workspace_uuid == cfg["workspace_uuid"]
    finally:
        again.release_lock()


def test_card_keeps_its_state_after_moving_mount_path(source, tmp_path):
    dest = _card(tmp_path, "ECHO MINI")
    mgr = PlaylistManager.init(source, dest)
    pid = mgr.create_playlist("Mix")
    mgr.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    mgr.backup_playlist_metadata(pid, "t1")
    mgr.release_lock()
    moved = tmp_path / "ECHO MINI 1"
    shutil.move(str(dest), str(moved))

    mgr = PlaylistManager.open(moved)
    try:
        assert [b["timestamp"] for b in list_backups(mgr.writer.root, pid)] == ["t1"]
    finally:
        mgr.release_lock()


def _make_legacy_card(source, dest) -> Path:
    """A card written by a version that keyed state by mount path."""
    mgr = PlaylistManager.init(source, dest)
    pid = mgr.create_playlist("Mix")
    mgr.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    root = mgr.writer.root
    mgr.release_lock()
    cfg_path = root / ".echolist" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.pop("workspace_uuid", None)
    cfg_path.write_text(json.dumps(cfg))
    # Everything the new version wrote under the uuid key never existed.
    for d in (config.BACKUPS_ROOT.iterdir() if config.BACKUPS_ROOT.exists() else []):
        shutil.rmtree(d)
    legacy = config.BACKUPS_ROOT / _legacy_id(root)
    (legacy / "mix").mkdir(parents=True)
    (legacy / "mix" / "t1.json").write_text(json.dumps(
        {"playlist_name": "Mix", "folder": "Mix", "tracks": []}))
    (legacy / "pending.json").write_text(json.dumps(
        {"adds": [{"pid": "mix", "src": "x.flac", "title": "X", "artist": "A"}],
         "removes": [], "reorders": {}}))
    return legacy


def test_existing_card_migrates_path_keyed_state(source, tmp_path):
    dest = _card(tmp_path)
    legacy = _make_legacy_card(source, dest)

    mgr = PlaylistManager.open(dest)
    try:
        assert mgr.config.workspace_uuid
        assert [b["timestamp"] for b in list_backups(mgr.writer.root, "mix")] == ["t1"]
        pend = json.loads(pending_file(mgr.writer.root).read_text())
        assert pend["adds"][0]["src"] == "x.flac"
        assert not legacy.exists()
    finally:
        mgr.release_lock()


def test_other_card_does_not_claim_path_keyed_state(source, tmp_path):
    dest = _card(tmp_path)
    legacy = _make_legacy_card(source, dest)
    _swap_card(dest, tmp_path / "card-A")
    other = PlaylistManager.init(source, dest)
    try:
        other.create_playlist("Different")
        assert list_backups(other.writer.root, "mix") == []
        assert legacy.exists()
    finally:
        other.release_lock()


def test_wiped_card_still_finds_last_snapshot(source, tmp_path):
    dest = _card(tmp_path)
    mgr = PlaylistManager.init(source, dest)
    mgr.create_playlist("Mix")
    mgr.save_snapshot()
    mgr.release_lock()
    shutil.rmtree(dest / "Playlists")  # card formatted

    snap = PlaylistManager.find_snapshot(dest)
    assert snap is not None
    assert "mix" in snap["store"]["playlists"]
