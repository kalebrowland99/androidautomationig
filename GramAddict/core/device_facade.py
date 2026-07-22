import logging
import shutil
import string
from datetime import datetime
from enum import Enum, auto
from inspect import stack
from pathlib import Path
from random import randint, uniform
from re import search
from subprocess import PIPE, run
from time import sleep
from typing import Optional

import uiautomator2

from GramAddict.core.utils import check_instagram_rate_limit, random_sleep

logger = logging.getLogger(__name__)

# Plain (unzipped) screen recordings land here — easy to browse.
VIDEOS_DIR = Path("videos")
VIDEOS_CRASHED_DIR = VIDEOS_DIR / "crashed"
VIDEOS_SESSIONS_DIR = VIDEOS_DIR / "sessions"
VIDEOS_TMP_DIR = VIDEOS_DIR / ".tmp"


def create_device(device_id, app_id):
    try:
        return DeviceFacade(device_id, app_id)
    except ImportError as e:
        logger.error(str(e))
        return None


def get_device_info(device):
    logger.debug(
        f"Phone Name: {device.get_info()['productName']}, SDK Version: {device.get_info()['sdkInt']}"
    )
    if int(device.get_info()["sdkInt"]) < 19:
        logger.warning("Only Android 4.4+ (SDK 19+) devices are supported!")
    logger.debug(
        f"Screen dimension: {device.get_info()['displayWidth']}x{device.get_info()['displayHeight']}"
    )
    logger.debug(
        f"Screen resolution: {device.get_info()['displaySizeDpX']}x{device.get_info()['displaySizeDpY']}"
    )
    logger.debug(f"Device ID: {device.deviceV2.serial}")


class Timeout(Enum):
    ZERO = auto()
    TINY = auto()
    SHORT = auto()
    MEDIUM = auto()
    LONG = auto()


class SleepTime(Enum):
    ZERO = auto()
    TINY = auto()
    SHORT = auto()
    DEFAULT = auto()


class Location(Enum):
    CUSTOM = auto()
    WHOLE = auto()
    CENTER = auto()
    BOTTOM = auto()
    RIGHT = auto()
    LEFT = auto()
    BOTTOMRIGHT = auto()
    LEFTEDGE = auto()
    RIGHTEDGE = auto()
    TOPLEFT = auto()


class Direction(Enum):
    UP = auto()
    DOWN = auto()
    RIGHT = auto()
    LEFT = auto()


class Mode(Enum):
    TYPE = auto()
    PASTE = auto()


