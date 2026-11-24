"""EchoList GUI — dark/red cassette-player theme with staged sync."""
from __future__ import annotations

import copy
import errno
import json
import re
import sys
import threading
import tkinter as tk
import unicodedata
import uuid
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from queue import Queue, Empty
from threading import Thread

import os
import signal
import platform
import string
from datetime import datetime

from .manager import PlaylistManager, WorkspaceLockError, AUDIO_EXTS
from .naming import pad_width, playlist_id, sanitize, track_filename
from .config import (load_defaults, save_defaults, DEFAULT_PLAYLIST_FOLDER,
                     journal_file, pending_file, validate_playlist_folder)
from .journal import INCOMPLETE, SyncJournal
from .m3u import parse_m3u, curate_playlist_name

M3U_EXTS = frozenset({".m3u", ".m3u8"})


def _detect_echo_mini() -> str | None:
    system = platform.system()
    if system == "Windows":
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(drive) == 2:  # DRIVE_REMOVABLE
                if ctypes.windll.kernel32.GetVolumeInformationW(
                    drive, buf, 1024, None, None, None, None, 0
                ):
                    if buf.value.upper() == "ECHO MINI":
                        return drive
    elif system == "Darwin":
        volumes = Path("/Volumes")
        if volumes.exists():
            for vol in volumes.iterdir():
                if vol.name.upper() == "ECHO MINI" and vol.is_dir():
                    return str(vol)
    else:
        for base in (Path("/media"), Path("/run/media")):
            if not base.exists():
                continue
            for user_dir in base.iterdir():
                if not user_dir.is_dir():
                    continue
                for vol in user_dir.iterdir():
                    if vol.name.upper() == "ECHO MINI" and vol.is_dir():
                        return str(vol)
    return None


def _default_source() -> str:
    music = Path.home() / "Music"
    if music.exists():
        return str(music)
    return str(Path.home())


def _is_external_path(path: str) -> bool:
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes
            drive = Path(path).anchor
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == 2
        except Exception:
            return False
    elif system == "Darwin":
        return path.startswith("/Volumes/")
    else:
        return path.startswith("/media/") or path.startswith("/run/media/")


# ── Theme colors ──
BG = "#1a1a1a"
BG_PANEL = "#222222"
BG_INPUT = "#2a2a2a"
FG = "#cccccc"
FG_DIM = "#888888"
FG_BRIGHT = "#eeeeee"
RED = "#cc3333"
RED_DARK = "#991111"
RED_BRIGHT = "#ff4444"
GREEN = "#44aa44"
YELLOW = "#ccaa33"
BORDER = "#333333"
PENDING_FG = "#cc9933"

MAX_TRACKS = 8192
PENDING_FILE = Path.home() / ".echolist" / "pending.json"
_HIDDEN_DIRS = frozenset({
    "playlists", "system volume information", "$recycle.bin",
    "recycler", "found.000", "msos",
})


def _read_tags_from_file(path: Path) -> tuple[str, str]:
    try:
        import mutagen
        m = mutagen.File(path, easy=True)
        if m:
            return m.get("title", [""])[0], m.get("artist", [""])[0]
    except Exception:
        pass
    return path.stem, ""


def _resolve_source_file(src_path: str, source_root: Path) -> Path | None:
    """Try to locate a source file using multiple strategies (read-only).

    Resolution order:
    1. Relative to source_root (the normal case)
    2. As an absolute path (if src_path is absolute)
    3. Relative to ancestors of source_root (handles changed source root)
    4. Just the filename, searched under source_root and its parent
    Returns None if not found anywhere.
    """
    if not src_path:
        return None
    p = Path(src_path.replace("\\", "/"))

    # 1. Relative to source_root
    if not p.is_absolute():
        candidate = source_root / p
        if candidate.exists():
            return candidate.resolve()

    # 2. Absolute path as-is
    if p.is_absolute() and p.exists():
        return p.resolve()

    # 3. Try ancestors of source_root (up to 3 levels)
    if not p.is_absolute():
        ancestor = source_root
        for _ in range(3):
            ancestor = ancestor.parent
            if ancestor == ancestor.parent:
                break
            candidate = ancestor / p
            if candidate.exists():
                return candidate.resolve()

    # 4. Filename search under source_root and its parent
    name = p.name
    search_roots = [source_root]
    if source_root.parent != source_root:
        search_roots.append(source_root.parent)
    for root in search_roots:
        try:
            for hit in root.rglob(name):
                if hit.is_file():
                    return hit.resolve()
        except OSError:
            pass

    return None


def _bind_context_menu(widget, handler) -> None:
    """Right-click: Button-3 on Windows/X11; Aqua Tk sends Button-2 and
    Control-click instead."""
    widget.bind("<Button-3>", handler)
    if platform.system() == "Darwin":
        widget.bind("<Button-2>", handler)
        widget.bind("<Control-Button-1>", handler)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


_INDEX_PREFIX_RE = re.compile(r"^\d+ - ")


def _title_part(copy_name: str) -> str:
    """A playlist file name without its "NN - " index prefix, in NFC.

    Stable across renumbering (removes, re-padding at 100 tracks) and across
    the NFC/NFD forms macOS and playlists.json may hold."""
    return _INDEX_PREFIX_RE.sub("", _nfc(copy_name), count=1)


def _same_track(staged: dict, track: dict) -> bool:
    """Does a staged remove (copy_name, optional src_path) still describe
    ``track``? Compares stable identity, never the zero-padded index."""
    name = staged.get("copy_name")
    if name and _title_part(name) != _title_part(track["copy_name"]):
        return False
    src = staged.get("src_path")
    if src and track.get("src_path") and src != track["src_path"]:
        return False
    return True


def _is_hidden_source_entry(name: str) -> bool:
    """Dotfiles (incl. macOS ._* AppleDouble sidecars) and system folders."""
    return name.startswith(".") or name.lower() in _HIDDEN_DIRS


def _walk_source_files(root: Path, exclude: Path | None):
    """Yield files under ``root``, skipping hidden entries and ``exclude``
    (the workspace itself, when the library contains the device)."""
    exclude = exclude.resolve() if exclude else None
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if not _is_hidden_source_entry(d)
            and (exclude is None or (here / d).resolve() != exclude)
        )
        for f in sorted(filenames):
            if not f.startswith("."):
                yield here / f


def _format_backup_timestamp(ts: str) -> str:
    try:
        dt = datetime.strptime(ts, "%Y%m%d_%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


class StagingState:
    """Tracks pending add/remove/reorder operations before SYNC.

    One instance per workspace: ``path`` is that workspace's pending.json
    (config.pending_file). Every mutation saves immediately, and the sync
    engine unstages each operation only after it succeeded, so pending.json
    never holds an operation that already ran.

    Keys in a reorder list name rows of the virtual playlist: ``c:N`` is the
    committed track with index N, ``p:M`` the M-th staged add for that pid.
    """

    _path: Path | None = None
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None, load: bool = True):
        self._path = path
        self._lock = threading.RLock()
        self.pending_adds: list[dict] = []
        self.pending_removes: list[dict] = []
        self.pending_reorders: dict[str, list] = {}
        if load:
            self._load()

    @property
    def path(self) -> Path:
        return self._path or PENDING_FILE

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            raise StagingLoadError(
                f"Could not read staged changes from {self.path}: {e}") from e
        self.pending_adds = data.get("adds", [])
        self.pending_removes = data.get("removes", [])
        self.pending_reorders = data.get("reorders", {})

    def save(self):
        from .safe_write import atomic_write_text
        with self._lock:
            atomic_write_text(self.path, json.dumps({
                "adds": self.pending_adds,
                "removes": self.pending_removes,
                "reorders": self.pending_reorders,
            }, indent=2))

    def clear(self):
        with self._lock:
            self.pending_adds.clear()
            self.pending_removes.clear()
            self.pending_reorders.clear()
            self.path.unlink(missing_ok=True)

    def stage_add(self, pid: str, src: str, title: str, artist: str):
        with self._lock:
            self.pending_adds.append({
                "pid": pid, "src": src, "title": title, "artist": artist,
            })
            self.save()

    def stage_remove(self, pid: str, index: int, copy_name: str, src_path: str = ""):
        entry = {"pid": pid, "index": index, "copy_name": copy_name}
        if src_path:
            entry["src_path"] = src_path
        with self._lock:
            self.pending_removes.append(entry)
            self.save()

    def set_reorder(self, pid: str, order: list[dict]):
        with self._lock:
            self.pending_reorders[pid] = order
            self.save()

    # ── used by sync / structural playlist changes ──

    def unstage_add(self, entry: dict):
        with self._lock:
            self.pending_adds = [a for a in self.pending_adds if a is not entry]
            self.save()

    def unstage_remove(self, entry: dict):
        with self._lock:
            self.pending_removes = [r for r in self.pending_removes if r is not entry]
            self.save()

    def unstage_reorder(self, pid: str):
        with self._lock:
            if self.pending_reorders.pop(pid, None) is not None:
                self.save()

    def drop_pid(self, pid: str):
        """Forget every staged change for a playlist that no longer exists."""
        with self._lock:
            self.pending_adds = [a for a in self.pending_adds if a["pid"] != pid]
            self.pending_removes = [r for r in self.pending_removes if r["pid"] != pid]
            self.pending_reorders.pop(pid, None)
            self.save()

    def rename_pid(self, old: str, new: str):
        """Move staged changes along with a renamed playlist."""
        if old == new:
            return
        with self._lock:
            for entry in self.pending_adds + self.pending_removes:
                if entry["pid"] == old:
                    entry["pid"] = new
            if old in self.pending_reorders:
                self.pending_reorders[new] = self.pending_reorders.pop(old)
            self.save()

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "adds": self.pending_adds,
                "removes": self.pending_removes,
                "reorders": self.pending_reorders,
            })

    def restore(self, snap: dict):
        with self._lock:
            snap = copy.deepcopy(snap)
            self.pending_adds = snap["adds"]
            self.pending_removes = snap["removes"]
            self.pending_reorders = snap["reorders"]
            self.save()

    def rebase(self, pid: str, slots: list[str], tracks: list[dict], add_keys: dict):
        """Re-key the staged changes left for ``pid`` after a partial sync.

        ``slots`` lists, in store order, the pre-sync key of every track now
        in the playlist (``c:N`` for tracks that were there, ``p:M`` for adds
        that just landed). ``add_keys`` maps id() of each still-staged add to
        its pre-sync ``p:M`` key. Afterwards every key/index points at the
        same track it did before the sync. A remove keeps the identity it was
        staged with (copy_name / src_path): taking the name of whatever now
        sits at its index would retarget a rejected remove at another track.
        """
        with self._lock:
            new_key = {k: f"c:{i}" for i, k in enumerate(slots, 1)}
            remaining = [a for a in self.pending_adds if a["pid"] == pid]
            for n, a in enumerate(remaining):
                old = add_keys.get(id(a))
                if old is not None:
                    new_key[old] = f"p:{n}"
            for r in self.pending_removes:
                if r["pid"] != pid:
                    continue
                k = new_key.get(f"c:{r['index']}", "")
                if k.startswith("c:"):
                    r["index"] = int(k[2:])
            if pid in self.pending_reorders:
                self.pending_reorders[pid] = [
                    {**e, "key": new_key[e["key"]]}
                    for e in self.pending_reorders[pid] if e["key"] in new_key
                ]
            self.save()

    @property
    def has_pending(self) -> bool:
        return bool(self.pending_adds or self.pending_removes or self.pending_reorders)

    @property
    def total_ops(self) -> int:
        return len(self.pending_adds) + len(self.pending_removes) + len(self.pending_reorders)

    def virtual_tracks(self, pid: str, committed_tracks: list[dict]) -> tuple[list[dict], list[dict]]:
        """Returns (active_tracks, removed_tracks)."""
        with self._lock:
            removes = list(self.pending_removes)
            adds = list(self.pending_adds)
            order = list(self.pending_reorders.get(pid, []))
            has_order = pid in self.pending_reorders
        removed_indices = {r["index"] for r in removes if r["pid"] == pid}
        result = []
        removed = []
        for t in committed_tracks:
            if t["index"] in removed_indices:
                removed.append({**t, "_pending": False, "_removed": True, "_key": f"c:{t['index']}"})
            else:
                result.append({**t, "_pending": False, "_key": f"c:{t['index']}"})

        pending_count = 0
        for a in adds:
            if a["pid"] == pid:
                result.append({
                    "index": 0,
                    "title": a["title"],
                    "artist": a["artist"],
                    "src_path": a["src"],
                    "_pending": True,
                    "_key": f"p:{pending_count}",
                })
                pending_count += 1

        if has_order:
            by_key = {t["_key"]: t for t in result}
            reordered = []
            for entry in order:
                if entry["key"] in by_key:
                    reordered.append(by_key.pop(entry["key"]))
            reordered.extend(by_key.values())
            result = reordered

        for i, t in enumerate(result, 1):
            t["index"] = i

        return result, removed

    def virtual_track_count(self, store) -> int:
        committed = sum(len(p["tracks"]) for p in store.playlists.values())
        return committed + len(self.pending_adds) - len(self.pending_removes)


class StagingLoadError(Exception):
    """pending.json exists but cannot be read; refuse to start with it empty."""


def _bind_legacy_staging(staging_path: Path, store) -> None:
    """Move the old global ~/.echolist/pending.json into this workspace.

    The legacy file has no workspace id, so it is adopted only when every
    playlist it names exists in this workspace; otherwise it is left alone.
    """
    if staging_path.exists() or not PENDING_FILE.exists():
        return
    legacy = StagingState(PENDING_FILE)
    pids = ({a["pid"] for a in legacy.pending_adds}
            | {r["pid"] for r in legacy.pending_removes}
            | set(legacy.pending_reorders))
    if not pids or not pids <= set(store.playlists):
        return
    moved = StagingState(staging_path, load=False)
    moved.restore(legacy.snapshot())
    PENDING_FILE.unlink()


# ── Sync engine ──
#
# Removes run first (highest index first, so lower staged indices stay valid),
# then adds, then reorders. ``slots`` mirrors each touched playlist as a list
# of pre-sync keys so reorder keys (c:N / p:M) resolve to the tracks they
# meant, whatever ran before them.

_FATAL_ERRNOS = {e for e in (errno.ENOSPC, errno.EIO, errno.EROFS, errno.ENODEV,
                             errno.ENXIO, getattr(errno, "EDQUOT", None)) if e}

TMP_PREFIX = "_echolist_tmp_"
_TMP_RE = re.compile(r"^_echolist_tmp_\d+_(.+)$")
COPY_PART_GLOB = ".echolist-copy-*.part"


class StaleStagingError(Exception):
    """A staged change no longer matches the playlist on the device."""


def _is_fatal(exc: BaseException) -> bool:
    """Device-level failures: stop writing instead of failing every next op."""
    return isinstance(exc, OSError) and exc.errno in _FATAL_ERRNOS


def _sorted_tracks(playlist: dict) -> list[dict]:
    return sorted(playlist["tracks"], key=lambda t: t["index"])


