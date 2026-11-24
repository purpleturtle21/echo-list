"""PlaylistManager — the four operations: create, add, remove, stats."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor

_MAX_TAG_WORKERS = 8
from datetime import datetime
from pathlib import Path

from .safe_write import SafeWriter, UnsafeWriteError
from .config import (
    CONFIG_REL, Config, DEFAULT_PLAYLIST_FOLDER,
    bind_workspace_state, new_workspace_uuid,
    save_backup, list_backups, list_all_backup_pids, load_backup,
    save_playlist_snapshot, load_playlist_snapshot,
)
from .formats import AUDIO_EXTS
from .store import Store
from .naming import (
    INDEX_PREFIX_RE, check_playlist_folder_name, pad_width, playlist_id, sanitize,
    track_filename,
)
from .tags import (
    apply_playlist_tags, read_playlist_tags, read_title, restore_tags, snapshot_tags,
    tags_supported,
)

# Prefix of a track parked mid-rename ("_echolist_tmp_<n>_<original name>").
# Rescan never adopts such a file.
TMP_PREFIX = "_echolist_tmp_"

LOCK_REL = ".echolist/workspace.lock"

# errno values meaning "another process holds the lock" (flock / msvcrt).
_LOCK_BUSY = {e for e in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES,
                          getattr(errno, "EDEADLK", None)) if e}


class WorkspaceLockError(Exception):
    ...


class PlaylistManager:
    def __init__(self, writer: SafeWriter, config: Config, store: Store):
        self.writer = writer
        self.config = config
        self.store = store
        self._lock_fd = None

    @classmethod
    def init(
        cls,
        source_root: str | Path,
        dest_root: str | Path,
        node_name: str = "* PLAYLISTS *",
        album_prefix: str = "",
        playlist_folder: str = DEFAULT_PLAYLIST_FOLDER,
        backup_interval: int = 5,
    ) -> PlaylistManager:
        source_root = Path(source_root).resolve()
        workspace = Path(dest_root).resolve() / playlist_folder
        _check_overlap(source_root, workspace)
        writer = SafeWriter(workspace)
        mgr = cls(writer, None, None)
        # Lock first: nothing on the card is read before this instance owns it.
        mgr._acquire_lock()
        try:
            mgr.config = Config(
                source_root=str(source_root),
                node_name=node_name,
                album_prefix=album_prefix,
                playlist_folder=playlist_folder,
                backup_interval=backup_interval,
                workspace_uuid=_existing_uuid(writer),
            )
            mgr.store = Store.load(writer)
            mgr._bind_workspace_state()
        except BaseException:
            mgr.release_lock()
            raise
        return mgr

    @classmethod
    def open(cls, dest_root: str | Path, playlist_folder: str | None = None) -> PlaylistManager:
        folder = playlist_folder or DEFAULT_PLAYLIST_FOLDER
        workspace = Path(dest_root).resolve() / folder
        writer = SafeWriter(workspace)
        if not (writer.root / CONFIG_REL).exists():
            raise ValueError("workspace not initialized — run 'echolist init' first")
        mgr = cls(writer, None, None)
        # Lock first: another instance may save the Store until it exits.
        mgr._acquire_lock()
        try:
            config = Config.load(writer)
            if not config.source_root:
                raise ValueError("workspace not initialized — run 'echolist init' first")
            _check_overlap(Path(config.source_root).resolve(), workspace)
            mgr.config = config
            mgr.store = Store.load(writer)
            mgr._bind_workspace_state()
        except BaseException:
            mgr.release_lock()
            raise
        return mgr

    def _bind_workspace_state(self) -> None:
        """Give the card a UUID if it has none (saving config), then move any
        mount-path-keyed local state to it (see config.bind_workspace_state)."""
        if not self.config.workspace_uuid:
            self.config.workspace_uuid = new_workspace_uuid()
        self.config.save(self.writer)
        bind_workspace_state(self.writer.root, self.store.playlists.keys())

    def _acquire_lock(self) -> None:
        # Errors opening the lock file come from the card itself (read-only
        # switch, I/O error) and propagate as they are; only a lock held by
        # someone else is "another instance".
        fd = self.writer.open_lock(LOCK_REL)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in _LOCK_BUSY:
                raise WorkspaceLockError(
                    "Another EchoList instance is using this workspace. "
                    "Close it first, or delete .echolist/workspace.lock if "
                    "the previous session crashed."
                ) from e
            raise
        self._lock_fd = fd

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def create_playlist(self, name: str) -> str:
        pid = playlist_id(name)
        if pid in self.store.playlists:
            raise ValueError(f"playlist '{pid}' already exists")
        folder = sanitize(name)
        check_playlist_folder_name(folder)
        # FAT32 folder names are case-insensitive: two playlists whose folders
        # differ only by case (or an older store whose pid predates
        # playlist_id() sanitizing) would share one folder on the card.
        for other_pid, other in self.store.playlists.items():
            if _name_key(other["folder"]) == _name_key(folder):
                raise ValueError(
                    f"playlist folder '{folder}' is already used by "
                    f"playlist '{other['name']}'"
                )
        # TODO: star prefix feature disabled — re-enable when stable
        # if self.config.star_prefix:
        #     folder = f"★ {folder}"
        self._ensure_workspace()
        # A new playlist owns its folder: never take over an existing one
        # (untracked music goes through adopt_playlist instead).
        try:
            self.writer.makedirs(folder, exist_ok=False)
        except FileExistsError:
            raise ValueError(
                f"a folder named '{folder}' already exists on the card; "
                "adopt it or pick another name"
            ) from None
        self.store.add_playlist(pid, name, folder)
        self.store.save()
        return pid

    # TODO: star prefix feature disabled — re-enable when stable
    # def set_star_prefix(self, enabled: bool, done_event: threading.Event | None = None) -> None:
    #     if enabled == self.config.star_prefix:
    #         if done_event:
    #             done_event.set()
    #         return
    #     self.config.star_prefix = enabled
    #     renames = []
    #     for pid, pl in self.store.playlists.items():
    #         old_folder = pl["folder"]
    #         base = old_folder.removeprefix("★ ") if old_folder.startswith("★ ") else old_folder
    #         new_folder = f"★ {base}" if enabled else base
    #         if old_folder != new_folder:
    #             renames.append((pl, old_folder, new_folder))
    #             pl["folder"] = new_folder
    #     self.store.save()
    #     self.config.save(self.writer)
    #
    #     def _do_renames():
    #         for pl, old, new in renames:
    #             try:
    #                 self.writer.rename(old, new)
    #             except Exception:
    #                 pass
    #         if done_event:
    #             done_event.set()
    #
    #     thread = threading.Thread(target=_do_renames, daemon=True)
    #     thread.start()
    #     self._star_rename_thread = thread

    def rescan_playlist(self, pid: str) -> bool:
        """Update playlist entries from what's actually on the drive."""
        if pid not in self.store.playlists:
            return False
        playlist = self.store.playlists[pid]
        folder_path = self.writer.root / playlist["folder"]
        if not folder_path.exists():
            return False

        disk_files = sorted(
            (f for f in folder_path.iterdir()
             if _is_real_audio(f) and not f.name.startswith(TMP_PREFIX)),
            key=lambda f: _track_sort_key(f.name),
        )

        # Compare names in NFC: macOS returns NFD directory entries while
        # playlists.json (written from tags / Tk input) holds NFC. The stored
        # copy_name is taken from disk so later path operations hit the file.
        by_name = {_nfc(t["copy_name"]): t for t in playlist["tracks"]}
        by_title = {}
        for t in playlist["tracks"]:
            title_part = _strip_index_prefix(_nfc(t["copy_name"]))
            if title_part not in by_title:
                by_title[title_part] = t

        new_tracks = []
        matched_ids = set()
        for i, f in enumerate(disk_files, 1):
            disk_name = _nfc(f.name)
            if disk_name in by_name and id(by_name[disk_name]) not in matched_ids:
                entry = {**by_name[disk_name], "index": i, "copy_name": f.name}
                new_tracks.append(entry)
                matched_ids.add(id(by_name[disk_name]))
            else:
                title_part = _strip_index_prefix(disk_name)
                if title_part in by_title and id(by_title[title_part]) not in matched_ids:
                    old = by_title[title_part]
                    entry = {**old, "index": i, "copy_name": f.name}
                    new_tracks.append(entry)
                    matched_ids.add(id(old))
                else:
                    new_tracks.append({
                        "index": i,
                        "src_path": "",
                        "copy_name": f.name,
                        "src_hash": "",
                    })

        old_state = [(t["index"], t["copy_name"]) for t in playlist["tracks"]]
        new_state = [(t["index"], t["copy_name"]) for t in new_tracks]
        if old_state != new_state:
            playlist["tracks"] = new_tracks
            self.store.save()
            return True
        return False

    def _ensure_workspace(self) -> None:
        """Recreate the workspace folder if it vanished (e.g. deleted in
        Finder). Its lock file went with it, so take the lock again."""
        if not self.writer.root.exists():
            self.writer.makedirs(".")
            self.release_lock()
            self._acquire_lock()
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
            src_rel = src.relative_to(source_root).as_posix()
        except ValueError:
            src_rel = src.as_posix()

        title = _read_title(src)
        ext = src.suffix
        index = len(playlist["tracks"]) + 1
        copy_name = track_filename(index, title, ext, pad_width(index))
        folder = playlist["folder"]
        if self._name_taken(pid, copy_name):
            # Names drifted from indices (e.g. a rescan after a file was
            # removed outside EchoList): renumber so index+1 is free again.
            self._renumber_tracks(pid)
            self.store.save()
        rel = f"{folder}/{copy_name}"

        original_tags = read_playlist_tags(src)

        album = self.config.album_prefix + playlist["name"]

        def _tag_temp_copy(tmp: Path) -> None:
            apply_playlist_tags(
                tmp, self.config.node_name, album, index, src_rel, pid, suffix=ext,
            )

        # Tag the temp copy before it gets its final name, so a failed tag
        # write never leaves an untagged track for rescan to adopt.
        # exclusive: never publish over an existing file (FileExistsError).
        _, src_hash = self.writer.copy_in(
            src, rel, progress_cb=progress_cb, compute_hash=True,
            before_commit=_tag_temp_copy, exclusive=True,
        )

        self.store.add_track(pid, {
            "index": index,
            "src_path": src_rel,
            "copy_name": copy_name,
            "src_hash": f"blake2b:{src_hash}",
            "original_tags": original_tags,
        })
        if pad_width(index) != pad_width(index - 1):
            # Crossed 99 -> 100 (or 999 -> 1000): re-pad the earlier tracks.
            self._renumber_tracks(pid)
        self.store.save()
        return rel

    def _name_taken(self, pid: str, name: str) -> bool:
        """True if ``name`` (compared as FAT32/macOS do) is a stored track's
        name or any file in the playlist folder."""
        playlist = self.store.playlists[pid]
        key = _name_key(name)
        if any(_name_key(t["copy_name"]) == key for t in playlist["tracks"]):
            return True
        folder_path = self.writer.root / playlist["folder"]
        return folder_path.is_dir() and any(
            _name_key(e.name) == key for e in folder_path.iterdir())

    def remove_track(self, pid: str, index: int) -> None:
        if pid not in self.store.playlists:
            raise KeyError(f"playlist '{pid}' does not exist")
        playlist = self.store.playlists[pid]
        target = next((t for t in playlist["tracks"] if t["index"] == index), None)
        if target is None:
            raise KeyError(f"no track with index {index} in playlist {pid}")
        # Delete first: if the card refuses, the Store still lists the track.
        self.writer.delete(f"{playlist['folder']}/{target['copy_name']}")
        self.store.remove_track(pid, index)
        try:
            self._renumber_tracks(pid)
        finally:
            # The file is gone either way; record that, with every name the
            # renumber reached (a parked _echolist_tmp_ name is restored and
            # rescanned on next open).
            self.store.save()

    def _renumber_tracks(self, pid: str) -> None:
        """Make every file name and embedded track number match t["index"].

        All names share one pad width (see naming.pad_width). A file whose
        TRACKNUMBER no longer matches its index is re-tagged, so the player's
        album view keeps playlist order after removes.
        """
        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        folder_path = self.writer.root / folder
        tracks = playlist["tracks"]
        pad = pad_width(len(tracks))

        moves = []
        for t in tracks:
            old_name = t["copy_name"]
            new_name = track_filename(
                t["index"], _title_from_copy_name(folder_path / old_name), Path(old_name).suffix, pad,
            )
            moves.append((t, new_name))
        self._move_tracks(folder, moves)

        album = self.config.album_prefix + playlist["name"]
        for t in tracks:
            path = folder_path / t["copy_name"]
            if not path.exists() or not tags_supported(path):
                continue
            if read_playlist_tags(path)["tracknumber"] != str(t["index"]):
                self.writer.edit_in_place(path, lambda p, t=t: apply_playlist_tags(
                    p, self.config.node_name, album, t["index"], t.get("src_path", ""), pid,
                ))

    def _move_tracks(self, folder: str, moves: list) -> None:
        """Rename tracks within ``folder``; ``moves`` is [(track, new_name)].

        Names are compared the way FAT32 and macOS do (case- and
        normalization-blind), so "01 - Y" and "01 - y" are one name. Before
        anything moves, every final name must be distinct and not held by a
        file that stays put; then every moving file is parked under a unique
        TMP_PREFIX name and only then given its final name, so no rename can
        overwrite another track whatever the order or cycles. Each track's
        copy_name follows its file as it moves.
        """
        folder_path = self.writer.root / folder
        moving = [(t, new) for t, new in moves if t["copy_name"] != new]
        if not moving:
            return
        moving_keys = {_name_key(t["copy_name"]) for t, _ in moving}
        finals = [_name_key(new) for _, new in moving]
        held = {_name_key(e.name) for e in folder_path.iterdir()} - moving_keys
        clash = (len(set(finals)) != len(finals)) or (set(finals) & held)
        if clash:
            raise RuntimeError(
                f"renaming tracks in {folder} would give two files the same name "
                f"({sorted(set(new for _, new in moving))}); nothing was renamed")
        for i, (t, _) in enumerate(moving, 1):
            parked = f"{TMP_PREFIX}{i}_{t['copy_name']}"
            self.writer.rename(f"{folder}/{t['copy_name']}", f"{folder}/{parked}")
            t["copy_name"] = parked
        for t, new in moving:
            self.writer.rename(f"{folder}/{t['copy_name']}", f"{folder}/{new}")
            t["copy_name"] = new

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
        # writer.root is <device>/<playlist_folder>; walk the device only.
        # (.parent.parent was /Volumes on macOS — every mounted volume.)
        device_root = self.writer.root
        for _ in Path(self.config.playlist_folder).parts:
            device_root = device_root.parent
        device_tracks = _count_audio_files(device_root)
        return device_tracks, workspace_bytes


    def audit_playlist_metadata(self, pid: str) -> list[dict]:
        """Check tracks for metadata that doesn't match EchoList expectations.

        Returns a list of dicts: {"copy_name", "path", "field", "expected", "actual"}
        """
        if pid not in self.store.playlists:
            return []
        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        folder_path = self.writer.root / folder
        if not folder_path.exists():
            return []

        expected_albumartist = self.config.node_name
        expected_album = self.config.album_prefix + playlist["name"]
        issues = []

        for t in playlist["tracks"]:
            copy_name = t["copy_name"]
            if copy_name.startswith("._"):
                continue
            track_path = folder_path / copy_name
            if not track_path.exists():
                continue
            if not tags_supported(track_path):
                # OGG, WMA, DSF, raw ADTS AAC...: copied but never tagged,
                # so they can't be "fixed" either.
                continue

            tags = read_playlist_tags(track_path)

            if tags["albumartist"] != expected_albumartist:
                issues.append({
                    "copy_name": copy_name,
                    "path": str(track_path),
                    "field": "albumartist",
                    "expected": expected_albumartist,
                    "actual": tags["albumartist"],
                })
            if tags["album"] != expected_album:
                issues.append({
                    "copy_name": copy_name,
                    "path": str(track_path),
                    "field": "album",
                    "expected": expected_album,
                    "actual": tags["album"],
                })
            if tags["tracknumber"] != str(t["index"]):
                issues.append({
                    "copy_name": copy_name,
                    "path": str(track_path),
                    "field": "tracknumber",
                    "expected": str(t["index"]),
                    "actual": tags["tracknumber"],
                })

        return issues

    def backup_playlist_metadata(self, pid: str, timestamp: str | None = None) -> Path | None:
        """Save current metadata of all tracks in a playlist before overwriting."""
        if pid not in self.store.playlists:
            return None
        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        folder_path = self.writer.root / folder

        entries = []
        for t in playlist["tracks"]:
            track_path = folder_path / t["copy_name"]
            entry = {
                "copy_name": t["copy_name"],
                "index": t["index"],
                "src_path": t.get("src_path", ""),
                "src_hash": t.get("src_hash", ""),
                "original_tags": t.get("original_tags", {}),
            }
            if track_path.exists():
                entry["tags"] = read_playlist_tags(track_path)
            entries.append(entry)

        if not entries:
            return None

        new_data = {
            "playlist_name": playlist["name"],
            "folder": folder,
            "tracks": entries,
        }

        # Reuse a backup only if it is the newest one: returning an older
        # identical backup would make a later, different state look current.
        backups = list_backups(self.writer.root, pid)
        if backups:
            newest = load_backup(self.writer.root, pid, backups[0]["timestamp"])
            if newest == new_data:
                return backups[0]["path"]

        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        return save_backup(self.writer.root, pid, timestamp, new_data)

    def fix_playlist_metadata(self, pid: str) -> int:
        """Fix metadata on tracks that don't match expectations. Backs up first.
        Returns the number of tracks fixed."""
        issues = self.audit_playlist_metadata(pid)
        if not issues:
            return 0

        self.backup_playlist_metadata(pid)

        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        track_by_name = {t["copy_name"]: t for t in playlist["tracks"]}

        fix_jobs = []
        fixed_files = set()
        for issue in issues:
            track_path = Path(issue["path"])
            if str(track_path) in fixed_files:
                continue
            fixed_files.add(str(track_path))
            t = track_by_name.get(issue["copy_name"])
            if t is None:
                continue
            fix_jobs.append((track_path, t["index"], t.get("src_path", "")))

        album = self.config.album_prefix + playlist["name"]
        def _apply(job):
            path, index, src_rel = job
            self.writer.edit_in_place(path, lambda p: apply_playlist_tags(
                p, self.config.node_name, album, index, src_rel, pid))

        with ThreadPoolExecutor(max_workers=_MAX_TAG_WORKERS) as pool:
            list(pool.map(_apply, fix_jobs))

        self._renumber_tracks(pid)
        self.store.save()

        return len(fixed_files)

    def restore_playlist_metadata(self, pid: str, timestamp: str | None = None) -> int:
        """Restore original metadata from backup. Returns number of tracks restored.
        If timestamp is None, restores the most recent backup."""
        backups = list_backups(self.writer.root, pid)
        if not backups:
            return 0

        if timestamp is None:
            timestamp = backups[0]["timestamp"]

        data = load_backup(self.writer.root, pid, timestamp)
        if not data:
            return 0

        folder = data.get("folder", "")
        if not folder:
            return 0

        folder_path = self.writer.root / folder

        current_tracks = {}
        if pid in self.store.playlists:
            for t in self.store.playlists[pid]["tracks"]:
                otags = t.get("original_tags", {})
                if otags:
                    key = (otags.get("artist", ""), otags.get("title", ""))
                    current_tracks[key] = t["copy_name"]

        restored = 0
        for entry in data.get("tracks", []):
            tags = entry.get("tags")
            if not tags:
                continue
            track_path = folder_path / entry["copy_name"]
            if not track_path.exists():
                otags = entry.get("original_tags", {})
                if otags:
                    key = (otags.get("artist", ""), otags.get("title", ""))
                    cur_name = current_tracks.get(key)
                    if cur_name:
                        track_path = folder_path / cur_name
            if not track_path.exists():
                continue
            self.writer.edit_in_place(track_path, lambda p, tags=tags: restore_tags(p, tags))
            restored += 1

        return restored

    def restore_playlist_to_point(self, pid: str, timestamp: str) -> dict:
        """Restore a playlist to the exact state of a restore point.

        Each restore-point entry is matched to a current track that holds the
        same audio: same source hash, else same source path, else same
        original tags (adopted tracks); a bare file name is trusted only when
        neither side records any of those. Then:

        - matched tracks keep their file, take the restore point's position
          and get its saved tags back (if it saved any);
        - entries with no match are returned in "to_stage" with the position
          ("index", 1-based) they had, for the caller to copy in again;
        - current tracks the restore point does not have are deleted; a file
          the card refuses to delete is kept, listed and reported in "failed".

        Positions stay contiguous: tracks are renumbered afterwards, except
        for a restore point with exact saved tags (an adoption point), whose
        files get their recorded names back.

        Returns {"restored": int, "removed": int,
                 "to_stage": [{"src_path", "src_hash", "index"}],
                 "failed": [str]}
        """
        if pid not in self.store.playlists:
            raise KeyError(f"playlist '{pid}' does not exist")

        data = load_backup(self.writer.root, pid, timestamp)
        if not data:
            raise KeyError(f"backup '{timestamp}' not found for playlist '{pid}'")

        self.backup_playlist_metadata(pid)

        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        folder_path = self.writer.root / folder

        backup_tracks = sorted(data.get("tracks", []), key=lambda t: t.get("index", 0))
        # Only tracks whose file is really there can be kept.
        unmatched = [t for t in playlist["tracks"] if (folder_path / t["copy_name"]).exists()]
        matches: dict[int, dict] = {}
        # Pass 1: same name and not contradicted by identity. Pass 2: identity.
        for by_name in (True, False):
            for pos, bt in enumerate(backup_tracks):
                if pos in matches:
                    continue
                for ct in unmatched:
                    if by_name:
                        ok = (_name_key(ct["copy_name"]) == _name_key(bt["copy_name"])
                              and _same_track(bt, ct) is not False)
                    else:
                        ok = _same_track(bt, ct) is True
                    if ok:
                        matches[pos] = ct
                        unmatched.remove(ct)
                        break

        failed: list[str] = []
        removed = 0
        kept_extra = []
        for ct in unmatched:
            try:
                self.writer.delete(f"{folder}/{ct['copy_name']}")
            except OSError as e:
                failed.append(f"{ct['copy_name']}: could not delete ({e.strerror or e})")
                kept_extra.append(ct)
                continue
            removed += 1

        restored = 0
        to_stage = []
        new_tracks = []
        exact = False
        for pos, bt in enumerate(backup_tracks):
            ct = matches.get(pos)
            if ct is None:
                if bt.get("src_path"):
                    to_stage.append({
                        "src_path": bt["src_path"],
                        "src_hash": bt.get("src_hash", ""),
                        "index": pos + 1,
                    })
                else:
                    failed.append(f"{bt['copy_name']}: missing and has no source to copy from")
                continue
            tags = bt.get("tags")
            if tags:
                self.writer.edit_in_place(
                    folder_path / ct["copy_name"], lambda p, tags=tags: restore_tags(p, tags))
                restored += 1
                exact = exact or isinstance(tags.get("raw"), dict)
            ct["src_path"] = bt.get("src_path", ct.get("src_path", ""))
            ct["src_hash"] = bt.get("src_hash", ct.get("src_hash", ""))
            if bt.get("original_tags"):
                ct["original_tags"] = bt["original_tags"]
            new_tracks.append((ct, bt))

        tracks = [ct for ct, _ in new_tracks] + kept_extra
        for i, ct in enumerate(tracks, 1):
            ct["index"] = i
        playlist["tracks"] = tracks
        try:
            if exact and not kept_extra and not to_stage:
                # An adoption point: give the files their recorded names back.
                self._move_tracks(folder, [(ct, bt["copy_name"]) for ct, bt in new_tracks])
            else:
                self._renumber_tracks(pid)
        finally:
            self.store.save()

        return {"restored": restored, "removed": removed,
                "to_stage": to_stage, "failed": failed}

    def list_metadata_backups(self, pid: str) -> list[dict]:
        return list_backups(self.writer.root, pid)

    def has_metadata_backup(self, pid: str) -> bool:
        return len(list_backups(self.writer.root, pid)) > 0

    def list_deleted_playlists(self) -> list[dict]:
        """Return backup info for playlists that have been deleted but have restore points."""
        all_pids = list_all_backup_pids(self.writer.root)
        deleted = []
        for pid in all_pids:
            if pid in self.store.playlists:
                continue
            backups = list_backups(self.writer.root, pid)
            if not backups:
                continue
            latest = load_backup(self.writer.root, pid, backups[0]["timestamp"])
            if not latest:
                continue
            deleted.append({
                "pid": pid,
                "name": latest.get("playlist_name", pid),
                "backups": backups,
            })
        return deleted

    def restore_deleted_playlist(self, pid: str, timestamp: str | None = None) -> list[dict]:
        """Recreate a deleted playlist from a backup. Returns track source info for staging.

        Each returned dict has: {"src_path": str, "src_hash": str}.
        The caller should stage these as pending adds (same as snapshot restore).
        """
        if pid in self.store.playlists:
            raise ValueError(f"playlist '{pid}' already exists")

        backups = list_backups(self.writer.root, pid)
        if not backups:
            raise KeyError(f"no backups found for playlist '{pid}'")

        if timestamp is None:
            timestamp = backups[0]["timestamp"]

        data = load_backup(self.writer.root, pid, timestamp)
        if not data:
            raise KeyError(f"backup '{timestamp}' not found for playlist '{pid}'")

        name = data.get("playlist_name", pid)
        self.create_playlist(name)

        sources = []
        for entry in data.get("tracks", []):
            src_path = entry.get("src_path", "")
            if src_path:
                sources.append({
                    "src_path": src_path,
                    "src_hash": entry.get("src_hash", ""),
                })
        return sources

    def check_external_import(self) -> bool:
        """Returns True if the Playlists folder exists but has no .echolist marker."""
        if not self.writer.root.exists():
            return False
        echolist_dir = self.writer.root / ".echolist"
        return not echolist_dir.exists()

    def detect_untracked_playlists(self) -> list[dict]:
        """Find folders with audio files inside the workspace that aren't tracked."""
        if not self.writer.root.exists():
            return []
        # FAT32 and macOS treat case/Unicode-form variants as one folder.
        tracked_folders = {_name_key(pl["folder"]) for pl in self.store.playlists.values()}
        untracked = []
        for d in sorted(self.writer.root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if _name_key(d.name) in tracked_folders:
                continue
            audio_files = [
                f for f in d.iterdir()
                if _is_real_audio(f)
            ]
            if audio_files:
                untracked.append({
                    "folder": d.name,
                    "pid": playlist_id(d.name),
                    "track_count": len(audio_files),
                })
        return untracked

    def adopt_playlist(self, folder_name: str) -> str:
        """Adopt an untracked folder as an EchoList playlist.

        Reads original tags, creates a 'before_echolist' backup, then applies
        EchoList metadata. Returns the playlist ID.
        """
        folder_path = self.writer.root / folder_name
        if not folder_path.is_dir():
            raise FileNotFoundError(f"folder not found: {folder_path}")

        check_playlist_folder_name(folder_name)
        pid = playlist_id(folder_name)
        if pid in self.store.playlists:
            raise ValueError(f"playlist '{pid}' already exists")

        # Numeric order: "100 - x" after "99 - y" (plain sorted() would not).
        audio_files = sorted(
            (f for f in folder_path.iterdir() if _is_real_audio(f)),
            key=lambda f: _track_sort_key(f.name),
        )
        if not audio_files:
            raise ValueError(f"no audio files in {folder_name}")

        self.store.add_playlist(pid, folder_name, folder_name)

        def _read(f):
            # read_playlist_tags for matching; snapshot_tags so the restore
            # point brings back every field the tagger is about to change.
            return read_playlist_tags(f), snapshot_tags(f)

        with ThreadPoolExecutor(max_workers=_MAX_TAG_WORKERS) as pool:
            read_results = list(pool.map(_read, audio_files))
        tag_results = [r[0] for r in read_results]
        snapshots = {f.name: r[1] for f, r in zip(audio_files, read_results)}

        for i, (f, original_tags) in enumerate(zip(audio_files, tag_results), 1):
            self.store.add_track(pid, {
                "index": i,
                "src_path": "",
                "copy_name": f.name,
                "src_hash": "",
                "original_tags": original_tags,
            })

        self.store.save()

        save_backup(self.writer.root, pid, "before_echolist", {
            "playlist_name": folder_name,
            "folder": folder_name,
            "tracks": [
                {
                    "copy_name": t["copy_name"],
                    "index": t["index"],
                    "src_path": "",
                    "src_hash": "",
                    "original_tags": t["original_tags"],
                    "tags": (
                        {**t["original_tags"], "raw": snapshots[t["copy_name"]]}
                        if snapshots.get(t["copy_name"]) else t["original_tags"]
                    ),
                }
                for t in self.store.playlists[pid]["tracks"]
            ],
        })

        album = self.config.album_prefix + folder_name
        def _apply_tags(t):
            track_path = folder_path / t["copy_name"]
            if track_path.exists():
                self.writer.edit_in_place(track_path, lambda p: apply_playlist_tags(
                    p, self.config.node_name, album, t["index"], "", pid,
                ))

        with ThreadPoolExecutor(max_workers=_MAX_TAG_WORKERS) as pool:
            list(pool.map(_apply_tags, self.store.playlists[pid]["tracks"]))

        self._renumber_tracks(pid)
        self.store.save()
        self.config.save(self.writer)
        return pid

    def save_snapshot(self) -> Path:
        """Save full playlist structure to ~/.echolist/ so it survives a device wipe."""
        from dataclasses import asdict
        return save_playlist_snapshot(
            self.writer.root,
            asdict(self.config),
            {"schema": 1, "playlists": self.store.playlists},
        )

    @staticmethod
    def find_snapshot(dest_root: str | Path, playlist_folder: str = DEFAULT_PLAYLIST_FOLDER) -> dict | None:
        workspace_root = Path(dest_root).resolve() / playlist_folder
        return load_playlist_snapshot(workspace_root)


def _existing_uuid(writer: SafeWriter) -> str:
    """The workspace UUID already on the card (re-init keeps the card's
    identity), or "" if there is none or the config is unreadable."""
    if not (writer.root / CONFIG_REL).exists():
        return ""
    try:
        return Config.load(writer).workspace_uuid
    except (ValueError, OSError):
        return ""


def _name_key(name: str) -> str:
    """How FAT32 and macOS compare names: case- and normalization-blind."""
    return unicodedata.normalize("NFC", name).casefold()


def _same_track(backup: dict, current: dict) -> bool | None:
    """Whether a restore-point entry and a current track hold the same audio:
    True/False from the strongest identity both record (source hash, then
    source path, then original tags), None when neither records any."""
    for key in ("src_hash", "src_path", "original_tags"):
        a, b = backup.get(key), current.get(key)
        if a and b:
            return a == b
    return None


def _is_real_audio(p: Path) -> bool:
    """True if p is a real audio file, excluding macOS AppleDouble /
    dot-underscore artifacts (._Foo.m4a) which only have an audio-like suffix."""
    return (
        p.is_file()
        and p.suffix.lower() in AUDIO_EXTS
        and not p.name.startswith("._")
    )


def _count_audio_files(root: Path, exclude: Path | None = None) -> int:
    count = 0
    try:
        for f in root.rglob("*"):
            if exclude and f.is_relative_to(exclude):
                continue
            if _is_real_audio(f):
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


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


_INDEX_PREFIX = re.compile(r"^(\d+) - ")


def _track_sort_key(name: str) -> tuple:
    """Order by the numeric "NN - " prefix, so "100 - x" follows "99 - y"
    even when 2- and 3-digit prefixes are mixed; unprefixed names go last."""
    m = _INDEX_PREFIX.match(name)
    if m:
        return (0, int(m.group(1)), name)
    return (1, 0, name)


def _strip_index_prefix(name: str) -> str:
    parts = name.split(" - ", 1)
    return parts[1] if len(parts) > 1 else name


def _read_title(path: Path) -> str:
    return read_title(path) or path.stem


def _title_from_copy_name(path: Path) -> str:
    """Title part of a playlist file name, for renaming it to a new index.

    EchoList names look like "07 - Title.ext": drop only that numeric prefix.
    Anything else (an adopted "01. Song.flac" or "Band - Title.flac") is
    named the way add_track names copies: from the title tag, else the stem.
    """
    stem = path.stem
    if INDEX_PREFIX_RE.match(stem):
        return INDEX_PREFIX_RE.sub("", stem, count=1)
    return _read_title(path)


