"""Auto-update and package install utilities for EchoList."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Thread
from urllib.request import urlopen, Request
from urllib.error import URLError

__version__ = "0.0.1"

GITHUB_REPO = "purpleturtle21/echo-list"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

USER_PACKAGES = Path.home() / ".echolist" / "packages"


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _platform_asset_name() -> str:
    import platform
    system = platform.system()
    if system == "Windows":
        return "echolist-windows.exe"
    elif system == "Darwin":
        return "echolist-macos-arm64"
    else:
        return "echolist-linux"


# ── User-local package install ──

def ensure_user_packages_on_path():
    """Add ~/.echolist/packages to sys.path so user-installed packages are found."""
    site_dir = str(USER_PACKAGES)
    if site_dir not in sys.path:
        sys.path.insert(0, site_dir)


def install_package(package_name: str, on_progress=None, on_done=None, on_error=None):
    """Install a pip package into ~/.echolist/packages/ in a background thread."""
    def worker():
        USER_PACKAGES.mkdir(parents=True, exist_ok=True)
        if on_progress:
            on_progress(f"Installing {package_name}...")
        try:
            python = sys.executable
            if _is_frozen():
                python = _find_system_python()
                if not python:
                    if on_error:
                        on_error(
                            "Python not found. Please install Python 3.9+ from "
                            "python.org and try again."
                        )
                    return

            cmd = [
                python, "-m", "pip", "install",
                "--target", str(USER_PACKAGES),
                "--upgrade",
                package_name,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip()
                if on_error:
                    on_error(f"pip install failed:\n{err[:500]}")
                return

            ensure_user_packages_on_path()
            if on_progress:
                on_progress(f"{package_name} installed successfully")
            if on_done:
                on_done()
        except subprocess.TimeoutExpired:
            if on_error:
                on_error("Installation timed out after 5 minutes")
        except Exception as e:
            if on_error:
                on_error(str(e))

    Thread(target=worker, daemon=True).start()


def _find_system_python() -> str | None:
    """Find a working system Python when running from a frozen .exe."""
    candidates = ["python3", "python"]
    import platform
    if platform.system() == "Windows":
        candidates = ["python", "python3", "py"]
    for name in candidates:
        try:
            result = subprocess.run(
                [name, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and "Python 3" in result.stdout:
                return name
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


# ── Auto-update ──

def check_for_update(on_update_available=None, on_no_update=None, on_error=None):
    """Check GitHub Releases for a newer version. Runs in a background thread."""
    def worker():
        try:
            req = Request(GITHUB_API_LATEST, headers={"Accept": "application/json"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            latest_tag = data.get("tag_name", "")
            latest_ver = latest_tag.lstrip("v")
            if not latest_ver:
                return

            if _version_newer(latest_ver, __version__):
                download_url = None
                asset_name = _platform_asset_name()
                for asset in data.get("assets", []):
                    if asset["name"] == asset_name:
                        download_url = asset["browser_download_url"]
                        break
                if on_update_available:
                    on_update_available(latest_ver, download_url, data.get("html_url", ""))
            else:
                if on_no_update:
                    on_no_update()
        except (URLError, OSError, json.JSONDecodeError):
            if on_error:
                on_error("Could not check for updates")

    Thread(target=worker, daemon=True).start()


def _version_newer(latest: str, current: str) -> bool:
    """Returns True if latest > current using simple tuple comparison."""
    try:
        l_parts = tuple(int(x) for x in latest.split("."))
        c_parts = tuple(int(x) for x in current.split("."))
        return l_parts > c_parts
    except (ValueError, AttributeError):
        return False


def download_and_replace(download_url: str, on_progress=None, on_done=None, on_error=None):
    """Download the new .exe and set up replacement on next launch."""
    def worker():
        try:
            if on_progress:
                on_progress("Downloading update...")

            req = Request(download_url)
            with urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                data = bytearray()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if on_progress and total:
                        pct = len(data) * 100 // total
                        on_progress(f"Downloading... {pct}%")

            current_exe = Path(sys.executable).resolve()
            new_exe = current_exe.with_suffix(".new.exe")
            new_exe.write_bytes(data)

            if on_progress:
                on_progress("Update downloaded. Restarting...")
            if on_done:
                on_done(str(current_exe), str(new_exe))
        except Exception as e:
            if on_error:
                on_error(f"Update failed: {e}")

    Thread(target=worker, daemon=True).start()


def apply_update_and_restart(current_exe: str, new_exe: str):
    """Replace the running .exe with the new one and restart.

    On Windows we can't overwrite the running exe, so we:
    1. Rename current → .old.exe
    2. Rename new → current name
    3. Launch the new exe
    4. Exit this process
    The .old.exe is cleaned up on next launch.
    """
    import platform
    current = Path(current_exe)
    new = Path(new_exe)
    old = current.with_suffix(".old.exe")

    if platform.system() == "Windows":
        try:
            if old.exists():
                old.unlink()
            current.rename(old)
            new.rename(current)
        except OSError as e:
            raise RuntimeError(f"Could not replace executable: {e}")
        os.execv(str(current), [str(current)])
    else:
        new.rename(current)
        current.chmod(0o755)
        os.execv(str(current), [str(current)])


def cleanup_old_exe():
    """Remove leftover .old.exe from a previous update."""
    if not _is_frozen():
        return
    try:
        old = Path(sys.executable).resolve().with_suffix(".old.exe")
        if old.exists():
            old.unlink()
    except OSError:
        pass
