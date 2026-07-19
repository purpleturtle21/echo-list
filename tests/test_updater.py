"""Tests for the auto-updater (echolist/updater.py).

This module can download a real binary from GitHub and replace the running
process via os.execv — every test here mocks urlopen/os.execv/subprocess so
nothing ever touches the real network or actually execs anything.
"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

from echolist import updater


class _ImmediateThread:
    """Stand-in for threading.Thread that runs the target synchronously,
    so tests don't need polling/timeouts to observe the callback result."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture(autouse=True)
def _run_threads_synchronously(monkeypatch):
    monkeypatch.setattr(updater, "Thread", _ImmediateThread)


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release_json(tag: str, assets=None, html_url="https://github.com/x/x/releases/tag/v9.9.9") -> bytes:
    return json.dumps({
        "tag_name": tag,
        "html_url": html_url,
        "assets": assets or [],
    }).encode()


# ── _version_newer ──

@pytest.mark.parametrize("latest,current,expected", [
    ("1.0.0", "0.9.9", True),
    ("0.4.0", "0.3.9", True),
    ("0.3.1", "0.3.0", True),
    ("0.3.0", "0.3.0", False),
    ("0.2.0", "0.3.0", False),
])
def test_version_newer(latest, current, expected):
    assert updater._version_newer(latest, current) is expected


def test_version_newer_malformed_returns_false():
    assert updater._version_newer("not-a-version", "0.3.0") is False
    assert updater._version_newer("0.3.0", "also-bad") is False


# ── _platform_asset_name ──

@pytest.mark.parametrize("system,expected", [
    ("Windows", "echolist-windows.exe"),
    ("Darwin", "echolist-macos-arm64"),
    ("Linux", "echolist-linux"),
])
def test_platform_asset_name(monkeypatch, system, expected):
    monkeypatch.setattr(platform, "system", lambda: system)
    assert updater._platform_asset_name() == expected


# ── check_for_update ──

class TestCheckForUpdate:
    def test_newer_version_available_with_matching_asset(self, monkeypatch):
        body = _release_json("v9.9.9", assets=[
            {"name": "echolist-linux", "browser_download_url": "https://x/echolist-linux"},
        ])
        monkeypatch.setattr(updater, "urlopen", lambda req, timeout=10: _FakeResponse(body))
        monkeypatch.setattr(platform, "system", lambda: "Linux")

        calls = []
        updater.check_for_update(on_update_available=lambda *a: calls.append(a))

        assert calls == [("9.9.9", "https://x/echolist-linux", "https://github.com/x/x/releases/tag/v9.9.9")]

    def test_newer_version_without_matching_asset(self, monkeypatch):
        body = _release_json("v9.9.9", assets=[
            {"name": "some-other-file", "browser_download_url": "https://x/other"},
        ])
        monkeypatch.setattr(updater, "urlopen", lambda req, timeout=10: _FakeResponse(body))
        monkeypatch.setattr(platform, "system", lambda: "Linux")

        calls = []
        updater.check_for_update(on_update_available=lambda *a: calls.append(a))

        assert len(calls) == 1
        assert calls[0][1] is None  # no download_url for this platform

    def test_no_update_when_current(self, monkeypatch):
        body = _release_json(f"v{updater.__version__}")
        monkeypatch.setattr(updater, "urlopen", lambda req, timeout=10: _FakeResponse(body))

        no_update_called = []
        update_available_called = []
        updater.check_for_update(
            on_no_update=lambda: no_update_called.append(True),
            on_update_available=lambda *a: update_available_called.append(a),
        )
        assert no_update_called == [True]
        assert update_available_called == []

    def test_no_update_when_current_is_newer(self, monkeypatch):
        body = _release_json("v0.0.1")
        monkeypatch.setattr(updater, "urlopen", lambda req, timeout=10: _FakeResponse(body))

        no_update_called = []
        updater.check_for_update(on_no_update=lambda: no_update_called.append(True))
        assert no_update_called == [True]

    def test_network_error_calls_on_error(self, monkeypatch):
        def _raise(*a, **k):
            raise URLError("no network")
        monkeypatch.setattr(updater, "urlopen", _raise)

        errors = []
        updater.check_for_update(on_error=errors.append)
        assert errors == ["Could not check for updates"]

    def test_malformed_json_calls_on_error(self, monkeypatch):
        monkeypatch.setattr(updater, "urlopen", lambda req, timeout=10: _FakeResponse(b"not json"))

        errors = []
        updater.check_for_update(on_error=errors.append)
        assert errors == ["Could not check for updates"]

    def test_missing_tag_name_is_silently_ignored(self, monkeypatch):
        body = _release_json("")
        monkeypatch.setattr(updater, "urlopen", lambda req, timeout=10: _FakeResponse(body))

        no_update = []
        errors = []
        updater.check_for_update(on_no_update=lambda: no_update.append(True), on_error=errors.append)
        assert no_update == []
        assert errors == []


# ── download_and_replace ──

