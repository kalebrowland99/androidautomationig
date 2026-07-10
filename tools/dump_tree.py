#!/usr/bin/env python3
"""Dump the on-screen UI element tree from a connected Android device."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import uiautomator2 as u2

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from android_devices import (  # noqa: E402
    Device,
    list_devices,
    pick_device_macos,
    pick_device_terminal,
    resolve_adb,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DUMP_ROOT = PROJECT_ROOT / "dump"


def wait_for_ready_macos() -> bool:
    script = (
        'display dialog "Navigate to the screen you want on the phone, '
        'then click Dump." buttons {"Cancel", "Dump"} default button "Dump"'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return "Dump" in result.stdout


def wait_for_ready_terminal() -> bool:
    print(
        "\nOn the phone, open the app/screen you want to inspect, "
        "then press Enter here to dump."
    )
    print("Type q to cancel.\n")
    choice = input("Ready to dump? [Enter/q] ").strip().lower()
    return choice not in {"q", "quit", "exit"}


def summarize_hierarchy(hierarchy_path: Path, output_path: Path) -> int:
    tree = ET.parse(hierarchy_path)
    lines: list[str] = []
    for node in tree.iter("node"):
        attrs = node.attrib
        text = attrs.get("text", "").strip()
        resource_id = attrs.get("resource-id", "").strip()
        content_desc = attrs.get("content-desc", "").strip()
        class_name = attrs.get("class", "").split(".")[-1]
        clickable = attrs.get("clickable", "false") == "true"
        checked = attrs.get("checked", "false") == "true"
        enabled = attrs.get("enabled", "true") == "true"
        bounds = attrs.get("bounds", "")

        if not any([text, resource_id, content_desc, clickable, checked]):
            continue

        parts: list[str] = []
        if text:
            parts.append(f'text="{text}"')
        if resource_id:
            parts.append(f'id="{resource_id}"')
        if content_desc:
            parts.append(f'desc="{content_desc}"')
        if class_name:
            parts.append(f"class={class_name}")
        flags = []
        if clickable:
            flags.append("clickable")
        if checked:
            flags.append("checked")
        if not enabled:
            flags.append("disabled")
        if flags:
            parts.append(",".join(flags))
        if bounds:
            parts.append(f"bounds={bounds}")
        lines.append(" | ".join(parts))

    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def write_app_info(device: u2.Device, output_path: Path) -> None:
    current = device.app_current()
    lines = [
        f"package: {current.get('package', '')}",
        f"activity: {current.get('activity', '')}",
        f"pid: {current.get('pid', '')}",
    ]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dump_screen(device: u2.Device, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    hierarchy_path = output_dir / "hierarchy.xml"
    hierarchy_path.write_text(device.dump_hierarchy(), encoding="utf-8")
    device.screenshot(str(output_dir / "screenshot.png"))
    write_app_info(device, output_dir / "app-info.txt")
    element_count = summarize_hierarchy(hierarchy_path, output_dir / "elements.txt")

    readme = f"""UI dump created: {datetime.now().isoformat(timespec='seconds')}

Files:
  screenshot.png  - what was on screen
  hierarchy.xml   - raw Android UI tree (use this for exact selectors)
  elements.txt    - flattened list of useful elements ({element_count} rows)
  app-info.txt    - package/activity of the foreground app

Tips:
  - Search elements.txt for button text, resource-id, or content-desc values.
  - Use hierarchy.xml when you need parent/child relationships.
  - Re-run Dump Tree.command after navigating to a new screen.
"""
    (output_dir / "README.txt").write_text(readme, encoding="utf-8")
    archive_path = shutil.make_archive(str(output_dir), "zip", root_dir=output_dir)
    return Path(archive_path)


def pick_device(
    adb: str,
    *,
    use_macos_picker: bool,
    serial: str | None,
) -> Device | None:
    if serial:
        devices = list_devices(adb)
        for device in devices:
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
                title="Dump UI Tree",
                prompt="Select a device to inspect:",
            )
            if picked is None:
                return None
            return picked

        action, picked = pick_device_terminal(devices, action_label="dump")
        if action == "quit":
            return None
        if action == "refresh":
            continue
        return picked


def connect_device(serial: str) -> u2.Device:
    try:
        return u2.connect(serial)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Could not connect to {serial}. "
            f"Try running: python -m uiautomator2 init --serial {serial}"
        ) from exc


def open_output(path: Path) -> None:
    if platform.system() == "Darwin":
        subprocess.run(["open", str(path)], check=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump the current Android screen UI element tree.",
    )
    parser.add_argument(
        "--terminal",
        action="store_true",
        help="Use terminal menus instead of macOS dialogs.",
    )
    parser.add_argument(
        "--device",
        help="Device serial to use (skips the picker).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the dump folder in Finder when finished.",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Dump immediately without waiting for confirmation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    use_macos_picker = platform.system() == "Darwin" and not args.terminal

    try:
        adb = resolve_adb()
        os.environ["ADB"] = adb
        device_info = pick_device(
            adb,
            use_macos_picker=use_macos_picker,
            serial=args.device,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if device_info is None:
        return 0

    if args.now:
        pass
    elif use_macos_picker:
        if not wait_for_ready_macos():
            print("Dump cancelled.")
            return 0
    elif not wait_for_ready_terminal():
        print("Dump cancelled.")
        return 0

    try:
        device = connect_device(device_info.serial)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = DUMP_ROOT / f"{timestamp}_{device_info.serial}"
    try:
        archive_path = dump_screen(device, output_dir)
    except Exception as exc:  # noqa: BLE001 - show dump failures clearly
        print(f"Dump failed: {exc}", file=sys.stderr)
        return 1

    current = device.app_current()
    print("\nUI dump saved.")
    print(f"Device:  {device_info.label}")
    print(f"App:     {current.get('package', 'unknown')}")
    print(f"Folder:  {output_dir}")
    print(f"Zip:     {archive_path}")
    print(f"Elements: {output_dir / 'elements.txt'}")

    if not args.no_open:
        open_output(output_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
