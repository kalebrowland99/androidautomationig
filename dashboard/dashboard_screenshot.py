"""Capture Farm device rows for Telegram — from live API data, not the SPA.

First principles: the interactive Farm page loads devices first, then accounts
asynchronously. Screenshotting that SPA races JS and often captures empty
"Set account" rows / the Brand pools section.

Instead we pull the same data the UI uses (devices + accounts), render a
devices-only HTML table that is already complete, then screenshot top + bottom
of that table (ending at device #19). No pools, no SPA wait.
"""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

CHROME_CANDIDATES = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium"),
    Path("/usr/bin/chromium-browser"),
)

DASHBOARD_BASE = "http://127.0.0.1:8080"


def _find_chrome() -> Path:
    for path in CHROME_CANDIDATES:
        if path.is_file():
            return path
    which = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which(
        "chromium-browser"
    )
    if which:
        return Path(which)
    raise RuntimeError("Google Chrome / Chromium not found for dashboard screenshots")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_devtools(port: int, timeout_s: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_err = "timeout"
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
            if resp.ok:
                return resp.json()
            last_err = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"Chrome DevTools not ready on :{port} ({last_err})")


class _Cdp:
    def __init__(self, ws_url: str) -> None:
        import websocket

        self._ws = websocket.create_connection(ws_url, timeout=30)
        self._next_id = 0

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def call(self, method: str, params: Optional[dict[str, Any]] = None, timeout_s: float = 30.0) -> Any:
        self._next_id += 1
        msg_id = self._next_id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            raw = self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method}: {data['error']}")
                return data.get("result")
        raise TimeoutError(f"CDP {method} timed out")


def _stack_top_bottom(png_top: bytes, png_bottom: bytes, *, max_width: int = 1200) -> bytes:
    from PIL import Image

    top = Image.open(io.BytesIO(png_top)).convert("RGB")
    bottom = Image.open(io.BytesIO(png_bottom)).convert("RGB")
    width = max(top.width, bottom.width)
    if top.width != width:
        top = top.resize((width, int(top.height * width / top.width)), Image.Resampling.BILINEAR)
    if bottom.width != width:
        bottom = bottom.resize(
            (width, int(bottom.height * width / bottom.width)), Image.Resampling.BILINEAR
        )
    gap = 6
    canvas = Image.new("RGB", (width, top.height + gap + bottom.height), (18, 18, 18))
    canvas.paste(top, (0, 0))
    canvas.paste(bottom, (0, top.height + gap))
    if canvas.width > max_width:
        ratio = max_width / float(canvas.width)
        canvas = canvas.resize(
            (max_width, max(1, int(canvas.height * ratio))),
            Image.Resampling.BILINEAR,
        )
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=70, optimize=True)
    return buf.getvalue()


def _short_serial(serial: str) -> str:
    serial = str(serial or "")
    return serial[-8:] if len(serial) > 8 else serial


def _bot_state_label(acct: Optional[dict[str, Any]]) -> str:
    if not acct:
        return ""
    if acct.get("disabled"):
        return "Disabled"
    progress = acct.get("progress") or {}
    if acct.get("running"):
        job = str(progress.get("current_job") or "session").replace("-", " ")
        return f"Running · {job}"
    if progress.get("rate_limited") or progress.get("state") == "action_limit":
        return "Action limit"
    if progress.get("sleeping") or progress.get("next_session_at") or progress.get("state") == "waiting":
        return "Waiting"
    return "Stopped"


