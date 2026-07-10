"""Weditor subprocess + device connection for the dashboard UI inspector."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
WEDITOR_PORT = int(os.environ.get("WEDITOR_PORT", "17310"))
WEDITOR_PATCH_MARKER = "<!-- device-lab-serial-autofill -->"

_proc: subprocess.Popen[str] | None = None


def _tools_on_path() -> None:
    tools = str(TOOLS_DIR)
    if tools not in sys.path:
        sys.path.insert(0, tools)


def ensure_installed() -> None:
    try:
        import weditor  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "weditor is not installed. Run: pip install -r tools/requirements.txt"
        ) from exc


def _wait_for_url(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.3)
    return False


def _patch_weditor() -> None:
    _tools_on_path()
    from weditor_patches import patch_weditor_english

    patch_weditor_english()
    _patch_weditor_serial_autofill()


def _patch_weditor_serial_autofill() -> None:
    import weditor

    index_path = Path(weditor.__file__).resolve().parent / "templates" / "index.html"
    content = index_path.read_text(encoding="utf-8")
    if WEDITOR_PATCH_MARKER in content:
        return

    patch = f"""  {WEDITOR_PATCH_MARKER}
  <script>
  (function () {{
    var params = new URLSearchParams(window.location.search);
    var serial = params.get('serial');
    var platform = params.get('platform');
    if (platform) localStorage.setItem('platform', platform);
    if (serial) localStorage.setItem('serial', serial);
  }})();
  </script>
"""
    if "</head>" not in content:
        raise RuntimeError("Could not patch weditor index.html for serial autofill.")
    index_path.write_text(content.replace("</head>", patch + "</head>", 1), encoding="utf-8")


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def status() -> dict[str, Any]:
    base = f"http://127.0.0.1:{WEDITOR_PORT}"
    alive = is_running() and _wait_for_url(f"{base}/api/v1/version", timeout=1.0)
    return {
        "running": alive,
        "port": WEDITOR_PORT,
        "base_url": base,
    }


def ensure_running() -> dict[str, Any]:
    global _proc
    ensure_installed()
    _patch_weditor()
    base = f"http://127.0.0.1:{WEDITOR_PORT}"
    if is_running() and _wait_for_url(f"{base}/api/v1/version", timeout=1.0):
        return status()
    stop()
    _proc = subprocess.Popen(
        [sys.executable, "-m", "weditor", "-q", "-p", str(WEDITOR_PORT), "-f"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_url(f"{base}/api/v1/version"):
        stop()
        raise RuntimeError("Failed to start Weditor")
    return status()


def stop() -> None:
    global _proc
    if is_running():
        subprocess.run(
            [sys.executable, "-m", "weditor", "--quit", "-p", str(WEDITOR_PORT)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _proc.terminate()
        try:
            _proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None


def weditor_url(serial: str) -> str:
    query = urllib.parse.urlencode({"serial": serial, "platform": "Android"})
    return f"http://127.0.0.1:{WEDITOR_PORT}/?{query}"


def connect_device(serial: str) -> bool:
    payload = urllib.parse.urlencode(
        {"platform": "Android", "deviceUrl": serial}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{WEDITOR_PORT}/api/v1/connect",
        data=payload,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except urllib.error.URLError:
        return False


def connect_and_url(serial: str) -> dict[str, Any]:
    ensure_running()
    connected = connect_device(serial)
    return {
        "url": weditor_url(serial),
        "connected": connected,
        **status(),
    }
