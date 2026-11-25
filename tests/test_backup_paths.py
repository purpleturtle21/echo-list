"""Backup folders: a playlist id never resolves outside the workspace's
backup folder, and renaming between Unicode forms keeps the backups."""

from __future__ import annotations

import json
import unicodedata

import pytest

import echolist.config as config
from echolist.config import (
    list_backups, load_backup, save_backup, rename_backup_pid, workspace_state_dir,
)


# ── Legacy pids stay inside the workspace's backup folder ──


def test_save_backup_refuses_escaping_legacy_pid(manager):
    root = manager.writer.root
    with pytest.raises(ValueError):
        save_backup(root, "../Mix", "t1", {"tracks": []})
    assert not list(config.BACKUPS_ROOT.rglob("t1.json"))


def test_list_and_load_refuse_escaping_pid(manager):
    root = manager.writer.root
    wid_dir = workspace_state_dir(root)
    wid_dir.mkdir(parents=True)
    outside = wid_dir.parent / "Mix"
    outside.mkdir(parents=True)
    (outside / "t1.json").write_text(json.dumps({"tracks": []}))
    assert list_backups(root, "../Mix") == []
    assert load_backup(root, "../Mix", "t1") is None


def test_legacy_nested_pid_still_saves_inside(manager):
    p = save_backup(manager.writer.root, "rock/pop", "t1", {"tracks": []})
    assert p.is_relative_to(workspace_state_dir(manager.writer.root))


# ── Renames between Unicode forms ──


def test_rename_backup_pid_between_unicode_forms_keeps_backups(manager):
    root = manager.writer.root
    nfd = unicodedata.normalize("NFD", "café")
    nfc = unicodedata.normalize("NFC", "café")
    save_backup(root, nfd, "t1", {"playlist_name": "café", "folder": "café", "tracks": []})
    if not (workspace_state_dir(root) / nfc).exists():
        pytest.skip("filesystem does not alias NFD and NFC names")

    rename_backup_pid(root, nfd, nfc, "Café")

    data = load_backup(root, nfc, "t1")
    assert data is not None and data["folder"] == "Café"
