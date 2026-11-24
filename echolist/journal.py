"""Sync journal — an atomically written record of in-progress sync operations,
so an interrupted sync can be detected and resumed."""

from __future__ import annotations

import json
from pathlib import Path

from .safe_write import atomic_write_text as _atomic_write_text

# Legacy global location. The GUI keeps one journal per workspace
# (config.journal_file); this default remains for callers that pass no path.
JOURNAL_FILE = Path.home() / ".echolist" / "sync_journal.json"

# A step that has not finished. "in_progress" is the step that was running
# when the process died — the one most likely to have left partial state.
INCOMPLETE = ("pending", "in_progress")


class SyncJournal:
    """Writes a step-by-step plan before sync; marks each step as it finishes.

    Statuses: "pending" -> "in_progress" -> "done" | "failed". On next
    startup, a journal with any pending/in_progress step means we crashed
    mid-sync. A "reorder" step can carry its rename ``plan`` and ``phase`` so
    recovery can finish the renames instead of guessing.
    """

    def __init__(self, actions: list[dict] | None = None, path: Path | None = None):
        self.actions: list[dict] = actions or []
        self.path = path

    def _file(self) -> Path:
        return self.path or JOURNAL_FILE

    @classmethod
    def begin(cls, removes: list[dict], adds: list[dict], reorders: dict,
              path: Path | None = None) -> SyncJournal:
        actions = []
        for r in removes:
            actions.append({"op": "remove", "pid": r["pid"], "index": r["index"],
                            "copy_name": r.get("copy_name", ""), "status": "pending"})
        for a in adds:
            actions.append({"op": "add", "pid": a["pid"], "src": a["src"],
                            "title": a.get("title", ""), "status": "pending"})
        for pid in reorders:
            actions.append({"op": "reorder", "pid": pid, "status": "pending"})
        journal = cls(actions, path)
        journal._save()
        return journal

    @classmethod
    def load_incomplete(cls, path: Path | None = None) -> SyncJournal | None:
        f = path or JOURNAL_FILE
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            actions = data.get("actions", [])
        except (json.JSONDecodeError, OSError):
            cls.discard(f)
            return None
        if not any(a.get("status") in INCOMPLETE for a in actions):
            cls.discard(f)
            return None
        return cls(actions, path)

    @property
    def pending_actions(self) -> list[dict]:
        """Steps that did not finish, including the one that was running."""
        return [a for a in self.actions if a.get("status") in INCOMPLETE]

    @property
    def has_unfinished(self) -> bool:
        return bool(self.pending_actions)

    def _set(self, index: int, **fields) -> None:
        if 0 <= index < len(self.actions):
            self.actions[index].update(fields)
            self._save()

    def mark_done(self, index: int) -> None:
        self._set(index, status="done")

    def mark_current(self, index: int) -> None:
        self._set(index, status="in_progress")

    def mark_failed(self, index: int, error: str) -> None:
        self._set(index, status="failed", error=error)

    def update(self, index: int, **fields) -> None:
        """Record extra state for a step (e.g. a reorder's plan and phase)."""
        self._set(index, **fields)

    def complete(self) -> None:
        self.discard(self._file())

    def _save(self) -> None:
        _atomic_write_text(self._file(), json.dumps({"actions": self.actions}, indent=2))

    @staticmethod
    def discard(path: Path | None = None) -> None:
        (path or JOURNAL_FILE).unlink(missing_ok=True)
