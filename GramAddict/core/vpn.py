import logging
import re
import time
from typing import Optional

from GramAddict.core.device_facade import DeviceFacade, Timeout
from GramAddict.core.utils import random_sleep, save_crash
from GramAddict.core.views import case_insensitive_re

logger = logging.getLogger(__name__)

VPN_CONNECT_DESC = "^Connect$"
VPN_STOP_DESC = "^Stop$"
DEFAULT_APP_NAME = "Shadowrocket"
DEFAULT_WAIT_TIMEOUT = 30


def _press_home(device: DeviceFacade) -> None:
    logger.debug("Pressing home.")
    device.deviceV2.press("home")
    random_sleep(1, 2, modulable=False)


def _find_by_description(device: DeviceFacade, pattern: str):
    return device.find_any(descriptionMatches=case_insensitive_re(pattern))


def _wait_for_vpn_ui(device: DeviceFacade, timeout: int = DEFAULT_WAIT_TIMEOUT) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _find_by_description(device, VPN_STOP_DESC).exists(Timeout.SHORT):
            return "stop"
        if _find_by_description(device, VPN_CONNECT_DESC).exists(Timeout.SHORT):
            return "connect"
        random_sleep(0.5, 1, modulable=False, log=False)
    return None


def ensure_shadowrocket_vpn(
    device: DeviceFacade,
    app_name: str = DEFAULT_APP_NAME,
) -> bool:
    logger.info(f"Ensuring VPN is on via {app_name}.")

    _press_home(device)

    app_icon = device.find_any(textMatches=case_insensitive_re(f"^{re.escape(app_name)}$"))
    if not app_icon.exists(Timeout.MEDIUM):
        app_icon = device.find_any(textMatches=case_insensitive_re(app_name))
    if not app_icon.exists(Timeout.MEDIUM):
        logger.error(f"Could not find {app_name} on the home screen.")
        save_crash(device)
        return False

    logger.info(f"Opening {app_name}.")
    app_icon.click()
    random_sleep(2, 3, modulable=False)

    state = _wait_for_vpn_ui(device)
    if state is None:
        logger.error("Timed out waiting for Connect or Stop in the VPN app.")
        save_crash(device)
        _press_home(device)
        return False

    if state == "stop":
        logger.info("VPN is already connected.")
        _press_home(device)
        return True

    logger.info("VPN is off. Tapping Connect.")
    _find_by_description(device, VPN_CONNECT_DESC).click()
    random_sleep(2, 3, modulable=False)

    logger.info("VPN connect tapped — continuing.")
    _press_home(device)
    return True
