import logging
import sys
import traceback
from datetime import datetime
from http.client import HTTPException
from socket import timeout

from colorama import Fore, Style
from requests.exceptions import ConnectionError as RequestsConnectionError
from uiautomator2.exceptions import UiObjectNotFoundError

from GramAddict.core.device_facade import DeviceFacade
from GramAddict.core.report import print_full_report
from GramAddict.core.utils import (
    ActionBlockedError,
    check_if_crash_popup_is_there,
    check_instagram_rate_limit,
    close_instagram,
    InstagramRateLimitError,
    open_instagram,
    random_sleep,
    save_crash,
    stop_bot,
    take_rate_limit_break,
)
from GramAddict.core.views import TabBarView
from GramAddict.plugins.telegram import send_telegram_alert

logger = logging.getLogger(__name__)


def _account_username(session_state, configs):
    return session_state.my_username or getattr(configs.args, "username", None)


def _notify_fatal_error(session_state, configs, title: str, details: str = "") -> None:
    try:
        send_telegram_alert(
            _account_username(session_state, configs),
            title,
            details,
            stopped=True,
        )
    except Exception:
        logger.debug("Telegram alert failed", exc_info=True)


def run_safely(device, device_id, sessions, session_state, screen_record, configs):
    def actual_decorator(func):
        def wrapper(*args, **kwargs):
            session_state = sessions[-1]
            try:
                func(*args, **kwargs)
            except KeyboardInterrupt:
                try:
                    # Catch Ctrl-C and ask if user wants to pause execution
                    logger.info(
                        "CTRL-C detected . . .",
                        extra={"color": f"{Style.BRIGHT}{Fore.YELLOW}"},
                    )
                    logger.info(
                        f"-------- PAUSED: {datetime.now().strftime('%I:%M:%S %p')} --------",
                        extra={"color": f"{Style.BRIGHT}{Fore.YELLOW}"},
                    )
                    logger.info(
                        "NOTE: This is a rudimentary pause. It will restart the action, while retaining session data.",
                        extra={"color": Style.BRIGHT},
                    )
                    logger.info(
                        "Press RETURN to resume or CTRL-C again to Quit: ",
                        extra={"color": Style.BRIGHT},
                    )

                    input("")

                    logger.info(
                        f"-------- RESUMING: {datetime.now().strftime('%I:%M:%S %p')} --------",
                        extra={"color": f"{Style.BRIGHT}{Fore.YELLOW}"},
                    )
                    TabBarView(device).navigateToProfile()
                except KeyboardInterrupt:
                    stop_bot(device, sessions, session_state)

            except DeviceFacade.AppHasCrashed:
                logger.warning("App has crashed / has been closed!")
                restart(
                    device,
                    sessions,
                    session_state,
                    configs,
                    normal_crash=False,
                    print_traceback=False,
                )

            except (
                DeviceFacade.JsonRpcError,
                IndexError,
                HTTPException,
                timeout,
                UiObjectNotFoundError,
                RequestsConnectionError,
            ):
                restart(
                    device,
                    sessions,
                    session_state,
                    configs,
                )

            except ActionBlockedError as e:
                if isinstance(e, InstagramRateLimitError):
                    logger.warning(str(e))
                    print_full_report(sessions, configs.args.scrape_to_file)
                    sessions.persist(directory=session_state.my_username)
                    raise
                _notify_fatal_error(
                    session_state,
                    configs,
                    "Action blocked",
                    str(e),
                )
                logger.error(traceback.format_exc())
                try:
                    save_crash(device)
                except RequestsConnectionError:
                    logger.warning("Could not save crash dump — device disconnected.")
                close_instagram(device)
                print_full_report(sessions, configs.args.scrape_to_file)
                sessions.persist(directory=session_state.my_username)
                raise e from e

            except Exception as e:
                _notify_fatal_error(
                    session_state,
                    configs,
                    "Session error",
                    f"{type(e).__name__}: {e}",
                )
                logger.error(traceback.format_exc())
                for exception_line in traceback.format_exception_only(type(e), e):
                    logger.critical(
                        f"'{exception_line}' -> This kind of exception will stop the bot (no restart)."
                    )
                try:
                    logger.info(
                        f"List of running apps: {', '.join(device.deviceV2.app_list_running())}"
                    )
                except RequestsConnectionError:
                    device.reconnect()
                try:
                    save_crash(device)
                except RequestsConnectionError:
                    logger.warning("Could not save crash dump — device disconnected.")
                close_instagram(device)
                print_full_report(sessions, configs.args.scrape_to_file)
                sessions.persist(directory=session_state.my_username)
                raise e from e

        return wrapper

    return actual_decorator


def restart(
    device: DeviceFacade,
    sessions,
    session_state,
    configs,
    normal_crash: bool = True,
    print_traceback: bool = True,
):
    if not device.is_alive():
        device.reconnect()
    if print_traceback:
        logger.error(traceback.format_exc())
        try:
            save_crash(device)
        except RequestsConnectionError:
            logger.warning("Could not save crash dump — device disconnected.")
    logger.info(
        f"List of running apps: {', '.join(device.deviceV2.app_list_running())}."
    )
    if configs.args.count_app_crashes or normal_crash:
        session_state.totalCrashes += 1
        if session_state.check_limit(
            limit_type=session_state.Limit.CRASHES, output=True
        ):
            logger.error(
                "Reached crashes limit. Bot has crashed too much! Please check what's going on."
            )
            _notify_fatal_error(
                session_state,
                configs,
                "Too many crashes",
            )
            stop_bot(device, sessions, session_state)
        logger.info("Something unexpected happened. Let's try again.")
    close_instagram(device)
    try:
        check_if_crash_popup_is_there(device)
    except InstagramRateLimitError:
        raise
    random_sleep()
    if not open_instagram(device):
        print_full_report(sessions, configs.args.scrape_to_file)
        sessions.persist(directory=session_state.my_username)
        sys.exit(2)
    TabBarView(device).navigateToProfile()
