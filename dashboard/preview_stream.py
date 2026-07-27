"""View-only ADB screencap previews for the mass mirror page."""

from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from dashboard.device_service import (
    DEVICE_SERIAL_FILTER,
    device_serial_allowed,
    get_adb_devices,
    require_allowed_serial,
)
from dashboard.gramaddict_config import get_farm_selection, username_for_device

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from android_devices import list_devices, resolve_adb  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover - clear error at request time
    Image = None  # type: ignore[assignment,misc]

QUALITY_PRESETS: dict[str, dict[str, int]] = {
    "low": {"max_size": 320, "fps": 1, "quality": 40},
    "med": {"max_size": 480, "fps": 2, "quality": 55},
    "high": {"max_size": 720, "fps": 4, "quality": 70},
}

_CAPTURE_TIMEOUT_S = 12.0
# Cap parallel screencaps so mass-mirror with ~18 phones doesn't black out ADB.
_CAPTURE_SLOTS = threading.BoundedSemaphore(4)
_BLACK_MEAN_MAX = 18.0
_BLACK_DARK_FRAC = 0.92
_REWAKE_EVERY_S = 25.0


def wake_device_for_preview(serial: str) -> None:
    """Wake the display so screencap is not a black/doze frame.

    Bots often leave screens Dozing even when the session is still running;
    ADB screencap then returns a black image. If the account has unlock-pin
    set, swipe + enter digits (no OK) when still locked.
    """
    require_allowed_serial(serial)
    adb = resolve_adb()
    commands = (
        ["shell", "input", "keyevent", "KEYCODE_WAKEUP"],
        # Keep display awake while mirroring (best-effort; ignored if denied).
        ["shell", "svc", "power", "stayon", "usb"],
        ["shell", "wm", "dismiss-keyguard"],
        ["shell", "input", "keyevent", "KEYCODE_MENU"],
    )
    for args in commands:
        try:
            subprocess.run(
                [adb, "-s", serial, *args],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            continue
    time.sleep(0.55)

    # PIN unlock when configured for this phone (digits only, no OK tap).
    try:
        from dashboard.gramaddict_config import unlock_pin_for_serial

        pin = unlock_pin_for_serial(serial)
    except Exception:
        pin = None
    if not pin:
        return
    try:
        # Swipe up to reveal PIN pad, then type digits via keyevents.
        subprocess.run(
            [adb, "-s", serial, "shell", "input", "swipe", "540", "1600", "540", "400", "300"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        time.sleep(0.8)
        for ch in str(pin):
            if not ch.isdigit():
                continue
            keycode = 7 + int(ch)  # KEYCODE_0=7 … KEYCODE_9=16
            subprocess.run(
                [adb, "-s", serial, "shell", "input", "keyevent", str(keycode)],
                capture_output=True,
                timeout=3,
                check=False,
            )
            time.sleep(0.2)
        time.sleep(0.8)
    except Exception:
        pass


def wake_all_preview_targets() -> dict[str, Any]:
    """Wake every farm/connected preview target (for Mass Mirror)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    info = list_preview_targets()
    targets = list(info.get("targets") or [])
    ok = 0
    failures: list[dict[str, str]] = []
    if not targets:
        return {"ok": 0, "failures": [], "count": 0}

    def _one(serial: str) -> tuple[str, str | None]:
        try:
            wake_device_for_preview(serial)
            return serial, None
        except Exception as exc:  # noqa: BLE001
            return serial, str(exc)

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        futs = [pool.submit(_one, str(t["serial"])) for t in targets]
        for fut in as_completed(futs):
            serial, err = fut.result()
            if err:
                failures.append({"serial": serial, "error": err})
            else:
                ok += 1
    return {"ok": ok, "failures": failures, "count": len(targets)}


def _jpeg_is_mostly_black(jpeg: bytes) -> bool:
    """True when a screencap is a doze/off black frame."""
    if Image is None or not jpeg:
        return False
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        img.load()
        sample = img.resize((48, 48))
        pixels = list(sample.getdata())
        if not pixels:
            return True
        mean = sum(sum(p) for p in pixels) / (len(pixels) * 3.0)
        dark = sum(1 for p in pixels if (p[0] + p[1] + p[2]) / 3.0 < 12) / len(pixels)
        return mean <= _BLACK_MEAN_MAX or dark >= _BLACK_DARK_FRAC
    except Exception:
        return False


def _require_pillow() -> None:
    if Image is None:
        raise RuntimeError(
            "Pillow is required for mass mirror. Install with: pip install Pillow"
        )


def clamp_preview_params(
    *,
    max_size: int | None = None,
    quality: int | None = None,
    fps: float | None = None,
    preset: str | None = None,
) -> dict[str, float | int]:
    """Resolve quality params from an optional preset + overrides."""
    base = dict(QUALITY_PRESETS.get((preset or "").lower()) or QUALITY_PRESETS["med"])
    if max_size is not None:
        base["max_size"] = int(max_size)
    if quality is not None:
        base["quality"] = int(quality)
    if fps is not None:
        base["fps"] = float(fps)
    base["max_size"] = max(160, min(1280, int(base["max_size"])))
    base["quality"] = max(20, min(95, int(base["quality"])))
    base["fps"] = max(0.25, min(10.0, float(base["fps"])))
    return base


def capture_jpeg(
    serial: str,
    max_size: int = 480,
    quality: int = 55,
    *,
    wake: bool = False,
    retry_if_black: bool = False,
) -> bytes:
    """Grab one device frame as a resized JPEG via ``adb exec-out screencap -p``."""
    _require_pillow()
    require_allowed_serial(serial)
    if wake:
        wake_device_for_preview(serial)

    def _grab() -> bytes:
        adb = resolve_adb()
        with _CAPTURE_SLOTS:
            try:
                result = subprocess.run(
                    [adb, "-s", serial, "exec-out", "screencap", "-p"],
                    capture_output=True,
                    timeout=_CAPTURE_TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"screencap timed out for {serial}") from exc
            except FileNotFoundError as exc:
                raise RuntimeError("adb not found") from exc

        if result.returncode != 0 or not result.stdout:
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(err or f"screencap failed for {serial}")

        try:
            image = Image.open(io.BytesIO(result.stdout))
            image.load()
        except Exception as exc:  # noqa: BLE001 - surface decode errors cleanly
            raise RuntimeError(f"Invalid screencap for {serial}: {exc}") from exc

        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")

        max_edge = max(160, min(1280, int(max_size)))
        image.thumbnail((max_edge, max_edge), Image.Resampling.BILINEAR)

        buf = io.BytesIO()
        image.save(
            buf,
            format="JPEG",
            quality=max(20, min(95, int(quality))),
            optimize=True,
        )
        return buf.getvalue()

    jpeg = _grab()
    if retry_if_black and _jpeg_is_mostly_black(jpeg):
        wake_device_for_preview(serial)
        jpeg = _grab()
    return jpeg


def mjpeg_frames(
    serial: str,
    *,
    max_size: int = 480,
    quality: int = 55,
    fps: float = 2.0,
) -> Iterator[bytes]:
    """Yield multipart MJPEG chunks until the client disconnects."""
    params = clamp_preview_params(max_size=max_size, quality=quality, fps=fps)
    boundary = b"--frame"
    interval = 1.0 / float(params["fps"])
    # Always wake at stream start — bots leave many phones dozing/black.
    try:
        wake_device_for_preview(serial)
    except Exception:
        pass
    last_wake = time.monotonic()
    while True:
        started = time.monotonic()
        try:
            if (started - last_wake) >= _REWAKE_EVERY_S:
                try:
                    wake_device_for_preview(serial)
                    last_wake = started
                except Exception:
                    pass
            jpeg = capture_jpeg(
                serial,
                max_size=int(params["max_size"]),
                quality=int(params["quality"]),
                retry_if_black=True,
            )
            if _jpeg_is_mostly_black(jpeg):
                # Still black after retry — force another wake next loop.
                last_wake = 0.0
            header = (
                boundary
                + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(jpeg)).encode("ascii")
                + b"\r\n\r\n"
            )
            yield header + jpeg + b"\r\n"
        except Exception as exc:  # noqa: BLE001 - keep stream alive across blips
            # Tiny 1x1 JPEG so the browser keeps the connection; label via delay.
            msg = str(exc).encode("utf-8", errors="replace")[:200]
            # Still sleep so we don't spin on hard failures.
            _ = msg
            time.sleep(min(2.0, interval * 2))
            continue

        elapsed = time.monotonic() - started
        delay = interval - elapsed
        if delay > 0:
            time.sleep(delay)


def list_preview_targets() -> dict[str, Any]:
    """Farm-checked serials when present; otherwise every connected device."""
    adb = resolve_adb()
    connected = list_devices(adb, serial_filter=DEVICE_SERIAL_FILTER or None)
    connected_serials = [d.serial for d in connected if device_serial_allowed(d.serial)]
    connected_set = set(connected_serials)

    selected = [
        s
        for s in (get_farm_selection().get("serials") or [])
        if str(s).strip() and str(s).strip() in connected_set
    ]
    serials = selected or connected_serials

    # Prefer ADB order, but keep selection order when using checked phones.
    if selected:
        order = selected
    else:
        order = connected_serials

    devices_meta = {d["serial"]: d for d in get_adb_devices(fast=True)}
    targets: list[dict[str, Any]] = []
    for serial in order:
        handle = username_for_device(serial)
        meta = devices_meta.get(serial) or {}
        targets.append(
            {
                "serial": serial,
                "short_serial": serial[-8:] if len(serial) > 8 else serial,
                "username": handle or "",
                "status": meta.get("status") or "device",
            }
        )
    return {
        "targets": targets,
        "source": "farm_selection" if selected else "all_connected",
        "presets": QUALITY_PRESETS,
    }


def _top_bottom_tile(image: Any, *, max_width: int = 360) -> Any:
    """Stack top + bottom of a phone screen so both ends stay readable.

    Tall phone UIs shrink too much in a grid; keeping the top (status/header)
    and bottom (nav/tabs) makes the full page readable in Telegram.
    """
    assert Image is not None
    img = image
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")

    w, h = img.size
    # Slightly more than half so chrome + nearby content stay visible.
    band = max(1, int(h * 0.45))
    top = img.crop((0, 0, w, band))
    bottom = img.crop((0, h - band, w, h))
    gap = 4
    stacked = Image.new("RGB", (w, top.height + gap + bottom.height), (18, 18, 18))
    stacked.paste(top, (0, 0))
    stacked.paste(bottom, (0, top.height + gap))

    max_w = max(120, min(1280, int(max_width)))
    if stacked.width > max_w:
        ratio = max_w / float(stacked.width)
        stacked = stacked.resize(
            (max_w, max(1, int(stacked.height * ratio))),
            Image.Resampling.BILINEAR,
        )
    return stacked


def capture_top_bottom_jpeg(
    serial: str, *, max_width: int = 360, quality: int = 60, wake: bool = True
) -> bytes:
    """One device frame as a top+bottom stacked JPEG."""
    _require_pillow()
    assert Image is not None
    # Capture larger first so the bands stay sharp after stacking.
    full = capture_jpeg(
        serial,
        max_size=max(720, max_width * 2),
        quality=min(85, quality + 15),
        wake=wake,
    )
    img = Image.open(io.BytesIO(full))
    img.load()
    tile = _top_bottom_tile(img, max_width=max_width)
    buf = io.BytesIO()
    tile.save(
        buf,
        format="JPEG",
        quality=max(20, min(95, int(quality))),
        optimize=True,
    )
    return buf.getvalue()


def build_farm_collage_jpeg(
    *,
    cell_max_size: int = 360,
    quality: int = 60,
    columns: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Capture all farm (or connected) phones into one labeled grid JPEG.

    Each tile shows the top and bottom of the screen stacked so the full page
    chrome is visible. Returns ``(jpeg_bytes, meta)``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _require_pillow()
    assert Image is not None

    info = list_preview_targets()
    targets = list(info.get("targets") or [])
    if not targets:
        raise RuntimeError("No farm phones connected")

    cols = columns or (3 if len(targets) <= 9 else 4)
    cols = max(1, min(6, int(cols)))

    # Wake all displays first so parallel captures aren't black.
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        list(pool.map(lambda t: wake_device_for_preview(str(t["serial"])), targets))
    time.sleep(0.4)

    def _capture_one(target: dict[str, Any]) -> tuple[dict[str, Any], Any, str | None]:
        serial = str(target["serial"])
        try:
            jpeg = capture_top_bottom_jpeg(
                serial, max_width=cell_max_size, quality=quality, wake=False
            )
            img = Image.open(io.BytesIO(jpeg))
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            elif img.mode == "L":
                img = img.convert("RGB")
            return target, img, None
        except Exception as exc:  # noqa: BLE001 - keep collage going
            return target, None, str(exc)

    captured: list[tuple[dict[str, Any], Any]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        futures = [pool.submit(_capture_one, t) for t in targets]
        results_by_serial: dict[str, tuple[dict[str, Any], Any, str | None]] = {}
        for fut in as_completed(futures):
            target, img, err = fut.result()
            results_by_serial[str(target["serial"])] = (target, img, err)

    # Preserve farm selection order.
    for target in targets:
        target, img, err = results_by_serial[str(target["serial"])]
        label = (target.get("username") or target.get("short_serial") or "").lstrip("@")
        if img is None:
            failures.append({"serial": str(target["serial"]), "error": err or "capture failed"})
            # Placeholder tile so layout stays stable.
            tile = Image.new("RGB", (cell_max_size, int(cell_max_size * 16 / 9)), (40, 40, 40))
        else:
            tile = img
        captured.append((target, _label_tile(tile, label or target.get("short_serial", "?"))))

    cell_w = max(im.width for _, im in captured)
    cell_h = max(im.height for _, im in captured)
    rows = (len(captured) + cols - 1) // cols
    pad = 8
    canvas = Image.new(
        "RGB",
        (cols * cell_w + (cols + 1) * pad, rows * cell_h + (rows + 1) * pad),
        (18, 18, 18),
    )
    for idx, (_, tile) in enumerate(captured):
        row, col = divmod(idx, cols)
        x = pad + col * (cell_w + pad) + (cell_w - tile.width) // 2
        y = pad + row * (cell_h + pad) + (cell_h - tile.height) // 2
        canvas.paste(tile, (x, y))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=max(20, min(95, int(quality))), optimize=True)
    meta = {
        "count": len(targets),
        "ok": len(targets) - len(failures),
        "failures": failures,
        "source": info.get("source"),
    }
    return buf.getvalue(), meta


def _label_tile(image: Any, label: str) -> Any:
    """Draw a dark label bar with the account handle at the bottom of a tile."""
    assert Image is not None
    from PIL import ImageDraw, ImageFont

    img = image.copy()
    draw = ImageDraw.Draw(img)
    text = f"@{label}" if label and not str(label).startswith("@") else (label or "?")
    try:
        font = ImageFont.load_default()
    except Exception:  # pragma: no cover
        font = None
    pad = 6
    # Approximate text size without depending on font.getbbox availability.
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = len(text) * 6, 11
    bar_h = th + pad * 2
    y0 = max(0, img.height - bar_h)
    draw.rectangle([0, y0, img.width, img.height], fill=(0, 0, 0))
    draw.text((pad, y0 + pad), text[:40], fill=(255, 255, 255), font=font)
    _ = tw  # unused; kept for future centering
    return img
