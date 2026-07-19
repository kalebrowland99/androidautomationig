import json
import logging
from datetime import datetime
from typing import Optional

import requests
import yaml
from colorama import Fore, Style

from GramAddict.core.plugin_loader import Plugin

logger = logging.getLogger(__name__)


def load_sessions(username) -> Optional[dict]:
    try:
        with open(f"accounts/{username}/sessions.json") as json_data:
            return json.load(json_data)
    except FileNotFoundError:
        logger.error("No session data found. Skipping report generation.")
        return None


def load_telegram_config(username) -> Optional[dict]:
    try:
        with open(f"accounts/{username}/telegram.yml", "r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {e}")
        return None


def telegram_bot_send_text(bot_api_token, bot_chat_ID, text, parse_mode="markdown"):
    try:
        method = "sendMessage"
        params = {"text": text, "chat_id": bot_chat_ID}
        if parse_mode:
            params["parse_mode"] = parse_mode
        url = f"https://api.telegram.org/bot{bot_api_token}/{method}"
        return requests.get(url, params=params, timeout=20).json()
    except Exception as e:
        # Usually a transient network/VPN hiccup reaching api.telegram.org.
        logger.error(f"Error sending Telegram message: {e}")
        return None


def telegram_alerts_enabled(telegram_config: dict) -> bool:
    return telegram_config.get("telegram-alerts", True) is not False


def send_telegram_alert(
    username: Optional[str],
    title: str,
    details: str = "",
    *,
    stopped: bool = False,
) -> bool:
    """Send a short Telegram alert (action block, crash, rate limit, etc.)."""
    if not username:
        return False
    telegram_config = load_telegram_config(username)
    if not telegram_config or not telegram_alerts_enabled(telegram_config):
        return False
    token = telegram_config.get("telegram-api-token")
    chat_id = telegram_config.get("telegram-chat-id")
    if not token or not chat_id:
        return False
    lines = [f"⚠️ @{username} — {title}"]
    detail = (details or "").strip()
    if detail:
        lines.append(detail)
    if stopped:
        lines.append("Stopped.")
    text = "\n".join(lines)
    response = telegram_bot_send_text(token, chat_id, text, parse_mode=None)
    if response and response.get("ok"):
        logger.info(
            "Telegram alert sent.",
            extra={"color": f"{Style.BRIGHT}{Fore.BLUE}"},
        )
        return True
    error = response.get("description") if response else "Unknown error"
    logger.error(f"Failed to send Telegram alert: {error}")
    return False


def _initialize_aggregated_data():
    return {
        "total_likes": 0,
        "total_watched": 0,
        "total_followed": 0,
        "total_unfollowed": 0,
        "total_comments": 0,
        "total_pm": 0,
        "duration": 0,
        "followers": float("inf"),
        "following": float("inf"),
        "followers_gained": 0,
    }


def _calculate_session_duration(session):
    try:
        start_datetime = datetime.strptime(
            session["start_time"], "%Y-%m-%d %H:%M:%S.%f"
        )
        finish_datetime = datetime.strptime(
            session["finish_time"], "%Y-%m-%d %H:%M:%S.%f"
        )
        return int((finish_datetime - start_datetime).total_seconds() / 60)
    except ValueError:
        logger.debug(
            f"{session['id']} has no finish_time. Skipping duration calculation."
        )
        return 0


def daily_summary(sessions):
    daily_aggregated_data = {}
    for session in sessions:
        date = session["start_time"][:10]
        daily_aggregated_data.setdefault(date, _initialize_aggregated_data())
        duration = _calculate_session_duration(session)
        daily_aggregated_data[date]["duration"] += duration

        for key in [
            "total_likes",
            "total_watched",
            "total_followed",
            "total_unfollowed",
            "total_comments",
            "total_pm",
        ]:
            daily_aggregated_data[date][key] += session.get(key, 0)

        daily_aggregated_data[date]["followers"] = min(
            session.get("profile", {}).get("followers", 0),
            daily_aggregated_data[date]["followers"],
        )
        daily_aggregated_data[date]["following"] = min(
            session.get("profile", {}).get("following", 0),
            daily_aggregated_data[date]["following"],
        )
    return _calculate_followers_gained(daily_aggregated_data)


