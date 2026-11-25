"""SafeWriter — the single chokepoint for all filesystem mutations."""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import tempfile
import unicodedata
from pathlib import Path


class UnsafeWriteError(Exception):
    ...


class SafeWriter:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resolve(self, rel_or_abs) -> Path:
        p = Path(rel_or_abs)
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        if not p.is_relative_to(self.root):
            raise UnsafeWriteError(f"outside workspace: {p}")
        return p

    def makedirs(self, rel, exist_ok: bool = True) -> Path:
        """Create a directory (and missing parents) inside the workspace.

        ``exist_ok=False`` claims the directory exclusively: it raises
        FileExistsError if the name — or, on FAT32 / macOS, a name differing
        only in case or Unicode form — is already taken.
        """
        p = self._resolve(rel)
        if not exist_ok and _occupied(p):
            raise FileExistsError(errno.EEXIST, "already exists", str(p))
        self._mkdirs(p, exist_ok)
        return p

    def _mkdirs(self, p: Path, exist_ok: bool = True) -> None:
        created = []
        d = p
        while not d.exists() and d != d.parent:
            created.append(d)
            d = d.parent
        p.mkdir(parents=True, exist_ok=exist_ok)
        for d in reversed(created):
            _fsync_dir(d.parent)
            self._drop_sidecar(d)

    def write_text(self, rel, text) -> Path:
        return self.write_bytes(rel, text.encode("utf-8"))

    def write_bytes(self, rel, data) -> Path:
        p = self._resolve(rel)
        self._mkdirs(p.parent)
        _atomic_write(p, data)
        self._drop_sidecar(p)
        return p

    def copy_in(self, src, rel, progress_cb=None, compute_hash=False, before_commit=None,
                exclusive=False):
        """Copy src into the workspace at rel, atomically.

        The bytes go to a hidden ``.part`` temp file in the destination
        directory, are fsynced, and only then renamed over ``rel``; the
        directory is then fsynced where the OS supports it. That orders the
        writes but does not flush the card's own cache: eject the card before
        pulling it. A failure part-way (ENOSPC, card pulled, progress_cb raising) removes the temp
        file and leaves nothing at ``rel``. The ``.part`` suffix is not an
        audio extension, so a temp file stranded by a hard crash is never
        adopted as a track by ``rescan_playlist``.

        ``before_commit(tmp_path)``, if given, runs on the complete temp copy
        before the fsync and rename (e.g. to write tags), so the file never
        appears at ``rel`` in an unfinished state; if it raises, nothing is
        left at ``rel``. The temp path has a ``.part`` suffix, not rel's.

        ``exclusive=True`` refuses to publish over an existing file (also one
        whose name differs only in case or Unicode form): FileExistsError,
        and nothing at ``rel`` changes.

        Only timestamps are carried over from src — never permission bits or
        BSD flags, which would make the copy read-only (un-taggable) or fail
        outright on FAT32 — and never extended
        attributes: on FAT32 macOS stores those in a ``._`` AppleDouble file,
        which is removed once the copy is published.
        """
        dest = self._resolve(rel)
        self._mkdirs(dest.parent)
        src = Path(src)
        st = src.stat()
        total = st.st_size
        h = hashlib.blake2b() if compute_hash else None
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".echolist-copy-",
                                   suffix=".part")
        try:
            with open(src, "rb") as fsrc, os.fdopen(fd, "wb") as fdst:
                fd = -1  # now owned by fdst
                copied = 0
                while True:
                    buf = fsrc.read(1024 * 256)
                    if not buf:
                        break
                    fdst.write(buf)
                    if h:
                        h.update(buf)
                    copied += len(buf)
                    if progress_cb:
                        progress_cb(copied, total)
                fdst.flush()
                if before_commit is None:
                    os.fsync(fdst.fileno())
            if before_commit is not None:
                before_commit(Path(tmp))
                _fsync_path(tmp)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            if exclusive and _occupied(dest):
                raise FileExistsError(errno.EEXIST, "refusing to overwrite", str(dest))
            os.replace(tmp, dest)
        except BaseException:
            _discard_temp(fd, tmp)
            raise
        _fsync_dir(dest.parent)
        self._drop_sidecar(dest)
        return dest, h.hexdigest() if h else None

    def edit_in_place(self, rel, edit):
        """Run ``edit(path)`` on an existing workspace file (e.g. a tag write).

        On FAT32 macOS re-creates the file's ``._`` AppleDouble sidecar
        whenever it is opened for writing; it is removed afterwards.
        """
        p = self._resolve(rel)
        result = edit(p)
        self._drop_sidecar(p)
        return result

    def open_lock(self, rel) -> int:
        """Open (creating if needed) the workspace lock file; returns an fd.

        The caller owns the descriptor and takes the lock on it. Errors from
        the card itself (read-only, I/O) propagate unchanged.
        """
        p = self._resolve(rel)
        self._mkdirs(p.parent)
        fd = os.open(str(p), os.O_CREAT | os.O_RDWR)
        self._drop_sidecar(p)
        return fd

    def delete(self, rel) -> None:
        p = self._resolve(rel)
        if p == self.root:
            raise UnsafeWriteError("refusing to delete workspace root")
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        else:
            return
        # The kernel removes a FAT32 ._ sidecar together with its file.
        _fsync_dir(p.parent)

    def rename(self, old_rel, new_rel) -> Path:
        old = self._resolve(old_rel)
        new = self._resolve(new_rel)
        self._mkdirs(new.parent)
        old.rename(new)  # the kernel renames a FAT32 ._ sidecar along with it
        _fsync_dir(new.parent)
        if old.parent != new.parent:
            _fsync_dir(old.parent)
        return new

    def resolve(self, rel) -> Path:
        return self._resolve(rel)

    def _drop_sidecar(self, p: Path) -> None:
        """Remove the ``._<name>`` AppleDouble file of a path this writer
        just created or wrote.

        macOS keeps extended attributes of files on FAT32 in such sidecars;
        it attaches ``com.apple.provenance`` to every file and folder an app
        creates, and that attribute cannot be removed — but the sidecar can,
        which drops it. The kernel already deletes and renames sidecars with
        their files, so only creation and in-place writes need this. On the
        player the sidecars are junk files that count toward its file limit.
        """
        if p.name.startswith("._"):
            return
        sidecar = p.with_name("._" + p.name)
        try:
            if not sidecar.is_file() or sidecar.is_symlink():
                return
        except OSError:
            return
        os.unlink(sidecar)