class DeviceFacade:
    def __init__(self, device_id, app_id):
        self.device_id = device_id
        self.app_id = app_id
        try:
            if device_id is None or "." not in device_id:
                self.deviceV2 = uiautomator2.connect(
                    "" if device_id is None else device_id
                )
            else:
                self.deviceV2 = uiautomator2.connect_adb_wifi(f"{device_id}")
        except ImportError:
            raise ImportError("Please install uiautomator2: pip3 install uiautomator2")

    def _get_current_app(self):
        try:
            return self.deviceV2.app_current()["package"]
        except uiautomator2.JSONRPCError as e:
            raise DeviceFacade.JsonRpcError(e)

    def _ig_is_opened(self) -> bool:
        return self._get_current_app() == self.app_id

    def check_if_ig_is_opened(func):
        def wrapper(self, **kwargs):
            # Callers that must work when IG is closed / not focused (crash recovery,
            # rate-limit dialogs, system popups).
            avoid_lst = {
                "choose_cloned_app",
                "check_if_crash_popup_is_there",
                "is_instagram_try_again_later_visible",
                "dismiss_instagram_try_again_later",
                "check_instagram_rate_limit",
                "restart",
            }
            caller = stack()[1].function
            if not self._ig_is_opened() and caller not in avoid_lst:
                raise DeviceFacade.AppHasCrashed("App has crashed / has been closed!")
            return func(self, **kwargs)

        return wrapper

    @check_if_ig_is_opened
    def find(
        self,
        index=None,
        **kwargs,
    ):
        return self.find_any(index=index, **kwargs)

    def find_any(
        self,
        index=None,
        **kwargs,
    ):
        try:
            view = self.deviceV2(**kwargs)
            if index is not None and view.count > 1:
                view = self.deviceV2(**kwargs)[index]
        except uiautomator2.JSONRPCError as e:
            raise DeviceFacade.JsonRpcError(e)
        return DeviceFacade.View(view=view, device=self.deviceV2)

    def back(self, modulable: bool = True):
        logger.debug("Press back button.")
        self.deviceV2.press("back")
        random_sleep(modulable=modulable)

    def _screenrecord_account_slug(self) -> str:
        """Best-effort account name for the video filename."""
        try:
            from GramAddict.core import utils as _utils

            username = getattr(getattr(_utils, "args", None), "username", None)
            if username:
                return str(username).lstrip("@").replace("/", "_")
        except Exception:
            pass
        serial = str(getattr(self, "device_id", "") or "device")
        return serial[-8:] if len(serial) > 8 else serial or "device"

    def _archive_screenrecord(self, src: Path, *, crashed: bool) -> Optional[Path]:
        """Move a finished recording into videos/crashed or videos/sessions."""
        if not src.is_file() or src.stat().st_size <= 0:
            try:
                src.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        dest_dir = VIDEOS_CRASHED_DIR if crashed else VIDEOS_SESSIONS_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        slug = self._screenrecord_account_slug()
        kind = "crash" if crashed else "session"
        dest = dest_dir / f"{stamp}_{slug}_{kind}.mp4"
        n = 2
        while dest.exists():
            dest = dest_dir / f"{stamp}_{slug}_{kind}_{n}.mp4"
            n += 1
        try:
            shutil.move(str(src), str(dest))
        except OSError as exc:
            logger.warning("Could not archive screen recording to %s: %s", dest, exc)
            return None
        logger.info(
            "Screen recording saved → %s",
            dest,
        )
        return dest

    def start_screenrecord(self, output=None, fps=20):
        """Start screen recording into videos/.tmp (archived on stop)."""
        import imageio

        def _run_MOD(self):
            from collections import deque

            pipelines = [self._pipe_limit, self._pipe_convert, self._pipe_resize]
            _iter = self._iter_minicap()
            for p in pipelines:
                _iter = p(_iter)

            # Keep ~30s of frames; always write them on stop (normal OR crash)
            # so videos/sessions gets usable clips too.
            with imageio.get_writer(self._filename, fps=self._fps) as wr:
                frames = deque(maxlen=self._fps * 30)
                for im in _iter:
                    frames.append(im)
                for frame in frames:
                    wr.append_data(frame)
            self._done_event.set()

        def stop_MOD(self, crash=True):
            """Stop record and finish writing the video."""
            if self._running:
                self.crash = crash
                self._stop_event.set()
                ret = self._done_event.wait(10.0)

                # reset
                self._stop_event.clear()
                self._done_event.clear()
                self._running = False
                return ret

        from uiautomator2 import screenrecord as _sr

        _sr.Screenrecord._run = _run_MOD
        _sr.Screenrecord.stop = stop_MOD

        VIDEOS_TMP_DIR.mkdir(parents=True, exist_ok=True)
        VIDEOS_CRASHED_DIR.mkdir(parents=True, exist_ok=True)
        VIDEOS_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        if output is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            serial = str(getattr(self, "device_id", "") or "dev")[-8:]
            output = str(VIDEOS_TMP_DIR / f"recording_{serial}_{stamp}.mp4")
        self._screenrecord_path = Path(output)
        self.deviceV2.screenrecord(output, fps)
        logger.warning("Screen recording has been started → %s", output)

    def stop_screenrecord(self, crash=True):
        """Stop recording and archive to videos/crashed or videos/sessions."""
        ok = False
        try:
            ok = bool(self.deviceV2.screenrecord.stop(crash=crash))
        except Exception as exc:
            logger.debug("Screen recorder stop raised: %s", exc)
        if ok:
            logger.warning("Screen recorder has been stopped successfully!")
        src = getattr(self, "_screenrecord_path", None)
        if src is None:
            # Fallback: newest file in the temp dir.
            try:
                candidates = sorted(
                    VIDEOS_TMP_DIR.glob("recording_*.mp4"),
                    key=lambda p: p.stat().st_mtime,
                )
                src = candidates[-1] if candidates else None
            except OSError:
                src = None
        if src is not None:
            self._archive_screenrecord(Path(src), crashed=bool(crash))
        return ok

    def screenshot(self, path=None):
        if path is None:
            return self.deviceV2.screenshot()
        else:
            self.deviceV2.screenshot(path)

    def dump_hierarchy(self, path):
        xml_dump = self.deviceV2.dump_hierarchy()
        with open(path, "w", encoding="utf-8") as outfile:
            outfile.write(xml_dump)

    def press_power(self):
        self.deviceV2.press("power")
        sleep(2)

    def close_all_apps(self):
        """Force-stop every running third-party app, then land on the Android
        home screen. Used at session start for a clean slate."""
        try:
            self.deviceV2.app_stop_all()
        except Exception as e:
            logger.debug(f"Could not stop all apps: {e}")
        try:
            self.deviceV2.press("home")
        except Exception as e:
            logger.debug(f"Could not press home: {e}")
        sleep(1)

    def is_screen_locked(self):
        data = run(
            f"adb -s {self.deviceV2.serial} shell dumpsys window",
            encoding="utf-8",
            stdout=PIPE,
            stderr=PIPE,
            shell=True,
        )
        if data != "":
            flag = search("mDreamingLockscreen=(true|false)", data.stdout)
            return flag is not None and flag.group(1) == "true"
        else:
            logger.debug(
                f"'adb -s {self.deviceV2.serial} shell dumpsys window' returns nothing!"
            )
            return None

    def _is_keyboard_show(self):
        data = run(
            f"adb -s {self.deviceV2.serial} shell dumpsys input_method",
            encoding="utf-8",
            stdout=PIPE,
            stderr=PIPE,
            shell=True,
        )
        if data != "":
            flag = search("mInputShown=(true|false)", data.stdout)
            return flag.group(1) == "true"
        else:
            logger.debug(
                f"'adb -s {self.deviceV2.serial} shell dumpsys input_method' returns nothing!"
            )
            return None

    def is_alive(self):
        try:
            return self.deviceV2._is_alive()  # deprecated method
        except AttributeError:
            return self.deviceV2.server.alive

    def wake_up(self):
        """Make sure agent is alive or bring it back up before starting."""
        if self.deviceV2 is not None:
            attempts = 0
            while not self.is_alive() and attempts < 5:
                self.get_info()
                attempts += 1
            self.disable_auto_rotate()

    def reconnect(self) -> bool:
        """Re-establish uiautomator2 after atx-agent/USB/WiFi hiccups."""
        serial = self.device_id
        if self.deviceV2 is not None:
            try:
                serial = self.deviceV2.serial or serial
            except Exception:
                pass
        try:
            logger.warning("Reconnecting to device %s...", serial)
            if serial is None or "." not in str(serial):
                self.deviceV2 = uiautomator2.connect(
                    "" if serial is None else serial
                )
            else:
                self.deviceV2 = uiautomator2.connect_adb_wifi(f"{serial}")
            self.wake_up()
            return self.is_alive()
        except Exception as e:
            logger.error("Device reconnect failed: %s", e)
            return False

    def disable_auto_rotate(self) -> None:
        """Keep portrait locked — auto-rotate causes missed UI elements on feed/reels."""
        try:
            self.deviceV2.shell("settings put system accelerometer_rotation 0")
            self.deviceV2.shell("settings put system user_rotation 0")
            logger.debug("Auto-rotate disabled (portrait locked).")
        except uiautomator2.JSONRPCError as e:
            logger.debug(f"Could not disable auto-rotate: {e}")

    def unlock(self):
        self.swipe(Direction.UP, 0.8)
        sleep(2)
        logger.debug(f"Screen locked: {self.is_screen_locked()}")
        if self.is_screen_locked():
            self.swipe(Direction.RIGHT, 0.8)
            sleep(2)
            logger.debug(f"Screen locked: {self.is_screen_locked()}")

    def screen_off(self):
        self.deviceV2.screen_off()

    def get_orientation(self):
        try:
            return self.deviceV2._get_orientation()
        except uiautomator2.JSONRPCError as e:
            raise DeviceFacade.JsonRpcError(e)

    def window_size(self):
        """return (width, height)"""
        try:
            self.deviceV2.window_size()
        except uiautomator2.JSONRPCError as e:
            raise DeviceFacade.JsonRpcError(e)

    def swipe(self, direction: Direction, scale=0.5):
        """Swipe finger in the `direction`.
        Scale is the sliding distance. Default to 50% of the screen width
        """
        swipe_dir = ""
        if direction == Direction.UP:
            swipe_dir = "up"
        elif direction == Direction.RIGHT:
            swipe_dir = "right"
        elif direction == Direction.LEFT:
            swipe_dir = "left"
        elif direction == Direction.DOWN:
            swipe_dir = "down"

        logger.debug(f"Swipe {swipe_dir}, scale={scale}")

        try:
            self.deviceV2.swipe_ext(swipe_dir, scale=scale)
            DeviceFacade.sleep_mode(SleepTime.TINY)
        except uiautomator2.JSONRPCError as e:
            raise DeviceFacade.JsonRpcError(e)

    def swipe_points(
        self,
        sx,
        sy,
        ex,
        ey,
        random_x=True,
        random_y=True,
        duration=None,
    ):
        if random_x:
            sx = int(sx * uniform(0.85, 1.15))
            ex = int(ex * uniform(0.85, 1.15))
        if random_y:
            ey = int(ey * uniform(0.98, 1.02))
        sy = int(sy)
        # Slow swipes (200–500ms) on the home feed feel like a hold to Instagram
        # (reels, carousels, etc.). Pass a shorter duration for feed flicks.
        if duration is None:
            duration = uniform(0.2, 0.5)
        try:
            logger.debug(
                f"Swipe from: ({sx},{sy}) to ({ex},{ey}), duration={duration:.3f}s."
            )
            self.deviceV2.swipe_points([[sx, sy], [ex, ey]], duration)
            DeviceFacade.sleep_mode(SleepTime.TINY)
        except uiautomator2.JSONRPCError as e:
            raise DeviceFacade.JsonRpcError(e)

    def double_tap_screen_center(self, jitter_ratio: float = 0.08) -> tuple[int, int]:
        """Double tap a random point near the center of the screen."""
        info = self.get_info()
        width = info["displayWidth"]
        height = info["displayHeight"]
        center_x = width / 2
        center_y = height / 2
        jitter_x = width * jitter_ratio
        jitter_y = height * jitter_ratio
        x = int(uniform(center_x - jitter_x, center_x + jitter_x))
        y = int(uniform(center_y - jitter_y, center_y + jitter_y))
        duration = uniform(0.050, 0.140)
        logger.debug(f"Double tap near screen center at ({x},{y})")
        try:
            self.deviceV2.double_click(x, y, duration=duration)
            DeviceFacade.sleep_mode(SleepTime.DEFAULT)
        except uiautomator2.JSONRPCError as e:
            raise DeviceFacade.JsonRpcError(e)
        return x, y

    def get_info(self):
        # {'currentPackageName': 'net.oneplus.launcher', 'displayHeight': 1920, 'displayRotation': 0, 'displaySizeDpX': 411,
        # 'displaySizeDpY': 731, 'displayWidth': 1080, 'productName': 'OnePlus5', '
        #  screenOn': True, 'sdkInt': 27, 'naturalOrientation': True}
        try:
            return self.deviceV2.info
        except uiautomator2.JSONRPCError as e:
            raise DeviceFacade.JsonRpcError(e)

    @staticmethod
    def sleep_mode(mode):
        mode = SleepTime.DEFAULT if mode is None else mode
        if mode == SleepTime.DEFAULT:
            random_sleep()
        elif mode == SleepTime.TINY:
            random_sleep(0, 1)
        elif mode == SleepTime.SHORT:
            random_sleep(1, 2)
        elif mode == SleepTime.ZERO:
            pass

    class View:
        deviceV2 = None  # uiautomator2
        viewV2 = None  # uiautomator2

        def __init__(self, view, device):
            self.viewV2 = view
            self.deviceV2 = device

        def __iter__(self):
            children = []
            try:
                children.extend(
                    DeviceFacade.View(view=item, device=self.deviceV2)
                    for item in self.viewV2
                )
                return iter(children)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def ui_info(self):
            try:
                return self.viewV2.info
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def get_desc(self):
            try:
                return self.viewV2.info["contentDescription"]
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def child(self, *args, **kwargs):
            try:
                view = self.viewV2.child(*args, **kwargs)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)
            return DeviceFacade.View(view=view, device=self.deviceV2)

        def sibling(self, *args, **kwargs):
            try:
                view = self.viewV2.sibling(*args, **kwargs)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)
            return DeviceFacade.View(view=view, device=self.deviceV2)

        def left(self, *args, **kwargs):
            try:
                view = self.viewV2.left(*args, **kwargs)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)
            return DeviceFacade.View(view=view, device=self.deviceV2)

        def right(self, *args, **kwargs):
            try:
                view = self.viewV2.right(*args, **kwargs)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)
            return DeviceFacade.View(view=view, device=self.deviceV2)

        def up(self, *args, **kwargs):
            try:
                view = self.viewV2.up(*args, **kwargs)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)
            return DeviceFacade.View(view=view, device=self.deviceV2)

        def down(self, *args, **kwargs):
            try:
                view = self.viewV2.down(*args, **kwargs)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)
            return DeviceFacade.View(view=view, device=self.deviceV2)

        def click_gone(self, maxretry=3, interval=1.0):
            try:
                self.viewV2.click_gone(maxretry, interval)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def _recover_and_reclick(
            self, error, mode, sleep, coord, crash_report_if_fails
        ):
            """A click failed because the element isn't there — often a popup is
            covering it. Ask the vision model to dismiss the popup and, if it did,
            retry the click once. Otherwise re-raise the original error."""
            check_instagram_rate_limit(self.deviceV2)
            try:
                from GramAddict.core.vision_popup import dismiss_popup_with_vision

                dismissed = dismiss_popup_with_vision(self.deviceV2, reason="click")
            except Exception:
                dismissed = False
            if not dismissed:
                raise DeviceFacade.JsonRpcError(error)
            random_sleep(1, 2, modulable=False)
            self.click(
                mode,
                sleep,
                coord,
                crash_report_if_fails,
                _allow_vision_retry=False,
            )

        def click(
            self,
            mode=None,
            sleep=None,
            coord=None,
            crash_report_if_fails=True,
            _allow_vision_retry=True,
        ):
            if coord is None:
                coord = []
            mode = Location.WHOLE if mode is None else mode
            if mode == Location.WHOLE:
                x_offset = uniform(0.15, 0.85)
                y_offset = uniform(0.15, 0.85)

            elif mode == Location.LEFT:
                x_offset = uniform(0.15, 0.4)
                y_offset = uniform(0.15, 0.85)

            elif mode == Location.LEFTEDGE:
                x_offset = uniform(0.1, 0.2)
                y_offset = uniform(0.40, 0.60)

            elif mode == Location.CENTER:
                x_offset = uniform(0.4, 0.6)
                y_offset = uniform(0.15, 0.85)

            elif mode == Location.RIGHT:
                x_offset = uniform(0.6, 0.85)
                y_offset = uniform(0.15, 0.85)

            elif mode == Location.RIGHTEDGE:
                x_offset = uniform(0.8, 0.9)
                y_offset = uniform(0.40, 0.60)

            elif mode == Location.BOTTOMRIGHT:
                x_offset = uniform(0.8, 0.9)
                y_offset = uniform(0.8, 0.9)

            elif mode == Location.TOPLEFT:
                x_offset = uniform(0.05, 0.15)
                y_offset = uniform(0.05, 0.25)
            elif mode == Location.CUSTOM:
                try:
                    logger.debug(f"Single click ({coord[0]},{coord[1]})")
                    self.deviceV2.click(coord[0], coord[1])
                    DeviceFacade.sleep_mode(sleep)
                    return
                except uiautomator2.JSONRPCError as e:
                    if crash_report_if_fails:
                        if _allow_vision_retry:
                            self._recover_and_reclick(
                                e, mode, sleep, coord, crash_report_if_fails
                            )
                            return
                        raise DeviceFacade.JsonRpcError(e)
                    else:
                        logger.debug("Trying to press on a obj which is gone.")

            else:
                x_offset = 0.5
                y_offset = 0.5

            try:
                visible_bounds = self.get_bounds()
                x_abs = int(
                    visible_bounds["left"]
                    + (visible_bounds["right"] - visible_bounds["left"]) * x_offset
                )
                y_abs = int(
                    visible_bounds["top"]
                    + (visible_bounds["bottom"] - visible_bounds["top"]) * y_offset
                )

                logger.debug(
                    f"Single click in ({x_abs},{y_abs}). Surface: ({visible_bounds['left']}-{visible_bounds['right']},{visible_bounds['top']}-{visible_bounds['bottom']})"
                )
                self.viewV2.click(
                    self.get_ui_timeout(Timeout.LONG),
                    offset=(x_offset, y_offset),
                )
                DeviceFacade.sleep_mode(sleep)

            except uiautomator2.JSONRPCError as e:
                if crash_report_if_fails:
                    if _allow_vision_retry:
                        self._recover_and_reclick(
                            e, mode, sleep, coord, crash_report_if_fails
                        )
                        return
                    raise DeviceFacade.JsonRpcError(e)
                else:
                    logger.debug("Trying to press on a obj which is gone.")

        def click_retry(self, mode=None, sleep=None, coord=None, maxretry=2):
            """return True if successfully open the element, else False"""
            if coord is None:
                coord = []
            self.click(mode, sleep, coord)

            while maxretry > 0:
                # we wait a little more before try again
                random_sleep(2, 4, modulable=False)
                if not self.exists():
                    return True
                logger.debug("UI element didn't open! Try again..")
                self.click(mode, sleep, coord)
                maxretry -= 1
            if not self.exists():
                return True
            logger.warning("Failed to open the UI element!")
            return False

        def double_click(self, padding=0.3, obj_over=0):
            """Double click randomly in the selected view using padding
            padding: % of how far from the borders we want the double
                    click to happen.
            """
            visible_bounds = self.get_bounds()
            horizontal_len = visible_bounds["right"] - visible_bounds["left"]
            vertical_len = visible_bounds["bottom"] - max(
                visible_bounds["top"], obj_over
            )
            horizontal_padding = int(padding * horizontal_len)
            vertical_padding = int(padding * vertical_len)
            random_x = int(
                uniform(
                    visible_bounds["left"] + horizontal_padding,
                    visible_bounds["right"] - horizontal_padding,
                )
            )
            random_y = int(
                uniform(
                    visible_bounds["top"] + vertical_padding,
                    visible_bounds["bottom"] - vertical_padding,
                )
            )

            time_between_clicks = uniform(0.050, 0.140)

            try:
                logger.debug(
                    f"Double click in ({random_x},{random_y}) with t={int(time_between_clicks*1000)}ms. Surface: ({visible_bounds['left']}-{visible_bounds['right']},{visible_bounds['top']}-{visible_bounds['bottom']})."
                )
                self.deviceV2.double_click(
                    random_x, random_y, duration=time_between_clicks
                )
                DeviceFacade.sleep_mode(SleepTime.DEFAULT)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def scroll(self, direction):
            try:
                if direction == Direction.UP:
                    self.viewV2.scroll.toBeginning(max_swipes=1)
                else:
                    self.viewV2.scroll.toEnd(max_swipes=1)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def fling(self, direction):
            try:
                if direction == Direction.UP:
                    self.viewV2.fling.toBeginning(max_swipes=5)
                else:
                    self.viewV2.fling.toEnd(max_swipes=5)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def exists(self, ui_timeout=None, ignore_bug: bool = False) -> bool:
            try:
                # Currently, the methods left, right, up and down from
                # uiautomator2 return None when a Selector does not exist.
                # All other selectors return an UiObject with exists() == False.
                # We will open a ticket to uiautomator2 to fix this inconsistency.
                if self.viewV2 is None:
                    return False
                exists: bool = self.viewV2.exists(self.get_ui_timeout(ui_timeout))
                if (
                    hasattr(self.viewV2, "count")
                    and not exists
                    and self.viewV2.count >= 1
                ):
                    logger.debug(
                        f"UIA2 BUG: exists return False, but there is/are {self.viewV2.count} element(s)!"
                    )
                    if ignore_bug:
                        return "BUG!"
                    # More info about that: https://github.com/openatx/uiautomator2/issues/689"
                    return False
                return exists
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def count_items(self) -> int:
            try:
                return self.viewV2.count
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def wait(self, ui_timeout=Timeout.MEDIUM):
            try:
                return self.viewV2.wait(timeout=self.get_ui_timeout(ui_timeout))
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def wait_gone(self, ui_timeout=None):
            try:
                return self.viewV2.wait_gone(timeout=self.get_ui_timeout(ui_timeout))
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def is_above_this(self, obj2) -> Optional[bool]:
            obj1 = self.viewV2
            obj2 = obj2.viewV2
            try:
                if obj1.exists() and obj2.exists():
                    return obj1.info["bounds"]["top"] < obj2.info["bounds"]["top"]
                else:
                    return None
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def get_bounds(self) -> dict:
            try:
                return self.viewV2.info["bounds"]
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def get_height(self) -> int:
            bounds = self.get_bounds()
            return bounds["bottom"] - bounds["top"]

        def get_width(self):
            bounds = self.get_bounds()
            return bounds["right"] - bounds["left"]

        def get_property(self, prop: str):
            try:
                return self.viewV2.info[prop]
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def is_scrollable(self):
            try:
                if self.viewV2.exists():
                    return self.viewV2.info["scrollable"]
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        @staticmethod
        def get_ui_timeout(ui_timeout: Timeout) -> int:
            """Map Timeout enum to seconds. Uses .name so reload-safe across importlib.reload."""
            if ui_timeout is None:
                return 5
            by_name = {
                "ZERO": 0,
                "TINY": 1,
                "SHORT": 3,
                "MEDIUM": 5,
                "LONG": 8,
            }
            name = getattr(ui_timeout, "name", None)
            if name in by_name:
                return by_name[name]
            if isinstance(ui_timeout, (int, float)):
                return int(ui_timeout)
            return 5

        def get_text(self, error=True, index=None):
            try:
                text = (
                    self.viewV2.info["text"]
                    if index is None
                    else self.viewV2[index].info["text"]
                )
                if text is not None:
                    return text
            except uiautomator2.JSONRPCError as e:
                if error:
                    raise DeviceFacade.JsonRpcError(e)
                else:
                    return ""
            logger.debug("Object exists but doesn't contain any text.")
            return ""

        def get_selected(self) -> bool:
            try:
                if self.viewV2.exists():
                    return self.viewV2.info["selected"]
                logger.debug(
                    "Object has disappeared! Probably too short video which has been liked!"
                )
                return True
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

        def set_text(self, text: str, mode: Mode = Mode.TYPE) -> None:
            punct_list = string.punctuation
            try:
                if mode == Mode.PASTE:
                    self.viewV2.set_text(text)
                else:
                    self.click(sleep=SleepTime.SHORT)
                    self.deviceV2.clear_text()
                    random_sleep(0.3, 1, modulable=False)
                    start = datetime.now()
                    sentences = text.splitlines()
                    for j, sentence in enumerate(sentences, start=1):
                        word_list = sentence.split()
                        n_words = len(word_list)
                        for n, word in enumerate(word_list, start=1):
                            i = 0
                            n_single_letters = randint(1, 3)
                            for char in word:
                                if i < n_single_letters:
                                    self.deviceV2.send_keys(char, clear=False)
                                    # random_sleep(0.01, 0.1, modulable=False, logging=False)
                                    i += 1
                                else:
                                    if word[-1] in punct_list:
                                        self.deviceV2.send_keys(word[i:-1], clear=False)
                                        # random_sleep(0.01, 0.1, modulable=False, logging=False)
                                        self.deviceV2.send_keys(word[-1], clear=False)
                                    else:
                                        self.deviceV2.send_keys(word[i:], clear=False)
                                    # random_sleep(0.01, 0.1, modulable=False, logging=False)
                                    break
                            if n < n_words:
                                self.deviceV2.send_keys(" ", clear=False)
                                # random_sleep(0.01, 0.1, modulable=False, logging=False)
                        if j < len(sentences):
                            self.deviceV2.send_keys("\n")

                    typed_text = self.viewV2.get_text()
                    if typed_text != text:
                        logger.warning(
                            "Failed to write in text field, let's try in the old way.."
                        )
                        self.viewV2.set_text(text)
                    else:
                        logger.debug(
                            f"Text typed in: {(datetime.now()-start).total_seconds():.2f}s"
                        )
                DeviceFacade.sleep_mode(SleepTime.SHORT)
            except uiautomator2.JSONRPCError as e:
                raise DeviceFacade.JsonRpcError(e)

    class JsonRpcError(Exception):
        pass

    class AppHasCrashed(Exception):
        pass
