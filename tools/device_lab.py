#!/usr/bin/env python3
"""Unified device lab: pick a phone, mirror it, and inspect UI elements in one browser UI."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from android_devices import (  # noqa: E402
    Device,
    list_devices,
    pick_device_macos,
    pick_device_terminal,
    resolve_adb,
    scrcpy_window_title,
)
from weditor_patches import patch_weditor_english  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEDITOR_PORT = 17310
WEDITOR_PATCH_MARKER = "<!-- device-lab-serial-autofill -->"


def resolve_scrcpy() -> str | None:
    if env_scrcpy := os.environ.get("SCRCPY"):
        return env_scrcpy
    if shutil.which("scrcpy"):
        return "scrcpy"
    arch = platform.machine()
    candidates = [
        PROJECT_ROOT / f"tools/scrcpy-macos-{arch}/scrcpy",
        PROJECT_ROOT / "tools/scrcpy-macos-x86_64-v3.3.4/scrcpy",
        PROJECT_ROOT / "tools/scrcpy-macos-aarch64-v3.3.4/scrcpy",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    bundled = next(PROJECT_ROOT.glob("tools/scrcpy-*/scrcpy"), None)
    if bundled and bundled.is_file():
        return str(bundled)
    return None


def wait_for_url(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.3)
    return False


def _username_for_device(serial: str) -> str | None:
    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from dashboard.gramaddict_config import username_for_device

        return username_for_device(serial)
    except ImportError:
        return None


def _scrcpy_title(device: Device) -> str:
    return scrcpy_window_title(device.serial, _username_for_device(device.serial))


def scrcpy_running_for_device(serial: str) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False

    serial_pattern = re.compile(rf"(?:-s\s+{re.escape(serial)})(?:\s|$)")
    for line in result.stdout.splitlines():
        if "scrcpy" not in line:
            continue
        if serial_pattern.search(line):
            return True
    return False


def start_weditor(force: bool = True) -> subprocess.Popen[str]:
    args = [sys.executable, "-m", "weditor", "-q", "-p", str(WEDITOR_PORT)]
    if force:
        args.append("-f")
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_scrcpy(adb: str, scrcpy: str, device: Device) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["ADB"] = adb
    title = _scrcpy_title(device)
    return subprocess.Popen(
        [scrcpy, "-s", device.serial, "--window-title", title, "--max-size", "1024"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ensure_scrcpy(adb: str, scrcpy: str, device: Device) -> subprocess.Popen[str] | None:
    if scrcpy_running_for_device(device.serial):
        print(f"Reusing existing scrcpy window for {device.serial}")
        return None
    proc = start_scrcpy(adb, scrcpy, device)
    print("Phone mirror window opened (scrcpy).")
    return proc


def patch_weditor_for_serial_autofill() -> None:
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


def connect_device_to_weditor(serial: str) -> bool:
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


def weditor_url(serial: str) -> str:
    query = urllib.parse.urlencode({"serial": serial, "platform": "Android"})
    return f"http://127.0.0.1:{WEDITOR_PORT}/?{query}"


def open_weditor_for_device(serial: str) -> None:
    connect_device_to_weditor(serial)
    webbrowser.open(weditor_url(serial))


def pick_device(adb: str, *, use_macos_picker: bool, serial: str | None) -> Device | None:
    if serial:
        for device in list_devices(adb):
            if device.serial == serial:
                return device
        raise RuntimeError(f"Device not connected: {serial}")

    while True:
        devices = list_devices(adb)
        if not devices:
            raise RuntimeError("No devices found. Connect a phone and enable USB debugging.")

        if use_macos_picker:
            picked = pick_device_macos(
                devices,
                title="Device Lab",
                prompt="Select a device to inspect:",
            )
            if picked is None:
                return None
            return picked

        action, picked = pick_device_terminal(devices, action_label="open")
        if action == "quit":
            return None
        if action == "refresh":
            continue
        return picked


def ensure_weditor_installed() -> None:
    try:
        import weditor  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "weditor is not installed. Run: pip install -r tools/requirements.txt"
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified Android device lab.")
    parser.add_argument("--terminal", action="store_true", help="Use terminal device picker.")
    parser.add_argument("--device", help="Device serial (skips picker).")
    parser.add_argument("--no-scrcpy", action="store_true", help="Do not open the phone mirror window.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the inspector in a browser.")
    parser.add_argument(
        "--mirror-only",
        action="store_true",
        help="Only open the phone mirror window (no inspector).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    use_macos_picker = platform.system() == "Darwin" and not args.terminal

    try:
        adb = resolve_adb()
        os.environ["ADB"] = adb
        device = pick_device(adb, use_macos_picker=use_macos_picker, serial=args.device)
    except (FileNotFoundError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if device is None:
        return 0

    if args.mirror_only:
        scrcpy_path = resolve_scrcpy()
        if scrcpy_path is None:
            print("scrcpy not found.", file=sys.stderr)
            return 1
        if scrcpy_running_for_device(device.serial):
            print(f"scrcpy already running for {device.label}")
            return 0
        print(f"Streaming {device.label}")
        return subprocess.call(
            [scrcpy_path, "-s", device.serial, "--max-size", "1024"],
            env={**os.environ, "ADB": adb},
        )

    ensure_weditor_installed()
    patch_weditor_for_serial_autofill()
    patch_weditor_english()

    scrcpy_path = None if args.no_scrcpy else resolve_scrcpy()
    if not args.no_scrcpy and scrcpy_path is None:
        print("Warning: scrcpy not found. Inspector will still open without a mirror window.")

    print(f"Device Lab: {device.label}")

    weditor_proc = start_weditor(force=True)
    if not wait_for_url(f"http://127.0.0.1:{WEDITOR_PORT}/api/v1/version"):
        weditor_proc.terminate()
        print("Failed to start Weditor.", file=sys.stderr)
        return 1

    scrcpy_proc = None
    if scrcpy_path:
        scrcpy_proc = ensure_scrcpy(adb, scrcpy_path, device)

    inspector_url = weditor_url(device.serial)
    if not args.no_browser:
        open_weditor_for_device(device.serial)
        print(f"Inspector: {inspector_url}")
    else:
        connect_device_to_weditor(device.serial)
        print(f"Open in browser: {inspector_url}")

    print()
    print("In the browser inspector:")
    print("  1. Serial should already be filled in — click 'Dump Hierarchy'")
    print("  2. Click elements in the tree to see their text, IDs, and selectors")
    print("  3. Use the phone mirror window to navigate, then dump again")
    print()
    print("Press Ctrl+C here to stop Device Lab.")

    try:
        while True:
            if weditor_proc.poll() is not None:
                print("Weditor stopped.")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping Device Lab...")
    finally:
        if scrcpy_proc and scrcpy_proc.poll() is None:
            scrcpy_proc.terminate()
        if weditor_proc.poll() is None:
            subprocess.run(
                [sys.executable, "-m", "weditor", "--quit", "-p", str(WEDITOR_PORT)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            weditor_proc.terminate()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
