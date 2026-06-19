"""PlaylistManager — the four operations: create, add, remove, stats."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .safe_write import SafeWriter, UnsafeWriteError
from .config import Config
from .store import Store
from .naming import playlist_id, sanitize, track_filename
from .tags import apply_playlist_tags

try:
    import mutagen
except ImportError:
    mutagen = None


class PlaylistManager:
    def __init__(self, writer: SafeWriter, config: Config, store: Store):
        self.writer = writer
        self.config = config
        self.store = store

    @classmethod
    def init(
        cls,
        source_root: str | Path,
        dest_root: str | Path,
        node_name: str = "* PLAYLISTS *",
        album_prefix: str = "",
    ) -> PlaylistManager:
        source_root = Path(source_root).resolve()
        workspace = Path(dest_root).resolve() / "Playlists"
        _check_overlap(source_root, workspace)
        writer = SafeWriter(workspace)
        config = Config(
            source_root=str(source_root),
            node_name=node_name,
            album_prefix=album_prefix,
        )
        store = Store.load(writer)
        return cls(writer, config, store)

    @classmethod
    def open(cls, dest_root: str | Path) -> PlaylistManager:
        workspace = Path(dest_root).resolve() / "Playlists"
        writer = SafeWriter(workspace)
        config = Config.load(writer)
        if not config.source_root:
            raise ValueError("workspace not initialized — run 'echolist init' first")
        source_root = Path(config.source_root).resolve()
        _check_overlap(source_root, workspace)
        store = Store.load(writer)
        return cls(writer, config, store)

    def create_playlist(self, name: str) -> str:
        pid = playlist_id(name)
        if pid in self.store.playlists:
            raise ValueError(f"playlist '{pid}' already exists")
        folder = sanitize(name)
        self.store.add_playlist(pid, name, folder)
        self._ensure_workspace()
        self.writer.makedirs(folder)
        self.store.save()
        return pid

    def _ensure_workspace(self) -> None:
        if not self.writer.root.exists():
            self.writer.root.mkdir(parents=True, exist_ok=True)
            self.config.save(self.writer)

    def add_track(self, pid: str, src: str | Path, progress_cb=None) -> str:
        if pid not in self.store.playlists:
            raise KeyError(f"playlist '{pid}' does not exist")
        playlist = self.store.playlists[pid]
        src = Path(src)
        if not src.is_absolute():
            src = Path(self.config.source_root) / src
        src = src.resolve()
        if not src.exists():
            raise FileNotFoundError(f"source not found: {src}")

        source_root = Path(self.config.source_root).resolve()
        try:
            src_rel = str(src.relative_to(source_root))
        except ValueError:
            src_rel = str(src)

        title = _read_title(src)
        ext = src.suffix
        index = len(playlist["tracks"]) + 1
        pad = 3 if index > 99 else 2
        copy_name = track_filename(index, title, ext, pad)
        folder = playlist["folder"]
        rel = f"{folder}/{copy_name}"

        _, src_hash = self.writer.copy_in(src, rel, progress_cb=progress_cb, compute_hash=True)

        album = self.config.album_prefix + playlist["name"]
        apply_playlist_tags(
            self.writer.resolve(rel),
            self.config.node_name,
            album,
            index,
            src_rel,
            pid,
        )

        self.store.add_track(pid, {
            "index": index,
            "src_path": src_rel,
            "copy_name": copy_name,
            "src_hash": f"blake2b:{src_hash}",
        })
        self.store.save()
        return rel

    def remove_track(self, pid: str, index: int) -> None:
        if pid not in self.store.playlists:
            raise KeyError(f"playlist '{pid}' does not exist")
        removed = self.store.remove_track(pid, index)
        folder = self.store.playlists[pid]["folder"]
        self.writer.delete(f"{folder}/{removed['copy_name']}")
        self._renumber_tracks(pid)
        self.store.save()

    def _renumber_tracks(self, pid: str) -> None:
        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        tracks = playlist["tracks"]
        pad = 3 if len(tracks) > 99 else 2
        for t in tracks:
            old_name = t["copy_name"]
            title = old_name.split(" - ", 1)[-1].rsplit(".", 1)[0]
            ext = Path(old_name).suffix
            new_name = track_filename(t["index"], title, ext, pad)
            if old_name != new_name:
                try:
                    self.writer.rename(f"{folder}/{old_name}", f"{folder}/{new_name}")
                except Exception:
                    pass
                t["copy_name"] = new_name

    def stats(self, cached_device_tracks: int | None = None,
              cached_workspace_bytes: int | None = None) -> dict:
        total_playlists = len(self.store.playlists)
        total_tracks = sum(
            len(p["tracks"]) for p in self.store.playlists.values()
        )
        workspace_bytes = cached_workspace_bytes if cached_workspace_bytes is not None else 0
        device_tracks = cached_device_tracks if cached_device_tracks is not None else 0
        try:
            usage = shutil.disk_usage(self.writer.root.parent)
        except OSError:
            usage = type("U", (), {"total": 1, "used": 0})()
        return {
            "playlists": total_playlists,
            "tracks": total_tracks,
            "device_tracks": device_tracks,
            "workspace_bytes": workspace_bytes,
            "drive_total": usage.total,
            "drive_used": usage.used,
            "drive_used_pct": round(usage.used / usage.total * 100, 1),
            "workspace_pct_of_drive": round(workspace_bytes / usage.total * 100, 4),
            "files_vs_8192": f"{total_tracks}/8192",
        }

    def compute_expensive_stats(self) -> tuple[int, int]:
        """Returns (device_tracks, workspace_bytes). Call from a background thread."""
        if self.writer.root.exists():
            workspace_bytes = sum(
                f.stat().st_size
                for f in self.writer.root.rglob("*")
                if f.is_file()
            )
        else:
            workspace_bytes = 0
        device_root = self.writer.root.parent.parent
        device_tracks = _count_audio_files(device_root)
        return device_tracks, workspace_bytes


AUDIO_EXTS = frozenset({".flac", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".wma", ".alac", ".aiff", ".dsf", ".dff"})


def _count_audio_files(root: Path, exclude: Path | None = None) -> int:
    count = 0
    try:
        for f in root.rglob("*"):
            if exclude and f.is_relative_to(exclude):
                continue
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                count += 1
    except OSError:
        pass
    return count


def _check_overlap(source: Path, workspace: Path) -> None:
    if source == workspace or source.is_relative_to(workspace):
        raise UnsafeWriteError(
            f"source is inside workspace: source={source}, workspace={workspace}"
        )


def _blake2b(path: Path) -> str:
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_title(path: Path) -> str:
    if mutagen is None:
        return path.stem
    try:
        m = mutagen.File(path, easy=True)
        if m and "title" in m:
            return m["title"][0]
    except Exception:
        pass
    return path.stem