def _progress_lines(acct: Optional[dict[str, Any]]) -> list[str]:
    if not acct:
        return []
    p = acct.get("progress") or {}
    lines: list[str] = []
    if acct.get("running") or p.get("state") in ("running", "waiting", "action_limit"):
        likes = p.get("likes")
        likes_lim = p.get("likes_limit")
        stories = p.get("story_likes") or p.get("watched") or 0
        stories_lim = p.get("story_likes_limit") or p.get("watches_limit")
        follows = p.get("follows") or 0
        parts = []
        if likes is not None or likes_lim:
            parts.append(f"Liked Posts {likes or 0}" + (f"/{likes_lim}" if likes_lim else ""))
        if stories or stories_lim:
            parts.append(
                f"Liked Stories {stories}" + (f"/{stories_lim}" if stories_lim else "")
            )
        parts.append(f"Story Accounts {p.get('story_accounts_liked') or 0}")
        if follows or p.get("follows_limit"):
            parts.append(
                f"Followed {follows}"
                + (f"/{p.get('follows_limit')}" if p.get("follows_limit") else "")
            )
        if parts:
            lines.append("Session · " + " · ".join(parts))
    today = p.get("today") if isinstance(p.get("today"), dict) else None
    if today:
        t_parts = []
        if today.get("likes") is not None or today.get("likes_goal"):
            t_parts.append(
                f"Liked Posts {today.get('likes') or 0}"
                + (f"/{today['likes_goal']}" if today.get("likes_goal") else "")
            )
        if today.get("story_likes") is not None or today.get("story_likes_goal"):
            t_parts.append(
                f"Liked Stories {today.get('story_likes') or 0}"
                + (f"/{today['story_likes_goal']}" if today.get("story_likes_goal") else "")
            )
        t_parts.append(f"Story Accounts {today.get('story_accounts_liked') or 0}")
        if today.get("follows") is not None or today.get("follows_goal"):
            t_parts.append(
                f"Followed {today.get('follows') or 0}"
                + (f"/{today['follows_goal']}" if today.get("follows_goal") else "")
            )
        if t_parts:
            lines.append("Today · " + " · ".join(t_parts))
    return lines


