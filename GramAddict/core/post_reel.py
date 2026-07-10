"""Instagram Reel posting automation (create flow + ADB media upload)."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from GramAddict.core.device_facade import DeviceFacade, Mode, Timeout
from GramAddict.core.utils import random_sleep

logger = logging.getLogger(__name__)

APP_ID = "com.instagram.android"
RID = lambda name: f"{APP_ID}:id/{name}"

CREATE_LEFT_CONTAINER = RID("action_bar_buttons_container_left")
GALLERY_THUMB = RID("gallery_grid_item_thumbnail")
NEXT_TOP = RID("next_button_textview")
CLIPS_NEXT = RID("clips_right_action_button")
CAPTION_INPUT = RID("caption_input_text_view")
SHARE_BUTTON = RID("share_button")

# Physical media folders scanned by the Android gallery / MediaStore.
# /sdcard is a symlink to /storage/emulated/0 on this device.
GALLERY_DIRS = (
    "/sdcard/DCIM/Camera",
    "/sdcard/DCIM/Camera1",
    "/sdcard/Pictures",
    "/sdcard/Movies",
    "/sdcard/Download",
)
DEVICE_MEDIA_DIR = "/sdcard/DCIM/Camera"

# The Instagram/gallery picker reads from the MediaStore content provider,
# not the folders directly. Clearing files alone leaves stale entries behind,
# so we must also delete these rows for the gallery to appear empty.
MEDIASTORE_VIDEO_URI = "content://media/external/video/media"
MEDIASTORE_IMAGE_URI = "content://media/external/images/media"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def resolve_adb_path() -> str:
    import sys

    tools = Path(__file__).resolve().parent.parent.parent / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    from android_devices import resolve_adb

    return resolve_adb()


def adb_shell(serial: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    adb = resolve_adb_path()
    cmd = [adb, "-s", serial, "shell", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def adb_push(serial: str, local_path: Path, remote_path: str, timeout: int = 300) -> bool:
    adb = resolve_adb_path()
    cmd = [adb, "-s", serial, "push", str(local_path), remote_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.error("adb push failed: %s", (result.stderr or result.stdout).strip())
        return False
    return True


def media_scan_file(serial: str, remote_path: str) -> None:
    adb_shell(
        serial,
        "am",
        "broadcast",
        "-a",
        "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d",
        f"file://{remote_path}",
        timeout=30,
    )


def count_mediastore_videos(serial: str) -> int:
    """Return how many videos the gallery/MediaStore currently indexes."""
    result = adb_shell(
        serial,
        "content",
        "query",
        "--uri",
        MEDIASTORE_VIDEO_URI,
        "--projection",
        "_id",
        timeout=30,
    )
    return (result.stdout or "").count("Row:")


def clear_device_gallery(serial: str, include_images: bool = True) -> None:
    """Empty the gallery as seen by Instagram's media picker.

    The picker reads from the MediaStore content provider, so we delete those
    rows (this also removes the underlying files on this Android version) and
    then remove any leftover physical files from the common media folders.
    """
    # 1) Delete the MediaStore rows the gallery actually reads from.
    adb_shell(serial, "content", "delete", "--uri", MEDIASTORE_VIDEO_URI, timeout=60)
    if include_images:
        adb_shell(serial, "content", "delete", "--uri", MEDIASTORE_IMAGE_URI, timeout=60)

    # 2) Remove any leftover physical files (incl. hidden ones) so a rescan
    #    can't re-add stale media. Run via `sh -c` so the device shell expands
    #    the glob; passing "dir/*" as a bare arg does not glob reliably.
    for directory in GALLERY_DIRS:
        adb_shell(
            serial,
            "sh",
            "-c",
            f"find {directory} -maxdepth 1 -type f -delete 2>/dev/null; "
            f"rm -f {directory}/*.* 2>/dev/null; true",
            timeout=60,
        )

    # 3) Rescan so the MediaStore reflects the now-empty folders.
    for directory in (DEVICE_MEDIA_DIR, "/sdcard/Pictures", "/sdcard/Movies"):
        media_scan_file(serial, directory)


def list_local_media(media_dir: Path) -> list[Path]:
    if not media_dir.is_dir():
        return []
    files = [
        p
        for p in sorted(media_dir.iterdir())
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    return files


def push_video_to_gallery(serial: str, local_path: Path) -> Optional[str]:
    """Push one video to DCIM/Camera and index it into the MediaStore.

    Returns the remote path once the gallery has picked it up, else None.
    """
    remote = f"{DEVICE_MEDIA_DIR}/{local_path.name}"
    if not adb_push(serial, local_path, remote):
        return None

    # Trigger a scan and wait for the MediaStore to index the file so it shows
    # up as the newest item in the gallery picker. MediaStore normalizes paths
    # to /storage/emulated/0/..., so match on the unique filename instead of the
    # /sdcard/... path (avoids adb arg-quoting issues with a --where clause).
    for attempt in range(6):
        media_scan_file(serial, remote)
        random_sleep(1.0, 1.5, modulable=False)
        check = adb_shell(
            serial,
            "content",
            "query",
            "--uri",
            MEDIASTORE_VIDEO_URI,
            "--projection",
            "_data",
            timeout=30,
        )
        if local_path.name in (check.stdout or ""):
            logger.info("Pushed video indexed in gallery: %s", remote)
            return remote
        logger.debug("Waiting for gallery to index %s (attempt %s)", remote, attempt + 1)

    logger.warning("Video pushed but not confirmed in MediaStore: %s", remote)
    return remote


def tap_create_button(device: DeviceFacade) -> bool:
    """Tap Instagram top-left + create button."""
    d = device.deviceV2
    left = d(resourceId=CREATE_LEFT_CONTAINER)
    if left.wait(timeout=5):
        img = left.child(className="android.widget.ImageView")
        if img.wait(timeout=3):
            img.click()
            random_sleep(0.4, 0.8, modulable=False)
            return True
    info = device.get_info()
    x = int(info["displayWidth"] * 0.08)
    y = int(info["displayHeight"] * 0.07)
    logger.warning("Create button selector failed — fallback tap at (%s, %s)", x, y)
    d.click(x, y)
    random_sleep(0.4, 0.8, modulable=False)
    return True


def select_recent_media(device: DeviceFacade, number: int) -> bool:
    """
    number is 1-based:
    1 = newest recent media, 2 = second newest, etc.
    """
    if number < 1:
        return False
    d = device.deviceV2
    thumbs = d(resourceId=GALLERY_THUMB)
    if not thumbs.wait(timeout=8):
        logger.error("Gallery thumbnails not found")
        return False
    index = number - 1
    count = thumbs.count
    if count <= index:
        logger.error("Gallery has %s item(s); need index %s (select #%s)", count, index, number)
        return False
    thumbs[index].click()
    random_sleep(0.5, 1.0, modulable=False)
    return True


def tap_next_top(device: DeviceFacade) -> bool:
    d = device.deviceV2
    btn = d(resourceId=NEXT_TOP)
    if btn.wait(timeout=8):
        btn.click()
        random_sleep(0.5, 1.0, modulable=False)
        return True
    return False


def dismiss_popups_center(device: DeviceFacade, taps: int = 3) -> None:
    """Tap screen center to dismiss overlays."""
    info = device.get_info()
    x = int(info["displayWidth"] / 2)
    y = int(info["displayHeight"] / 2)
    d = device.deviceV2
    for _ in range(taps):
        d.click(x, y)
        random_sleep(0.25, 0.45, modulable=False)


def tap_next_clips(device: DeviceFacade) -> bool:
    d = device.deviceV2
    btn = d(resourceId=CLIPS_NEXT)
    if btn.wait(timeout=10):
        btn.click()
        random_sleep(0.5, 1.0, modulable=False)
        return True
    return False


def tap_caption_field(device: DeviceFacade) -> bool:
    field = device.find(resourceId=CAPTION_INPUT)
    if field.exists(Timeout.MEDIUM):
        field.click()
        random_sleep(0.3, 0.6, modulable=False)
        return True
    d = device.deviceV2
    caption = d(resourceId=CAPTION_INPUT)
    if caption.wait(timeout=5):
        caption.click()
        random_sleep(0.3, 0.6, modulable=False)
        return True
    return False


def enter_caption(device: DeviceFacade, text: str, *, paste: bool = True) -> bool:
    if not tap_caption_field(device):
        return False
    field = device.find(resourceId=CAPTION_INPUT)
    if not field.exists(Timeout.SHORT):
        return False
    field.set_text(text, Mode.PASTE if paste else Mode.TYPE)
    random_sleep(0.4, 0.8, modulable=False)
    return True


def tap_share(device: DeviceFacade) -> bool:
    d = device.deviceV2
    btn = d(resourceId=SHARE_BUTTON)
    if btn.wait(timeout=8):
        btn.click()
        random_sleep(1.0, 2.0, modulable=False)
        return True
    return False


def wait_for_post_success(device: DeviceFacade, timeout: float = 45.0) -> bool:
    """Wait until share UI closes (reel upload finished or left composer)."""
    d = device.deviceV2
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not d(resourceId=SHARE_BUTTON).exists(timeout=1):
            if not d(resourceId=CAPTION_INPUT).exists(timeout=1):
                random_sleep(1.0, 2.0, modulable=False)
                return True
        random_sleep(0.8, 1.2, modulable=False)
    return False


def prepare_gallery_with_media(
    serial: str,
    media_dir: Path,
    media_index: int,
    *,
    clear_first: bool = True,
) -> Optional[Path]:
    """Clear gallery (optional), push one local video. media_index is 0-based."""
    files = list_local_media(media_dir)
    if not files:
        logger.error("No video files in %s", media_dir)
        return None
    if media_index >= len(files):
        logger.error("Media index %s out of range (%s files)", media_index, len(files))
        return None
    local = files[media_index]
    if clear_first:
        clear_device_gallery(serial)
    remote = push_video_to_gallery(serial, local)
    if not remote:
        return None
    return local


def run_single_reel_post(
    device: DeviceFacade,
    serial: str,
    *,
    media_dir: Path,
    media_index: int,
    gallery_select_number: int,
    caption: str,
    clear_gallery: bool = True,
    paste_caption: bool = True,
) -> dict[str, Any]:
    """
    Full single-reel flow. Returns dict with success, message, and steps completed.
    Does NOT increment any counter — caller handles persistence after success.
    """
    steps: list[str] = []

    local = prepare_gallery_with_media(
        serial, media_dir, media_index, clear_first=clear_gallery
    )
    if local is None:
        return {"success": False, "message": "Failed to prepare gallery media", "steps": steps}

    steps.append(f"pushed:{local.name}")

    if not tap_create_button(device):
        return {"success": False, "message": "Could not tap create (+) button", "steps": steps}
    steps.append("tap_create")

    random_sleep(1.0, 2.0, modulable=False)

    if not select_recent_media(device, gallery_select_number):
        return {
            "success": False,
            "message": f"Could not select gallery item #{gallery_select_number}",
            "steps": steps,
        }
    steps.append(f"select_media:{gallery_select_number}")

    if not tap_next_top(device):
        return {"success": False, "message": "Could not tap top-right Next", "steps": steps}
    steps.append("next_top")

    dismiss_popups_center(device, taps=3)
    steps.append("dismiss_popups")

    if not tap_next_clips(device):
        return {"success": False, "message": "Could not tap clips Next", "steps": steps}
    steps.append("next_clips")

    if not enter_caption(device, caption, paste=paste_caption):
        return {"success": False, "message": "Could not enter caption", "steps": steps}
    steps.append("caption")

    if not tap_share(device):
        return {"success": False, "message": "Could not tap Share", "steps": steps}
    steps.append("share")

    if not wait_for_post_success(device):
        return {
            "success": False,
            "message": "Share tapped but post success not confirmed",
            "steps": steps,
        }
    steps.append("confirmed")

    return {
        "success": True,
        "message": f"Reel posted ({local.name})",
        "steps": steps,
        "media_file": local.name,
    }
