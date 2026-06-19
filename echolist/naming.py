"""Sanitize names and filenames for exFAT/FAT-safe storage."""

import re

ILLEGAL = set('\\/:*?"<>|')


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
