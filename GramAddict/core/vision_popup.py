"""Vision-assisted popup dismissal.

When automation can't find an expected element, the real cause is often a modal,
dialog, or permission sheet covering the UI. This module captures a screenshot,
asks OpenAI's vision model to locate the popup's dismiss button, and taps it so
the session can recover instead of failing.

Safety:
- Only taps when the model actually reports a blocking popup.
- Per-device cooldown so a retry loop can't spam OpenAI or the screen.
- Coordinates are bounds-checked and scaled from image space to device space.
- Never raises: any failure returns False and the caller proceeds as before.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Per-device timestamp of the last vision attempt (seconds since epoch).
_last_attempt: dict[str, float] = {}
_DEFAULT_COOLDOWN = 6.0

_PROMPT = (
    "You are looking at a screenshot of an Android phone, usually running Instagram. "
    "A popup, modal, dialog, bottom sheet, or system permission prompt may be covering "
    "the main UI and blocking automation. "
    "The image is {w} pixels wide and {h} pixels tall (top-left is 0,0). "
    "Decide if a popup is blocking the screen. Reply with EXACTLY ONE of these two "
    "things and nothing else:\n"
    "1. If there IS a blocking popup: the pixel coordinate of the single best button "
    "to DISMISS it, formatted exactly as `x,y` (two integers, e.g. `540,1180`). "
    "Prefer a safe, non-destructive button such as 'Not now', 'Cancel', 'Close', the "
    "X, 'Dismiss', 'Skip', 'Later', 'OK', 'Got it', 'Allow', or 'Continue' — never a "
    "button that deletes, discards, logs out, or reports.\n"
    "2. If there is NO popup blocking the screen: reply with exactly `no popup`.\n"
    "Do not explain. Reply with only `x,y` or only `no popup`."
)


def _resolve_account_key(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    try:
        from GramAddict.core import utils

        return getattr(getattr(utils, "args", None), "username", None)
    except Exception:
        return None


def _vision_enabled(account_key: str) -> bool:
    try:
        from GramAddict.core.follow_vision_account import get_account_follow_vision

        settings = get_account_follow_vision(account_key)
    except Exception:
        return False
    value = settings.get("vision-popup-dismiss")
    if value is None:
        return True  # default on when an OpenAI key is configured
    return bool(value)


def _parse_response(raw: str) -> Optional[tuple[float, float]]:
    """Parse the model's reply into a tap coordinate.

    Contract: the model replies with EITHER `x,y` (a popup to dismiss) OR
    `no popup`. Returns (x, y) when a coordinate is given, or None for the
    "no popup" case (which means the error is most likely real).
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if "no popup" in text or "no-popup" in text:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _device_serial(device) -> str:
    """Serial for either a DeviceFacade (.device_id) or raw uiautomator2 (.serial)."""
    return str(
        getattr(device, "device_id", None) or getattr(device, "serial", None) or ""
    )


def _device_display_info(device) -> dict:
    """Display info dict for a DeviceFacade (.get_info()) or raw device (.info)."""
    if hasattr(device, "get_info"):
        return device.get_info()
    return getattr(device, "info", {}) or {}


def _raw_device(device):
    """Underlying uiautomator2 device — a DeviceFacade wraps it as .deviceV2."""
    return getattr(device, "deviceV2", device)


def dismiss_popup_with_vision(
    device,
    account_key: Optional[str] = None,
    *,
    reason: str = "",
    respect_cooldown: bool = True,
) -> bool:
    """Ask OpenAI where a blocking popup's dismiss button is, then tap it.

    Accepts either a DeviceFacade or a raw uiautomator2 device.
    Returns True only if a popup was detected and a tap was issued.
    """
    account_key = _resolve_account_key(account_key)
    if not account_key or not _vision_enabled(account_key):
        return False

    try:
        from GramAddict.core.utils import check_instagram_rate_limit

        check_instagram_rate_limit(device)
    except Exception as exc:
        from GramAddict.core.utils import InstagramRateLimitError

        if isinstance(exc, InstagramRateLimitError):
            raise

    serial = _device_serial(device)
    now = time.time()
    if respect_cooldown and now - _last_attempt.get(serial, 0.0) < _DEFAULT_COOLDOWN:
        return False
    _last_attempt[serial] = now

    try:
        from GramAddict.core.follow_vision_account import _openai_api_key
    except Exception:
        return False
    api_key = _openai_api_key(account_key)
    if not api_key:
        return False

    try:
        image = device.screenshot()
        img_w, img_h = image.size
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85)
        jpeg = buf.getvalue()
    except Exception as exc:
        logger.debug("Vision popup: could not capture screenshot (%s).", exc)
        return False
    if not img_w or not img_h:
        return False

    try:
        from openai import OpenAI
    except ImportError:
        logger.debug("Vision popup: openai package not installed.")
        return False

    from GramAddict.core.follow_vision_account import VISION_MODEL

    b64 = base64.b64encode(jpeg).decode("ascii")
    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT.format(w=img_w, h=img_h)},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
            max_tokens=60,
            temperature=0,
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("Vision popup: OpenAI request failed (%s).", exc)
        return False

    coord = _parse_response(raw)
    if coord is None:
        # "no popup" (or an unparseable reply) — nothing is blocking the screen,
        # so the original error is most likely real. Leave it to normal handling.
        logger.info(
            "Vision popup: no popup detected (%s) — treating the error as real.",
            reason or "on error",
        )
        return False

    img_x, img_y = coord
    if not (0 <= img_x <= img_w and 0 <= img_y <= img_h):
        logger.debug("Vision popup: coordinate (%s,%s) out of bounds.", img_x, img_y)
        return False

    # Map image-space coordinates to device display coordinates (usually 1:1,
    # but screenshot resolution can differ from the reported display size).
    try:
        info = _device_display_info(device)
        disp_w = int(info["displayWidth"])
        disp_h = int(info["displayHeight"])
    except Exception:
        disp_w, disp_h = img_w, img_h
    dev_x = int(round(img_x * disp_w / img_w))
    dev_y = int(round(img_y * disp_h / img_h))

    logger.info(
        "Vision popup: popup detected — tapping (%s,%s) to dismiss [%s].",
        dev_x,
        dev_y,
        reason or "on error",
    )
    try:
        _raw_device(device).click(dev_x, dev_y)
    except Exception as exc:
        logger.warning("Vision popup: tap failed (%s).", exc)
        return False
    return True
