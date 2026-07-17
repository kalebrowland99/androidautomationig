"""Shared helpers for listing and picking connected Android devices."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_ADB = Path(
    "/Users/kalebrowland/Desktop/ig-navigator/tools/platform-tools/adb"
)


@dataclass
class Device:
    serial: str
    model: str = ""
    manufacturer: str = ""
    hardware_id: str = ""

    @property
    def label(self) -> str:
        details = " ".join(part for part in (self.manufacturer, self.model) if part)
        return f"{self.serial}  ({details})" if details else self.serial


def short_serial(serial: str) -> str:
    serial = serial.strip()
    if not serial:
        return "?"
    return serial if len(serial) <= 4 else serial[-4:]


def scrcpy_window_title(serial: str, username: str | None = None) -> str:
    """Window title: @handle + last 4 of serial (e.g. @myuser 1a2b)."""
    tail = short_serial(serial)
    handle = (username or "").strip().lstrip("@")
    title = f"@{handle} {tail}" if handle else tail
    cleaned = re.sub(r"[^\w\s@.-]", "", title).strip()
    return (cleaned[:60] if cleaned else tail)


def resolve_adb() -> str:
    if env_adb := os.environ.get("ADB"):
        return env_adb
    if shutil.which("adb"):
        return "adb"
    if FALLBACK_ADB.is_file():
        return str(FALLBACK_ADB)
    bundled = next(PROJECT_ROOT.glob("tools/scrcpy-*/adb"), None)
    if bundled and bundled.is_file():
        return str(bundled)
    raise FileNotFoundError("adb not found. Install platform-tools or set ADB.")


def run_command(args: list[str], timeout: int = 10) -> str:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def list_device_serials(adb: str) -> list[str]:
    """Fast: adb devices only (no per-device getprop)."""
    output = run_command([adb, "devices"], timeout=5)
    serials: list[str] = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def serial_matches_filter(serial: str, pattern: str) -> bool:
    pattern = (pattern or "").strip()
    if not pattern:
        return True
    return serial.endswith(pattern)


def get_hardware_id(adb: str, serial: str) -> str:
    """Stable per-phone id that survives ADB serial / wireless IP changes.

    Uses the hardware serial number (ro.serialno / ro.boot.serialno); falls back
    to the Android secure id. Returns "" if none can be read.
    """
    getprops = ("ro.serialno", "ro.boot.serialno")
    for prop in getprops:
        try:
            value = run_command(
                [adb, "-s", serial, "shell", "getprop", prop], timeout=3
            ).strip()
        except (RuntimeError, subprocess.TimeoutExpired):
            continue
        if value:
            return value
    try:
        value = run_command(
            [adb, "-s", serial, "shell", "settings", "get", "secure", "android_id"],
            timeout=3,
        ).strip()
    except (RuntimeError, subprocess.TimeoutExpired):
        value = ""
    return value if value and value.lower() != "null" else ""


def list_devices(
    adb: str,
    *,
    serial_filter: str | None = None,
    include_props: bool = True,
) -> list[Device]:
    serials = list_device_serials(adb)
    if serial_filter:
        serials = [s for s in serials if serial_matches_filter(s, serial_filter)]

    devices: list[Device] = []
    for serial in serials:
        model = ""
        manufacturer = ""
        hardware_id = ""
        if include_props:
            try:
                model = run_command(
                    [adb, "-s", serial, "shell", "getprop", "ro.product.model"],
                    timeout=3,
                ).strip()
                manufacturer = run_command(
                    [adb, "-s", serial, "shell", "getprop", "ro.product.manufacturer"],
                    timeout=3,
                ).strip()
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            hardware_id = get_hardware_id(adb, serial)
        devices.append(
            Device(
                serial=serial,
                model=model,
                manufacturer=manufacturer,
                hardware_id=hardware_id,
            )
        )
    return devices


def pick_device_macos(
    devices: list[Device],
    *,
    title: str = "Android Devices",
    prompt: str = "Select a device:",
) -> Device | None:
    escaped_labels = [
        device.label.replace("\\", "\\\\").replace('"', '\\"') for device in devices
    ]
    list_items = ", ".join(f'"{label}"' for label in escaped_labels)
    script = f'''
set deviceList to {{{list_items}}}
set picked to choose from list deviceList with title "{title}" with prompt "{prompt}" default items (item 1 of deviceList)
if picked is false then
    return ""
end if
return item 1 of picked
'''.strip()
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Device picker cancelled.")
    picked = result.stdout.strip()
    if not picked:
        return None
    for device in devices:
        if device.label == picked:
            return device
    raise RuntimeError(f"Unknown device selected: {picked}")


def pick_device_terminal(
    devices: list[Device],
    *,
    action_label: str = "select",
) -> tuple[str, Device | None]:
    print("\nConnected Android devices:\n")
    for index, device in enumerate(devices, start=1):
        print(f"  {index:>2}) {device.label}")
    print(f"\nCommands: number = {action_label}, r = refresh, q = quit\n")

    while True:
        choice = input("Pick a device: ").strip().lower()
        if choice in {"q", "quit", "exit"}:
            return "quit", None
        if choice in {"r", "refresh"}:
            return "refresh", None
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(devices):
                return "select", devices[index - 1]
        print("Invalid choice. Enter a number, r, or q.")