class TestDownloadAndReplace:
    def test_downloads_and_writes_new_exe(self, tmp_path, monkeypatch):
        fake_exe = tmp_path / "echolist.exe"
        fake_exe.write_bytes(b"old binary")
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        payload = b"NEW BINARY CONTENT"
        monkeypatch.setattr(
            updater, "urlopen",
            lambda req, timeout=120: _FakeResponse(payload, headers={"Content-Length": str(len(payload))}),
        )

        progress = []
        done = []
        updater.download_and_replace(
            "https://x/echolist.exe",
            on_progress=progress.append,
            on_done=lambda cur, new: done.append((cur, new)),
        )

        assert len(done) == 1
        current_exe, new_exe = done[0]
        assert current_exe == str(fake_exe.resolve())
        assert Path(new_exe).name == "echolist.new.exe"
        assert Path(new_exe).read_bytes() == payload
        assert any("Downloading" in m for m in progress)
        assert any("%" in m for m in progress)  # percentage progress reported

    def test_network_error_calls_on_error_not_on_done(self, tmp_path, monkeypatch):
        fake_exe = tmp_path / "echolist.exe"
        fake_exe.write_bytes(b"x")
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        def _raise(*a, **k):
            raise OSError("connection reset")
        monkeypatch.setattr(updater, "urlopen", _raise)

        errors = []
        done = []
        updater.download_and_replace("https://x", on_error=errors.append, on_done=lambda *a: done.append(a))

        assert done == []
        assert len(errors) == 1
        assert "Update failed" in errors[0]
        assert not (tmp_path / "echolist.new.exe").exists()


# ── apply_update_and_restart ──

class TestApplyUpdateAndRestart:
    def test_posix_renames_chmods_and_execs(self, tmp_path, monkeypatch):
        """Regression: new.chmod() used to run on the OLD `new` Path object
        AFTER it had already been renamed onto `current` — the file at that
        path no longer existed, so this raised FileNotFoundError before ever
        reaching os.execv. The update-and-restart flow was broken on every
        non-Windows platform."""
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        current = tmp_path / "echolist"
        new = tmp_path / "echolist.new.exe"
        current.write_bytes(b"old")
        new.write_bytes(b"new")

        exec_calls = []
        monkeypatch.setattr(updater.os, "execv", lambda path, args: exec_calls.append((path, args)))

        updater.apply_update_and_restart(str(current), str(new))

        assert not new.exists()
        assert current.read_bytes() == b"new"
        assert exec_calls == [(str(current), [str(current)])]

    def test_windows_backs_up_old_and_execs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        current = tmp_path / "echolist.exe"
        new = tmp_path / "echolist.new.exe"
        current.write_bytes(b"old")
        new.write_bytes(b"new")

        exec_calls = []
        monkeypatch.setattr(updater.os, "execv", lambda path, args: exec_calls.append((path, args)))

        updater.apply_update_and_restart(str(current), str(new))

        old = tmp_path / "echolist.old.exe"
        assert old.read_bytes() == b"old"
        assert current.read_bytes() == b"new"
        assert not new.exists()
        assert exec_calls == [(str(current), [str(current)])]

    def test_windows_removes_stale_old_exe_first(self, tmp_path, monkeypatch):
        """A crashed update from a previous run can leave .old.exe behind —
        must not fail trying to overwrite it."""
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        current = tmp_path / "echolist.exe"
        new = tmp_path / "echolist.new.exe"
        old = tmp_path / "echolist.old.exe"
        current.write_bytes(b"old")
        new.write_bytes(b"new")
        old.write_bytes(b"stale leftover")

        monkeypatch.setattr(updater.os, "execv", lambda path, args: None)

        updater.apply_update_and_restart(str(current), str(new))
        assert old.read_bytes() == b"old"

    def test_windows_rename_failure_raises_runtime_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        current = tmp_path / "echolist.exe"
        new = tmp_path / "echolist.new.exe"
        current.write_bytes(b"old")
        # new deliberately not created -> rename fails

        exec_calls = []
        monkeypatch.setattr(updater.os, "execv", lambda path, args: exec_calls.append((path, args)))

        with pytest.raises(RuntimeError, match="Could not replace executable"):
            updater.apply_update_and_restart(str(current), str(new))
        assert exec_calls == []


# ── cleanup_old_exe ──

class TestCleanupOldExe:
    def test_removes_old_exe_when_frozen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        exe = tmp_path / "echolist.exe"
        exe.write_bytes(b"current")
        old = tmp_path / "echolist.old.exe"
        old.write_bytes(b"stale")
        monkeypatch.setattr(sys, "executable", str(exe))

        updater.cleanup_old_exe()
        assert not old.exists()

    def test_noop_when_not_frozen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        exe = tmp_path / "echolist.exe"
        old = tmp_path / "echolist.old.exe"
        old.write_bytes(b"stale")
        monkeypatch.setattr(sys, "executable", str(exe))

        updater.cleanup_old_exe()
        assert old.exists()  # untouched — not running from a frozen build

    def test_noop_when_nothing_to_clean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        exe = tmp_path / "echolist.exe"
        monkeypatch.setattr(sys, "executable", str(exe))
        updater.cleanup_old_exe()  # must not raise
