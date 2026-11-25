"""AppleDouble sidecars on a real FAT32 volume.

macOS stores extended attributes on FAT32 in "._<name>" files. Files and
folders Python creates carry com.apple.provenance, so every EchoList write
used to leave a sidecar on the card, which the Echo Mini then lists as a
broken track. This test formats a disposable FAT32 disk image with hdiutil,
runs a whole EchoList session on it, and checks no sidecar is left. It only
runs on macOS; elsewhere there are no sidecars to test for.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from conftest import _make_flac
from echolist.manager import PlaylistManager

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("hdiutil") is None,
    reason="needs macOS hdiutil to make a FAT32 image",
)


@pytest.fixture
def fat_volume(tmp_path):
    image = tmp_path / "card.dmg"
    mount = tmp_path / "mnt"
    mount.mkdir()
    subprocess.run(["hdiutil", "create", "-size", "64m", "-fs", "MS-DOS",
                    "-volname", "ELTEST", str(image)],
                   check=True, capture_output=True)
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount),
                    str(image)], check=True, capture_output=True)
    try:
        yield mount
    finally:
        subprocess.run(["hdiutil", "detach", "-force", str(mount)],
                       check=True, capture_output=True)
        image.unlink()


def _sidecars(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("._*"))


def test_echolist_session_leaves_no_appledouble_files(fat_volume, tmp_path):
    lib = tmp_path / "library"
    for n in ("a", "b", "c"):
        _make_flac(lib / f"{n}.flac", "Artist", n.upper())

    mgr = PlaylistManager.init(lib, fat_volume)
    try:
        pid = mgr.create_playlist("Mix")
        for n in ("a", "b", "c"):
            mgr.add_track(pid, lib / f"{n}.flac")
        mgr.backup_playlist_metadata(pid, "t1")
        mgr.remove_track(pid, 1)          # delete, renumber, retag in place
        mgr.restore_playlist_to_point(pid, "t1")
        mgr.writer.write_text("Mix/notes.txt", "x")
        mgr.writer.rename("Mix/notes.txt", "Mix/notes2.txt")
        mgr.writer.delete("Mix/notes2.txt")
    finally:
        mgr.release_lock()
    again = PlaylistManager.open(fat_volume)  # reopens the lock file for writing
    again.release_lock()

    assert _sidecars(fat_volume) == []
