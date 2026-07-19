"""Sanitize names and filenames for exFAT/FAT-safe storage."""
from __future__ import annotations

import re
from pathlib import Path

ILLEGAL = set('\\/:*?"<>|')

_CHAR_MAP = str.maketrans({
    "‘": "'",   # left single curly quote
    "’": "'",   # right single curly quote (smart apostrophe)
    "‚": "'",   # single low-9 quotation mark
    "“": '"',   # left double curly quote
    "”": '"',   # right double curly quote
    "„": '"',   # double low-9 quotation mark
    "–": "-",   # en-dash
    "—": "-",   # em-dash
    "…": ".",   # ellipsis → single dot (good enough for matching)
    "´": "'",   # acute accent
    "`": "'",   # grave accent
})


def sanitize(name: str, maxlen: int = 60) -> str:
    cleaned = "".join("_" if c in ILLEGAL else c for c in name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(" .")
    cleaned = cleaned[:maxlen]
    cleaned = cleaned.rstrip(" .")
    return cleaned or "untitled"


def playlist_id(name: str) -> str:
    return re.sub(r"\s+", "_", name.strip()).lower()


def track_filename(index: int, title: str, ext: str, pad: int = 2) -> str:
    return f"{index:0{pad}d} - {sanitize(title)}{ext}"


def normalize_chars(name: str) -> str:
    return name.translate(_CHAR_MAP)


def fuzzy_resolve(candidate: Path) -> Path | None:
    """If *candidate* doesn't exist, try matching its filename against the
    directory listing after normalizing common Unicode look-alikes."""
    if candidate.exists():
        return candidate
    parent = candidate.parent
    if not parent.is_dir():
        return None
    target = normalize_chars(candidate.name).casefold()
    try:
        for entry in parent.iterdir():
            if normalize_chars(entry.name).casefold() == target:
                return entry
    except OSError:
        pass
    return None
