"""M1 guard tests — SafeWriter must never write outside its root."""

import pytest
from echolist.safe_write import SafeWriter, UnsafeWriteError


def test_write_bytes_inside(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    p = w.write_bytes("a/b.txt", b"hello")
    assert p.exists()
    assert p.read_bytes() == b"hello"


def test_write_text_inside(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    p = w.write_text("note.txt", "content")
    assert p.read_text(encoding="utf-8") == "content"


def test_absolute_outside_raises(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    outside = tmp_path / "outside.txt"
    with pytest.raises(UnsafeWriteError):
        w.write_bytes(str(outside), b"bad")
    assert not outside.exists()


def test_traversal_raises(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    with pytest.raises(UnsafeWriteError):
        w.write_bytes("../escape.txt", b"bad")
    assert not (tmp_path / "escape.txt").exists()


def test_copy_in_traversal_raises(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    src = tmp_path / "src.txt"
    src.write_bytes(b"data")
    with pytest.raises(UnsafeWriteError):
        w.copy_in(src, "../x")
    assert not (tmp_path / "x").exists()


def test_delete_outside_raises(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    external = tmp_path / "precious.txt"
    external.write_bytes(b"important")
    with pytest.raises(UnsafeWriteError):
        w.delete("../../precious.txt")
    assert external.exists()
    assert external.read_bytes() == b"important"


def test_delete_root_raises(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    with pytest.raises(UnsafeWriteError):
        w.delete("")


def test_makedirs_inside(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    p = w.makedirs("deep/nested/dir")
    assert p.is_dir()


def test_copy_in_inside(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    src = tmp_path / "source.dat"
    src.write_bytes(b"payload")
    dest, _ = w.copy_in(src, "imported/file.dat")
    assert dest.exists()
    assert dest.read_bytes() == b"payload"


def test_delete_file_inside(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    w.write_bytes("to_delete.txt", b"bye")
    w.delete("to_delete.txt")
    assert not (w.root / "to_delete.txt").exists()


def test_delete_dir_inside(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    w.write_bytes("dir/file.txt", b"x")
    w.delete("dir")
    assert not (w.root / "dir").exists()


def test_rename_inside(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    w.write_bytes("old.txt", b"data")
    p = w.rename("old.txt", "new.txt")
    assert p.exists()
    assert p.read_bytes() == b"data"
    assert not (w.root / "old.txt").exists()


def test_rename_dir_inside(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    w.write_bytes("olddir/file.txt", b"x")
    w.rename("olddir", "newdir")
    assert (w.root / "newdir" / "file.txt").exists()
    assert not (w.root / "olddir").exists()


def test_rename_outside_raises(tmp_path):
    w = SafeWriter(tmp_path / "ws")
    w.write_bytes("inside.txt", b"data")
    with pytest.raises(UnsafeWriteError):
        w.rename("inside.txt", "../../escaped.txt")
    assert (w.root / "inside.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
