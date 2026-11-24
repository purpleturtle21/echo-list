"""Sanitize names and filenames for exFAT/FAT-safe storage."""

import re
import unicodedata

ILLEGAL = set('\\/:*?"<>|')


def _is_illegal(c: str) -> bool:
    # FAT32 also forbids control characters 0x00-0x1F; DEL is replaced too.
    return c in ILLEGAL or ord(c) < 0x20 or ord(c) == 0x7F


def sanitize(name: str, maxlen: int = 60) -> str:
    # NFC first: macOS hands back NFD names, and slicing NFD text can cut a
    # combining accent off its base letter.
    name = unicodedata.normalize("NFC", name)
    cleaned = "".join("_" if _is_illegal(c) else c for c in name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(" .")
    cleaned = cleaned[:maxlen]
    cleaned = cleaned.rstrip(" .")
    return cleaned or "untitled"


def playlist_id(name: str) -> str:
    """Stable id for a playlist name, derived from its folder name.

    The id is used as a path segment (backups live in
    ~/.echolist/backups/<wid>/<pid>/), so it is built from sanitize() — no
    separators, FAT-illegal or control characters — and runs of dots are
    replaced so it can never contain "..". Two names that would share a
    FAT32 folder (which is case-insensitive) share an id.
    """
    pid = re.sub(r"\s+", "_", sanitize(name)).lower()
    return re.sub(r"\.{2,}", lambda m: "_" * len(m.group()), pid)


def track_filename(index: int, title: str, ext: str, pad: int = 2) -> str:
    return f"{index:0{pad}d} - {sanitize(title)}{ext}"
