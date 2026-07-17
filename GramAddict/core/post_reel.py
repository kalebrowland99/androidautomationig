"""Instagram Reel posting automation (create flow + ADB media upload)."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from GramAddict.core.device_facade import DeviceFacade, Direction, Mode, Timeout
from GramAddict.core.utils import random_sleep

logger = logging.getLogger(__name__)

APP_ID = "com.instagram.android"
RID = lambda name: f"{APP_ID}:id/{name}"

CREATE_LEFT_CONTAINER = RID("action_bar_buttons_container_left")
TAB_BAR = RID("tab_bar")
HOME_TAB_DESC = "Home"
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


def adb_shell_safe(serial: str, *args: str, timeout: int = 120) -> Optional[subprocess.CompletedProcess[str]]:
    """adb_shell that never raises on timeout — returns None if it times out.

    Used for best-effort device cleanup (e.g. clearing the gallery) where a slow
    or unresponsive device must not abort the whole flow.
    """
    try:
        return adb_shell(serial, *args, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(
            "adb shell timed out after %ss (continuing): %s", timeout, " ".join(args)
        )
        return None


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
    adb_shell_safe(serial, "content", "delete", "--uri", MEDIASTORE_VIDEO_URI, timeout=60)
    if include_images:
        adb_shell_safe(serial, "content", "delete", "--uri", MEDIASTORE_IMAGE_URI, timeout=60)

    # 2) Remove any leftover physical files (incl. hidden ones) so a rescan
    #    can't re-add stale media. Run via `sh -c` so the device shell expands
    #    the glob; passing "dir/*" as a bare arg does not glob reliably. A slow
    #    device must not abort the clear, so these are best-effort (never raise).
    for directory in GALLERY_DIRS:
        adb_shell_safe(
            serial,
            "sh",
            "-c",
            f"find {directory} -maxdepth 1 -type f -delete 2>/dev/null; "
            f"rm -f {directory}/*.* 2>/dev/null; true",
            timeout=90,
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


def tap_home_tab(device: DeviceFacade) -> bool:
    """Land on the Home feed via the bottom-left Home tab.

    The top-left create (+) button only exists on the Home feed, so we always
    tap Home first — otherwise the create selector fails wherever Instagram
    happened to be. Falls back to a bottom-left tap if the tab can't be found.
    """
    d = device.deviceV2
    tab_bar = d(resourceId=TAB_BAR)
    home = tab_bar.child(description=HOME_TAB_DESC) if tab_bar.exists else d(description=HOME_TAB_DESC)
    if home.wait(timeout=5):
        home.click()
        random_sleep(0.6, 1.2, modulable=False)
        return True
    info = device.get_info()
    x = int(info["displayWidth"] * 0.1)
    y = int(info["displayHeight"] * 0.965)
    logger.warning("Home tab selector failed — fallback tap at (%s, %s)", x, y)
    d.click(x, y)
    random_sleep(0.6, 1.2, modulable=False)
    return True


def tap_create_button(device: DeviceFacade) -> bool:
    """Tap Instagram top-left + create button (always lands on Home first)."""
    tap_home_tab(device)
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


# Text on the Home-feed rows Instagram shows while a post is still uploading,
# e.g. "Keep Instagram open to finish posting…". Matched via substrings so it
# survives minor wording/ellipsis differences across app versions.
_UPLOAD_PENDING_SUBSTRINGS = ("finish posting", "Keep Instagram open")


def _upload_pending(device: DeviceFacade) -> bool:
    """True while at least one 'Keep Instagram open to finish posting…' row shows."""
    d = device.deviceV2
    for needle in _UPLOAD_PENDING_SUBSTRINGS:
        if d(textContains=needle).exists(timeout=1):
            return True
    return False


def _uploads_cleared(device: DeviceFacade) -> bool:
    """Confirm no pending-upload text remains, with a second check to avoid a
    false positive during a transient feed-render gap."""
    if _upload_pending(device):
        return False
    random_sleep(1.5, 2.5, modulable=False)
    return not _upload_pending(device)


def _open_reels_tab(device: DeviceFacade) -> bool:
    """Open the Reels/Clips tab (the one right of Home). Best-effort."""
    tap_home_tab(device)
    random_sleep(0.5, 1.0, modulable=False)
    try:
        from GramAddict.core.navigation import _tap_tab_right_of_home

        return bool(_tap_tab_right_of_home(device))
    except Exception as exc:
        logger.debug("Could not open Reels tab: %s", exc)
        return False


def _scroll_reels_for(device: DeviceFacade, duration: float) -> None:
    """Swipe up through Reels for ~`duration` seconds to keep the app active
    (Instagram pauses/slows background uploads when the app looks idle)."""
    deadline = time.time() + max(0.0, duration)
    while time.time() < deadline:
        try:
            device.swipe(Direction.UP, scale=0.9)
        except Exception as exc:
            logger.debug("Reels scroll swipe failed: %s", exc)
        # Watch each reel a few seconds before flicking to the next.
        random_sleep(3.0, 6.0, modulable=False)


def wait_for_uploads_to_finish(
    device: DeviceFacade,
    *,
    timeout: float = 1500.0,
    appear_timeout: float = 15.0,
    check_interval: float = 30.0,
) -> bool:
    """Wait for all pending uploads to finish, scrolling Reels while we wait.

    After posting, Instagram keeps uploading in the background and shows
    "Keep Instagram open to finish posting…" at the top of the Home feed;
    closing the app too early can drop the post. It also slows uploads when the
    app looks idle, so instead of staring at Home we scroll the Reels tab to
    keep it active, then pop back to Home every `check_interval` seconds to see
    if the "…finish posting…" text has cleared (the row may linger, but that
    text going away is the reliable "done" signal). Uploads can take 20+ min, so
    `timeout` defaults to 25 minutes. Returns True once cleared, else False.
    """
    tap_home_tab(device)
    random_sleep(1.5, 2.5, modulable=False)

    # Phase 1 — confirm at least one pending-upload row shows up. If none ever
    # appears, the upload was already instant/complete, so we're done.
    appear_deadline = time.time() + appear_timeout
    saw_pending = False
    while time.time() < appear_deadline:
        if _upload_pending(device):
            saw_pending = True
            break
        random_sleep(1.0, 1.5, modulable=False)
    if not saw_pending:
        logger.info("No pending-upload rows on Home — uploads already finished.")
        return True

    # Phase 2 — scroll Reels to keep the app active, checking Home every
    # `check_interval` seconds until the pending-upload text clears.
    logger.info(
        "Uploads in progress — scrolling Reels and checking Home every %ss…",
        int(check_interval),
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _open_reels_tab(device):
            _scroll_reels_for(device, min(check_interval, deadline - time.time()))
        else:
            # Couldn't reach Reels — just wait out the interval before rechecking.
            random_sleep(check_interval, check_interval + 2.0, modulable=False)

        tap_home_tab(device)
        random_sleep(1.5, 2.5, modulable=False)
        if _uploads_cleared(device):
            logger.info("All uploads finished — 'finish posting' text cleared.")
            return True
        logger.info("Still uploading — back to Reels to keep the app active…")

    logger.warning(
        "Upload wait timed out after %ss — a 'finish posting' row is still visible.",
        int(timeout),
    )
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


def _step_or_recover(device: DeviceFacade, step, *, name: str) -> bool:
    """Run a reel step; on failure, try to dismiss a blocking popup via the
    vision model and retry the step once. A modal/permission sheet covering the
    UI is a common reason a tap/selector "can't find" its target."""
    if step():
        return True
    try:
        from GramAddict.core.vision_popup import dismiss_popup_with_vision

        if dismiss_popup_with_vision(device, reason=f"post_reel:{name}"):
            random_sleep(0.8, 1.4, modulable=False)
            return step()
    except Exception as exc:
        logger.debug("Vision popup recovery failed for %s: %s", name, exc)
    return False


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

    if not _step_or_recover(device, lambda: tap_create_button(device), name="tap_create"):
        return {"success": False, "message": "Could not tap create (+) button", "steps": steps}
    steps.append("tap_create")

    random_sleep(1.0, 2.0, modulable=False)

    if not _step_or_recover(
        device, lambda: select_recent_media(device, gallery_select_number), name="select_media"
    ):
        return {
            "success": False,
            "message": f"Could not select gallery item #{gallery_select_number}",
            "steps": steps,
        }
    steps.append(f"select_media:{gallery_select_number}")

    if not _step_or_recover(device, lambda: tap_next_top(device), name="next_top"):
        return {"success": False, "message": "Could not tap top-right Next", "steps": steps}
    steps.append("next_top")

    dismiss_popups_center(device, taps=3)
    steps.append("dismiss_popups")

    if not _step_or_recover(device, lambda: tap_next_clips(device), name="next_clips"):
        return {"success": False, "message": "Could not tap clips Next", "steps": steps}
    steps.append("next_clips")

    if not _step_or_recover(
        device, lambda: enter_caption(device, caption, paste=paste_caption), name="caption"
    ):
        return {"success": False, "message": "Could not enter caption", "steps": steps}
    steps.append("caption")

    if not _step_or_recover(device, lambda: tap_share(device), name="share"):
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