def run_sync(mgr, staging: StagingState, journal_path: Path | None,
             progress=None, file_progress=None) -> tuple[list[str], bool]:
    """Apply every staged change. Returns (errors, journal_closed).

    Each operation is unstaged (and pending.json saved) only after it
    succeeded; a failed one stays staged with its error reported. Device
    errors (ENOSPC, EIO, ...) stop the remaining writes, which stay staged.
    ``journal_closed`` is False only when a reorder could not be completed
    nor rolled back (or its store save failed) — the journal then stays and
    is recovered before the next sync or on the next open.
    """
    if journal_path is not None:
        earlier = SyncJournal.load_incomplete(journal_path)
        if earlier is not None:
            # Starting over would overwrite the record of a reorder whose
            # files moved but whose store save failed (it would be shuffled
            # twice). Finish it first; refuse to sync if that fails.
            notes, unresolved = recover_interrupted_sync(mgr, staging, earlier)
            if unresolved:
                return [f"Sync not started: {n}" for n in notes] or [
                    "Sync not started: an interrupted sync could not be finished"], False
            SyncJournal.discard(journal_path)

    with staging._lock:
        removes = sorted(staging.pending_removes, key=lambda r: r["index"], reverse=True)
        adds = list(staging.pending_adds)
        reorders = {pid: list(order) for pid, order in staging.pending_reorders.items()}

    touched = {r["pid"] for r in removes} | {a["pid"] for a in adds} | set(reorders)
    slots = {}
    for pid in touched:
        pl = mgr.store.playlists.get(pid)
        if pl is not None:
            slots[pid] = [f"c:{t['index']}" for t in _sorted_tracks(pl)]
    add_keys = {}
    counters: dict[str, int] = {}
    for a in adds:
        n = counters.get(a["pid"], 0)
        add_keys[id(a)] = f"p:{n}"
        counters[a["pid"]] = n + 1

    journal = SyncJournal.begin(removes, adds, reorders, path=journal_path)
    errors: list[str] = []
    failed_pids: set[str] = set()
    aborted = False
    j = 0
    done = 0

    def fail(idx, pid, label, exc):
        nonlocal aborted
        msg = f"{label}: {exc}"
        errors.append(msg)
        journal.mark_failed(idx, msg)
        failed_pids.add(pid)
        if _is_fatal(exc):
            aborted = True

    for r in removes:
        label = f"Remove {r.get('copy_name') or '#' + str(r['index'])} from {r['pid']}"
        if aborted:
            journal.mark_failed(j, "not attempted: sync stopped after a device error")
        else:
            pl = mgr.store.playlists.get(r["pid"])
            if pl is not None:
                # Lets recovery tell whether this remove ran before a crash.
                journal.update(j, count_before=len(pl["tracks"]))
            journal.mark_current(j)
            done += 1
            if progress:
                progress(done, "Removing...")
            try:
                _remove_staged(mgr, r, slots)
            except StaleStagingError as e:
                # The track it named is gone or replaced: it can never apply.
                staging.unstage_remove(r)
                fail(j, r["pid"], label, f"{e} (the removal was unstaged)")
            except Exception as e:  # reported to the user; the remove stays staged
                fail(j, r["pid"], label, e)
            else:
                staging.unstage_remove(r)
                journal.mark_done(j)
        j += 1

    for a in adds:
        name = Path(a["src"]).name
        if aborted:
            journal.mark_failed(j, "not attempted: sync stopped after a device error")
        else:
            pl = mgr.store.playlists.get(a["pid"])
            if pl is not None:
                # Lets recovery find a copy published before the store save.
                journal.update(j, count_before=len(pl["tracks"]))
            journal.mark_current(j)
            done += 1
            if progress:
                progress(done, a.get("title") or name)
            try:
                if a["pid"] not in mgr.store.playlists:
                    raise KeyError(f"playlist '{a['pid']}' does not exist")
                mgr.add_track(a["pid"], a["src"], progress_cb=file_progress)
            except Exception as e:  # reported; the add stays staged
                fail(j, a["pid"], name, e)
            else:
                slots[a["pid"]].append(add_keys[id(a)])
                staging.unstage_add(a)
                journal.mark_done(j)
        j += 1

    closed = True
    for pid, order in reorders.items():
        if aborted:
            journal.mark_failed(j, "not attempted: sync stopped after a device error")
        elif pid not in mgr.store.playlists:
            fail(j, pid, f"Reorder {pid}", KeyError(f"playlist '{pid}' does not exist"))
        elif pid in failed_pids:
            journal.mark_failed(j, "skipped: another change to this playlist failed")
            errors.append(f"Reorder {pid}: kept staged until its other changes sync")
        else:
            journal.mark_current(j)
            done += 1
            if progress:
                progress(done, "Reordering...")
            try:
                tag_errors = _apply_reorder(mgr, pid, order, slots[pid], journal, j)
            except ReorderSaveFailed as e:
                # Files and the in-memory store are in the new order; only the
                # save is missing. The kept journal finishes it, so the staged
                # reorder must not run a second time.
                errors.append(f"Reorder {pid}: {e}")
                closed = False
                slots[pid] = [f"c:{i}" for i in range(1, len(slots[pid]) + 1)]
                staging.unstage_reorder(pid)
            except ReorderInterrupted as e:
                errors.append(f"Reorder {pid}: {e}")
                closed = False
            except Exception as e:  # rolled back; the reorder stays staged
                fail(j, pid, f"Reorder {pid}", e)
            else:
                errors.extend(tag_errors)
                slots[pid] = [f"c:{i}" for i in range(1, len(slots[pid]) + 1)]
                staging.unstage_reorder(pid)
                journal.mark_done(j)
        j += 1

    for pid, pid_slots in slots.items():
        pl = mgr.store.playlists.get(pid)
        if pl is not None and len(pid_slots) == len(pl["tracks"]):
            staging.rebase(pid, pid_slots, _sorted_tracks(pl), add_keys)

    if closed:
        journal.complete()
    return errors, closed


def _remove_staged(mgr, r: dict, slots: dict) -> None:
    pid = r["pid"]
    if pid not in mgr.store.playlists:
        raise KeyError(f"playlist '{pid}' does not exist")
    key = f"c:{r['index']}"
    if key not in slots[pid]:
        raise StaleStagingError("the track is no longer in the playlist")
    index = slots[pid].index(key) + 1
    track = _sorted_tracks(mgr.store.playlists[pid])[index - 1]
    if not _same_track(r, track):
        raise StaleStagingError(
            f"expected {r['copy_name']!r} at #{index} but found "
            f"{track['copy_name']!r}; the playlist changed since this was "
            f"staged, so nothing was removed")
    mgr.remove_track(pid, index)
    slots[pid].remove(key)


class ReorderInterrupted(Exception):
    """A reorder failed mid-rename and could not be rolled back."""


class ReorderSaveFailed(ReorderInterrupted):
    """Every file was renamed but the store could not be saved."""


def _reorder_plan(mgr, pid: str, order: list[dict], slots: list[str]) -> list[dict]:
    from .manager import _title_from_copy_name
    playlist = mgr.store.playlists[pid]
    folder_path = mgr.writer.root / playlist["folder"]
    tracks = _sorted_tracks(playlist)
    if len(tracks) != len(slots):
        raise StaleStagingError("the playlist changed during sync; reorder not applied")
    wanted = [slots.index(e["key"]) for e in order if e["key"] in slots]
    wanted += [i for i in range(len(tracks)) if i not in wanted]
    pad = pad_width(len(tracks))
    plan = []
    for new_idx, pos in enumerate(wanted, 1):
        old_name = tracks[pos]["copy_name"]
        title = _title_from_copy_name(folder_path / old_name)
        plan.append({
            "old": old_name,
            "tmp": f"{TMP_PREFIX}{new_idx}_{old_name}",
            "new": track_filename(new_idx, title, Path(old_name).suffix, pad),
            "index": new_idx,
        })
    return plan


def _exists(mgr, folder: str, name: str) -> bool:
    return (mgr.writer.root / folder / name).exists()


def _finish_reorder_renames(mgr, folder: str, plan: list[dict], phase: str,
                            set_phase=None) -> None:
    """Two-pass rename (old -> tmp -> new), resumable from ``phase``.

    Phase "tmp": files still at their old name have not moved yet. Phase
    "final": every moving file is at its tmp name or already at its new one.
    """
    moving = [p for p in plan if p["old"] != p["new"]]
    if phase == "tmp":
        for p in moving:
            if _exists(mgr, folder, p["tmp"]):
                continue
            if not _exists(mgr, folder, p["old"]):
                raise FileNotFoundError(f"{folder}/{p['old']} is missing")
            mgr.writer.rename(f"{folder}/{p['old']}", f"{folder}/{p['tmp']}")
        if set_phase:
            set_phase("final")
    for p in moving:
        if _exists(mgr, folder, p["tmp"]):
            mgr.writer.rename(f"{folder}/{p['tmp']}", f"{folder}/{p['new']}")
        elif not _exists(mgr, folder, p["new"]):
            raise FileNotFoundError(f"{folder}/{p['new']} is missing")


def _rollback_reorder_renames(mgr, folder: str, plan: list[dict], phase: str) -> None:
    """Put every file of a failed reorder back under its old name.

    In phase "tmp" a file at some new name may still be another track's
    original, so only tmp -> old is undone. In phase "final" every moving file
    is at its tmp or new name, so new -> tmp is undone first.
    """
    moving = [p for p in plan if p["old"] != p["new"]]
    if phase == "final":
        for p in moving:
            if not _exists(mgr, folder, p["tmp"]) and _exists(mgr, folder, p["new"]):
                mgr.writer.rename(f"{folder}/{p['new']}", f"{folder}/{p['tmp']}")
    for p in moving:
        if _exists(mgr, folder, p["tmp"]):
            mgr.writer.rename(f"{folder}/{p['tmp']}", f"{folder}/{p['old']}")


def _commit_reorder(mgr, pid: str, plan: list[dict], token: str | None = None) -> list[str]:
    """Point the store at the renamed files, save it, then re-tag them.

    ``token`` (from the journal) is written into the playlist with the same
    save, so a re-run after a crash knows exactly whether the store already
    holds the new order. Names cannot tell: for a swap of two same-title
    tracks the old and new name lists are identical. Journals written before
    the token existed fall back to comparing names.
    """
    from .tags import apply_playlist_tags, tags_supported
    playlist = mgr.store.playlists[pid]
    folder = playlist["folder"]
    tracks = _sorted_tracks(playlist)
    if token is not None:
        committed = playlist.get("reorder_commit") == token
    else:
        committed = [t["copy_name"] for t in tracks] == [p["new"] for p in plan]
    if committed:
        new_tracks = tracks
    else:
        by_name = {t["copy_name"]: t for t in tracks}
        new_tracks = []
        for p in plan:
            t = by_name.get(p["old"])
            if t is None:
                raise StaleStagingError(f"{p['old']} is not in playlist {pid}")
            new_tracks.append(t)
        for p, t in zip(plan, new_tracks):
            t["index"] = p["index"]
            t["copy_name"] = p["new"]
        playlist["tracks"] = new_tracks
    if token is not None:
        playlist["reorder_commit"] = token
    mgr.store.save()

    errors = []
    album = mgr.config.album_prefix + playlist["name"]
    for t in new_tracks:
        path = mgr.writer.resolve(f"{folder}/{t['copy_name']}")
        if not tags_supported(path):
            continue
        try:
            apply_playlist_tags(path, mgr.config.node_name, album, t["index"],
                                t.get("src_path", ""), pid)
        except Exception as e:  # file order is right; "Fix metadata" can retry tags
            errors.append(f"Tag {t['copy_name']}: {e}")
    return errors


def _apply_reorder(mgr, pid: str, order: list[dict], slots: list[str],
                   journal: SyncJournal, j: int) -> list[str]:
    plan = _reorder_plan(mgr, pid, order, slots)
    folder = mgr.store.playlists[pid]["folder"]
    phase = ["tmp"]

    def set_phase(ph):
        phase[0] = ph
        journal.update(j, phase=ph)

    token = uuid.uuid4().hex
    journal.update(j, plan=plan, folder=folder, phase="tmp", token=token)
    try:
        _finish_reorder_renames(mgr, folder, plan, "tmp", set_phase=set_phase)
    except Exception as e:
        try:
            _rollback_reorder_renames(mgr, folder, plan, phase[0])
        except Exception as rb:
            raise ReorderInterrupted(
                f"{e}; rolling back also failed ({rb}). EchoList will finish "
                f"this reorder the next time the workspace is opened.") from e
        raise
    journal.update(j, phase="commit")
    try:
        return _commit_reorder(mgr, pid, plan, token)
    except OSError as e:
        raise ReorderSaveFailed(
            f"the files were reordered but the playlist list could not be "
            f"saved ({e}). EchoList saves it before the next sync, or when "
            f"the workspace is next opened.") from e


# ── Crash recovery (run when a workspace is opened) ──

def _restore_stranded_temp_files(mgr, skip_folders=frozenset()) -> list[str]:
    """Rename leftover _echolist_tmp_* files back to their original names.

    They are real tracks parked mid-reorder: never delete them. If the
    original name is taken, pick a free "(recovered N)" name so rescan
    adopts the file instead. Folders in ``skip_folders`` belong to a reorder
    whose journal is kept for another attempt; their files stay parked.
    """
    notes = []
    skip = {(mgr.writer.root / f).resolve() for f in skip_folders}
    for f in sorted(mgr.writer.root.rglob(TMP_PREFIX + "*")):
        m = _TMP_RE.match(f.name)
        if not m or not f.is_file() or f.parent.resolve() in skip:
            continue
        target = f.with_name(m.group(1))
        n = 1
        while target.exists():
            stem, ext = Path(m.group(1)).stem, Path(m.group(1)).suffix
            target = f.with_name(f"{stem} (recovered {n}){ext}")
            n += 1
        mgr.writer.rename(f, target)
        notes.append(f"Restored {f.parent.name}/{target.name}")
    return notes


def _sweep_copy_parts(mgr) -> None:
    """Delete partial copies stranded by a crash during SafeWriter.copy_in.

    Safe because the workspace lock is held: no other EchoList is copying.
    """
    for f in mgr.writer.root.rglob(COPY_PART_GLOB):
        if f.is_file():
            mgr.writer.delete(f)


def _src_in_playlist(mgr, pid: str, src: str) -> bool:
    source_root = Path(mgr.config.source_root).resolve()
    p = Path(src)
    if not p.is_absolute():
        p = source_root / p
    p = p.resolve()
    for t in mgr.store.playlists[pid]["tracks"]:
        sp = Path(t.get("src_path", ""))
        if not t.get("src_path"):
            continue
        if not sp.is_absolute():
            sp = source_root / sp
        if sp.resolve() == p:
            return True
    return False


def _adopt_published_add(mgr, a: dict, count_before: int) -> bool:
    """Attach a crash-interrupted add whose copy was already published.

    add_track publishes the file, then saves the store. A crash between the
    two leaves a file rescan only knows as an unknown track (no src_path).
    If the track at the add's position is unknown and carries the name this
    source gets, it is that copy: record its source instead of copying again.
    """
    from .manager import _read_title
    from .tags import read_playlist_tags
    tracks = _sorted_tracks(mgr.store.playlists[a["pid"]])
    if len(tracks) <= count_before:
        return False
    t = tracks[count_before]
    if t.get("src_path"):
        return False
    source_root = Path(mgr.config.source_root).resolve()
    src = Path(a["src"])
    if not src.is_absolute():
        src = source_root / src
    src = src.resolve()
    if not src.is_file():
        return False
    if _title_part(t["copy_name"]) != _nfc(sanitize(_read_title(src)) + src.suffix):
        return False
    try:
        t["src_path"] = src.relative_to(source_root).as_posix()
    except ValueError:
        t["src_path"] = src.as_posix()
    t["original_tags"] = read_playlist_tags(src)
    mgr.store.save()
    return True


def _reconcile_staged_removes(mgr, staging, actions, notes) -> None:
    removes = [a for a in actions if a.get("op") == "remove"]
    for r in list(staging.pending_removes):
        pl = mgr.store.playlists.get(r["pid"])
        if pl is None:
            continue
        tracks = _sorted_tracks(pl)
        act = next((a for a in removes if a["pid"] == r["pid"]
                    and a["index"] == r["index"]
                    and a.get("copy_name", "") == r.get("copy_name", "")), None)
        if act is not None and act.get("status") == "in_progress":
            if "count_before" in act:
                ran = len(tracks) < act["count_before"]
            else:
                ran = not any(_same_track(r, t) for t in tracks)
            if ran:
                staging.unstage_remove(r)
                continue
        if 0 < r["index"] <= len(tracks) and _same_track(r, tracks[r["index"] - 1]):
            continue
        matches = [t for t in tracks if _same_track(r, t)]
        if len(matches) == 1:
            r["index"] = matches[0]["index"]
        else:
            staging.unstage_remove(r)
            notes.append(f"A staged removal of {r.get('copy_name') or '#' + str(r['index'])} "
                         f"from {r['pid']} was dropped: that track is no longer "
                         f"in the playlist.")


def recover_interrupted_sync(mgr, staging: StagingState,
                             journal: SyncJournal | None) -> tuple[list[str], set[str]]:
    """Bring the workspace and staging back to a consistent state after a crash.

    1. Finish any reorder whose rename plan was journaled (roll forward,
       recording each phase); if that fails, roll it back; if that fails too,
       the reorder is unresolved and its folder is left exactly as it is.
    2. Rename any other stranded _echolist_tmp_* file back (never delete).
    3. Rescan playlists, then drop staged ops the crash already applied (and
       adopt an add whose copy was published before the store save). A staged
       reorder is dropped only if its playlist changed; a reorder the crash
       never reached is kept.

    Returns (notes for the user, pids of unresolved reorders). The caller
    discards the journal only when nothing is unresolved.
    """
    notes: list[str] = []
    finished: set[str] = set()
    unresolved: set[str] = set()
    unresolved_folders: set[str] = set()
    actions = journal.actions if journal else []
    for idx, a in enumerate(actions):
        if (a.get("status") not in INCOMPLETE or a.get("op") != "reorder"
                or not a.get("plan") or a["pid"] not in mgr.store.playlists):
            continue
        pid = a["pid"]

        def set_phase(ph, idx=idx):
            journal.update(idx, phase=ph)

        try:
            if a.get("phase", "tmp") != "commit":
                _finish_reorder_renames(mgr, a["folder"], a["plan"],
                                        a.get("phase", "tmp"), set_phase=set_phase)
                set_phase("commit")
            notes.extend(_commit_reorder(mgr, pid, a["plan"], a.get("token")))
        except (OSError, StaleStagingError) as e:
            rolled_back = False
            if a.get("phase", "tmp") != "commit":
                try:
                    _rollback_reorder_renames(mgr, a["folder"], a["plan"],
                                              a.get("phase", "tmp"))
                    rolled_back = True
                except OSError as rb:
                    e = f"{e}; rolling back also failed ({rb})"
            if rolled_back:
                journal.mark_failed(idx, f"rolled back on recovery: {e}")
                notes.append(f"Could not finish reordering {pid} ({e}); the files "
                             f"were put back in their old order.")
            else:
                unresolved.add(pid)
                unresolved_folders.add(a["folder"])
                notes.append(f"Could not finish reordering {pid}: {e}. Its files "
                             f"were left as they are; EchoList tries again before "
                             f"the next sync and when the workspace is next opened.")
        else:
            journal.mark_done(idx)
            finished.add(pid)
            notes.append(f"Finished the interrupted reorder of {pid}.")
    notes.extend(_restore_stranded_temp_files(mgr, unresolved_folders))
    if journal is None:
        return notes, unresolved

    changed = {pid for pid in list(mgr.store.playlists)
               if pid not in unresolved and mgr.rescan_playlist(pid)}
    # Adds/removes that ran (or may have) shift the keys of a staged reorder.
    shifted = {a["pid"] for a in actions if a.get("op") in ("add", "remove")
               and a.get("status") in ("in_progress", "done")}
    with staging._lock:
        add_actions = [a for a in actions if a.get("op") == "add"
                       and a.get("status") == "in_progress" and "count_before" in a]
        for a in list(staging.pending_adds):
            if a["pid"] not in mgr.store.playlists:
                continue
            if _src_in_playlist(mgr, a["pid"], a["src"]):
                staging.unstage_add(a)
                continue
            act = next((x for x in add_actions
                        if x["pid"] == a["pid"] and x["src"] == a["src"]), None)
            if act is not None and _adopt_published_add(mgr, a, act["count_before"]):
                staging.unstage_add(a)
                notes.append(f"Kept the copy of {Path(a['src']).name} that the "
                             f"interrupted sync had already written.")
        _reconcile_staged_removes(mgr, staging, actions, notes)
        for pid in list(staging.pending_reorders):
            if pid in finished:
                staging.unstage_reorder(pid)
            elif pid in unresolved:
                continue
            elif pid in changed or pid in shifted:
                staging.unstage_reorder(pid)
                notes.append(f"The staged reorder of {pid} was dropped: the "
                             f"playlist changed during the interrupted sync.")
        staging.save()
    return notes, unresolved