def _calculate_followers_gained(aggregated_data):
    dates_sorted = sorted(aggregated_data.keys())
    previous_followers = None
    for date in dates_sorted:
        current_followers = aggregated_data[date]["followers"]
        if previous_followers is not None:
            followers_gained = current_followers - previous_followers
            aggregated_data[date]["followers_gained"] = followers_gained
        previous_followers = current_followers
    return aggregated_data


def generate_report(
    username,
    last_session,
    daily_aggregated_data,
    weekly_average_data,
    followers_now,
    following_now,
):
    prev_followers = last_session.get("profile", {}).get("followers", 0) or 0
    delta = followers_now - prev_followers
    session_bits = [
        f"{last_session['duration']}m",
        f"{last_session['total_likes']} likes",
        f"{last_session['total_followed']} follows",
        f"{last_session['total_watched']} stories",
    ]
    if last_session.get("total_unfollowed"):
        session_bits.append(f"{last_session['total_unfollowed']} unfollows")
    if last_session.get("total_comments"):
        session_bits.append(f"{last_session['total_comments']} comments")
    today_bits = [
        f"{daily_aggregated_data.get('duration', 0)}m",
        f"{daily_aggregated_data.get('total_likes', 0)} likes",
        f"{daily_aggregated_data.get('total_followed', 0)} follows",
        f"{daily_aggregated_data.get('total_watched', 0)} stories",
    ]
    gained = daily_aggregated_data.get("followers_gained", 0)
    return (
        f"*@{username}*\n"
        f"{followers_now} followers ({delta:+}) · {following_now} following\n"
        f"Session: {' · '.join(session_bits)}\n"
        f"Today: {' · '.join(today_bits)} · {gained:+} followers"
    )


def weekly_average(daily_aggregated_data, today) -> dict:
    weekly_average_data = _initialize_aggregated_data()

    for date in daily_aggregated_data:
        if (today - datetime.strptime(date, "%Y-%m-%d")).days > 7:
            continue
        for key in [
            "total_likes",
            "total_watched",
            "total_followed",
            "total_unfollowed",
            "total_comments",
            "total_pm",
            "duration",
            "followers_gained",
        ]:
            weekly_average_data[key] += daily_aggregated_data[date][key]
    return weekly_average_data


class TelegramReports(Plugin):
    """Generate reports at the end of the session and send them using telegram"""

    def __init__(self):
        super().__init__()
        self.description = "Generate reports at the end of the session and send them using telegram. You have to configure 'telegram.yml' in your account folder"
        self.arguments = [
            {
                "arg": "--telegram-reports",
                "help": "at the end of every session send a report to your telegram account",
                "action": "store_true",
                "operation": True,
            }
        ]

    def run(self, config, plugin, followers_now, following_now, time_left):
        username = config.args.username
        if username is None:
            logger.error("You have to specify a username for getting reports!")
            return

        sessions = load_sessions(username)
        if not sessions:
            logger.error(
                f"No session data found for {username}. Skipping report generation."
            )
            return

        last_session = sessions[-1]
        last_session["duration"] = _calculate_session_duration(last_session)

        telegram_config = load_telegram_config(username)
        if not telegram_config:
            logger.error(
                f"No telegram configuration found for {username}. Skipping report generation."
            )
            return

        daily_aggregated_data = daily_summary(sessions)
        today_data = daily_aggregated_data.get(last_session["start_time"][:10], {})
        today = datetime.now()
        weekly_average_data = weekly_average(daily_aggregated_data, today)
        report = generate_report(
            username,
            last_session,
            today_data,
            weekly_average_data,
            followers_now,
            following_now,
        )
        response = telegram_bot_send_text(
            telegram_config.get("telegram-api-token"),
            telegram_config.get("telegram-chat-id"),
            report,
        )
        if response and response.get("ok"):
            logger.info(
                "Telegram message sent successfully.",
                extra={"color": f"{Style.BRIGHT}{Fore.BLUE}"},
            )
        else:
            error = response.get("description") if response else "Unknown error"
            logger.error(f"Failed to send Telegram message: {error}")
