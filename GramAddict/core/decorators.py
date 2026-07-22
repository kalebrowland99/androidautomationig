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
    close_instagram,
    InstagramRateLimitError,
    open_instagram,
    random_sleep,
    save_crash,
    stop_bot,
)
from GramAddict.core.views import TabBarView
from GramAddict.plugins.telegram import maybe_send_restart_alert, send_telegram_alert


logger = logging.getLogger(__name__)


def _account_username(session_state, configs):
    return session_state.my_username or getattr(configs.args, "username", None)


def _notify_error(
    session_state, configs, title: str, details: str = "", *, stopped: bool = False
) -> None:
    try:
        send_telegram_alert(
            _account_username(session_state, configs),
            title,
            details,
            stopped=stopped,
        )
    except Exception:
        logger.debug("Telegram alert failed", exc_info=True)


def run_safely(device, device_id, sessions, session_state, screen_record, configs):
    """Wrap a job so transient errors restart Instagram instead of killing the bot.

    Plugins call the wrapped job in a loop until ``is_job_completed``. On error we
    recover and return so that loop can retry — we only stop for Ctrl-C, crash
    limits, or Instagram rate-limit breaks (handled by bot_flow).
    """

    def actual_decorator(func):
        def wrapper(*args, **kwargs):
            session_state = sessions[-1]
            try:
                func(*args, **kwargs)
            except KeyboardInterrupt:
                try:
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

            except InstagramRateLimitError as e:
                # bot_flow takes the progressive cooldown break.
                logger.warning(str(e))
                print_full_report(sessions, configs.args.scrape_to_file)
                sessions.persist(directory=session_state.my_username)
                raise

            except ActionBlockedError as e:
                # Serious IG blocks used to kill the bot — pause + restart instead.
                logger.error(str(e))
                logger.error(traceback.format_exc())
                _notify_error(
                    session_state,
                    configs,
                    "Blocked",
                    "pausing then restarting",
                    stopped=False,
                )
                try:
                    save_crash(device)
                except RequestsConnectionError:
                    logger.warning("Could not save crash dump — device disconnected.")
                logger.warning(
                    "Action block detected — closing Instagram, waiting, then continuing.",
                    extra={"color": f"{Style.BRIGHT}{Fore.YELLOW}"},
                )
                close_instagram(device)
                random_sleep(60, 120, modulable=False, minimum=45)
                restart(
                    device,
                    sessions,
                    session_state,
                    configs,
                    normal_crash=True,
                    print_traceback=False,
                )

            except (
                DeviceFacade.AppHasCrashed,
                DeviceFacade.JsonRpcError,
                IndexError,
                HTTPException,
                timeout,
                UiObjectNotFoundError,
                RequestsConnectionError,
                Exception,
            ) as e:
                if isinstance(e, (SystemExit, KeyboardInterrupt)):
                    raise
                if isinstance(e, DeviceFacade.AppHasCrashed):
                    logger.warning("App has crashed / has been closed!")
                    print_tb = False
                    count_as_normal = False
                    maybe_send_restart_alert(
                        _account_username(session_state, configs),
                        kind="Crash",
                    )
                else:
                    logger.error(
                        "Recoverable error — restarting Instagram and continuing "
                        f"({type(e).__name__}: {e}).",
                        extra={"color": f"{Style.BRIGHT}{Fore.YELLOW}"},
                    )
                    print_tb = True
                    count_as_normal = True
                    maybe_send_restart_alert(
                        _account_username(session_state, configs),
                        kind="Error",
                    )
                try:
                    restart(
                        device,
                        sessions,
                        session_state,
                        configs,
                        normal_crash=count_as_normal,
                        print_traceback=print_tb,
                    )
                except InstagramRateLimitError:
                    print_full_report(sessions, configs.args.scrape_to_file)
                    sessions.persist(directory=session_state.my_username)
                    raise
                except Exception as restart_err:
                    logger.error(
                        "First recovery failed (%s) — trying once more.",
                        type(restart_err).__name__,
                    )
                    logger.error(traceback.format_exc())
                    try:
                        random_sleep(8, 15, modulable=False, minimum=5)
                        restart(
                            device,
                            sessions,
                            session_state,
                            configs,
                            normal_crash=True,
                            print_traceback=False,
                        )
                    except InstagramRateLimitError:
                        print_full_report(sessions, configs.args.scrape_to_file)
                        sessions.persist(directory=session_state.my_username)
                        raise
                    except Exception:
                        _notify_error(
                            session_state,
                            configs,
                            "Stopped",
                            "could not recover",
                            stopped=True,
                        )
                        logger.critical(
                            "Recovery failed twice — stopping bot so it can be restarted cleanly."
                        )
                        try:
                            save_crash(device)
                        except Exception:
                            pass
                        close_instagram(device)
                        print_full_report(sessions, configs.args.scrape_to_file)
                        sessions.persist(directory=session_state.my_username)
                        stop_bot(device, sessions, session_state)

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
        try:
            device.reconnect()
        except Exception as e:
            logger.warning("Device reconnect failed: %s", e)
    if print_traceback:
        logger.error(traceback.format_exc())
        try:
            save_crash(device)
        except RequestsConnectionError:
            logger.warning("Could not save crash dump — device disconnected.")
        except Exception:
            pass
    try:
        logger.info(
            f"List of running apps: {', '.join(device.deviceV2.app_list_running())}."
        )
    except Exception:
        logger.info("Could not list running apps (device may be reconnecting).")
    if configs.args.count_app_crashes or normal_crash:
        session_state.totalCrashes += 1
        if session_state.check_limit(
            limit_type=session_state.Limit.CRASHES, output=True
        ):
            logger.error(
                "Reached crashes limit. Bot has crashed too much! Please check what's going on."
            )
            _notify_error(
                session_state,
                configs,
                "Stopped",
                "too many crashes",
                stopped=True,
            )
            stop_bot(device, sessions, session_state)
        logger.info("Something unexpected happened. Let's try again.")
    # Check dialogs while IG may still be up, then close and reopen.
    try:
        check_if_crash_popup_is_there(device)
    except InstagramRateLimitError:
        raise
    except DeviceFacade.AppHasCrashed:
        logger.debug("Crash/rate-limit check skipped — Instagram not in foreground.")
    except Exception as e:
        logger.debug("Crash-popup check failed: %s", e)

    close_instagram(device)
    random_sleep()

    opened = False
    last_open_err = None
    for attempt in range(1, 4):
        try:
            if open_instagram(device):
                opened = True
                break
            last_open_err = "open_instagram returned False"
        except Exception as e:
            last_open_err = e
            logger.warning(
                "open_instagram attempt %s/3 failed: %s", attempt, e
            )
        random_sleep(3, 6, modulable=False, minimum=2)

    if not opened:
        logger.error(
            "Could not reopen Instagram after error (%s). Will keep trying from the job loop.",
            last_open_err,
        )
        # Don't sys.exit — raise so run_safely can retry recovery, or the
        # job loop can call us again after a wait.
        raise DeviceFacade.AppHasCrashed(
            f"Could not reopen Instagram ({last_open_err})"
        )

    try:
        TabBarView(device).navigateToProfile()
    except Exception as e:
        logger.warning(
            "Could not navigate to profile after reopen (%s) — continuing anyway.",
            e,
        )
