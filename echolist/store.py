"""JSON playlist store backed by SafeWriter."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .safe_write import SafeWriter

STORE_REL = ".echolist/playlists.json"


class Store:
    def __init__(self, writer: SafeWriter, data: dict):
        self._writer = writer
        self._data = data

    @classmethod
    def load(cls, writer: SafeWriter) -> Store:
        p = writer.root / STORE_REL
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
        else:
            data = {"schema": 1, "playlists": {}}
        return cls(writer, data)

    def save(self) -> None:
        self._writer.write_text(STORE_REL, json.dumps(self._data, indent=2))

    @property
    def playlists(self) -> dict:
        return self._data["playlists"]

    def add_playlist(self, pid: str, name: str, folder: str) -> None:
        self._data["playlists"][pid] = {
            "name": name,
            "folder": folder,
            "tracks": [],
        }

    def add_track(self, pid: str, track_dict: dict) -> None:
        self._data["playlists"][pid]["tracks"].append(track_dict)

    def remove_track(self, pid: str, index: int) -> dict:
        tracks = self._data["playlists"][pid]["tracks"]
        for i, t in enumerate(tracks):
            if t["index"] == index:
                removed = tracks.pop(i)
                for j, t2 in enumerate(tracks):
                    t2["index"] = j + 1
                return removed
        raise KeyError(f"no track with index {index} in playlist {pid}")
