#!/usr/bin/env python3
"""List connected Android devices and stream one with scrcpy."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
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


def resolve_scrcpy() -> str:
    if env_scrcpy := os.environ.get("SCRCPY"):
        return env_scrcpy
    if shutil.which("scrcpy"):
        return "scrcpy"
    arch = platform.machine()
    project_root = Path(__file__).resolve().parent.parent
    candidates = [
        project_root / f"tools/scrcpy-macos-{arch}/scrcpy",
        project_root / "tools/scrcpy-macos-x86_64-v3.3.4/scrcpy",
        project_root / "tools/scrcpy-macos-aarch64-v3.3.4/scrcpy",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    bundled = next(project_root.glob("tools/scrcpy-*/scrcpy"), None)
    if bundled and bundled.is_file():
        return str(bundled)
    raise FileNotFoundError(
        "scrcpy not found. Download it into tools/ or set SCRCPY."
    )


def stream_device(adb: str, scrcpy: str, device: Device) -> None:
    env = os.environ.copy()
    env["ADB"] = adb
    handle = None
    project_root = Path(__file__).resolve().parent.parent
    try:
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        from dashboard.gramaddict_config import username_for_device

        handle = username_for_device(device.serial)
    except ImportError:
        pass
    title = scrcpy_window_title(device.serial, handle)
    args = [
        scrcpy,
        "-s",
        device.serial,
        "--window-title",
        title,
        "--max-size",
        "1024",
    ]
    print(f"Streaming {title}")
    subprocess.Popen(args, env=env)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pick an Android device and stream it.")
    parser.add_argument(
        "--terminal",
        action="store_true",
        help="Use a numbered terminal menu instead of the macOS picker dialog.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        adb = resolve_adb()
        scrcpy = resolve_scrcpy()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    use_macos_picker = platform.system() == "Darwin" and not args.terminal

    while True:
        try:
            devices = list_devices(adb)
        except Exception as exc:  # noqa: BLE001 - surface adb errors to the user
            print(f"Failed to list devices: {exc}", file=sys.stderr)
            return 1

        if not devices:
            print("No devices found. Connect a phone and enable USB debugging.")
            if not use_macos_picker:
                again = input("Refresh now? [y/N] ").strip().lower()
                if again == "y":
                    continue
            return 1

        if use_macos_picker:
            try:
                picked = pick_device_macos(
                    devices,
                    title="Android Device Streamer",
                    prompt="Select a device to stream:",
                )
            except RuntimeError as exc:
                print(exc, file=sys.stderr)
                return 1
            if picked is None:
                return 0
        else:
            action, picked = pick_device_terminal(devices, action_label="stream")
            if action == "quit":
                return 0
            if action == "refresh":
                continue

        stream_device(adb, scrcpy, picked)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
