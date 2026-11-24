"""Parse .m3u / .m3u8 playlist files into track lists."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname


def parse_m3u(m3u_path: Path, source_root: Path | None = None) -> dict:
    """Parse an .m3u or .m3u8 file.

    Returns {"name": str, "tracks": [Path, ...], "missing": [str, ...]}
    where tracks are resolved paths that exist and missing are raw entries
    that couldn't be found.
    """
    m3u_path = Path(m3u_path)
    name = m3u_path.stem

    # utf-8-sig for both: Windows players write .m3u8 files with a BOM, and
    # "\ufeff#EXTM3U" would otherwise be read as a track path.
    try:
        lines = m3u_path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        lines = m3u_path.read_text(encoding="cp1252").splitlines()

    tracks: list[Path] = []
    missing: list[str] = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("#"):
            if line.upper().startswith("#PLAYLIST:"):
                name = line.split(":", 1)[1].strip() or name
            continue
        if line.lower().startswith(("http://", "https://", "mms://", "rtsp://")):
            continue

        if line[:7].lower() == "file://":
            entry = _file_uri_to_path(line)
            if entry is None:
                missing.append(line)
                continue
        else:
            entry = line.translate({ord("\\"): "/"})
        resolved = _resolve_entry(entry, m3u_path.parent, source_root)
        if resolved and resolved.is_file():
            tracks.append(resolved)
        else:
            missing.append(line)

    return {"name": name, "tracks": tracks, "missing": missing}


def _file_uri_to_path(uri: str) -> str | None:
    """file:///a/b%20c.flac or file://localhost/a/... -> "/a/b c.flac".

    Returns None for a file URI naming another host, which cannot be read
    as a local path.
    """
    parts = urlsplit(uri)
    # Host names are case-insensitive (file://LOCALHOST/...); the path is not.
    if (parts.hostname or "") not in ("", "localhost") or parts.port is not None:
        return None
    # url2pathname percent-decodes (and handles drive letters on Windows).
    return url2pathname(parts.path)


def _resolve_entry(entry: str, m3u_dir: Path, source_root: Path | None) -> Path | None:
    p = Path(entry)
    if p.is_absolute() and p.exists():
        return p.resolve()

    relative = m3u_dir / entry
    if relative.exists():
        return relative.resolve()

    if source_root:
        from_source = source_root / entry
        if from_source.exists():
            return from_source.resolve()

    return None


def curate_playlist_name(raw_name: str, existing_names: set[str]) -> str:
    """Clean up a playlist name from a filename, avoiding collisions.

    ``existing_names`` holds playlist ids. The " (N)" suffix is appended to
    a base shortened enough that sanitize()'s length limit cannot cut the
    suffix off again, so every returned name has a free id.
    """
    from .naming import sanitize, playlist_id

    # A leading dot would make a hidden folder on the player and on macOS.
    name = sanitize(sanitize(raw_name).lstrip(". "))
    if playlist_id(name) not in existing_names:
        return name

    n = 2
    while True:
        suffix = f" ({n})"
        candidate = f"{sanitize(name, maxlen=60 - len(suffix))}{suffix}"
        if playlist_id(candidate) not in existing_names:
            return candidate
        n += 1
