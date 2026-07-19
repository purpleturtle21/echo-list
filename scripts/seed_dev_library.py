#!/usr/bin/env python3
"""Seed dev-data/source with a library for local development.

Prefers copying a couple of small real albums from ~/Music — real tags,
real filename quirks (inconsistent track-number padding, unicode
apostrophes, non-audio files like cover art/logs mixed in) make for a
more honest test than synthetic files. Falls back to a tiny synthetic
tagged FLAC library on a machine that doesn't have those albums.

Safe to re-run — skips anything already copied/generated.
"""

import base64
import shutil
from pathlib import Path

from mutagen.flac import FLAC

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "dev-data" / "source"

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".mp4", ".aac"}

# Small real albums to copy in, if present (picked for size + filename/tag variety).
REAL_ALBUMS = [
    Path.home() / "Music" / "80s" / "1977 - Let There Be Rock",
    Path.home() / "Music" / "80s" / "Thriller",
]

# Minimal valid FLAC (1 sample, 44100 Hz, 16-bit mono) — same fixture tests/conftest.py uses.
TINY_FLAC = base64.b64decode(
    "ZkxhQwAAACIAAQABAAAAAAAACsRA8AAAAAHEED8SLSdnfJ2xRMrhOUpmhAAADQUAAABTaWRlQgAAAAD/+GkIAAAdAAAAoCc="
)

SYNTHETIC_TRACKS = [
    ("Nova Tide", "Bright Horizon", 1, "Sundown"),
    ("Nova Tide", "Bright Horizon", 2, "Static Bloom"),
    ("Nova Tide", "Bright Horizon", 3, "Afterglow"),
    ("Kestrel & Wire", "Low Orbit", 1, "Vantage"),
    ("Kestrel & Wire", "Low Orbit", 2, "Drift"),
    ("Marigold Static", "Paper Weather", 1, "Paper Weather"),
    ("Marigold Static", "Paper Weather", 2, "Thin Ice"),
]


def _copy_album(album_dir: Path) -> int:
    dest = SOURCE / album_dir.parent.name / album_dir.name
    if dest.exists():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in sorted(album_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
            shutil.copy2(f, dest / f.name)
            count += 1
    return count


def _make_synthetic_track(artist: str, album: str, track_no: int, title: str) -> None:
    path = SOURCE / artist / album / f"{track_no:02d} {title}.flac"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_FLAC)
    f = FLAC(path)
    f["ARTIST"] = artist
    f["ALBUM"] = album
    f["TITLE"] = title
    f["TRACKNUMBER"] = str(track_no)
    f.save()


def main() -> None:
    SOURCE.mkdir(parents=True, exist_ok=True)

    any_real = False
    total_copied = 0
    for album in REAL_ALBUMS:
        if not album.is_dir():
            continue
        already_present = (SOURCE / album.parent.name / album.name).exists()
        total_copied += _copy_album(album)
        any_real = any_real or already_present or total_copied

    if any_real:
        if total_copied:
            print(f"Copied {total_copied} real track(s) from {Path.home() / 'Music'}")
        else:
            print(f"Real album(s) already present under {SOURCE}")
        return

    for artist, album, track_no, title in SYNTHETIC_TRACKS:
        _make_synthetic_track(artist, album, track_no, title)
    print(f"No real albums found — seeded {len(SYNTHETIC_TRACKS)} synthetic track(s)")


if __name__ == "__main__":
    main()