def _match_account(
    serial: str, accounts: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    serial = str(serial or "")
    for acct in accounts:
        if str(acct.get("device") or "") == serial:
            return acct
    # hardware id suffix / device_id match (same idea as the Farm UI)
    for acct in accounts:
        device_id = str(acct.get("device_id") or "")
        if device_id and (serial.endswith(device_id) or device_id in serial):
            return acct
    try:
        from dashboard.gramaddict_config import username_for_device

        handle = username_for_device(serial)
    except Exception:
        handle = None
    if handle:
        handle_l = handle.lstrip("@").lower()
        for acct in accounts:
            if str(acct.get("username") or "").lstrip("@").lower() == handle_l:
                return acct
            if str(acct.get("id") or "").lower() == handle_l:
                return acct
    return None


def _load_farm_rows() -> list[dict[str, Any]]:
    """Build fully-populated device rows from live dashboard data."""
    devices: list[dict[str, Any]] = []
    accounts: list[dict[str, Any]] = []

    # Prefer in-process calls (no HTTP race) when running inside the dashboard.
    try:
        from dashboard.device_service import get_adb_devices
        from dashboard.gramaddict_config import list_accounts

        devices = list(get_adb_devices(fast=True) or [])
        accounts = list(list_accounts() or [])
    except Exception as exc:
        logger.debug("In-process farm data failed (%s); falling back to HTTP", exc)

    if not devices:
        devices = requests.get(
            f"{DASHBOARD_BASE}/api/devices", params={"fast": "true"}, timeout=15
        ).json()
    if not accounts:
        accounts = requests.get(
            f"{DASHBOARD_BASE}/api/gramaddict/accounts", timeout=30
        ).json()

    if not isinstance(devices, list):
        devices = []
    if not isinstance(accounts, list):
        accounts = []

    rows: list[dict[str, Any]] = []
    for index, device in enumerate(devices, start=1):
        serial = str(device.get("serial") or "")
        acct = _match_account(serial, accounts)
        handle = ""
        if acct:
            handle = str(acct.get("username") or acct.get("id") or "").lstrip("@")
        rows.append(
            {
                "slot": index,
                "serial": serial,
                "short_serial": _short_serial(serial),
                "status": str(device.get("status") or "CONNECTED"),
                "handle": handle,
                "bot_state": _bot_state_label(acct),
                "progress": _progress_lines(acct),
                "running": bool(acct and acct.get("running")),
                "note": str((acct or {}).get("note") or ""),
            }
        )
    return rows


def _farm_snapshot_html(rows: list[dict[str, Any]]) -> str:
    """Self-contained devices table — already filled, no Brand pools."""
    body_rows: list[str] = []
    for row in rows:
        handle_html = (
            f'<div class="handle">@{html.escape(row["handle"])}</div>'
            if row["handle"]
            else '<div class="handle muted">— not linked —</div>'
        )
        state = html.escape(row["bot_state"]) if row["bot_state"] else ""
        state_class = "running" if row["running"] else "stopped"
        progress_html = "".join(
            f'<div class="progress">{html.escape(line)}</div>' for line in row["progress"]
        )
        note_html = (
            f'<div class="note">{html.escape(row["note"])}</div>' if row["note"] else ""
        )
        body_rows.append(
            f"""
            <tr class="device-row" data-slot="{row['slot']}">
              <td class="slot">{row['slot']}</td>
              <td class="serial">{html.escape(row['short_serial'])}</td>
              <td class="account">
                {handle_html}
                {f'<div class="state {state_class}">{state}</div>' if state else ''}
                {progress_html}
                {note_html}
              </td>
              <td class="conn"><span class="dot"></span>{html.escape(row['status'])}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Farm devices</title>
<style>
  html, body {{
    margin: 0; padding: 0;
    background: #f7f7f5;
    color: #111;
    font: 13px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .wrap {{
    padding: 16px 20px 24px;
    max-width: 920px;
  }}
  h1 {{
    margin: 0 0 4px;
    font-size: 20px;
    font-weight: 650;
  }}
  .sub {{
    margin: 0 0 14px;
    color: #666;
    font-size: 12px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    border: 1px solid #e5e5e2;
    border-radius: 12px;
    overflow: hidden;
  }}
  th, td {{
    padding: 8px 10px;
    text-align: left;
    vertical-align: top;
    border-bottom: 1px solid #eee;
  }}
  th {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #666;
    background: #fafafa;
  }}
  tr.device-row:last-child td {{ border-bottom: none; }}
  .slot {{ width: 36px; color: #888; font-variant-numeric: tabular-nums; }}
  .serial {{ width: 96px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }}
  .handle {{ font-weight: 600; }}
  .handle.muted {{ color: #999; font-weight: 500; }}
  .state {{ margin-top: 2px; font-size: 12px; }}
  .state.running {{ color: #0a7a3e; }}
  .state.stopped {{ color: #666; }}
  .progress {{ margin-top: 2px; font-size: 11px; color: #444; }}
  .note {{ margin-top: 2px; font-size: 11px; color: #888; }}
  .conn {{ width: 110px; white-space: nowrap; }}
  .dot {{
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: #22c55e; margin-right: 6px; vertical-align: middle;
  }}
</style>
</head>
<body>
  <div class="wrap" id="farm-shot">
    <h1>Farm devices</h1>
    <p class="sub">{len(rows)} phones · account info from live dashboard data</p>
    <table>
      <thead>
        <tr><th>#</th><th>Serial</th><th>Account</th><th>Status</th></tr>
      </thead>
      <tbody>
        {''.join(body_rows)}
      </tbody>
    </table>
  </div>
</body>
</html>
"""


def capture_dashboard_farm_jpeg(
    *,
    viewport_width: int = 1000,
    viewport_height: int = 900,
    **_ignored: Any,
) -> bytes:
    """Return top+bottom JPEG of the Farm devices table with account info filled in."""
    try:
        probe = requests.get(f"{DASHBOARD_BASE}/", timeout=5)
        if probe.status_code >= 500:
            raise RuntimeError(f"Dashboard HTTP {probe.status_code}")
    except Exception as exc:
        raise RuntimeError(f"Dashboard not reachable at {DASHBOARD_BASE}/ ({exc})") from exc

    rows = _load_farm_rows()
    if not rows:
        raise RuntimeError("No devices connected to screenshot")

    linked = sum(1 for r in rows if r["handle"])
    logger.info(
        "Farm snapshot rows=%s linked=%s (slot 1..%s)",
        len(rows),
        linked,
        rows[-1]["slot"],
    )

    html_doc = _farm_snapshot_html(rows)
    chrome = _find_chrome()
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="ga-farm-shot-")
    html_path = Path(profile) / "farm.html"
    html_path.write_text(html_doc, encoding="utf-8")
    file_url = html_path.resolve().as_uri()

    proc: Optional[subprocess.Popen] = None
    cdp: Optional[_Cdp] = None
    try:
        proc = subprocess.Popen(
            [
                str(chrome),
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--hide-scrollbars",
                f"--user-data-dir={profile}",
                f"--window-size={viewport_width},{viewport_height}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        version = _wait_devtools(port)
        targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
        ws_url = next(
            (
                t.get("webSocketDebuggerUrl")
                for t in targets
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            ),
            None,
        ) or version.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("No Chrome DevTools WebSocket URL")

        cdp = _Cdp(ws_url)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": viewport_width,
                "height": viewport_height,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        cdp.call("Page.navigate", {"url": file_url})
        # Static file — brief paint wait only.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            result = cdp.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "({ready: document.readyState === 'complete' && "
                        "!!document.querySelector('tr.device-row'), "
                        "rows: document.querySelectorAll('tr.device-row').length})"
                    ),
                    "returnByValue": True,
                },
            )
            value = (result or {}).get("result", {}).get("value") or {}
            if value.get("ready") and int(value.get("rows") or 0) == len(rows):
                break
            time.sleep(0.15)
        time.sleep(0.2)

        cdp.call(
            "Runtime.evaluate",
            {"expression": "window.scrollTo(0, 0); true", "returnByValue": True},
        )
        time.sleep(0.2)
        top = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        top_png = base64.b64decode(top["data"])

        # Scroll so the last device row (e.g. #19) sits on the viewport bottom.
        cdp.call(
            "Runtime.evaluate",
            {
                "expression": r"""
(() => {
  const rows = [...document.querySelectorAll('tr.device-row')];
  const target =
    rows.find((r) => r.getAttribute('data-slot') === '19') || rows[rows.length - 1];
  if (!target) return { ok: false };
  const absBottom = window.scrollY + target.getBoundingClientRect().bottom;
  window.scrollTo(0, Math.max(0, Math.ceil(absBottom - window.innerHeight)));
  for (let i = 0; i < 3; i++) {
    const delta = target.getBoundingClientRect().bottom - window.innerHeight;
    if (Math.abs(delta) <= 1) break;
    window.scrollBy(0, delta);
  }
  // Never scroll past the table itself.
  const table = document.querySelector('table');
  if (table) {
    const maxScroll = Math.max(
      0,
      Math.ceil(window.scrollY + table.getBoundingClientRect().bottom - window.innerHeight)
    );
    if (window.scrollY > maxScroll) window.scrollTo(0, maxScroll);
  }
  return {
    ok: true,
    slot: target.getAttribute('data-slot'),
    scrollY: window.scrollY,
    rowBottom: target.getBoundingClientRect().bottom,
    viewport: window.innerHeight,
  };
})()
""",
                "returnByValue": True,
            },
        )
        time.sleep(0.25)
        bottom = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        bottom_png = base64.b64decode(bottom["data"])
        return _stack_top_bottom(top_png, bottom_png)
    finally:
        if cdp is not None:
            cdp.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
