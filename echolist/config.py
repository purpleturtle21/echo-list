"""Config dataclass + load/save via SafeWriter."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .safe_write import SafeWriter

from .safe_write import atomic_write_text as _atomic_write_text

CONFIG_REL = ".echolist/config.json"
DEFAULT_FILE = Path.home() / ".echolist" / "default.json"
BACKUPS_ROOT = Path.home() / ".echolist" / "backups"


# ── Workspace identity ──
#
# Local state for a card (restore points, snapshot, staged changes, sync
# journal) lives in ~/.echolist/backups/<wid>/. <wid> comes from a random
# UUID stored on the card in .echolist/config.json, so two cards mounted at
# the same path (every Echo Mini card mounts as /Volumes/ECHO MINI) never
# share state, and a card keeps its state when its mount path changes.
#
# Before the UUID, <wid> was a hash of the mount path. bind_workspace_state()
# moves such state to the UUID key the first time a card is opened, and
# records which card was last seen at each mount path so a wiped card (no
# config.json any more) can still be offered its snapshot.

MOUNTS_DIR = "mounts"  # BACKUPS_ROOT/mounts/<path hash>.json -> last card's UUID


def new_workspace_uuid() -> str:
    return uuid.uuid4().hex


def _path_id(workspace_root: str | Path) -> str:
    """Hash of the workspace path: the pre-UUID workspace id."""
    return hashlib.sha256(str(Path(workspace_root).resolve()).encode()).hexdigest()[:12]


def _card_uuid(workspace_root: Path) -> str:
    p = workspace_root / CONFIG_REL
    if not p.exists():
        return ""
    data = json.loads(p.read_text(encoding="utf-8"))
    return str(data.get("workspace_uuid") or "") if isinstance(data, dict) else ""


def _mount_pointer(workspace_root: Path) -> Path:
    return BACKUPS_ROOT / MOUNTS_DIR / f"{_path_id(workspace_root)}.json"


def _last_uuid_at(workspace_root: Path) -> str:
    p = _mount_pointer(workspace_root)
    if not p.exists():
        return ""
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("workspace_uuid") or "")
    except (json.JSONDecodeError, AttributeError):
        return ""


def _workspace_id(workspace_root: str | Path) -> str:
    """Key of this card's local state directory under BACKUPS_ROOT.

    The card's own UUID when it has one; for a card without config.json
    (wiped, or not initialized yet), the UUID of the card last opened at
    this path; else the legacy path hash.
    """
    root = Path(workspace_root).resolve()
    uid = _card_uuid(root) or _last_uuid_at(root)
    return uid if uid else _path_id(root)


def bind_workspace_state(workspace_root: str | Path, store_pids) -> None:
    """Called with the workspace lock held, once the card has a UUID.

    Moves state that an older version keyed by mount path to the card's
    UUID key — only if that state is about this card (it names one of its
    playlists, or names none at all), so a second card at the same mount
    path never inherits another card's pending removes or journal — and
    records this card as the last one seen at this path.
    """
    root = Path(workspace_root).resolve()
    uid = _card_uuid(root)
    if not uid:
        raise ValueError(f"workspace {root} has no workspace_uuid")
    legacy = BACKUPS_ROOT / _path_id(root)
    target = BACKUPS_ROOT / uid
    if legacy.is_dir() and not target.exists() and _state_names_pids(legacy, set(store_pids)):
        legacy.rename(target)
    _atomic_write_text(_mount_pointer(root), json.dumps({"workspace_uuid": uid}))


def _state_names_pids(state_dir: Path, store_pids: set) -> bool:
    """True if the playlists ``state_dir`` refers to overlap ``store_pids``
    (or it refers to none)."""
    named = {d.name for d in state_dir.iterdir() if d.is_dir()}
    for name in ("pending.json", "sync_journal.json", "snapshot.json"):
        p = state_dir / name
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ("adds", "removes", "actions"):
            named |= {a.get("pid") for a in data.get(key, []) if isinstance(a, dict)}
        named |= set(data.get("reorders", {}) or {})
        store = data.get("store")
        if isinstance(store, dict):
            named |= set(store.get("playlists", {}) or {})
    named.discard(None)
    return not named or bool(named & store_pids)


def load_defaults() -> dict:
    if DEFAULT_FILE.exists():
        return json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    return {}


def save_defaults(source: str, dest: str,
                  playlist_folder: str = "Playlists") -> None:
    """Remember the last workspace, including its playlist folder.

    The folder must be stored too: without it the next launch opens
    ``<dest>/Playlists`` and silently starts a second, empty workspace.
    """
    _atomic_write_text(DEFAULT_FILE, json.dumps({
        "source": str(Path(source).resolve()),
        "dest": str(Path(dest).resolve()),
        "playlist_folder": playlist_folder,
    }))


def validate_playlist_folder(dest: str | Path, folder: str) -> str:
    """Return ``folder`` stripped, or raise ValueError if it is unusable.

    The playlist folder is joined onto the destination, so it must be a
    relative path that stays strictly inside it (no absolute paths, no ``..``).
    """
    folder = folder.strip()
    if not folder:
        raise ValueError("The playlist folder name is empty.")
    p = Path(folder)
    if p.is_absolute() or p.anchor or ".." in p.parts:
        raise ValueError(
            f"Playlist folder {folder!r} must be a folder name inside the "
            f"destination, not an absolute path or a path with '..'.")
    dest_root = Path(dest).resolve()
    resolved = (dest_root / p).resolve()
    if resolved == dest_root or not resolved.is_relative_to(dest_root):
        raise ValueError(
            f"Playlist folder {folder!r} resolves outside the destination "
            f"{dest_root}.")
    return folder


# ── Per-workspace local state (~/.echolist/backups/<wid>/) ──
#
# Staged changes and the sync journal describe one workspace (one card), so
# they live next to that workspace's backups instead of in a global file that
# would replay card A's changes against card B.

def workspace_state_dir(workspace_root: str | Path) -> Path:
    return BACKUPS_ROOT / _workspace_id(workspace_root)


def pending_file(workspace_root: str | Path) -> Path:
    return workspace_state_dir(workspace_root) / "pending.json"


def journal_file(workspace_root: str | Path) -> Path:
    return workspace_state_dir(workspace_root) / "sync_journal.json"


# ── Metadata backups (stored in ~/.echolist/backups/) ──

def save_backup(workspace_root: str | Path, pid: str, timestamp: str, data: dict) -> Path:
    backup_dir = _backup_dir(workspace_root, pid, legacy=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    p = backup_dir / f"{timestamp}.json"
    _atomic_write_text(p, json.dumps(data, indent=2))
    return p


def list_backups(workspace_root: str | Path, pid: str) -> list[dict]:
    try:
        backup_dir = _backup_dir(workspace_root, pid, legacy=True)
    except ValueError:  # a legacy pid that would escape the backups folder
        return []
    if not backup_dir.exists():
        return []
    # Newest first. Timestamps ("20250101_120000") sort by time; a named
    # point such as "before_echolist" predates all of them, so it goes last.
    files = [f for f in backup_dir.iterdir() if f.suffix == ".json"]
    files.sort(key=lambda f: (f.stem[:1].isdigit(), f.stem), reverse=True)
    return [{"timestamp": f.stem, "path": f} for f in files]


def list_all_backup_pids(workspace_root: str | Path) -> list[str]:
    """Return all playlist IDs that have at least one backup for this workspace."""
    wid = _workspace_id(workspace_root)
    wid_dir = BACKUPS_ROOT / wid
    if not wid_dir.exists():
        return []
    return sorted(
        d.name for d in wid_dir.iterdir()
        if d.is_dir() and any(f.suffix == ".json" for f in d.iterdir())
    )


def _backup_dir(workspace_root: str | Path, pid: str, legacy: bool = False) -> Path:
    """Resolve ``~/.echolist/backups/<wid>/<pid>`` and refuse anything outside it.

    New pids come from playlist_id() and never contain separators. Stores
    written before playlist_id() was sanitized can hold pids such as
    ``"rock/pop"`` whose backups live in nested folders; ``legacy=True``
    accepts those as long as they stay inside this workspace's backup folder.
    """
    if not pid or (not legacy and ("/" in pid or "\\" in pid)):
        raise ValueError(f"invalid playlist id: {pid!r}")
    base = (BACKUPS_ROOT / _workspace_id(workspace_root)).resolve()
    d = (base / pid).resolve()
    if d == base or not d.is_relative_to(base):
        raise ValueError(f"backup path escapes backups root: {d}")
    return d


def _remove_empty_dirs(d: Path, stop: Path) -> None:
    """rmdir ``d`` and its now-empty parents, up to (not including) ``stop``."""
    while d != stop and d.is_relative_to(stop):
        try:
            d.rmdir()
        except OSError:  # not empty (or already gone): stop climbing
            return
        d = d.parent


def rename_backup_pid(workspace_root: str | Path, old_pid: str, new_pid: str,
                      new_folder: str | None = None) -> None:
    """Move all backups from old_pid to new_pid directory.

    If new_folder is provided, update the 'folder' field inside each backup
    so restore operations look in the correct location — also when the pid
    is unchanged (e.g. a case-only rename "road trip" -> "Road Trip").
    """
    old_dir = _backup_dir(workspace_root, old_pid, legacy=True)
    new_dir = _backup_dir(workspace_root, new_pid)
    if not old_dir.exists():
        return
    # On macOS an old NFD pid and its NFC successor name the same folder
    # (APFS/HFS+ ignore normalization): rewrite in place, delete nothing.
    same_dir = new_dir.exists() and old_dir.samefile(new_dir)
    if same_dir:
        new_dir = old_dir
    new_dir.mkdir(parents=True, exist_ok=True)
    for f in old_dir.iterdir():
        if not (f.is_file() and f.suffix == ".json"):
            continue
        target = new_dir / f.name
        data = None
        if new_folder:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = None  # unreadable backup: move it as-is
        if isinstance(data, dict):
            data["folder"] = new_folder
            _atomic_write_text(target, json.dumps(data, indent=2))
            if f != target:
                f.unlink()
        elif f != target:
            f.rename(target)
    if not same_dir and old_dir != new_dir:
        _remove_empty_dirs(old_dir,
                           (BACKUPS_ROOT / _workspace_id(workspace_root)).resolve())


def delete_all_backups(workspace_root: str | Path, pid: str) -> None:
    """Permanently remove all backup files for a playlist.

    Only deletes .json files from ~/.echolist/backups/{wid}/{pid}/. Only
    top-level pids are listed as deleted playlists, so separators are refused.
    """
    backup_dir = _backup_dir(workspace_root, pid)
    if not backup_dir.exists():
        return
    for f in backup_dir.iterdir():
        if f.is_file() and f.suffix == ".json":
            f.unlink()
    _remove_empty_dirs(backup_dir,
                       (BACKUPS_ROOT / _workspace_id(workspace_root)).resolve())


def load_backup(workspace_root: str | Path, pid: str, timestamp: str) -> dict | None:
    try:
        p = _backup_dir(workspace_root, pid, legacy=True) / f"{timestamp}.json"
    except ValueError:  # a legacy pid that would escape the backups folder
        return None
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "tracks" not in data:
        return None
    if not isinstance(data["tracks"], list):
        return None
    return data


# ── Playlist snapshot (full playlist structure backup) ──

def save_playlist_snapshot(workspace_root: str | Path, config_data: dict, store_data: dict) -> Path:
    wid = _workspace_id(workspace_root)
    snapshot_dir = BACKUPS_ROOT / wid
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    p = snapshot_dir / "snapshot.json"
    _atomic_write_text(p, json.dumps({
        "config": config_data,
        "store": store_data,
    }, indent=2))
    return p


def load_playlist_snapshot(workspace_root: str | Path) -> dict | None:
    wid = _workspace_id(workspace_root)
    p = BACKUPS_ROOT / wid / "snapshot.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if "config" not in data or "store" not in data:
        return None
    return data


DEFAULT_PLAYLIST_FOLDER = "Playlists"


@dataclass
class Config:
    schema: int = 1
    source_root: str = ""
    node_name: str = "* PLAYLISTS *"
    album_prefix: str = ""
    star_prefix: bool = False
    playlist_folder: str = DEFAULT_PLAYLIST_FOLDER
    backup_interval: int = 5
    _sync_count: int = 0
    workspace_uuid: str = ""  # identifies this card's local state (see _workspace_id)

    @classmethod
    def load(cls, writer: SafeWriter) -> Config:
        p = writer.root / CONFIG_REL
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls(
                schema=data.get("schema", 1),
                source_root=data.get("source_root", ""),
                node_name=data.get("node_name", "* PLAYLISTS *"),
                album_prefix=data.get("album_prefix", ""),
                star_prefix=data.get("star_prefix", False),
                playlist_folder=data.get("playlist_folder", DEFAULT_PLAYLIST_FOLDER),
                backup_interval=data.get("backup_interval", 5),
                _sync_count=data.get("_sync_count", 0),
                workspace_uuid=data.get("workspace_uuid", ""),
            )
        return cls()

    def save(self, writer: SafeWriter) -> None:
        writer.write_text(CONFIG_REL, json.dumps(asdict(self), indent=2))

    def should_backup(self) -> bool:
        return self._sync_count % self.backup_interval == 0

    def increment_sync(self, writer: SafeWriter) -> None:
        self._sync_count += 1
        self.save(writer)
