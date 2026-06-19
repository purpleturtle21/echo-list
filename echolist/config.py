"""Config dataclass + load/save via SafeWriter."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .safe_write import SafeWriter

CONFIG_REL = ".echolist/config.json"
DEFAULT_FILE = Path.home() / ".echolist" / "default.json"


def load_defaults() -> dict:
    if DEFAULT_FILE.exists():
        return json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    return {}


def save_defaults(source: str, dest: str) -> None:
    DEFAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_FILE.write_text(json.dumps({
        "source": str(Path(source).resolve()),
        "dest": str(Path(dest).resolve()),
    }), encoding="utf-8")


@dataclass
class Config:
    schema: int = 1
    source_root: str = ""
    node_name: str = "* PLAYLISTS *"
    album_prefix: str = ""

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
            )
        return cls()

    def save(self, writer: SafeWriter) -> None:
        writer.write_text(CONFIG_REL, json.dumps(asdict(self), indent=2))