ICON_PATH = Path(__file__).with_name("echolist.ico")
ICON_PNG_PATH = Path(__file__).with_name("echolist.png")


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("EchoList")
        self.root.configure(bg=BG)
        self._set_icon()
        self._center_window(420, 700)
        self.root.minsize(380, 550)
        self._init_state()
        self._poll_callbacks()
        self._apply_theme()
        self._show_setup()

    def _init_state(self):
        """Every attribute the App relies on, before any screen is built.

        Kept separate from __init__ so tests can build an App on a withdrawn
        root without the setup screen and still get exactly this state.
        """
        self.mgr = None
        self.source = None
        self.dest = None
        self.current_pid = None
        # Bound to a workspace's pending.json in _open_workspace.
        self.staging = StagingState(load=False)
        self._undo_stack: list[dict] = []
        # Close was requested while a sync or playlist operation ran: shut
        # down (releasing the workspace lock) once the last one finishes.
        self._exit_after_sync = False
        self._sort_col = None
        self._sort_reverse = False
        self._drag_data = None
        self._cached_device_tracks = 0
        self._cached_workspace_bytes = 0
        self._stats_pending = False
        self._alive = True
        self._syncing = False
        self._busy_playlists: set[str] = set()
        self._tag_cache: dict[str, tuple[str, str]] = {}
        self._audit_cache: dict[str, list[dict]] = {}
        self._tracks_loading = False
        self._tracks_gen = 0
        self._track_data: list[dict] = []
        self._removed_data: list[dict] = []
        self._callback_queue: Queue = Queue()
        self._workers: list[Thread] = []

    def _busy(self) -> bool:
        """A sync or a background playlist operation owns the store/card.

        Every action that changes the store, staging, the card or the
        workspace checks this first, so only one writer runs at a time.
        """
        return self._syncing or bool(self._busy_playlists)

    def _can_rescan(self) -> bool:
        """Rescans rewrite the store from the files on the card. Not while a
        worker owns the store, and not while a kept sync journal still has to
        finish a reorder: its parked files would drop out of the store."""
        return (not self._busy()
                and not journal_file(self.mgr.writer.root).exists())

    def _spawn(self, target, *args) -> Thread:
        """Start a daemon worker thread and remember it (see _workers)."""
        self._workers = [t for t in self._workers if t.is_alive()]
        t = Thread(target=target, args=args, daemon=True)
        self._workers.append(t)
        t.start()
        return t

    def _invalidate_caches(self, pid: str | None = None):
        """Clear tag and audit caches. If pid given, only that playlist."""
        if pid is None:
            self._tag_cache.clear()
            self._audit_cache.clear()
        else:
            self._audit_cache.pop(pid, None)
            pl = self.mgr.store.playlists.get(pid) if self.mgr else None
            if pl:
                prefix = f"{pl['folder']}/"
                self._tag_cache = {k: v for k, v in self._tag_cache.items()
                                   if not k.startswith(prefix)}

    def _poll_callbacks(self):
        """Drain the callback queue on the main thread (tkinter is not thread-safe on Windows)."""
        try:
            self._drain_callbacks()
        finally:
            if self._alive:
                self.root.after(16, self._poll_callbacks)

    def _drain_callbacks(self) -> int:
        """Run every queued callback; returns how many ran.

        A callback that raises goes to Tk's report_callback_exception, like
        an exception in any other Tk callback, and the rest still run.
        """
        ran = 0
        while True:
            try:
                fn = self._callback_queue.get_nowait()
            except Empty:
                return ran
            ran += 1
            try:
                fn()
            except Exception:
                self.root.report_callback_exception(*sys.exc_info())

    def _schedule_callback(self, fn):
        """Thread-safe way to schedule a function on the main thread."""
        self._callback_queue.put(fn)

    def _set_icon(self):
        if ICON_PNG_PATH.exists():
            try:
                icon = tk.PhotoImage(file=str(ICON_PNG_PATH))
                self.root.iconphoto(True, icon)
                self._icon_ref = icon
            except tk.TclError:
                pass
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("echolist.app")
        except Exception:
            pass

    def run(self):
        self.root.mainloop()

    def _center_window(self, w, h):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _apply_theme(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG, fieldbackground=BG_INPUT,
                         bordercolor=BORDER, darkcolor=BG, lightcolor=BG,
                         troughcolor=BG_PANEL, selectbackground=RED_DARK,
                         selectforeground=FG_BRIGHT, font=("Consolas", 9))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TButton", background=BG_PANEL, foreground=FG, bordercolor=BORDER, padding=4)
        style.map("TButton",
                   background=[("active", RED_DARK), ("pressed", RED)],
                   foreground=[("active", FG_BRIGHT)])
        style.configure("Accent.TButton", background=RED_DARK, foreground=FG_BRIGHT)
        style.map("Accent.TButton",
                   background=[("active", RED), ("pressed", RED_BRIGHT)])
        style.configure("Sync.TButton", background=RED, foreground=FG_BRIGHT,
                         font=("Consolas", 11, "bold"), padding=6)
        style.map("Sync.TButton",
                   background=[("active", RED_BRIGHT), ("pressed", RED_DARK)])
        style.configure("TEntry", fieldbackground=BG_INPUT, foreground=FG_BRIGHT,
                         insertcolor=FG_BRIGHT, bordercolor=BORDER)
        style.configure("Title.TLabel", font=("Consolas", 18, "bold"), foreground=RED, background=BG)
        style.configure("Section.TLabel", font=("Consolas", 10, "bold"), foreground=RED, background=BG)
        style.configure("Treeview", background=BG_INPUT, foreground=FG, fieldbackground=BG_INPUT,
                         bordercolor=BORDER, font=("Consolas", 9), rowheight=22)
        style.configure("Treeview.Heading", background=BG_PANEL, foreground=RED,
                         bordercolor=BORDER, font=("Consolas", 9, "bold"))
        style.map("Treeview",
                   background=[("selected", RED_DARK)],
                   foreground=[("selected", FG_BRIGHT)])
        style.configure("TScrollbar", background=BG_PANEL, troughcolor=BG,
                         bordercolor=BG, arrowcolor=FG_DIM)
        style.configure("TPanedwindow", background=BORDER)
        style.configure("Red.Horizontal.TProgressbar", troughcolor=BG_PANEL,
                         background=RED, bordercolor=BORDER)
        style.configure("Green.Horizontal.TProgressbar", troughcolor=BG_PANEL,
                         background=GREEN, bordercolor=BORDER)
        style.configure("Yellow.Horizontal.TProgressbar", troughcolor=BG_PANEL,
                         background=YELLOW, bordercolor=BORDER)
        style.configure("Sync.Horizontal.TProgressbar", troughcolor=BG_PANEL,
                         background=RED, bordercolor=BORDER)

    # ── Setup ──

    def _show_setup(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.config(menu=tk.Menu(self.root))
        self._center_window(420, 280)

        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="ECHOLIST", style="Title.TLabel").pack(pady=(0, 20))

        self._setup_status = tk.Label(frame, text="",
                                       font=("Consolas", 10), bg=BG, fg=FG_DIM,
                                       wraplength=350)
        self._setup_status.pack(pady=(0, 10))

        self._setup_btn_frame = ttk.Frame(frame)
        self._setup_btn_frame.pack(pady=(5, 10))

        adv_lbl = tk.Label(frame, text="Settings...",
                            font=("Consolas", 9, "underline"),
                            bg=BG, fg=FG_DIM, cursor="hand2")
        adv_lbl.pack(pady=(10, 0))
        adv_lbl.bind("<Button-1>", lambda e: self._show_settings())

        self.root.after(200, self._detect_and_show)

    def _detect_and_show(self):
        defaults = load_defaults()
        saved_source = defaults.get("source", "")
        saved_dest = defaults.get("dest", "")
        echo_mini = _detect_echo_mini()

        for w in self._setup_btn_frame.winfo_children():
            w.destroy()

        if saved_source and saved_dest:
            dest_exists = Path(saved_dest).exists()
            if echo_mini:
                self._setup_status.config(
                    text=f"Echo Mini connected at {echo_mini}", fg=GREEN)
            elif dest_exists:
                self._setup_status.config(
                    text=f"Workspace: {saved_dest}", fg=FG)
            else:
                self._setup_status.config(
                    text=f"Saved destination not found:\n{saved_dest}\n\n"
                         f"Connect your device or change workspace in Settings.",
                    fg=FG_DIM)
                btn_row = ttk.Frame(self._setup_btn_frame)
                btn_row.pack()
                ttk.Button(btn_row, text="Retry",
                            command=self._detect_and_show).pack(side="left", padx=5)
                ttk.Button(btn_row, text="Browse...",
                            command=self._browse_dest).pack(side="left", padx=5)
                return

            dest = echo_mini or saved_dest
            ttk.Button(self._setup_btn_frame, text="[ OPEN ]",
                        style="Accent.TButton",
                        command=lambda: self._open_with_dest(dest)).pack(pady=5)
        elif echo_mini:
            self._setup_status.config(
                text=f"Echo Mini found at {echo_mini}", fg=GREEN)
            ttk.Button(self._setup_btn_frame, text="[ OPEN ]",
                        style="Accent.TButton",
                        command=lambda: self._open_with_dest(echo_mini)).pack(pady=5)
        else:
            self._setup_status.config(
                text="Echo Mini not detected.\nConnect your player or choose a folder.",
                fg=FG_DIM)
            btn_row = ttk.Frame(self._setup_btn_frame)
            btn_row.pack()
            ttk.Button(btn_row, text="Retry",
                        command=self._detect_and_show).pack(side="left", padx=5)
            ttk.Button(btn_row, text="Browse...",
                        command=self._browse_dest).pack(side="left", padx=5)

    def _browse_dest(self):
        d = filedialog.askdirectory(title="Select destination device or folder")
        if not d:
            return
        if not _is_external_path(d):
            result = messagebox.askokcancel(
                "Local folder selected",
                "You selected a local folder. EchoList works best with "
                "an external music player like Echo Mini.\n\n"
                "Playlists created in a local folder won't be "
                "playable on a device.\n\n"
                "Continue anyway?")
            if not result:
                return
        self._open_with_dest(d)

    def _open_with_dest(self, dest):
        defaults = load_defaults()
        source = defaults.get("source") or _default_source()
        folder = DEFAULT_PLAYLIST_FOLDER
        if defaults.get("dest") == str(Path(dest).resolve()):
            folder = defaults.get("playlist_folder") or DEFAULT_PLAYLIST_FOLDER
        self._open_workspace(source, dest, playlist_folder=folder)

    def _show_settings(self):
        """Unified settings screen — used both for initial setup and in-app config."""
        if self.mgr is not None and self._busy():
            return
        for w in self.root.winfo_children():
            w.destroy()
        self.root.config(menu=tk.Menu(self.root))
        self._center_window(460, 440)

        is_open = self.mgr is not None

        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="SETTINGS", style="Title.TLabel").pack(pady=(0, 15))

        frame = ttk.Frame(outer)
        frame.pack(fill="x")
        frame.columnconfigure(1, weight=1)

        defaults = load_defaults()
        cwd = str(Path.cwd())

        cur_source = self.source or defaults.get("source", cwd)
        cur_dest = self.dest or defaults.get("dest", cwd)
        existing_config = self._load_existing_config(cur_dest) if not is_open else {}

        row = 0

        # ── Source ──
        ttk.Label(frame, text="SOURCE").grid(row=row, column=0, sticky="w", pady=(4, 0))
        self._source_var = tk.StringVar(value=cur_source)
        src_row = ttk.Frame(frame)
        src_row.grid(row=row, column=1, columnspan=2, sticky="ew", padx=5, pady=(4, 0))
        ttk.Entry(src_row, textvariable=self._source_var, width=32).pack(side="left", fill="x", expand=True)
        ttk.Button(src_row, text="...", width=3,
                    command=lambda: self._browse_var(self._source_var, "Select source music library")).pack(side="left", padx=(4, 0))
        row += 1
        tk.Label(frame, text="Your music library — files are copied from here, never modified",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 6))
        row += 1

        # ── Dest ──
        ttk.Label(frame, text="DEST").grid(row=row, column=0, sticky="w", pady=(4, 0))
        self._dest_var = tk.StringVar(value=cur_dest)
        dest_row = ttk.Frame(frame)
        dest_row.grid(row=row, column=1, columnspan=2, sticky="ew", padx=5, pady=(4, 0))
        ttk.Entry(dest_row, textvariable=self._dest_var, width=32).pack(side="left", fill="x", expand=True)
        ttk.Button(dest_row, text="...", width=3,
                    command=lambda: self._browse_var(self._dest_var, "Select destination device or folder")).pack(side="left", padx=(4, 0))
        row += 1
        tk.Label(frame, text="Device or folder where playlists are stored",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 6))
        row += 1

        # ── Separator ──
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=8)
        row += 1

        # ── Playlist folder ──
        ttk.Label(frame, text="FOLDER").grid(row=row, column=0, sticky="w", pady=(4, 0))
        default_folder = (self.mgr.config.playlist_folder if is_open
                          else existing_config.get("playlist_folder", DEFAULT_PLAYLIST_FOLDER))
        self._folder_var = tk.StringVar(value=default_folder)
        ttk.Entry(frame, textvariable=self._folder_var, width=20).grid(
            row=row, column=1, sticky="w", padx=5, pady=(4, 0))
        row += 1
        tk.Label(frame, text="Root folder name on the device (default: Playlists)",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 6))
        row += 1

        # ── Backup interval ──
        ttk.Label(frame, text="BACKUP EVERY").grid(row=row, column=0, sticky="w", pady=(4, 0))
        default_interval = (str(self.mgr.config.backup_interval) if is_open
                            else str(existing_config.get("backup_interval", 5)))
        self._backup_var = tk.StringVar(value=default_interval)
        backup_frame = ttk.Frame(frame)
        backup_frame.grid(row=row, column=1, sticky="w", padx=5, pady=(4, 0))
        vcmd = (frame.register(lambda v: v.isdigit() or v == ""), "%P")
        ttk.Entry(backup_frame, textvariable=self._backup_var, width=5,
                  validate="key", validatecommand=vcmd).pack(side="left")
        ttk.Label(backup_frame, text=" syncs").pack(side="left")
        row += 1
        tk.Label(frame, text="How often to create automatic restore points (1 = every sync)",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 6))
        row += 1

        # ── Buttons (centered) ──
        btn_row = ttk.Frame(outer)
        btn_row.pack(pady=(15, 0))
        if is_open:
            ttk.Button(btn_row, text="Cancel",
                        command=self._show_main).pack(side="left", padx=5)
        else:
            ttk.Button(btn_row, text="Back",
                        command=self._show_setup).pack(side="left", padx=5)
        ttk.Button(btn_row, text="[ OPEN ]", style="Accent.TButton",
                    command=self._on_settings_open).pack(side="left", padx=5)
        self.root.bind("<Return>", lambda e: self._on_settings_open())

    def _load_existing_config(self, dest: str) -> dict:
        """Try to read config from an existing workspace to pre-populate settings."""
        for folder in (DEFAULT_PLAYLIST_FOLDER, "Music", "Playlists"):
            config_path = Path(dest) / folder / ".echolist" / "config.json"
            if config_path.exists():
                try:
                    import json as _json
                    return _json.loads(config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return {}

    def _browse_var(self, var, title="Select folder"):
        d = filedialog.askdirectory(initialdir=var.get(), title=title)
        if d:
            var.set(d)

    def _on_settings_open(self):
        source = self._source_var.get().strip()
        dest = self._dest_var.get().strip()
        if not source or not dest:
            messagebox.showwarning("Missing paths",
                                    "Both source and destination are required.")
            return
        try:
            folder = validate_playlist_folder(
                dest, self._folder_var.get().strip() or DEFAULT_PLAYLIST_FOLDER)
        except ValueError as e:
            messagebox.showerror("Invalid playlist folder", str(e))
            return
        try:
            interval = max(1, int(self._backup_var.get().strip() or "5"))
        except ValueError:
            interval = 5

        if self.mgr:
            if self._busy():
                return
            new_root = (Path(dest) / folder).resolve()
            if new_root == Path(self.mgr.writer.root).resolve():
                # Same workspace: only its settings change.
                self.mgr.config.backup_interval = interval
                self.mgr.config.source_root = str(Path(source).resolve())
                try:
                    self.mgr.config.save(self.mgr.writer)
                except OSError as e:
                    messagebox.showerror("Could not save settings", str(e))
                    return
                self.source = source
                save_defaults(source, dest, folder)
                self._show_main()
            else:
                # A different workspace: open it first and keep this one's
                # lock (and config) untouched until that has succeeded.
                self._open_workspace(source, dest,
                                     playlist_folder=folder,
                                     backup_interval=interval)
        else:
            self._open_workspace(source, dest,
                                 playlist_folder=folder,
                                 backup_interval=interval)

    def _open_workspace(self, source: str, dest: str,
                        playlist_folder: str = DEFAULT_PLAYLIST_FOLDER,
                        backup_interval: int = 5):
        try:
            playlist_folder = validate_playlist_folder(dest, playlist_folder)
            playlists_dir = Path(dest) / playlist_folder
            config_file = playlists_dir / ".echolist" / "config.json"
            is_external = playlists_dir.exists() and not config_file.exists()
            is_empty = not config_file.exists()

            if config_file.exists():
                mgr = PlaylistManager.open(dest, playlist_folder=playlist_folder)
                resolved_source = str(Path(source).resolve())
                if mgr.config.source_root != resolved_source:
                    mgr.config.source_root = resolved_source
                    mgr.config.save(mgr.writer)
            else:
                snapshot = PlaylistManager.find_snapshot(dest, playlist_folder=playlist_folder)
                if snapshot and self._offer_snapshot_restore(snapshot, source, dest):
                    return
                mgr = PlaylistManager.init(
                    source, dest,
                    playlist_folder=playlist_folder,
                    backup_interval=backup_interval,
                )
            save_defaults(source, dest, playlist_folder)
        except WorkspaceLockError as e:
            messagebox.showerror("Workspace locked", str(e))
            return
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self._adopt_manager(mgr, source, dest)

        if is_external and is_empty:
            has_audio = any(
                f.suffix.lower() in AUDIO_EXTS and not f.name.startswith(".")
                for f in playlists_dir.rglob("*") if f.is_file()
            )
            if has_audio:
                messagebox.showwarning(
                    "Possible external playlist",
                    f"The folder:\n{playlists_dir}\n\n"
                    "contains audio files but was not created by EchoList. "
                    "You may have imported this playlist from an external source.\n\n"
                    "EchoList will need to modify metadata (album artist, album, "
                    "track number) on files in this folder to make playlists work "
                    "on the device.\n\n"
                    "Make sure this is not your only copy of these files.",
                )

        self._show_main()

    def _adopt_manager(self, mgr, source: str, dest: str) -> None:
        """Switch to an opened (and locked) workspace, then release the old
        one. The old lock is held until the new workspace is open, so a failed
        switch leaves the app on the workspace it still owns."""
        old = self.mgr
        self.mgr = mgr
        self.source = source
        self.dest = dest
        if old is not None and old is not mgr:
            old.release_lock()
        self.root.unbind("<Return>")
        self._bind_workspace_state(mgr)

    def _bind_workspace_state(self, mgr):
        """Load this workspace's staged changes and recover from a crash.

        Staging and the sync journal are per workspace (config.pending_file /
        journal_file), so another card's changes never apply here.
        """
        self._undo_stack.clear()
        path = pending_file(mgr.writer.root)
        try:
            _bind_legacy_staging(path, mgr.store)
        except StagingLoadError as e:
            messagebox.showwarning(
                "Old staged changes unreadable",
                f"{e}\n\nThe file was left where it is and not applied to "
                f"this workspace.")
        try:
            self.staging = StagingState(path)
        except StagingLoadError as e:
            aside = path.with_name(f"pending.unreadable-{datetime.now():%Y%m%d_%H%M%S}.json")
            path.rename(aside)
            self.staging = StagingState(path)
            messagebox.showwarning(
                "Staged changes unreadable",
                f"{e}\n\nThe file was kept as:\n{aside}\n\nStarting with no staged changes.")

        _sweep_copy_parts(mgr)
        jpath = journal_file(mgr.writer.root)
        journal = SyncJournal.load_incomplete(jpath)
        legacy = SyncJournal.load_incomplete()  # pre-0.3 global journal
        if legacy is not None:
            if {a["pid"] for a in legacy.actions} <= set(mgr.store.playlists):
                if journal is None:
                    # Move it to this workspace first, so whatever recovery
                    # records (or leaves unfinished) stays with this card.
                    journal = SyncJournal(legacy.actions, jpath)
                    journal.update(0)  # writes the journal to jpath
                SyncJournal.discard()
        n = len(journal.pending_actions) if journal is not None else 0
        notes, unresolved = recover_interrupted_sync(mgr, self.staging, journal)
        if unresolved:
            detail = "\n".join(f"- {x}" for x in notes[:15])
            messagebox.showwarning(
                "Interrupted sync not finished",
                f"A previous sync was interrupted and could not be completed "
                f"now. The record of it was kept; EchoList will try again "
                f"before the next sync and when this workspace is next "
                f"opened.\n\n{detail}".rstrip(),
            )
        elif journal is not None:
            SyncJournal.discard(jpath)
            detail = "\n".join(f"- {x}" for x in notes[:15])
            messagebox.showwarning(
                "Interrupted sync recovered",
                f"A previous sync was interrupted with {n} operation(s) "
                f"unfinished. The playlists were rescanned from the device "
                f"and staged changes that had already been applied were "
                f"cleared; the rest are still staged.\n\n{detail}".rstrip(),
            )
        elif notes:
            messagebox.showwarning("Recovered files", "\n".join(notes[:15]))

    def _offer_snapshot_restore(self, snapshot: dict, source: str, dest: str) -> bool:
        """Show restore dialog. Returns True if restore was accepted (and workspace opened)."""
        store_data = snapshot.get("store", {})
        playlists = store_data.get("playlists", {})
        if not playlists:
            return False

        snap_config = snapshot.get("config", {})
        snap_source = snap_config.get("source_root", source)

        snap_folder = snap_config.get("playlist_folder", DEFAULT_PLAYLIST_FOLDER)
        workspace = Path(dest) / snap_folder
        total_tracks = sum(len(pl.get("tracks", [])) for pl in playlists.values())
        pl_list = "\n".join(
            f"  - {workspace / pl['folder']}"
            for pl in list(playlists.values())[:8]
        )
        if len(playlists) > 8:
            pl_list += f"\n  ... and {len(playlists) - 8} more"

        result = messagebox.askyesno(
            "Previous playlists found",
            f"This device had EchoList playlists before.\n\n"
            f"Playlists will be restored to:\n{pl_list}\n\n"
            f"{total_tracks} track(s) total.\n\n"
            f"Source library: {snap_source}\n\n"
            f"Restore these playlists? The tracks will be staged as "
            f"pending — press Sync to re-copy them to the device.",
        )
        if not result:
            return False

        try:
            mgr = PlaylistManager.init(
                snap_source, dest,
                node_name=snap_config.get("node_name", "* PLAYLISTS *"),
                album_prefix=snap_config.get("album_prefix", ""),
                playlist_folder=snap_folder,
                backup_interval=snap_config.get("backup_interval", 5),
            )
            save_defaults(snap_source, dest, snap_folder)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return False

        self._adopt_manager(mgr, snap_source, dest)

        missing_sources = []
        source_root = Path(snap_source)
        for pid, pl in playlists.items():
            try:
                mgr.create_playlist(pl["name"])
            except ValueError:
                pass
            for t in pl.get("tracks", []):
                src_path = t.get("src_path", "")
                if not src_path:
                    continue
                full = _resolve_source_file(src_path, source_root)
                if full:
                    title, artist = _read_tags_from_file(full)
                    self.staging.stage_add(pid, str(full), title, artist)
                else:
                    missing_sources.append(src_path)

        self._show_main()

        if missing_sources:
            n = len(missing_sources)
            sample = "\n".join(f"  - {s}" for s in missing_sources[:10])
            if n > 10:
                sample += f"\n  ... and {n - 10} more"
            messagebox.showwarning(
                "Some source files missing",
                f"{n} track(s) could not be found in the source library "
                f"and were skipped:\n\n{sample}\n\n"
                f"The remaining tracks are staged and ready to sync.",
            )

        return True

    # ── Main ──

    def _show_main(self):
        for w in self.root.winfo_children():
            w.destroy()

        self._center_window(850, 700)
        self.root.minsize(750, 600)

        # Pack status bar FIRST (side=bottom) so it never gets hidden
        self._build_status_bar()

        # Main content above status
        main_pw = ttk.PanedWindow(self.root, orient="horizontal")
        main_pw.pack(fill="both", expand=True, padx=4, pady=(4, 0))

        # ── LEFT COLUMN ──
        left_col = ttk.Frame(main_pw)
        main_pw.add(left_col, weight=1)

        left_pw = ttk.PanedWindow(left_col, orient="vertical")
        left_pw.pack(fill="both", expand=True)

        # Source browser
        src_frame = ttk.Frame(left_pw)
        left_pw.add(src_frame, weight=3)

        src_header = ttk.Frame(src_frame)
        src_header.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(src_header, text="SOURCE", style="Section.TLabel").pack(side="left")
        ttk.Button(src_header, text="Browse...", command=self._add_files_dialog).pack(side="right")

        # Source search bar
        self._src_search_var = tk.StringVar()
        self._src_search_var.trace_add("write", lambda *_: self._on_source_search())
        src_search_entry = tk.Entry(
            src_frame, textvariable=self._src_search_var,
            bg=BG_INPUT, fg=FG, insertbackground=FG,
            relief="flat", font=("Consolas", 9))
        src_search_entry.pack(fill="x", padx=4, pady=(0, 2))
        self._src_search_entry = src_search_entry
        self._src_search_after_id = None
        self._src_search_entry.bind("<Escape>", lambda e: self._src_search_clear())
        self._src_search_entry.bind("<FocusIn>", self._src_search_focus_in)
        self._src_search_entry.bind("<FocusOut>", self._src_search_focus_out)
        self._src_search_placeholder = True
        self._src_search_show_placeholder()

        src_tree_frame = ttk.Frame(src_frame)
        src_tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 2))
        self.source_tree = ttk.Treeview(src_tree_frame, selectmode="extended")
        self.source_tree.heading("#0", text="", anchor="w")
        src_scroll = ttk.Scrollbar(src_tree_frame, orient="vertical", command=self.source_tree.yview)
        self.source_tree.configure(yscrollcommand=src_scroll.set)
        self.source_tree.pack(side="left", fill="both", expand=True)
        src_scroll.pack(side="right", fill="y")
        self.source_tree.bind("<<TreeviewOpen>>", self._on_source_expand)
        self.source_tree.bind("<Double-1>", self._on_source_double_click)

        # Drag-and-drop bindings (add="+" so default selection still works)
        self.source_tree.bind("<ButtonPress-1>", self._drag_start, add="+")
        self.source_tree.bind("<B1-Motion>", self._drag_motion, add="+")
        self.source_tree.bind("<ButtonRelease-1>", self._drag_drop, add="+")
        self._drag_indicator = None

        # Playlists
        pl_frame = ttk.Frame(left_pw)
        left_pw.add(pl_frame, weight=1)

        pl_header = ttk.Frame(pl_frame)
        pl_header.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(pl_header, text="PLAYLISTS", style="Section.TLabel").pack(side="left")
        ttk.Button(pl_header, text="+ New", command=self._create_playlist).pack(side="right", padx=(4, 0))
        ttk.Button(pl_header, text=".m3u", command=self._import_m3u_dialog).pack(side="right", padx=(4, 0))
        ttk.Button(pl_header, text="Delete", command=self._delete_playlist).pack(side="right")


        # TODO: star prefix feature disabled — re-enable when stable
        # self._star_var = tk.BooleanVar(value=self.mgr.config.star_prefix)
        # self._star_cb = tk.Checkbutton(pl_header, text="★", variable=self._star_var,
        #                                 command=self._toggle_star_prefix,
        #                                 bg=BG, fg=FG, selectcolor=BG_INPUT,
        #                                 activebackground=BG, activeforeground=FG,
        #                                 font=("Consolas", 11))
        # self._star_cb.pack(side="right", padx=(0, 4))

        pl_tree_frame = ttk.Frame(pl_frame)
        pl_tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.playlist_tree = ttk.Treeview(pl_tree_frame, columns=("status", "name"),
                                           show="tree", selectmode="browse")
        self.playlist_tree.column("#0", width=0, stretch=False)
        self.playlist_tree.column("status", width=20, minwidth=20, stretch=False, anchor="center")
        self.playlist_tree.column("name", width=180)
        self.playlist_tree.pack(side="left", fill="both", expand=True)
        self.playlist_tree.tag_configure("imported", foreground="#dd8833")
        self.playlist_tree.tag_configure("busy", foreground=FG_DIM)
        self.playlist_tree.tag_configure("offloaded", foreground=FG_DIM)
        self.playlist_tree.tag_configure("pending_pl", foreground=PENDING_FG)
        self.playlist_tree.tag_configure("highlight", foreground=YELLOW)
        self.playlist_tree.bind("<<TreeviewSelect>>", self._on_playlist_select)
        _bind_context_menu(self.playlist_tree, self._playlist_context_menu)

        self._rename_entry = None
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0
        self._spinner_after_id = None

        # ── RIGHT COLUMN: tracks ──
        right_col = ttk.Frame(main_pw)
        main_pw.add(right_col, weight=2)

        trk_header = ttk.Frame(right_col)
        trk_header.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(trk_header, text="TRACKS", style="Section.TLabel").pack(side="left")
        ttk.Button(trk_header, text="Remove", command=self._remove_track).pack(side="right")
        self.fix_meta_btn = ttk.Button(trk_header, text="Fix metadata",
                                        command=self._fix_metadata_clicked)
        self.undo_btn = ttk.Button(trk_header, text="Undo", command=self._do_undo)
        self.undo_btn.pack(side="right", padx=(0, 4))
        self.undo_lbl = tk.Label(trk_header, text="", font=("Consolas", 8),
                                  bg=BG, fg=FG_DIM, anchor="w")
        self.undo_lbl.pack(side="right", padx=(0, 4))

        trk_tree_frame = ttk.Frame(right_col)
        trk_tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 2))

        cols = ("index", "title", "artist")
        self.track_tree = ttk.Treeview(trk_tree_frame, columns=cols, show="headings",
                                        selectmode="extended")
        self.track_tree.heading("index", text="#", command=lambda: self._sort_tracks("index"))
        self.track_tree.heading("title", text="TITLE", command=lambda: self._sort_tracks("title"))
        self.track_tree.heading("artist", text="ARTIST", command=lambda: self._sort_tracks("artist"))
        self.track_tree.column("index", width=35, minwidth=35, stretch=False)
        self.track_tree.column("title", width=250)
        self.track_tree.column("artist", width=160)

        trk_scroll = ttk.Scrollbar(trk_tree_frame, orient="vertical", command=self.track_tree.yview)
        self.track_tree.configure(yscrollcommand=trk_scroll.set)
        self.track_tree.pack(side="left", fill="both", expand=True)
        trk_scroll.pack(side="right", fill="y")

        # Track tree tags for pending items
        self.track_tree.tag_configure("pending", foreground=PENDING_FG)
        self.track_tree.tag_configure("removed", foreground="#555555")
        self.track_tree.tag_configure("offloaded_track", foreground=FG_DIM)
        self.track_tree.tag_configure("loading", foreground=FG_DIM)

        # Right-click context menu on tracks
        _bind_context_menu(self.track_tree, self._track_context_menu)

        # Delete/Backspace on track tree removes selected tracks
        self.track_tree.bind("<Delete>", self._remove_track)
        self.track_tree.bind("<BackSpace>", self._remove_track)

        # Drag-to-reorder on track tree
        self._trk_drag_iid = None
        self.track_tree.bind("<ButtonPress-1>", self._trk_drag_start, add="+")
        self.track_tree.bind("<B1-Motion>", self._trk_drag_motion, add="+")
        self.track_tree.bind("<ButtonRelease-1>", self._trk_drag_end, add="+")

        self._build_menu()
        self._refresh_playlists()
        self._update_status()
        self._refresh_expensive_stats()
        self.root.bind("<Control-z>", lambda e: self._do_undo())
        if platform.system() == "Darwin":
            self.root.bind("<Command-z>", lambda e: self._do_undo())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        source_root = Path(self.mgr.config.source_root).resolve()
        if source_root.exists():
            self._populate_source_tree(source_root)

    def _build_status_bar(self):
        status_outer = tk.Frame(self.root, bg=BG_PANEL, bd=0)
        status_outer.pack(fill="x", side="bottom", padx=4, pady=4)

        inner = tk.Frame(status_outer, bg=BG_PANEL, padx=10, pady=8)
        inner.pack(fill="x")

        # Pending label
        pending_row = tk.Frame(inner, bg=BG_PANEL)
        pending_row.pack(fill="x", pady=(0, 4))
        self.pending_lbl = tk.Label(pending_row, text="", font=("Consolas", 9),
                                     bg=BG_PANEL, fg=PENDING_FG, anchor="center")
        self.pending_lbl.pack(fill="x")

        # SYNC row — button and progress bar share the same space
        self.sync_frame = tk.Frame(inner, bg=BG_PANEL)
        self.sync_frame.pack(fill="x", pady=(0, 8))

        self.sync_btn = ttk.Button(self.sync_frame, text="[  S Y N C  ]", style="Sync.TButton",
                                    command=self._do_sync)
        self.sync_btn.pack(anchor="center", ipadx=20, ipady=4)

        self.sync_progress = ttk.Progressbar(self.sync_frame, length=200, mode="determinate",
                                              style="Sync.Horizontal.TProgressbar")
        self.sync_status_lbl = tk.Label(self.sync_frame, text="", font=("Consolas", 9),
                                         bg=BG_PANEL, fg=FG_DIM, anchor="center")
        self.sync_file_bar = ttk.Progressbar(self.sync_frame, length=200, mode="determinate",
                                              style="Yellow.Horizontal.TProgressbar")

        # Stats
        row1 = tk.Frame(inner, bg=BG_PANEL)
        row1.pack(fill="x", pady=(0, 4))
        self.lbl_playlists = tk.Label(row1, text="PLAYLISTS: 0", font=("Consolas", 10, "bold"),
                                       bg=BG_PANEL, fg=FG, anchor="w")
        self.lbl_playlists.pack(side="left")
        self.lbl_workspace = tk.Label(row1, text="0 bytes", font=("Consolas", 9),
                                       bg=BG_PANEL, fg=FG_DIM, anchor="e")
        self.lbl_workspace.pack(side="right")

        row2 = tk.Frame(inner, bg=BG_PANEL)
        row2.pack(fill="x", pady=2)
        self.lbl_tracks = tk.Label(row2, text=f"TRACKS: 0 / {MAX_TRACKS}",
                                    font=("Consolas", 10, "bold"), bg=BG_PANEL, fg=FG, anchor="w")
        self.lbl_tracks.pack(side="left")
        self.track_pct_lbl = tk.Label(row2, text="0%", font=("Consolas", 9),
                                       bg=BG_PANEL, fg=FG_DIM, anchor="e")
        self.track_pct_lbl.pack(side="right")
        self.track_bar = ttk.Progressbar(inner, length=200, mode="determinate",
                                          style="Green.Horizontal.TProgressbar")
        self.track_bar.pack(fill="x", pady=(0, 6))

        row3 = tk.Frame(inner, bg=BG_PANEL)
        row3.pack(fill="x", pady=2)
        self.lbl_drive = tk.Label(row3, text="DRIVE: 0%", font=("Consolas", 10, "bold"),
                                   bg=BG_PANEL, fg=FG, anchor="w")
        self.lbl_drive.pack(side="left")
        self.drive_pct_lbl = tk.Label(row3, text="", font=("Consolas", 9),
                                       bg=BG_PANEL, fg=FG_DIM, anchor="e")
        self.drive_pct_lbl.pack(side="right")
        self.drive_bar = ttk.Progressbar(inner, length=200, mode="determinate",
                                          style="Green.Horizontal.TProgressbar")
        self.drive_bar.pack(fill="x")

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=BG_PANEL, fg=FG, activebackground=RED_DARK,
                          activeforeground=FG_BRIGHT, borderwidth=0)
        self.root.config(menu=menubar)
        file_menu = tk.Menu(menubar, tearoff=0, bg=BG_PANEL, fg=FG,
                            activebackground=RED_DARK, activeforeground=FG_BRIGHT)
        file_menu.add_command(label="Refresh", command=self._refresh_all, accelerator="F5")
        file_menu.add_separator()
        file_menu.add_command(label="Settings...", command=self._show_settings)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        self.root.bind("<F5>", lambda e: self._refresh_all())
        menubar.add_cascade(label="File", menu=file_menu)

        self._playlists_menu = tk.Menu(menubar, tearoff=0, bg=BG_PANEL, fg=FG,
                                        activebackground=RED_DARK, activeforeground=FG_BRIGHT)
        menubar.add_cascade(label="Playlists", menu=self._playlists_menu)
        self._refresh_playlists_menu()

    def _set_ui_locked(self, locked: bool):
        state = "disabled" if locked else "!disabled"
        for w in (self.undo_btn,):
            try:
                w.state([state])
            except Exception:
                pass
        if locked:
            self.playlist_tree.configure(selectmode="none")
            self.source_tree.configure(selectmode="none")
        else:
            self.playlist_tree.configure(selectmode="browse")
            self.source_tree.configure(selectmode="extended")

    def _refresh_playlists_menu(self):
        self._playlists_menu.delete(0, "end")
        if not self.mgr:
            return
        for pid, pl in self.mgr.store.playlists.items():
            sub = tk.Menu(self._playlists_menu, tearoff=0, bg=BG_PANEL, fg=FG,
                          activebackground=RED_DARK, activeforeground=FG_BRIGHT)

            sub.add_command(
                label="Create new restore point",
                command=lambda p=pid: self._create_restore_point(p),
            )
            sub.add_separator()

            backups = self.mgr.list_metadata_backups(pid)
            if backups:
                for b in backups:
                    ts = b["timestamp"]
                    label = _format_backup_timestamp(ts)
                    sub.add_command(
                        label=f"Restore: {label}",
                        command=lambda p=pid, t=ts: self._restore_from_point(p, t),
                    )
            else:
                sub.add_command(label="(no restore points)", state="disabled")

            self._playlists_menu.add_cascade(label=pl["name"], menu=sub)

        deleted = self.mgr.list_deleted_playlists()
        if deleted:
            self._playlists_menu.add_separator()
            for info in deleted:
                pid = info["pid"]
                name = info["name"]
                sub = tk.Menu(self._playlists_menu, tearoff=0, bg=BG_PANEL, fg=FG,
                              activebackground=RED_DARK, activeforeground=FG_BRIGHT)
                for b in info["backups"]:
                    ts = b["timestamp"]
                    label = _format_backup_timestamp(ts)
                    sub.add_command(
                        label=f"Restore: {label}",
                        command=lambda p=pid, t=ts: self._restore_deleted(p, t),
                    )
                sub.add_separator()
                sub.add_command(
                    label="Permanently delete",
                    command=lambda p=pid, n=name: self._permanently_delete_playlist(p, n),
                )
                self._playlists_menu.add_cascade(label=f"{name} (deleted)", menu=sub)

    def _permanently_delete_playlist(self, pid: str, name: str):
        if self._busy():
            return
        confirm = messagebox.askyesno(
            "Permanently delete",
            f"Permanently delete all restore points for '{name}'?\n\n"
            f"This cannot be undone.",
        )
        if not confirm:
            return
        from .config import delete_all_backups
        delete_all_backups(self.mgr.writer.root, pid)
        self.staging.drop_pid(pid)
        self._refresh_playlists_menu()

    def _create_restore_point(self, pid: str):
        if self._busy():
            return
        playlist = self.mgr.store.playlists.get(pid)
        if not playlist:
            return
        folder_path = self.mgr.writer.root / playlist["folder"]
        result = self.mgr.backup_playlist_metadata(pid)
        if result:
            self._refresh_playlists_menu()
            messagebox.showinfo(
                "Restore point created",
                f"Saved metadata snapshot for '{playlist['name']}'.\n\n"
                f"Tracks in:\n{folder_path}",
            )
        else:
            messagebox.showinfo(
                "No tracks",
                f"Playlist '{playlist['name']}' has no tracks to back up.",
            )

    def _restore_from_point(self, pid: str, timestamp: str):
        if self._busy():
            return
        playlist = self.mgr.store.playlists.get(pid)
        if not playlist:
            return
        label = _format_backup_timestamp(timestamp)
        confirm = messagebox.askyesno(
            "Restore to point",
            f"Restore playlist '{playlist['name']}' to '{label}'?\n\n"
            f"This will restore the playlist to exactly how it was at that point:\n"
            f"- Tracks added since then will be removed\n"
            f"- Tracks deleted since then will be re-staged for sync\n"
            f"- Metadata will be restored on remaining tracks\n\n"
            f"A restore point of the current state will be saved first.\n\n"
            f"Continue?",
        )
        if not confirm:
            return
        name = playlist["name"]

        def restore():
            return self.mgr.restore_playlist_to_point(pid, timestamp)

        def on_done(result):
            source_root = Path(self.mgr.config.source_root).resolve()
            missing = []
            for s in result["to_stage"]:
                src_path = s["src_path"]
                full = _resolve_source_file(src_path, source_root)
                if full:
                    title, artist = _read_tags_from_file(full)
                    self.staging.stage_add(pid, str(full), title, artist)
                else:
                    missing.append(src_path)

            self._invalidate_caches(pid)
            self._refresh_playlists()
            self._refresh_tracks()
            self._update_status()

            parts = []
            if result["restored"]:
                parts.append(f"Restored metadata on {result['restored']} track(s).")
            if result["removed"]:
                parts.append(f"Removed {result['removed']} track(s) added after this point.")
            if result["to_stage"]:
                staged = len(result["to_stage"]) - len(missing)
                if staged:
                    parts.append(f"Staged {staged} track(s) for re-syncing.")
            if missing:
                parts.append(f"{len(missing)} source file(s) could not be found.")
            messagebox.showinfo("Playlist restored", "\n".join(parts) if parts else "No changes needed.")

        self._run_playlist_op(pid, name, restore, on_done)

    def _restore_deleted(self, pid: str, timestamp: str):
        if self._busy():
            return
        label = _format_backup_timestamp(timestamp)
        from .config import load_backup
        data = load_backup(self.mgr.writer.root, pid, timestamp)
        if not data:
            messagebox.showerror("Error", "Could not load backup data.")
            return
        name = data.get("playlist_name", pid)
        tracks = data.get("tracks", [])
        src_count = sum(1 for t in tracks if t.get("src_path"))

        confirm = messagebox.askyesno(
            "Restore deleted playlist",
            f"Restore playlist '{name}' from '{label}'?\n\n"
            f"{src_count} track(s) will be staged for syncing.\n\n"
            f"Press Sync after restoring to copy the tracks back to the device.",
        )
        if not confirm:
            return

        self.playlist_tree.insert("", "end", iid=pid, values=("⟳", name))

        def restore():
            return self.mgr.restore_deleted_playlist(pid, timestamp)

        def on_done(sources):
            source_root = Path(self.mgr.config.source_root).resolve()
            missing = []
            for s in sources:
                src_path = s["src_path"]
                full = _resolve_source_file(src_path, source_root)
                if full:
                    title, artist = _read_tags_from_file(full)
                    self.staging.stage_add(pid, str(full), title, artist)
                else:
                    missing.append(src_path)

            self._invalidate_caches(pid)
            self._refresh_playlists()
            self._update_status()

            if missing:
                n = len(missing)
                sample = "\n".join(f"  - {s}" for s in missing[:10])
                if n > 10:
                    sample += f"\n  ... and {n - 10} more"
                messagebox.showwarning(
                    "Some source files missing",
                    f"{n} track(s) could not be found in the source library "
                    f"and were skipped:\n\n{sample}\n\n"
                    f"The remaining tracks are staged and ready to sync.",
                )

        self._run_playlist_op(pid, name, restore, on_done)

    # ── Source tree ──

    def _populate_source_tree(self, root_path: Path):
        self.source_tree.delete(*self.source_tree.get_children())
        self._insert_children("", root_path)

    def _insert_children(self, parent_iid: str, path: Path):
        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        workspace = self.mgr.writer.root if self.mgr else None
        for entry in entries:
            if _is_hidden_source_entry(entry.name):
                continue
            if workspace is not None and entry.is_dir() and entry.resolve() == workspace:
                continue
            if entry.is_file() and entry.suffix.lower() not in AUDIO_EXTS | M3U_EXTS:
                continue
            display = entry.name + ("/" if entry.is_dir() else "")
            iid = self.source_tree.insert(parent_iid, "end", text=display, values=(str(entry),))
            if entry.is_dir():
                self.source_tree.insert(iid, "end", text="...")

    def _on_source_expand(self, event):
        iid = self.source_tree.focus()
        children = self.source_tree.get_children(iid)
        if len(children) == 1 and self.source_tree.item(children[0], "text") == "...":
            self.source_tree.delete(children[0])
            path = Path(self.source_tree.item(iid, "values")[0])
            self._insert_children(iid, path)

    def _on_source_double_click(self, event):
        iid = self.source_tree.focus()
        if not iid:
            return
        values = self.source_tree.item(iid, "values")
        if not values:
            return
        path = Path(values[0])
        if path.is_file():
            if path.suffix.lower() in M3U_EXTS:
                self._import_m3u_file(path)
            else:
                self._stage_add_files([path])

    # ── Source search ──

    def _src_search_show_placeholder(self):
        self._src_search_placeholder = True
        self._src_search_entry.config(fg=FG_DIM)
        self._src_search_var.set("")
        self._src_search_entry.insert(0, "Search...")

    def _src_search_focus_in(self, event):
        if self._src_search_placeholder:
            self._src_search_entry.delete(0, "end")
            self._src_search_entry.config(fg=FG)
            self._src_search_placeholder = False

    def _src_search_focus_out(self, event):
        if not self._src_search_var.get().strip():
            self._src_search_show_placeholder()

    def _src_search_clear(self):
        self._src_search_entry.delete(0, "end")
        self._src_search_show_placeholder()
        self.root.focus_set()
        source_root = Path(self.mgr.config.source_root).resolve()
        if source_root.exists():
            self._populate_source_tree(source_root)

    def _on_source_search(self):
        if self._src_search_placeholder:
            return
        if self._src_search_after_id:
            self.root.after_cancel(self._src_search_after_id)
        self._src_search_after_id = self.root.after(150, self._do_source_search)

    def _do_source_search(self):
        self._src_search_after_id = None
        query = _nfc(self._src_search_var.get().strip()).casefold()
        if not query:
            source_root = Path(self.mgr.config.source_root).resolve()
            if source_root.exists():
                self._populate_source_tree(source_root)
            return
        source_root = Path(self.mgr.config.source_root).resolve()
        if not source_root.exists():
            return
        self.source_tree.delete(*self.source_tree.get_children())
        matches = []
        limit = 200
        for p in _walk_source_files(source_root, self.mgr.writer.root):
            if p.suffix.lower() in AUDIO_EXTS | M3U_EXTS:
                if query in _nfc(p.name).casefold():
                    matches.append(p)
                    if len(matches) >= limit:
                        break
        for p in matches:
            try:
                rel = p.relative_to(source_root)
                display = f"{p.name}  ({rel.parent})"
            except ValueError:
                display = p.name
            self.source_tree.insert("", "end", text=display, values=(str(p),))

    # ── Track context menu ──

    def _current_playlist_offloaded(self) -> bool:
        if not self.current_pid:
            return False
        pl = self.mgr.store.playlists.get(self.current_pid)
        return bool(pl and pl.get("offloaded"))

    def _show_popup_menu(self, menu, event):
        """Show a context menu and ensure any previous one is dismissed."""
        if hasattr(self, "_popup_menu") and self._popup_menu:
            try:
                self._popup_menu.unpost()
                self._popup_menu.destroy()
            except Exception:
                pass
        self._popup_menu = menu
        menu.tk_popup(event.x_root, event.y_root)

    def _track_context_menu(self, event):
        if self._current_playlist_offloaded():
            return
        iid = self.track_tree.identify_row(event.y)
        if not iid:
            return
        self.track_tree.selection_set(iid)
        idx = self.track_tree.index(iid)
        if idx >= len(self._track_data):
            return
        track = self._track_data[idx]
        src_path = track.get("src_path", "")

        menu = tk.Menu(self.root, tearoff=0, bg=BG_PANEL, fg=FG,
                       activebackground=RED_DARK, activeforeground=FG_BRIGHT)
        menu.add_command(label="Show in source",
                         command=lambda: self._show_track_in_source(src_path),
                         state="normal" if src_path else "disabled")
        menu.add_command(label="Show in playlists",
                         command=lambda: self._show_track_in_playlists(src_path),
                         state="normal" if src_path else "disabled")
        self._show_popup_menu(menu, event)

    def _show_track_in_source(self, src_path: str):
        if not src_path:
            return
        source_root = Path(self.mgr.config.source_root).resolve().resolve()
        full = _resolve_source_file(src_path, source_root)
        if not full:
            messagebox.showinfo("Not found", f"Source file not found:\n{src_path}")
            return
        # Clear any active search
        if not self._src_search_placeholder:
            self._src_search_clear()
        try:
            rel = full.relative_to(source_root)
        except ValueError:
            # File outside source root — insert its parent folder as a
            # browsable top-level entry, then select the file inside it
            parent_dir = full.parent
            display = str(parent_dir) + "/"
            folder_iid = self.source_tree.insert("", 0, text=display,
                                                  values=(str(parent_dir),))
            self._insert_children(folder_iid, parent_dir)
            self.source_tree.item(folder_iid, open=True)
            # Find and select the file
            for child in self.source_tree.get_children(folder_iid):
                if Path(self.source_tree.item(child, "values")[0]).name == full.name:
                    self.source_tree.selection_set(child)
                    self.source_tree.see(child)
                    self.source_tree.focus(child)
                    break
            return
        # Ensure the source tree is populated
        if not self.source_tree.get_children():
            if source_root.exists():
                self._populate_source_tree(source_root)
        parts = list(rel.parts)
        parent_iid = ""
        for i, part in enumerate(parts):
            children = self.source_tree.get_children(parent_iid)
            # Expand lazy placeholder if present
            if len(children) == 1 and self.source_tree.item(children[0], "text") == "...":
                self.source_tree.delete(children[0])
                parent_path = source_root / Path(*parts[:i]) if i > 0 else source_root
                self._insert_children(parent_iid, parent_path)
                children = self.source_tree.get_children(parent_iid)
            # Search for this path component among children
            found = None
            for child in children:
                child_path = Path(self.source_tree.item(child, "values")[0])
                if child_path.name == part:
                    found = child
                    break
            if not found:
                # Node not in tree yet — load it from disk
                node_path = source_root / Path(*parts[:i + 1])
                if not node_path.exists():
                    return
                is_dir = node_path.is_dir()
                display = part + ("/" if is_dir else "")
                found = self.source_tree.insert(parent_iid, "end", text=display,
                                                values=(str(node_path),))
                if is_dir:
                    self._insert_children(found, node_path)
            elif i < len(parts) - 1:
                self.source_tree.item(found, open=True)
                sub_children = self.source_tree.get_children(found)
                if len(sub_children) == 1 and self.source_tree.item(sub_children[0], "text") == "...":
                    self.source_tree.delete(sub_children[0])
                    child_path = Path(self.source_tree.item(found, "values")[0])
                    self._insert_children(found, child_path)
            parent_iid = found
        self.source_tree.selection_set(found)
        self.source_tree.see(found)
        self.source_tree.focus(found)

    def _show_track_in_playlists(self, src_path: str):
        if not src_path:
            return
        matching_pids = []
        for pid, pl in self.mgr.store.playlists.items():
            for t in pl["tracks"]:
                if t.get("src_path") == src_path:
                    matching_pids.append(pid)
                    break
        if not matching_pids:
            messagebox.showinfo("Not found", "This track is not in any other playlist.")
            return
        for pid in matching_pids:
            try:
                self.playlist_tree.item(pid, tags=("highlight",))
            except Exception:
                pass
        self.root.after(2000, lambda: self._clear_playlist_highlight(matching_pids))

    def _clear_playlist_highlight(self, pids: list[str]):
        for pid in pids:
            try:
                pl = self.mgr.store.playlists.get(pid)
                if pl:
                    tags = ("offloaded",) if pl.get("offloaded") else ()
                    self.playlist_tree.item(pid, tags=tags)
            except Exception:
                pass

    # ── Playlist context menu (offload/onload) ──

    def _playlist_context_menu(self, event):
        if self._busy():
            return
        iid = self.playlist_tree.identify_row(event.y)
        if not iid or iid.startswith("_imported:"):
            return
        self.playlist_tree.selection_set(iid)
        pl = self.mgr.store.playlists.get(iid)
        if not pl:
            return

        is_offloaded = pl.get("offloaded", False)
        has_pending = self._playlist_has_pending(iid)
        has_orphans = any(not t.get("src_path") for t in pl.get("tracks", []))

        menu = tk.Menu(self.root, tearoff=0, bg=BG_PANEL, fg=FG,
                       activebackground=RED_DARK, activeforeground=FG_BRIGHT)
        if is_offloaded:
            menu.add_command(label="Onload (restore to device)",
                            command=lambda: self._onload_playlist(iid))
        else:
            menu.add_command(label="Reindex",
                            command=lambda: self._reindex_playlist(iid),
                            state="normal" if pl.get("tracks") and not is_offloaded else "disabled")
            can_offload = not has_pending and not has_orphans
            offload_label = ("Offload (has tracks without source)"
                             if has_orphans else "Offload (remove from device)")
            menu.add_command(label=offload_label,
                            command=lambda: self._offload_playlist(iid),
                            state="normal" if can_offload else "disabled")
        menu.add_separator()
        menu.add_command(label="Rename", command=lambda: self._start_inline_rename(iid))
        menu.add_command(label="Delete", command=self._delete_playlist)
        self._show_popup_menu(menu, event)

    def _reindex_playlist(self, pid: str):
        if self._busy():
            return
        pl = self.mgr.store.playlists.get(pid)
        if not pl or not pl.get("tracks"):
            return
        order = [{"key": f"c:{t['index']}"} for t in pl["tracks"]]
        self._push_undo(f"Reindex {pid}")
        self.staging.set_reorder(pid, order)
        self._refresh_playlists()
        self._refresh_tracks()
        self._update_status()

    def _offload_playlist(self, pid: str):
        if self._busy():
            return
        pl = self.mgr.store.playlists.get(pid)
        if not pl:
            return
        if any(not t.get("src_path") for t in pl.get("tracks", [])):
            return
        if self._playlist_has_pending(pid):
            messagebox.showinfo(
                "Pending changes",
                f"'{pl['name']}' has staged changes. Sync or undo them "
                f"before offloading the playlist.")
            return
        name = pl["name"]
        confirm = messagebox.askyesno(
            "Offload playlist",
            f"Offload '{name}'?\n\n"
            f"This will remove {len(pl['tracks'])} track(s) from the device "
            f"to free up space. A restore point is saved automatically.\n\n"
            f"You can onload it back later to re-sync the tracks.",
        )
        if not confirm:
            return

        def offload():
            # A missing restore point would make the offload unrecoverable,
            # so a backup failure aborts before anything is deleted.
            backup = self.mgr.backup_playlist_metadata(pid)
            self.mgr.writer.delete(pl["folder"])
            return backup

        def on_done(backup):
            saved = {k: pl[k] for k in ("tracks", "offloaded", "offload_backup")
                     if k in pl}
            pl["tracks"] = []
            pl["offloaded"] = True
            if backup is not None:
                pl["offload_backup"] = Path(backup).stem
            try:
                self.mgr.store.save()
            except OSError as e:
                pl.pop("offload_backup", None)
                pl.update(saved)
                messagebox.showerror(
                    "Offload incomplete",
                    f"The tracks were removed from the device, but the "
                    f"playlist list could not be saved:\n{e}")
            self._invalidate_caches(pid)
            self._refresh_playlists()
            self._refresh_tracks()
            self._update_status()

        self._run_playlist_op(pid, name, offload, on_done)

    def _offload_backup_timestamp(self, pid: str) -> str | None:
        """The restore point taken when ``pid`` was offloaded. Offloading
        records it (backup_playlist_metadata can dedupe to an *older* point);
        older stores without the record fall back to the newest backup."""
        from .config import list_backups
        backups = list_backups(self.mgr.writer.root, pid)
        recorded = self.mgr.store.playlists.get(pid, {}).get("offload_backup")
        if recorded and any(b["timestamp"] == recorded for b in backups):
            return recorded
        return backups[0]["timestamp"] if backups else None

    def _onload_playlist(self, pid: str):
        if self._busy():
            return
        pl = self.mgr.store.playlists.get(pid)
        if not pl:
            return
        from .config import load_backup
        timestamp = self._offload_backup_timestamp(pid)
        if timestamp is None:
            messagebox.showerror("No backup", "No restore point found for this playlist.")
            return

        name = pl["name"]

        def onload():
            data = load_backup(self.mgr.writer.root, pid, timestamp)
            if not data:
                raise ValueError("Could not load backup data")
            return data.get("tracks", [])

        def on_done(tracks):
            # Recreate the folder so sync can write into it
            try:
                self.mgr.writer.makedirs(pl["folder"])
            except OSError as e:
                messagebox.showerror("Onload failed",
                                     f"Could not create the playlist folder:\n{e}")
                return
            saved = {k: pl[k] for k in ("tracks", "offloaded", "offload_backup")
                     if k in pl}
            pl["offloaded"] = False
            pl["tracks"] = []
            pl.pop("offload_backup", None)
            try:
                self.mgr.store.save()
            except OSError as e:
                pl.update(saved)
                messagebox.showerror("Onload failed",
                                     f"Could not save the playlist list:\n{e}")
                return

            # Stage all tracks for re-copying from source
            source_root = Path(self.mgr.config.source_root).resolve()
            staged = 0
            missing = []
            for entry in tracks:
                src_path = entry.get("src_path", "")
                if not src_path:
                    missing.append(entry.get("copy_name", "(unknown)"))
                    continue
                full = _resolve_source_file(src_path, source_root)
                if full:
                    title, artist = _read_tags_from_file(full)
                    self.staging.stage_add(pid, str(full), title, artist)
                    staged += 1
                else:
                    missing.append(src_path)

            self._invalidate_caches(pid)
            self._refresh_playlists()
            self._refresh_tracks()
            self._update_status()
            parts = []
            if staged:
                parts.append(f"Staged {staged} track(s) for syncing.")
            if missing:
                parts.append(f"{len(missing)} source file(s) not found.")
            parts.append("Press Sync to copy tracks back to the device.")
            messagebox.showinfo("Playlist onloaded", "\n".join(parts))

        self._run_playlist_op(pid, name, onload, on_done)

    # ── Drag and drop ──

    def _drag_start(self, event):
        iid = self.source_tree.identify_row(event.y)
        if not iid:
            self._drag_data = None
            return
        # Capture selection now, before the default handler clears it to one item
        saved = self.source_tree.selection()
        if iid not in saved:
            # Clicking a new item — drag just that one, not the old selection
            saved = (iid,)
        self._drag_data = {
            "x": event.x_root, "y": event.y_root, "started": False,
            "selection": saved,
        }
        # Restore multi-selection after the default handler deselects
        if len(saved) > 1:
            self.root.after_idle(lambda: self.source_tree.selection_set(saved))

    def _drag_motion(self, event):
        if not self._drag_data:
            return
        dx = abs(event.x_root - self._drag_data["x"])
        dy = abs(event.y_root - self._drag_data["y"])
        if not self._drag_data["started"] and (dx > 8 or dy > 8):
            self._drag_data["started"] = True
            selected = self._drag_data.get("selection", ())
            if not selected:
                self._drag_data = None
                return
            n = len(selected)
            if self._drag_indicator:
                self._drag_indicator.destroy()
            self._drag_indicator = tk.Label(self.root, text=f"+ {n} file{'s' if n != 1 else ''}",
                                             bg=RED_DARK, fg=FG_BRIGHT,
                                             font=("Consolas", 8), padx=4, pady=2)

        if self._drag_data and self._drag_data.get("started") and self._drag_indicator:
            rx = event.x_root - self.root.winfo_rootx()
            ry = event.y_root - self.root.winfo_rooty()
            self._drag_indicator.place(x=rx + 14, y=ry + 14)
            self._drag_indicator.lift()

    def _drag_drop(self, event):
        was_dragging = self._drag_data and self._drag_data.get("started")
        saved_selection = self._drag_data.get("selection", ()) if self._drag_data else ()

        if self._drag_indicator:
            self._drag_indicator.destroy()
            self._drag_indicator = None
        self._drag_data = None

        if not was_dragging:
            return

        # Check if dropped over the right column (tracks area or its parent)
        rx = event.x_root
        ry = event.y_root
        try:
            tx = self.track_tree.winfo_rootx()
            ty = self.track_tree.winfo_rooty()
            tw = self.track_tree.winfo_width()
            th = self.track_tree.winfo_height()
            # generous drop zone: anywhere on the right half of the window
            win_mid = self.root.winfo_rootx() + self.root.winfo_width() // 2
            if rx >= win_mid or (tx <= rx <= tx + tw and ty <= ry <= ty + th):
                self._add_selected_from_source(saved_selection)
        except Exception:
            pass

    def _add_selected_from_source(self, selected=None):
        if self._busy():
            return
        if selected is None:
            selected = self.source_tree.selection()
        if not selected:
            return

        m3u_files = []
        audio_paths = []
        for iid in selected:
            values = self.source_tree.item(iid, "values")
            if values:
                p = Path(values[0])
                if p.is_file():
                    if p.suffix.lower() in M3U_EXTS:
                        m3u_files.append(p)
                    else:
                        audio_paths.append(p)
                elif p.is_dir():
                    audio_paths.extend(
                        f for f in _walk_source_files(p, self.mgr.writer.root)
                        if f.suffix.lower() in AUDIO_EXTS
                    )

        for m3u in m3u_files:
            self._import_m3u_file(m3u)

        if audio_paths:
            if not self.current_pid:
                messagebox.showinfo("No playlist", "Select or create a playlist first.")
                return
            self._stage_add_files(audio_paths)

    # ── Staging operations ──

    def _stage_add_files(self, paths: list[Path]):
        if self._busy() or self._current_playlist_offloaded():
            return
        if not self.current_pid:
            messagebox.showinfo("No playlist", "Select or create a playlist first.")
            return

        # Collect existing source paths for duplicate check
        existing = set()
        source_root = Path(self.mgr.config.source_root).resolve()
        playlist = self.mgr.store.playlists.get(self.current_pid, {})
        for t in playlist.get("tracks", []):
            sp = Path(t.get("src_path", ""))
            if not sp.is_absolute():
                sp = source_root / sp
            existing.add(str(sp.resolve()))
        for a in self.staging.pending_adds:
            if a["pid"] == self.current_pid:
                existing.add(str(Path(a["src"]).resolve()))

        before = self.staging.snapshot()
        added_indices = []
        skipped = []
        for p in paths:
            resolved = str(p.resolve())
            if resolved in existing:
                skipped.append(p.name)
                continue
            existing.add(resolved)
            title, artist = _read_tags_from_file(p)
            self.staging.stage_add(self.current_pid, str(p), title, artist)
            added_indices.append(len(self.staging.pending_adds) - 1)

        if added_indices:
            n = len(added_indices)
            self._push_undo(f"Add {n} track{'s' if n != 1 else ''} to {self.current_pid}",
                            before)
        if skipped:
            messagebox.showinfo("Duplicates skipped",
                                f"{len(skipped)} track(s) already in playlist:\n" +
                                "\n".join(skipped[:10]))
        self._refresh_tracks()
        self._update_status()

    def _stage_remove_track(self, pid: str, index: int, copy_name: str):
        if self._busy():
            return
        before = self.staging.snapshot()
        track = next((t for t in self.mgr.store.playlists.get(pid, {}).get("tracks", [])
                      if t["index"] == index), {})
        self.staging.stage_remove(pid, index, copy_name, track.get("src_path", ""))
        self._push_undo(f"Remove #{index} from {pid}", before)
        self._refresh_tracks()
        self._update_status()

    def _push_undo(self, desc: str, before: dict | None = None):
        """Record the staging state to return to. Call with a snapshot taken
        *before* the change (or none: snapshot now, before changing)."""
        self._undo_stack.append({
            "desc": desc,
            "staging": before if before is not None else self.staging.snapshot(),
        })

    def _do_undo(self):
        if not self._undo_stack or self._busy():
            return
        action = self._undo_stack.pop()
        self.staging.restore(action["staging"])
        self._refresh_playlists()
        self._refresh_tracks()
        self._update_status()

    def _on_close(self):
        if self._busy():
            # Keep the workspace lock until the worker writing the card ends;
            # the sync / playlist-op finish handler shuts down once idle.
            self._exit_after_sync = True
            messagebox.showinfo("Operation in progress",
                                "EchoList will close when the current "
                                "operation finishes.")
            return
        if self.staging and self.staging.has_pending:
            n = self.staging.total_ops
            result = messagebox.askyesnocancel(
                "Unsaved changes",
                f"You have {n} pending change{'s' if n != 1 else ''}.\n\n"
                "Yes = Sync now, then exit\n"
                "No = Exit (changes are saved, sync next time)\n"
                "Cancel = Stay",
            )
            if result is None:
                return
            if result:
                errors = self._do_sync_blocking()
                if errors:
                    self._refresh_playlists()
                    self._update_status()
                    self._show_sync_errors(errors)
                    return
        self._shutdown()

    def _shutdown(self):
        self._alive = False
        if self.mgr:
            self.mgr.release_lock()
        t = getattr(self, "_stats_thread", None)
        if t:
            t.join(timeout=5)
        self.root.destroy()

    def _on_interrupt(self):
        """Ctrl+C in the terminal: exit cleanly, after running operations."""
        if self._busy():
            self._exit_after_sync = True
            return
        self._shutdown()

    def _show_sync_errors(self, errors: list[str]):
        more = f"\n... and {len(errors) - 15} more" if len(errors) > 15 else ""
        messagebox.showwarning(
            "Sync errors",
            "Some changes could not be synced and are still staged:\n\n"
            + "\n".join(errors[:15]) + more)

    def _backup_before_sync(self):
        """Create a restore point for affected playlists, respecting backup_interval."""
        if not self.mgr.config.should_backup():
            self.mgr.config.increment_sync(self.mgr.writer)
            return
        affected = set()
        for a in self.staging.pending_adds:
            affected.add(a["pid"])
        for r in self.staging.pending_removes:
            affected.add(r["pid"])
        for pid in self.staging.pending_reorders:
            affected.add(pid)
        for pid in affected:
            if pid in self.mgr.store.playlists:
                self.mgr.backup_playlist_metadata(pid)
        self.mgr.config.increment_sync(self.mgr.writer)

    def _journal_path(self) -> Path:
        return journal_file(self.mgr.writer.root)

    def _after_sync(self):
        """Bookkeeping shared by both sync paths, on the main thread."""
        # Undo snapshots describe the pre-sync playlists; restoring one now
        # would re-stage changes that already ran.
        self._undo_stack.clear()
        self._invalidate_caches()
        try:
            self.mgr.save_snapshot()
        except OSError:
            pass  # the snapshot is a convenience backup; the sync itself is done

    def _do_sync_blocking(self) -> list[str]:
        """Synchronous sync for use during close. Returns the errors."""
        try:
            self._backup_before_sync()
        except OSError as e:
            return [f"Sync not started: could not save the restore point "
                    f"or sync counter: {e}"]
        errors, _ = run_sync(self.mgr, self.staging, self._journal_path())
        self._after_sync()
        return errors

    # ── SYNC ──

    def _do_sync(self):
        if not self.staging.has_pending or self._busy():
            return
        try:
            self._backup_before_sync()
        except OSError as e:
            messagebox.showerror(
                "Sync not started",
                f"Could not save the restore point or sync counter:\n{e}\n\n"
                f"Nothing was changed on the device; your changes are still "
                f"staged.")
            return

        self._syncing = True
        self._set_ui_locked(True)
        total = self.staging.total_ops

        # Swap button for progress bars
        self.sync_btn.pack_forget()
        self.sync_status_lbl.pack(fill="x")
        self.sync_progress.pack(fill="x", pady=(2, 0))
        self.sync_file_bar.pack(fill="x", pady=(2, 0))
        self.sync_progress["maximum"] = total
        self.sync_progress["value"] = 0
        self.sync_file_bar["maximum"] = 100
        self.sync_file_bar["value"] = 0

        def _ui(fn):
            self._schedule_callback(fn)

        def on_progress(done, label):
            _ui(lambda: _update_progress(done, label))
            _ui(lambda: _update_file_progress(0))

        def on_copy_progress(copied, file_total):
            pct = int(copied / file_total * 100) if file_total else 100
            _ui(lambda: _update_file_progress(pct))

        def worker():
            try:
                errors, _ = run_sync(self.mgr, self.staging, self._journal_path(),
                                     progress=on_progress, file_progress=on_copy_progress)
            except Exception as e:  # unexpected: report it; unfinished ops stay staged
                errors = [f"Sync stopped: {e}"]
            self._schedule_callback(lambda: _finish(errors))

        def _update_progress(done, label=""):
            self.sync_progress["value"] = done
            self.sync_status_lbl.config(text=f"Syncing {done}/{total}  {label}")

        def _update_file_progress(pct):
            self.sync_file_bar["value"] = pct

        def _finish(errors):
            self._syncing = False
            self._set_ui_locked(False)
            self._after_sync()
            self.sync_progress.pack_forget()
            self.sync_status_lbl.pack_forget()
            self.sync_file_bar.pack_forget()
            self.sync_btn.pack(anchor="center", ipadx=20, ipady=4)
            self._refresh_playlists()
            self._update_status()
            self._refresh_expensive_stats()
            if errors:
                self._show_sync_errors(errors)
            else:
                self.pending_lbl.config(text="  ✓ Sync complete", fg=GREEN)
                self.root.after(4000, lambda: self.pending_lbl.config(text="", fg=PENDING_FG))
            if self._exit_after_sync and not self._busy():
                self._shutdown()

        self._spawn(worker)

    # ── Playlists ──

    def _refresh_all(self):
        """Reload source tree, playlists, tracks, and stats."""
        if self._busy():
            return
        source_root = Path(self.mgr.config.source_root).resolve()
        if source_root.exists():
            self._populate_source_tree(source_root)
        if self._can_rescan():
            for pid in list(self.mgr.store.playlists):
                self.mgr.rescan_playlist(pid)
        self._refresh_playlists()
        self._refresh_tracks()
        self._update_status()
        self._refresh_expensive_stats()

    def _playlist_status_icon(self, pid: str, pl: dict) -> str:
        if pl.get("offloaded"):
            return "◌"
        return "⏏"

    def _playlist_has_pending(self, pid: str) -> bool:
        return any(
            a["pid"] == pid for a in self.staging.pending_adds
        ) or any(
            r["pid"] == pid for r in self.staging.pending_removes
        ) or pid in self.staging.pending_reorders

    def _refresh_playlists(self):
        self.playlist_tree.delete(*self.playlist_tree.get_children())
        for pid, pl in self.mgr.store.playlists.items():
            icon = self._playlist_status_icon(pid, pl)
            if pl.get("offloaded"):
                tags = ("offloaded",)
            elif self._playlist_has_pending(pid):
                tags = ("pending_pl",)
            else:
                tags = ()
            self.playlist_tree.insert("", "end", iid=pid, values=(icon, pl["name"]),
                                      tags=tags)
        self._untracked_playlists = {}
        try:
            for info in self.mgr.detect_untracked_playlists():
                iid = f"_imported:{info['folder']}"
                self.playlist_tree.insert(
                    "", "end", iid=iid,
                    values=("", f"{info['folder']} ({info['track_count']} tracks)"),
                    tags=("imported",),
                )
                self._untracked_playlists[iid] = info
        except Exception:
            pass
        children = self.playlist_tree.get_children()
        if children:
            if self.current_pid and self.current_pid in children:
                self.playlist_tree.selection_set(self.current_pid)
            else:
                self.playlist_tree.selection_set(children[0])
            self._on_playlist_select(None)
        if hasattr(self, "_playlists_menu"):
            self._refresh_playlists_menu()

    def _run_playlist_op(self, pid: str, display_name: str, operation, on_done=None):
        """Run a blocking playlist operation in a background thread with spinner.

        ``operation`` runs on the worker and should only do file I/O; changes
        to the Store (and store.save()) belong in ``on_done``, which runs on
        the Tk thread. Every other writer is refused (``_busy()``) meanwhile.
        """
        self._busy_playlists.add(pid)
        self._set_ui_locked(True)
        try:
            self.playlist_tree.item(pid, values=("⟳", f"{display_name} {self._spinner_frames[0]}"),
                                    tags=("busy",))
        except Exception:
            pass
        self._start_spinner()

        def worker():
            error = None
            result = None
            try:
                result = operation()
            except Exception as e:
                error = e
            self._schedule_callback(lambda: finish(result, error))

        def finish(result, error):
            self._busy_playlists.discard(pid)
            if not self._busy_playlists:
                self._stop_spinner()
            try:
                if pid in [c for c in self.playlist_tree.get_children()]:
                    pl = self.mgr.store.playlists.get(pid)
                    name = pl["name"] if pl else display_name
                    icon = self._playlist_status_icon(pid, pl) if pl else "⏏"
                    tags = ()
                    if pl and pl.get("offloaded"):
                        tags = ("offloaded",)
                    elif pl and self._playlist_has_pending(pid):
                        tags = ("pending_pl",)
                    self.playlist_tree.item(pid, values=(icon, name), tags=tags)
            except Exception:
                pass
            try:
                if error:
                    messagebox.showerror("Error", str(error))
                elif on_done:
                    on_done(result)
            finally:
                if not self._busy():
                    self._set_ui_locked(False)
                    if self._exit_after_sync:
                        self._shutdown()

        self._spawn(worker)

    def _start_spinner(self):
        if self._spinner_after_id is not None:
            return
        self._tick_spinner()

    def _stop_spinner(self):
        if self._spinner_after_id is not None:
            self.root.after_cancel(self._spinner_after_id)
            self._spinner_after_id = None

    def _tick_spinner(self):
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
        frame = self._spinner_frames[self._spinner_idx]
        for pid in list(self._busy_playlists):
            try:
                old = self.playlist_tree.item(pid, "values")
                if old and len(old) >= 2:
                    base = old[1].rsplit(" ", 1)[0]
                    self.playlist_tree.item(pid, values=("⟳", f"{base} {frame}"))
            except Exception:
                pass
        if self._busy_playlists:
            self._spinner_after_id = self.root.after(80, self._tick_spinner)
        else:
            self._spinner_after_id = None

    def _on_playlist_select(self, event):
        sel = self.playlist_tree.selection()
        if not sel:
            self.current_pid = None
            self.track_tree.delete(*self.track_tree.get_children())
            return
        selected = sel[0]
        if selected in self._busy_playlists:
            self.playlist_tree.selection_remove(selected)
            if self.current_pid and self.current_pid not in self._busy_playlists:
                try:
                    self.playlist_tree.selection_set(self.current_pid)
                except Exception:
                    pass
            return
        if selected.startswith("_imported:"):
            self.current_pid = None
            self.track_tree.delete(*self.track_tree.get_children())
            self.fix_meta_btn.pack_forget()
            info = self._untracked_playlists.get(selected)
            if info:
                self._offer_adopt_playlist(info)
            return
        self.current_pid = selected
        pl = self.mgr.store.playlists.get(selected)
        if pl and pl.get("offloaded"):
            self._show_offloaded_tracks(selected)
            self.fix_meta_btn.pack_forget()
            return
        self._refresh_tracks()
        self._check_metadata_sync()

    def _check_metadata_sync(self):
        if not self.current_pid:
            self.fix_meta_btn.pack_forget()
            return

        pid = self.current_pid
        cached = self._audit_cache.get(pid)
        if cached is not None:
            self._apply_audit_result(pid, cached)
            return

        def _audit():
            issues = self.mgr.audit_playlist_metadata(pid)
            self._audit_cache[pid] = issues
            if not self._alive:
                return
            self._schedule_callback(lambda: self._apply_audit_result(pid, issues))

        self._spawn(_audit)

    def _apply_audit_result(self, pid: str, issues: list[dict]):
        if self.current_pid != pid:
            return
        if issues:
            n = len(set(i["copy_name"] for i in issues))
            self.fix_meta_btn.config(text=f"Fix metadata ({n})")
            self.fix_meta_btn.pack(side="right", padx=(0, 4))
            self._set_fix_meta_tooltip(issues)
        else:
            self.fix_meta_btn.pack_forget()
            self._clear_fix_meta_tooltip()

    def _set_fix_meta_tooltip(self, issues: list[dict]):
        by_file: dict[str, list[str]] = {}
        for i in issues:
            by_file.setdefault(i["copy_name"], []).append(
                f"{i['field']}: '{i['actual']}' → '{i['expected']}'"
            )
        lines = []
        for name, fixes in list(by_file.items())[:8]:
            lines.append(name)
            for f in fixes:
                lines.append(f"  {f}")
        if len(by_file) > 8:
            lines.append(f"... and {len(by_file) - 8} more file(s)")
        tip_text = "\n".join(lines)

        tip_win = [None]

        def show(event):
            tw = tk.Toplevel(self.root)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
            lbl = tk.Label(tw, text=tip_text, justify="left",
                           bg="#333333", fg="#eeeeee", font=("Consolas", 9),
                           padx=6, pady=4, wraplength=500)
            lbl.pack()
            tip_win[0] = tw

        def hide(event):
            if tip_win[0]:
                tip_win[0].destroy()
                tip_win[0] = None

        self.fix_meta_btn.bind("<Enter>", show)
        self.fix_meta_btn.bind("<Leave>", hide)

    def _clear_fix_meta_tooltip(self):
        self.fix_meta_btn.unbind("<Enter>")
        self.fix_meta_btn.unbind("<Leave>")

    def _fix_metadata_clicked(self):
        if self._busy():
            return
        if not self.current_pid:
            return
        issues = self.mgr.audit_playlist_metadata(self.current_pid)
        if not issues:
            self.fix_meta_btn.pack_forget()
            return

        affected_files = sorted(set(i["copy_name"] for i in issues))
        playlist = self.mgr.store.playlists[self.current_pid]
        folder_path = self.mgr.writer.root / playlist["folder"]

        file_list = "\n".join(f"  - {f}" for f in affected_files[:10])
        if len(affected_files) > 10:
            file_list += f"\n  ... and {len(affected_files) - 10} more"

        result = messagebox.askyesno(
            "Metadata out of sync",
            f"{len(affected_files)} track(s) in this playlist have metadata that "
            f"doesn't match what EchoList expects.\n\n"
            f"Folder:\n{folder_path}\n\n"
            f"Affected files:\n{file_list}\n\n"
            f"EchoList needs to update the album artist, album name, and track "
            f"numbers on these files so they appear correctly on the device.\n\n"
            f"A backup of the original metadata will be saved so you can "
            f"restore it later (Playlists menu > playlist name > Restore).\n\n"
            f"Make sure this is not your only copy of these files.\n\n"
            f"Update metadata now?",
        )
        if result:
            pid = self.current_pid
            pl = self.mgr.store.playlists[pid]

            def fix():
                return self.mgr.fix_playlist_metadata(pid)

            def on_done(fixed):
                self._invalidate_caches(pid)
                self._refresh_playlists_menu()
                self.fix_meta_btn.pack_forget()
                self._refresh_tracks()
                if fixed:
                    messagebox.showinfo(
                        "Metadata updated",
                        f"Updated metadata on {fixed} track(s).\n\n"
                        f"A restore point has been saved. Use the Playlists menu "
                        f"to restore if needed.",
                    )

            self._run_playlist_op(pid, pl["name"], fix, on_done)

    def _offer_adopt_playlist(self, info: dict):
        if self._busy():
            return
        folder = info["folder"]
        count = info["track_count"]
        folder_path = self.mgr.writer.root / folder
        result = messagebox.askyesno(
            "Imported playlist detected",
            f"The folder '{folder}' contains {count} audio file(s) but wasn't "
            f"created by EchoList.\n\n"
            f"Location:\n{folder_path}\n\n"
            f"To make this playlist work on the device, EchoList needs to "
            f"modify the metadata (album artist, album, track numbers) on "
            f"these files.\n\n"
            f"A restore point will be saved with the original metadata "
            f"so you can undo this later.\n\n"
            f"Adopt this playlist?",
        )
        if not result:
            return

        pid = playlist_id(folder)
        iid = f"_imported:{folder}"
        try:
            self.playlist_tree.delete(iid)
        except Exception:
            pass
        self.playlist_tree.insert("", "end", iid=pid, values=("⟳", folder))

        def adopt():
            return self.mgr.adopt_playlist(folder)

        def on_done(_result):
            self.current_pid = pid
            self._refresh_playlists()
            self._update_status()
            messagebox.showinfo(
                "Playlist adopted",
                f"'{folder}' is now managed by EchoList.\n\n"
                f"A 'before_echolist' restore point has been saved in the "
                f"Playlists menu.",
            )

        self._run_playlist_op(pid, folder, adopt, on_done)

    def _create_playlist(self):
        if self._busy():
            return
        temp_name = "New Playlist"
        try:
            pid = self.mgr.create_playlist(temp_name)
        except ValueError:
            n = 2
            while True:
                try:
                    temp_name = f"New Playlist {n}"
                    pid = self.mgr.create_playlist(temp_name)
                    break
                except ValueError:
                    n += 1
                    if n > 100:
                        messagebox.showerror("Error", "Could not create playlist.")
                        return

        self._refresh_playlists()
        self._update_status()
        self.playlist_tree.selection_set(pid)
        self.playlist_tree.see(pid)
        self.current_pid = pid
        self._refresh_tracks()
        self.root.after(50, lambda: self._start_inline_rename(pid))

    # ── .m3u import ──

    def _import_m3u_dialog(self):
        """Open a file picker to import .m3u/.m3u8 playlists."""
        if self._busy():
            return
        files = filedialog.askopenfilenames(
            title="Import .m3u playlists",
            filetypes=[("M3U playlists", "*.m3u *.m3u8"), ("All files", "*.*")],
            initialdir=self.source or str(Path.home()),
        )
        if not files:
            return
        for f in files:
            self._import_m3u_file(Path(f))

    def _import_m3u_file(self, m3u_path: Path):
        """Parse one .m3u file, create a playlist, and stage found tracks."""
        if self._busy():
            return
        source_root = Path(self.mgr.config.source_root).resolve()
        result = parse_m3u(m3u_path, source_root=source_root)

        existing_pids = set(self.mgr.store.playlists.keys())
        name = curate_playlist_name(result["name"], existing_pids)
        tracks = result["tracks"]
        missing = result["missing"]

        if not tracks and not missing:
            messagebox.showinfo("Empty playlist",
                                f"'{m3u_path.name}' contains no track entries.")
            return

        if not tracks and missing:
            messagebox.showwarning(
                "No tracks found",
                f"'{m3u_path.name}' has {len(missing)} entry(ies) but none "
                f"could be found on disk.\n\n"
                f"Make sure the source library path is correct in Settings.",
            )
            return

        try:
            pid = self.mgr.create_playlist(name)
        except ValueError:
            messagebox.showerror("Error", f"Could not create playlist '{name}'.")
            return

        for track_path in tracks:
            title, artist = _read_tags_from_file(track_path)
            self.staging.stage_add(pid, str(track_path), title, artist)

        self._refresh_playlists()
        self.playlist_tree.selection_set(pid)
        self.current_pid = pid
        self._refresh_tracks()
        self._update_status()

        parts = [f"Created playlist '{name}' from {m3u_path.name}",
                 f"{len(tracks)} track(s) staged for sync."]
        if missing:
            parts.append(f"\n{len(missing)} track(s) could not be found:")
            for m in missing[:10]:
                parts.append(f"  - {m}")
            if len(missing) > 10:
                parts.append(f"  ... and {len(missing) - 10} more")
        messagebox.showinfo("Playlist imported", "\n".join(parts))

    def _start_inline_rename(self, pid):
        if self._busy():
            return
        if pid not in self.mgr.store.playlists:
            return
        self.playlist_tree.update_idletasks()
        try:
            bbox = self.playlist_tree.bbox(pid, column="name")
        except Exception:
            return
        if not bbox:
            return

        x, y, w, h = bbox
        entry = tk.Entry(self.playlist_tree, bg=BG_INPUT, fg=FG_BRIGHT,
                         insertbackground=FG_BRIGHT, font=("Consolas", 9),
                         relief="flat", highlightthickness=1, highlightcolor=RED)
        entry.place(x=x, y=y, width=w, height=h)

        current_name = self.mgr.store.playlists[pid]["name"]
        entry.insert(0, current_name)
        entry.select_range(0, "end")
        entry.focus_set()

        def commit(event=None):
            new_name = entry.get().strip()
            entry.destroy()
            self._rename_entry = None
            if not new_name or new_name == current_name:
                return
            self._rename_playlist(pid, new_name)

        def cancel(event=None):
            entry.destroy()
            self._rename_entry = None

        entry.bind("<Return>", commit)
        entry.bind("<Escape>", cancel)
        entry.bind("<FocusOut>", commit)
        self._rename_entry = entry

    def _rename_playlist(self, pid: str, new_name: str) -> bool:
        """Rename a playlist: folder on the device, store, backups, staging."""
        if self._busy():
            return False
        new_pid = playlist_id(new_name)
        if new_pid != pid and new_pid in self.mgr.store.playlists:
            messagebox.showerror("Error", f"Playlist '{new_name}' already exists.")
            return False
        pl = self.mgr.store.playlists[pid]
        old_folder = pl["folder"]
        new_folder = sanitize(new_name)
        # FAT32 folder names are case-insensitive (same rule as
        # PlaylistManager.create_playlist), and an older store's pid may not
        # match playlist_id(), so compare folders, not only pids.
        for other_pid, other in self.mgr.store.playlists.items():
            if other_pid != pid and other["folder"].casefold() == new_folder.casefold():
                messagebox.showerror(
                    "Error",
                    f"Playlist folder '{new_folder}' is already used by "
                    f"playlist '{other['name']}'.")
                return False

        # Rename folder on disk first — abort if it fails
        if old_folder != new_folder:
            try:
                if old_folder.casefold() == new_folder.casefold():
                    # Case-only rename: FAT32 (vfat) refuses a direct rename.
                    step = f"{new_folder}.echolist-rename"
                    self.mgr.writer.rename(old_folder, step)
                    self.mgr.writer.rename(step, new_folder)
                else:
                    self.mgr.writer.rename(old_folder, new_folder)
            except OSError as e:
                messagebox.showerror("Rename failed",
                                     f"Could not rename folder on device:\n{e}")
                return False

        pl["name"] = new_name
        pl["folder"] = new_folder
        if new_pid != pid:
            self.mgr.store.playlists[new_pid] = pl
            del self.mgr.store.playlists[pid]
            if self.current_pid == pid:
                self.current_pid = new_pid
        self.mgr.store.save()
        self.staging.rename_pid(pid, new_pid)
        # Undo snapshots name the old pid; restoring one would orphan entries.
        self._undo_stack.clear()
        from .config import rename_backup_pid
        try:
            rename_backup_pid(self.mgr.writer.root, pid, new_pid, new_folder)
        except (OSError, ValueError) as e:
            messagebox.showwarning(
                "Restore points not moved",
                f"The playlist was renamed, but its restore points could not "
                f"be moved:\n{e}")
        self._invalidate_caches()
        self._refresh_playlists()
        self._update_status()
        return True

    def _delete_playlist(self):
        if self._busy():
            return
        if not self.current_pid:
            return
        pl = self.mgr.store.playlists.get(self.current_pid)
        if not pl:
            return
        orphans = [t for t in pl.get("tracks", []) if not t.get("src_path")]
        warning = ""
        if orphans:
            warning = (f"\n\n{len(orphans)} track(s) have no source file "
                       f"(e.g. from an adopted folder) and cannot be restored.")
        if not messagebox.askyesno("Delete playlist",
                                   f"Delete playlist '{pl['name']}'?\n\n"
                                   "This will remove the playlist folder and all the\n"
                                   "tracks that were copied into it.\n"
                                   "Your original music files are never touched.\n\n"
                                   f"A restore point will be saved so you can bring it back.{warning}"):
            return
        pid = self.current_pid
        name = pl["name"]
        self.current_pid = None

        def delete():
            # The restore point is what makes this undoable: if it cannot be
            # written, nothing is deleted. A folder that is already gone is
            # fine (writer.delete ignores it); any other error aborts.
            self.mgr.backup_playlist_metadata(pid)
            self.mgr.writer.delete(pl["folder"])

        def on_done(_result):
            del self.mgr.store.playlists[pid]
            try:
                self.mgr.store.save()
            except OSError as e:
                self.mgr.store.playlists[pid] = pl
                messagebox.showerror(
                    "Delete incomplete",
                    f"The playlist folder was removed from the device, but the "
                    f"playlist list could not be saved:\n{e}")
                self._refresh_playlists()
                self._update_status()
                return
            self.staging.drop_pid(pid)
            self._undo_stack.clear()
            self._refresh_playlists()
            self._update_status()

        self._run_playlist_op(pid, name, delete, on_done)

    # TODO: star prefix feature disabled — re-enable when stable
    # def _toggle_star_prefix(self):
    #     enabled = self._star_var.get()
    #     self._star_cb.config(state="disabled", fg=FG_DIM)
    #     self.mgr.set_star_prefix(enabled)
    #     self._refresh_playlists()
    #     self._update_status()
    #     self._poll_star_rename()

    # def _poll_star_rename(self):
    #     thread = getattr(self.mgr, "_star_rename_thread", None)
    #     if thread and thread.is_alive():
    #         self.root.after(50, self._poll_star_rename)
    #     else:
    #         self._star_cb.config(state="normal", fg=FG)

    # ── Tracks ──

    def _show_offloaded_tracks(self, pid: str):
        """Display tracks from a backup as grey read-only list for offloaded playlists."""
        self.track_tree.delete(*self.track_tree.get_children())
        self._track_data = []
        self._removed_data = []
        from .config import load_backup
        timestamp = self._offload_backup_timestamp(pid)
        if timestamp is None:
            self.track_tree.insert("", "end", values=("", "(offloaded — no backup found)", ""),
                                   tags=("offloaded_track",))
            return
        data = load_backup(self.mgr.writer.root, pid, timestamp)
        if not data:
            return
        for entry in data.get("tracks", []):
            tags_data = entry.get("tags", {})
            title = tags_data.get("title", entry.get("copy_name", ""))
            artist = tags_data.get("artist", "")
            idx = entry.get("index", "")
            self.track_tree.insert("", "end", values=(idx, title, artist),
                                   tags=("offloaded_track",))

    def _refresh_tracks(self):
        self.track_tree.delete(*self.track_tree.get_children())
        if not self.current_pid or self.current_pid not in self.mgr.store.playlists:
            self._track_data = []
            self._removed_data = []
            return
        pid = self.current_pid
        # Rescan here, on the main thread: it rewrites the store, which must
        # not happen from the tag-loading thread (or during a sync / a
        # background playlist operation, which own the store meanwhile).
        if self._can_rescan():
            self.mgr.rescan_playlist(pid)
        playlist = self.mgr.store.playlists[pid]
        folder = playlist["folder"]

        # Check if all committed track tags are cached — if so, skip the thread
        committed = [t for t in playlist["tracks"]
                     if t.get("copy_name")]
        all_cached = all(
            f"{folder}/{t['copy_name']}" in self._tag_cache
            for t in committed
        )

        self._tracks_gen += 1
        gen = self._tracks_gen

        if all_cached:
            self._load_tracks_sync(pid, gen)
        else:
            self._tracks_loading = True
            self.track_tree.insert("", "end", iid="_loading",
                                   values=("", "Loading...", ""), tags=("loading",))
            self._spawn(self._load_tracks_bg, pid, gen)

    def _build_track_data(self, pid: str):
        """Build track/removed data lists. Called from any thread; read-only
        on the store (the rescan happens in _refresh_tracks)."""
        playlist = self.mgr.store.playlists.get(pid)
        if not playlist:
            return [], []
        active, removed = self.staging.virtual_tracks(pid, playlist["tracks"])

        track_data = []
        for t in active:
            if t.get("_pending"):
                title = t.get("title", "")
                artist = t.get("artist", "")
            else:
                title, artist = self._read_track_tags(
                    playlist["folder"], t.get("copy_name", ""), t.get("src_path", "")
                )
            track_data.append({
                "index": t["index"],
                "title": title,
                "artist": artist,
                "pending": t.get("_pending", False),
                "copy_name": t.get("copy_name", ""),
                "src_path": t.get("src_path", ""),
                "key": t.get("_key", ""),
            })

        removed_data = []
        for t in removed:
            title, artist = self._read_track_tags(
                playlist["folder"], t.get("copy_name", ""), t.get("src_path", "")
            )
            removed_data.append({
                "index": t["index"],
                "title": title,
                "artist": artist,
                "copy_name": t.get("copy_name", ""),
                "key": t.get("_key", ""),
            })

        return track_data, removed_data

    def _load_tracks_sync(self, pid: str, gen: int):
        track_data, removed_data = self._build_track_data(pid)
        if self._tracks_gen != gen:
            return
        self._track_data = track_data
        self._removed_data = removed_data
        self._display_tracks()

    def _load_tracks_bg(self, pid: str, gen: int):
        try:
            track_data, removed_data = self._build_track_data(pid)
        except BaseException:
            self._tracks_loading = False
            raise
        if not self._alive or self._tracks_gen != gen:
            self._tracks_loading = False
            return

        def _apply():
            self._tracks_loading = False
            if self._tracks_gen != gen:
                return
            self._track_data = track_data
            self._removed_data = removed_data
            self._display_tracks()

        self._schedule_callback(_apply)

    def _display_tracks(self):
        self.track_tree.delete(*self.track_tree.get_children())
        for row in self._track_data:
            tag = ("pending",) if row["pending"] else ()
            prefix = "~ " if row["pending"] else ""
            self.track_tree.insert("", "end", values=(row["index"], prefix + row["title"], row["artist"]),
                                    tags=tag)
        for row in self._removed_data:
            self.track_tree.insert("", "end",
                                    values=(row["index"], row["title"], row["artist"]),
                                    tags=("removed",))

    def _sort_tracks(self, col):
        if self._sort_col == col:
            if self._sort_reverse:
                self._sort_col = None
                self._sort_reverse = False
                self._track_data.sort(key=lambda r: r["index"])
                self._display_tracks()
                for c in ("index", "title", "artist"):
                    label = {"index": "#", "title": "TITLE", "artist": "ARTIST"}[c]
                    self.track_tree.heading(c, text=label)
                return
            else:
                self._sort_reverse = True
        else:
            self._sort_col = col
            self._sort_reverse = False

        def sort_key(row):
            val = row[col]
            if col == "index":
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return 0
            return str(val).lower()

        self._track_data.sort(key=sort_key, reverse=self._sort_reverse)
        self._display_tracks()

        arrow = " ▼" if self._sort_reverse else " ▲"
        for c in ("index", "title", "artist"):
            label = {"index": "#", "title": "TITLE", "artist": "ARTIST"}[c]
            self.track_tree.heading(c, text=label + (arrow if c == col else ""))

    # ── Track reorder drag ──

    def _trk_drag_start(self, event):
        if self._busy() or self._current_playlist_offloaded():
            self._trk_drag_iid = None
            return
        iid = self.track_tree.identify_row(event.y)
        region = self.track_tree.identify_region(event.x, event.y)
        if region == "heading":
            self._trk_drag_iid = None
            return
        self._trk_drag_iid = iid
        self._trk_drag_origin_y = event.y

    def _trk_drag_motion(self, event):
        if not self._trk_drag_iid:
            return
        if abs(event.y - self._trk_drag_origin_y) < 5:
            return
        target = self.track_tree.identify_row(event.y)
        if target and target != self._trk_drag_iid:
            try:
                bbox = self.track_tree.bbox(target)
            except Exception:
                return
            if not bbox:
                return
            mid = bbox[1] + bbox[3] // 2
            if event.y < mid:
                pos = self.track_tree.index(target)
            else:
                pos = self.track_tree.index(target) + 1
            self.track_tree.move(self._trk_drag_iid, "", pos)

    def _trk_drag_end(self, event):
        if not self._trk_drag_iid:
            return
        self._trk_drag_iid = None
        if self._busy():
            self._display_tracks()  # undo the drag's visual moves
            return

        # Read new order from tree, rebuild _track_data and save reorder
        all_iids = self.track_tree.get_children()
        if not all_iids or not self._track_data or not self.current_pid:
            return

        # Build a map from displayed values to track_data row
        old_data = list(self._track_data)

        # Map tree row positions to old _track_data entries by matching display values
        # Since display may have been sorted, we match by the current tree content
        old_by_display = {}
        for row in old_data:
            prefix = "~ " if row["pending"] else ""
            disp_key = (str(row["index"]), prefix + row["title"], row["artist"])
            old_by_display[disp_key] = row

        new_order = []
        for iid in all_iids:
            vals = self.track_tree.item(iid, "values")
            disp_key = (str(vals[0]), str(vals[1]), str(vals[2]))
            if disp_key in old_by_display:
                new_order.append(old_by_display[disp_key])

        if len(new_order) != len(old_data):
            return

        # Check if order actually changed
        if all(n["key"] == o["key"] for n, o in zip(new_order, old_data)):
            return

        # Renumber indices
        for i, row in enumerate(new_order, 1):
            row["index"] = i

        self._track_data = new_order
        self._display_tracks()

        # Save reorder to staging
        reorder_list = [{"key": row["key"]} for row in new_order]
        self._push_undo(f"Reorder tracks in {self.current_pid}")
        self.staging.set_reorder(self.current_pid, reorder_list)
        self._update_status()

    def _read_track_tags(self, folder: str, copy_name: str, src_path: str = "") -> tuple[str, str]:
        if copy_name:
            cache_key = f"{folder}/{copy_name}"
            cached = self._tag_cache.get(cache_key)
            if cached is not None:
                return cached

            try:
                import mutagen
                path = self.mgr.writer.root / folder / copy_name
                m = mutagen.File(path, easy=True)
                if m:
                    title = m.get("title", [""])[0]
                    artist = m.get("artist", [""])[0]
                    if title or artist:
                        self._tag_cache[cache_key] = (title, artist)
                        return title, artist
            except Exception:
                pass

        if src_path:
            source_root = Path(self.mgr.config.source_root).resolve()
            src_full = _resolve_source_file(src_path, source_root)
            if src_full:
                result = _read_tags_from_file(src_full)
                if copy_name:
                    self._tag_cache[f"{folder}/{copy_name}"] = result
                return result

        return copy_name or "", ""

    def _remove_track(self, event=None):
        if self._busy() or self._current_playlist_offloaded():
            return
        if not self.current_pid:
            return
        selected = self.track_tree.selection()
        if not selected:
            return

        before = self.staging.snapshot()
        all_iids = self.track_tree.get_children()
        selected_rows = []
        for iid in selected:
            row_idx = all_iids.index(iid)
            if row_idx < len(self._track_data):
                selected_rows.append(self._track_data[row_idx])

        pending_keys = []
        committed_keys = []
        for row in selected_rows:
            if row["pending"]:
                pending_keys.append(row["key"])
            else:
                committed_keys.append(row["key"])

        if pending_keys:
            pending_offsets = set()
            for k in pending_keys:
                # key format is "p:N"
                pending_offsets.add(int(k.split(":")[1]))
            indices_to_pop = []
            count = 0
            for i, a in enumerate(self.staging.pending_adds):
                if a["pid"] == self.current_pid:
                    if count in pending_offsets:
                        indices_to_pop.append(i)
                    count += 1
            for i in sorted(indices_to_pop, reverse=True):
                self.staging.unstage_add(self.staging.pending_adds[i])

        if committed_keys:
            playlist = self.mgr.store.playlists[self.current_pid]
            orphan_count = 0
            for k in committed_keys:
                orig_idx = int(k.split(":")[1])
                for t in playlist["tracks"]:
                    if t["index"] == orig_idx and not t.get("src_path"):
                        orphan_count += 1
            if orphan_count:
                proceed = messagebox.askyesno(
                    "Track has no source",
                    f"{orphan_count} selected track(s) have no source file "
                    f"(e.g. from an adopted folder).\n\n"
                    f"Removing them is permanent — they can't be re-added "
                    f"without the original file.\n\n"
                    f"Continue?",
                )
                if not proceed:
                    if pending_keys:
                        self._push_undo(f"Remove {len(pending_keys)} pending track(s)", before)
                        self._refresh_tracks()
                        self._update_status()
                    return
            for k in committed_keys:
                orig_idx = int(k.split(":")[1])
                track = next((t for t in playlist["tracks"] if t["index"] == orig_idx), {})
                self.staging.stage_remove(self.current_pid, orig_idx,
                                          track.get("copy_name", ""),
                                          track.get("src_path", ""))

        # Clear reorder for this playlist since its keys changed
        self.staging.unstage_reorder(self.current_pid)
        n = len(selected_rows)
        self._push_undo(f"Remove {n} track{'s' if n != 1 else ''} from {self.current_pid}",
                        before)

        self._refresh_tracks()
        self._update_status()

    def _add_files_dialog(self):
        folder = filedialog.askdirectory(
            title="Add folder to source browser",
            initialdir=self.source,
        )
        if not folder:
            return
        folder_path = Path(folder)
        display = folder_path.name + "/"
        iid = self.source_tree.insert("", "end", text=display, values=(str(folder_path),))
        self.source_tree.insert(iid, "end", text="...")

    # ── Status ──

    def _refresh_expensive_stats(self):
        if self._stats_pending:
            return
        self._stats_pending = True

        def worker():
            try:
                dt, wb = self.mgr.compute_expensive_stats()
            except Exception:
                dt, wb = 0, 0
            if self._alive:
                self._schedule_callback(lambda: self._on_expensive_stats(dt, wb))

        self._stats_thread = self._spawn(worker)

    def _on_expensive_stats(self, device_tracks, workspace_bytes):
        self._cached_device_tracks = device_tracks
        self._cached_workspace_bytes = workspace_bytes
        self._stats_pending = False
        self._update_status()

    def _update_status(self):
        try:
            s = self.mgr.stats(
                cached_device_tracks=self._cached_device_tracks,
                cached_workspace_bytes=self._cached_workspace_bytes,
            )
        except Exception:
            return

        self.lbl_playlists.config(text=f"PLAYLISTS: {s['playlists']}")
        self.lbl_workspace.config(text=f"{s['workspace_bytes']:,} bytes")

        pending_delta = len(self.staging.pending_adds) - len(self.staging.pending_removes)
        virtual_count = s.get("device_tracks", 0) + pending_delta
        track_pct = round(virtual_count / MAX_TRACKS * 100, 1)
        self.lbl_tracks.config(text=f"TRACKS: {virtual_count} / {MAX_TRACKS}")
        self.track_pct_lbl.config(text=f"{track_pct}%")
        self.track_bar["value"] = track_pct
        self._color_bar(self.track_bar, track_pct)
        self.lbl_tracks.config(fg=RED_BRIGHT if track_pct >= 90 else FG)

        drive_pct = s["drive_used_pct"]
        self.lbl_drive.config(text=f"DRIVE: {drive_pct}%")
        self.drive_bar["value"] = drive_pct
        self._color_bar(self.drive_bar, drive_pct)
        self.lbl_drive.config(fg=RED_BRIGHT if drive_pct >= 90 else FG)

        if self.staging.has_pending:
            n = self.staging.total_ops
            self.pending_lbl.config(text=f"{n} pending change{'s' if n != 1 else ''}")
        else:
            self.pending_lbl.config(text="")

        if self._undo_stack:
            self.undo_btn.state(["!disabled"])
            self.undo_lbl.config(text=self._undo_stack[-1]["desc"])
        else:
            self.undo_btn.state(["disabled"])
            self.undo_lbl.config(text="")

    def _color_bar(self, bar, pct):
        if pct >= 90:
            bar.configure(style="Red.Horizontal.TProgressbar")
        elif pct >= 70:
            bar.configure(style="Yellow.Horizontal.TProgressbar")
        else:
            bar.configure(style="Green.Horizontal.TProgressbar")


def run():
    app = App()
    # Python runs signal handlers between bytecodes; the 16 ms callback poll
    # keeps that happening during mainloop. Hand off to the Tk thread so the
    # lock is released and windows close normally.
    signal.signal(signal.SIGINT, lambda *_: app._schedule_callback(app._on_interrupt))
    app.run()


if __name__ == "__main__":
    run()
