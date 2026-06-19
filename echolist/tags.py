"""Write playlist tags to copies (never originals)."""

from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4


def apply_playlist_tags(
    path: Path, node_name: str, album: str, index: int, src_rel: str, playlist_id: str
) -> None:
    suffix = path.suffix.lower()
    if suffix == ".flac":
        _tag_flac(path, node_name, album, index, src_rel, playlist_id)
    elif suffix == ".mp3":
        _tag_easy(EasyID3, path, node_name, album, index)
    elif suffix in (".m4a", ".mp4", ".aac"):
        _tag_easy(EasyMP4, path, node_name, album, index)


def _tag_flac(
    path: Path, node_name: str, album: str, index: int, src_rel: str, pid: str
) -> None:
    f = FLAC(path)
    f["ALBUMARTIST"] = node_name
    f["ALBUM"] = album
    f["TRACKNUMBER"] = str(index)
    f["DISCNUMBER"] = "1"
    f["ECHOLIST_ROLE"] = "playlist-copy"
    f["ECHOLIST_PLAYLIST"] = pid
    f["ECHOLIST_INDEX"] = str(index)
    f["ECHOLIST_SRC"] = src_rel
    f.save()


def _tag_easy(easy_cls, path: Path, node_name: str, album: str, index: int) -> None:
    try:
        tags = easy_cls(path)
    except mutagen.MutagenError:
        tags = easy_cls()
        tags.filename = str(path)
    tags["albumartist"] = node_name
    tags["album"] = album
    tags["tracknumber"] = str(index)
    tags["discnumber"] = "1"
    tags.save(path)
