"""Android device operations for the browser dashboard."""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import uiautomator2 as u2

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from android_devices import (  # noqa: E402
    Device,
    list_devices,
    resolve_adb,
    scrcpy_window_title,
    serial_matches_filter,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DUMP_ROOT = PROJECT_ROOT / "dump"

_connections: dict[str, u2.Device] = {}
_device_locks_guard = threading.Lock()
_devices_cache: dict[str, Any] = {"fast": [], "full": [], "fast_at": 0.0, "full_at": 0.0}
_FAST_DEVICES_TTL = 4.0
_FULL_DEVICES_TTL = 20.0
# Optional: set DASHBOARD_DEVICE_FILTER to a serial suffix to limit the dashboard (dev only).
DEVICE_SERIAL_FILTER = os.environ.get("DASHBOARD_DEVICE_FILTER", "").strip()
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_DEVICE_LOCK_TIMEOUT = float(os.environ.get("DASHBOARD_DEVICE_LOCK_TIMEOUT", "90"))


class DeviceBusyError(RuntimeError):
    """Device lock could not be acquired — usually a stuck debug run after Stop."""


_device_locks: dict[str, threading.RLock] = {}
_lock_generations: dict[str, int] = {}
_device_busy: set[str] = set()
_unblock_timers: dict[str, threading.Timer] = {}


def device_serial_allowed(serial: str) -> bool:
    return serial_matches_filter(serial, DEVICE_SERIAL_FILTER)


def require_allowed_serial(serial: str) -> None:
    if not device_serial_allowed(serial):
        hint = DEVICE_SERIAL_FILTER or "(none)"
        raise RuntimeError(
            f"Device {serial} is not enabled. Dashboard is limited to serials matching {hint!r}."
        )


def get_device_filter() -> str:
    return DEVICE_SERIAL_FILTER


def _lock_for_serial(serial: str) -> threading.RLock:
    with _device_locks_guard:
        lock = _device_locks.get(serial)
        if lock is None:
            lock = threading.RLock()
            _device_locks[serial] = lock
            _lock_generations[serial] = 0
        return lock


@contextmanager
def device_operation(serial: str, timeout: float | None = None) -> Iterator[None]:
    """Serialize uiautomator2/adb work per device. Times out instead of hanging forever."""
    require_allowed_serial(serial)
    lock = _lock_for_serial(serial)
    generation = _lock_generations.get(serial, 0)
    wait = _DEVICE_LOCK_TIMEOUT if timeout is None else timeout
    acquired = lock.acquire(timeout=wait)
    if not acquired:
        raise DeviceBusyError(
            f"Device {serial} is still busy. Wait a moment after Stop, then try again."
        )
    try:
        yield
    finally:
        if _lock_generations.get(serial, 0) == generation:
            lock.release()


def mark_device_busy(serial: str) -> None:
    with _device_locks_guard:
        _device_busy.add(serial)


def mark_device_idle(serial: str) -> None:
    with _device_locks_guard:
        _device_busy.discard(serial)


def is_device_busy(serial: str) -> bool:
    with _device_locks_guard:
        return serial in _device_busy


def force_unblock_device(serial: str) -> None:
    """Drop a stuck lock + u2 session so the next dashboard action can proceed."""
    with _device_locks_guard:
        _lock_generations[serial] = _lock_generations.get(serial, 0) + 1
        _device_locks[serial] = threading.RLock()
        _device_busy.discard(serial)
        timer = _unblock_timers.pop(serial, None)
        if timer is not None:
            timer.cancel()
    release_device_session(serial)


def schedule_force_unblock(serial: str, delay: float = 1.0) -> None:
    def _fire() -> None:
        if is_device_busy(serial):
            force_unblock_device(serial)

    with _device_locks_guard:
        old = _unblock_timers.pop(serial, None)
        if old is not None:
            old.cancel()
        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        _unblock_timers[serial] = timer
        timer.start()


def stop_device_work(serial: str, *, force_delay: float = 1.0) -> None:
    """Cooperative stop: signal cancel, then force-unblock if work is still running."""
    from dashboard.debug_tests import request_debug_cancel

    request_debug_cancel(serial)
    schedule_force_unblock(serial, delay=force_delay)


_hardware_id_by_serial: dict[str, str] = {}


def _device_dict(device: Device) -> dict[str, str]:
    hardware_id = device.hardware_id or _hardware_id_by_serial.get(device.serial, "")
    if device.hardware_id:
        _hardware_id_by_serial[device.serial] = device.hardware_id
    return {
        "serial": device.serial,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "hardware_id": hardware_id,
        "label": device.label,
        "status": "CONNECTED",
    }


def get_hardware_id(serial: str) -> str:
    """Stable hardware id for a serial (cached; falls back to a live adb read)."""
    cached = _hardware_id_by_serial.get(serial)
    if cached:
        return cached
    try:
        from android_devices import get_hardware_id as _adb_hardware_id

        hardware_id = _adb_hardware_id(resolve_adb(), serial)
    except Exception:
        hardware_id = ""
    if hardware_id:
        _hardware_id_by_serial[serial] = hardware_id
    return hardware_id


def get_devices_with_hardware_ids() -> list[dict[str, str]]:
    """Full device list including hardware ids (runs getprop; slower than fast path)."""
    adb = resolve_adb()
    devices = list_devices(adb, serial_filter=DEVICE_SERIAL_FILTER or None, include_props=True)
    return [_device_dict(d) for d in devices]


def get_adb_devices(*, fast: bool = False) -> list[dict[str, str]]:
    now = time.monotonic()
    cache_key = "fast" if fast else "full"
    ttl = _FAST_DEVICES_TTL if fast else _FULL_DEVICES_TTL
    cached = _devices_cache.get(cache_key) or []
    cached_at = _devices_cache.get(f"{cache_key}_at", 0.0)
    if cached and (now - cached_at) < ttl:
        return list(cached)

    adb = resolve_adb()
    devices = list_devices(
        adb,
        serial_filter=DEVICE_SERIAL_FILTER or None,
        include_props=not fast,
    )
    result = [_device_dict(d) for d in devices]
    _devices_cache[cache_key] = result
    _devices_cache[f"{cache_key}_at"] = now
    return result


def invalidate_devices_cache() -> None:
    _devices_cache["fast"] = []
    _devices_cache["full"] = []
    _devices_cache["fast_at"] = 0.0
    _devices_cache["full_at"] = 0.0


def connect(serial: str) -> u2.Device:
    require_allowed_serial(serial)
    if serial not in _connections:
        _connections[serial] = u2.connect(serial)
    return _connections[serial]


def disconnect(serial: str) -> None:
    _connections.pop(serial, None)


def release_device_session(serial: str) -> None:
    """Drop cached u2 / DeviceFacade session (e.g. on device change or explicit disconnect)."""
    disconnect(serial)
    try:
        from dashboard.debug_tests import release_device_facade

        release_device_facade(serial)
    except ImportError:
        pass


def warmup_device(serial: str) -> None:
    """Pre-connect uiautomator so the first debug tap is instant."""
    require_allowed_serial(serial)
    with device_operation(serial):
        dev = connect(serial)
        dev.info


def parse_bounds(bounds: str) -> list[int] | None:
    match = BOUNDS_RE.match(bounds.strip())
    if not match:
        return None
    return [int(match.group(i)) for i in range(1, 5)]


def _node_is_interesting(attrs: dict[str, str]) -> bool:
    text = attrs.get("text", "").strip()
    resource_id = attrs.get("resource-id", "").strip()
    content_desc = attrs.get("content-desc", "").strip()
    clickable = attrs.get("clickable", "false") == "true"
    checked = attrs.get("checked", "false") == "true"
    return bool(text or resource_id or content_desc or clickable or checked)


def _parse_node(node: ET.Element, index: int, depth: int) -> dict[str, Any] | None:
    attrs = node.attrib
    if not _node_is_interesting(attrs):
        children: list[dict[str, Any]] = []
        child_index = index
        for child in node:
            if child.tag != "node":
                continue
            parsed = _parse_node(child, child_index, depth)
            if parsed is None:
                continue
            children.append(parsed)
            child_index = parsed["endIndex"] + 1
        if not children:
            return None
        return {
            "index": index,
            "endIndex": child_index - 1 if children else index,
            "depth": depth,
            "text": "",
            "resourceId": "",
            "contentDesc": "",
            "className": "",
            "clickable": False,
            "checked": False,
            "enabled": True,
            "bounds": None,
            "children": children,
        }

    class_name = attrs.get("class", "").split(".")[-1]
    bounds = parse_bounds(attrs.get("bounds", ""))
    element: dict[str, Any] = {
        "index": index,
        "depth": depth,
        "text": attrs.get("text", "").strip(),
        "resourceId": attrs.get("resource-id", "").strip(),
        "contentDesc": attrs.get("content-desc", "").strip(),
        "className": class_name,
        "clickable": attrs.get("clickable", "false") == "true",
        "checked": attrs.get("checked", "false") == "true",
        "enabled": attrs.get("enabled", "true") == "true",
        "bounds": bounds,
        "children": [],
    }
    child_index = index + 1
    for child in node:
        if child.tag != "node":
            continue
        parsed = _parse_node(child, child_index, depth + 1)
        if parsed is None:
            continue
        element["children"].append(parsed)
        child_index = parsed["endIndex"] + 1
    element["endIndex"] = child_index - 1
    return element


def flatten_elements(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        flat.append(
            {
                "index": node["index"],
                "depth": node["depth"],
                "text": node["text"],
                "resourceId": node["resourceId"],
                "contentDesc": node["contentDesc"],
                "className": node["className"],
                "clickable": node["clickable"],
                "checked": node["checked"],
                "enabled": node["enabled"],
                "bounds": node["bounds"],
            }
        )
        for child in node.get("children", []):
            walk(child)

    for node in nodes:
        walk(node)
    return flat


def parse_hierarchy(xml: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml)
    hierarchy: list[dict[str, Any]] = []
    index = 0
    for child in root:
        if child.tag != "node":
            continue
        parsed = _parse_node(child, index, 0)
        if parsed is None:
            continue
        hierarchy.append(parsed)
        index = parsed["endIndex"] + 1
    return hierarchy


def _capture_device_ui(device: u2.Device) -> tuple[str, dict[str, Any], str]:
    """One uiautomator session: hierarchy + screenshot (avoids duplicate cold starts)."""
    xml = device.dump_hierarchy()
    current = device.app_current()
    tree = parse_hierarchy(xml)
    hierarchy = {
        "package": current.get("package", ""),
        "activity": current.get("activity", ""),
        "elements": flatten_elements(tree),
        "tree": tree,
    }
    img = device.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return xml, hierarchy, f"data:image/png;base64,{encoded}"


def get_inspector(serial: str) -> dict[str, Any]:
    with device_operation(serial):
        _, hierarchy, image = _capture_device_ui(connect(serial))
        return {"image": image, **hierarchy}


def get_screenshot(serial: str) -> str:
    with device_operation(serial):
        device = connect(serial)
        img = device.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"


def get_hierarchy(serial: str) -> dict[str, Any]:
    with device_operation(serial):
        _, hierarchy, _ = _capture_device_ui(connect(serial))
        return hierarchy


def tap_bounds(serial: str, bounds: list[int]) -> None:
    with device_operation(serial):
        device = connect(serial)
        x1, y1, x2, y2 = bounds
        device.click((x1 + x2) // 2, (y1 + y2) // 2)


def press_home(serial: str) -> None:
    with device_operation(serial):
        connect(serial).press("home")


def dump_to_disk(serial: str) -> dict[str, Any]:
    from dump_tree import summarize_hierarchy, write_app_info  # noqa: E402

    with device_operation(serial):
        device = connect(serial)
        xml, hierarchy, image = _capture_device_ui(device)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = DUMP_ROOT / f"{serial}_{stamp}"
        output_dir.mkdir(parents=True, exist_ok=True)
        hierarchy_path = output_dir / "hierarchy.xml"
        hierarchy_path.write_text(xml, encoding="utf-8")
        (output_dir / "screenshot.png").write_bytes(base64.b64decode(image.split(",", 1)[1]))
        write_app_info(device, output_dir / "app-info.txt")
        element_count = summarize_hierarchy(hierarchy_path, output_dir / "elements.txt")
        readme = f"""UI dump created: {datetime.now().isoformat(timespec='seconds')}

Files:
  screenshot.png  - what was on screen
  hierarchy.xml   - raw Android UI tree (use this for exact selectors)
  elements.txt    - flattened list of useful elements ({element_count} rows)
  app-info.txt    - package/activity of the foreground app
"""
        (output_dir / "README.txt").write_text(readme, encoding="utf-8")
        zip_path = Path(shutil.make_archive(str(output_dir), "zip", root_dir=output_dir))
        return {
            "dir": str(output_dir),
            "zip": str(zip_path),
            "zip_name": zip_path.name,
            "image": image,
            "package": hierarchy["package"],
            "activity": hierarchy["activity"],
            "elements": hierarchy["elements"],
        }


def resolve_scrcpy() -> str | None:
    import os
    import platform

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


def start_mirror(serial: str) -> dict[str, str]:
    import os

    from dashboard.gramaddict_config import username_for_device

    # Diagnostic: scrcpy is only ever launched here. If a scrcpy window appears
    # during a run and this line does NOT print in the dashboard terminal, the
    # mirror is being opened from outside the dashboard (e.g. tools/device_lab.py).
    print(f"[mirror] start_mirror called for {serial}", flush=True)

    scrcpy = resolve_scrcpy()
    if not scrcpy:
        raise RuntimeError("scrcpy not found. Install scrcpy or set SCRCPY env var.")
    if scrcpy_running_for_device(serial):
        return {"status": "already_running"}
    adb = resolve_adb()
    devices = list_devices(adb, serial_filter=DEVICE_SERIAL_FILTER or None)
    device = next((d for d in devices if d.serial == serial), None)
    if device is None:
        raise RuntimeError(f"Device not connected: {serial}")
    title = scrcpy_window_title(serial, username_for_device(serial))
    env = os.environ.copy()
    env["ADB"] = adb
    subprocess.Popen(
        [scrcpy, "-s", serial, "--window-title", title, "--max-size", "1024"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"status": "started"}