def _fsync_path(path) -> None:
    # O_RDWR: Windows' fsync (_commit) needs a writable handle.
    fd = os.open(path, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard_temp(fd: int, tmp) -> None:
    """Close and remove a temp file while another exception propagates.

    Cleanup errors (the card was pulled, it went read-only) are dropped so
    they never replace the error that caused the cleanup. A ``.part`` temp
    left behind is swept the next time the workspace is opened.
    """
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        os.unlink(tmp)
    except OSError:
        pass


# fsync on a directory is not supported everywhere (some network and FUSE
# filesystems return EINVAL/ENOTSUP); Windows cannot open a directory at all.
_DIR_FSYNC_UNSUPPORTED = {e for e in (errno.EINVAL, errno.ENOTSUP,
                                      getattr(errno, "EOPNOTSUPP", None)) if e}


def _fsync_dir(path) -> None:
    """Ask the OS to flush a directory's entries (a create or rename in it)
    to the device, where the filesystem supports directory fsync. This
    orders writes; it does not flush the card's own cache. Unsupported
    (EINVAL/ENOTSUP) is ignored; real I/O errors propagate."""
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in _DIR_FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(fd)


def _name_key(name: str) -> str:
    """How FAT32 and macOS compare names: case- and normalization-blind."""
    return unicodedata.normalize("NFC", name).casefold()


def _occupied(p: Path) -> bool:
    """True if p, or a sibling FAT32/macOS would treat as the same name, exists."""
    if os.path.lexists(p):
        return True
    if not p.parent.is_dir():
        return False
    key = _name_key(p.name)
    return any(_name_key(e.name) == key for e in os.scandir(p.parent))


def _write_all(fd: int, data: bytes) -> None:
    """os.write may write fewer bytes than asked; loop until all are written."""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _atomic_write(dest: Path, data: bytes) -> None:
    """Write data to dest atomically: temp file + fsync + os.replace, then
    fsync the directory where supported (see SafeWriter.copy_in)."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, dest)
    except BaseException:
        _discard_temp(fd, tmp)
        raise
    _fsync_dir(dest.parent)


def atomic_write_text(dest: Path, text: str) -> None:
    """Write text to dest atomically. Safe for use outside the workspace."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(dest, text.encode("utf-8"))
