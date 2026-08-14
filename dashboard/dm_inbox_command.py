"""Pause farm phones, open Instagram DM inbox on each, then screenshot."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from dashboard.preview_stream import (
    build_farm_collage_jpeg,
    list_preview_targets,
    wake_device_for_preview,
)

logger = logging.getLogger(__name__)

DEFAULT_APP_ID = "com.instagram.android"
INBOX_URL = "https://www.instagram.com/direct/inbox/"
MAX_WORKERS = 6

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def _ensure_gramaddict_resources(app_id: str = DEFAULT_APP_ID) -> None:
    """Point GramAddict view helpers at this Instagram package id."""
    from GramAddict.core import resources as ga_resources
    from GramAddict.core import utils as ga_utils
    from GramAddict.core import views as ga_views

    resource_ids = ga_resources.ResourceID(app_id)
    ga_views.ResourceID = resource_ids
    ga_utils.ResourceID = resource_ids


def _pause_farm_bots(targets: list[dict[str, Any]]) -> list[str]:
    """Force-stop running bots for farm phones. Returns paused account ids."""
    from dashboard.gramaddict_config import (
        _account_bot_running,
        account_id_for_device,
        stop_bot,
    )

    paused: list[str] = []
    seen: set[str] = set()
    for target in targets:
        serial = str(target.get("serial") or "").strip()
        if not serial:
            continue
        account_id = account_id_for_device(serial)
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        if not _account_bot_running(account_id):
            continue
        try:
            result = stop_bot(account_id, force=True)
            if result.get("stopped"):
                paused.append(account_id)
                logger.info("Paused bot for %s (Telegram DMs)", account_id)
        except Exception as exc:
            logger.warning("Could not pause %s: %s", account_id, exc)
    # Give ATX / Instagram a moment after killing bot sessions.
    if paused:
        time.sleep(1.5)
    return paused


def _open_inbox_via_intent(serial: str, app_id: str = DEFAULT_APP_ID) -> bool:
    """Deep-link straight into Direct inbox (best-effort)."""
    from android_devices import resolve_adb

    adb = resolve_adb()
    cmd = [
        adb,
        "-s",
        serial,
        "shell",
        "am",
        "start",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        INBOX_URL,
        app_id,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=12, check=False, text=True
        )
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and "Error" in out:
            logger.debug(
                "Inbox intent failed on %s: %s", serial[-8:], out.strip()[:200]
            )
            return False
        time.sleep(2.0)
        return True
    except Exception as exc:
        logger.debug("Inbox intent error on %s: %s", serial[-8:], exc)
        return False


def _device_facade(serial: str, app_id: str = DEFAULT_APP_ID):
    from dashboard import device_service
    from GramAddict.core.device_facade import DeviceFacade

    facade = DeviceFacade.__new__(DeviceFacade)
    facade.device_id = serial
    facade.app_id = app_id
    facade.deviceV2 = device_service.connect(serial)
    try:
        facade.disable_auto_rotate()
    except Exception:
        pass
    return facade


def _ensure_instagram_foreground(device, serial: str, app_id: str) -> bool:
    try:
        current = (device.deviceV2.app_current() or {}).get("package") or ""
        if current == app_id:
            return True
        device.deviceV2.app_start(app_id, use_monkey=True)
        time.sleep(1.2)
        current = (device.deviceV2.app_current() or {}).get("package") or ""
        return current == app_id
    except Exception as exc:
        logger.debug("Could not foreground IG on %s: %s", serial[-8:], exc)
        return False


def _open_inbox_via_ui(device) -> bool:
    from GramAddict.core.views import HomeView, TabBarView

    try:
        TabBarView(device).navigateToHome()
        time.sleep(0.6)
    except Exception as exc:
        logger.debug("navigateToHome failed: %s", exc)
    home = HomeView(device)
    if home.is_inbox_open():
        return True
    if home.navigateToInbox():
        time.sleep(0.8)
        return True
    return False


def open_dm_inbox_on_device(serial: str, *, app_id: str = DEFAULT_APP_ID) -> dict[str, Any]:
    """Wake phone, open IG Direct inbox. Returns status dict."""
    serial = str(serial).strip()
    result: dict[str, Any] = {
        "serial": serial,
        "ok": False,
        "method": None,
        "error": None,
    }
    try:
        _ensure_gramaddict_resources(app_id)
        wake_device_for_preview(serial)
        intent_ok = _open_inbox_via_intent(serial, app_id=app_id)
        device = _device_facade(serial, app_id=app_id)
        if not _ensure_instagram_foreground(device, serial, app_id):
            result["error"] = "Instagram not in foreground"
            return result

        from GramAddict.core.views import HomeView

        home = HomeView(device)
        if intent_ok and home.is_inbox_open():
            result["ok"] = True
            result["method"] = "intent"
            return result

        if _open_inbox_via_ui(device):
            result["ok"] = True
            result["method"] = "ui" if not intent_ok else "intent+ui"
            return result

        # Intent may have landed on inbox even if detection failed.
        if intent_ok:
            result["ok"] = True
            result["method"] = "intent-unverified"
            return result

        result["error"] = "Could not open Direct inbox"
        return result
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("open_dm_inbox_on_device(%s) failed: %s", serial[-8:], exc)
        return result


def open_dm_inbox_on_all_farm_phones(
    *, max_workers: int = MAX_WORKERS
) -> dict[str, Any]:
    """Pause bots, open inbox on every farm/connected phone in parallel."""
    info = list_preview_targets()
    targets = list(info.get("targets") or [])
    if not targets:
        return {
            "paused": [],
            "results": [],
            "ok": 0,
            "count": 0,
            "source": info.get("source"),
            "error": "No farm phones connected",
        }

    paused = _pause_farm_bots(targets)
    results: list[dict[str, Any]] = []
    _ensure_gramaddict_resources(DEFAULT_APP_ID)

    def _one(target: dict[str, Any]) -> dict[str, Any]:
        serial = str(target["serial"])
        row = open_dm_inbox_on_device(serial)
        row["username"] = (target.get("username") or "").lstrip("@")
        return row

    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(targets)))) as pool:
        futures = [pool.submit(_one, t) for t in targets]
        by_serial: dict[str, dict[str, Any]] = {}
        for fut in as_completed(futures):
            row = fut.result()
            by_serial[str(row["serial"])] = row

    # Preserve farm order.
    for target in targets:
        results.append(by_serial[str(target["serial"])])

    ok = sum(1 for r in results if r.get("ok"))
    return {
        "paused": paused,
        "results": results,
        "ok": ok,
        "count": len(results),
        "source": info.get("source"),
        "error": None,
    }


def capture_dm_inbox_collage() -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Pause → open inboxes → wait → farm collage.

    Returns ``(jpeg, collage_meta, nav_meta)``.
    """
    nav = open_dm_inbox_on_all_farm_phones()
    if nav.get("error") and not nav.get("count"):
        raise RuntimeError(nav["error"])

    # Brief settle so all UIs finish animating before the grid capture.
    time.sleep(1.0)
    jpeg, collage_meta = build_farm_collage_jpeg()
    return jpeg, collage_meta, nav
