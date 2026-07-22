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
    """Send a short one-line Telegram alert."""
    if not username:
        return False
    telegram_config = load_telegram_config(username)
    if not telegram_config or not telegram_alerts_enabled(telegram_config):
        return False
    token = telegram_config.get("telegram-api-token")
    chat_id = telegram_config.get("telegram-chat-id")
    if not token or not chat_id:
        return False

    # One short line: "@user — Crash · restarting"
    detail = (details or "").strip().replace("\n", " ")
    if detail and len(detail) > 80:
        detail = detail[:77] + "…"
    parts = [f"@{username.lstrip('@')} — {title}"]
    if detail:
        parts[0] = f"{parts[0]} · {detail}"
    if stopped and "stop" not in title.lower():
        parts[0] = f"{parts[0]} · stopped"
    text = parts[0]

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


# Avoid spamming Telegram when the bot hits several errors in a row.
_last_restart_alert_at: dict[str, datetime] = {}


def maybe_send_restart_alert(
    username: Optional[str],
    *,
    kind: str = "Error",
    cooldown_minutes: int = 8,
) -> bool:
    """Short restart ping, rate-limited so recovery loops don't flood the chat."""
    if not username:
        return False
    key = username.lstrip("@").casefold()
    now = datetime.now()
    last = _last_restart_alert_at.get(key)
    if last and (now - last).total_seconds() < cooldown_minutes * 60:
        return False
    _last_restart_alert_at[key] = now
    return send_telegram_alert(username, kind, "restarting")


def _initialize_aggregated_data():
    return {
        "total_likes": 0,
        "total_watched": 0,
        "total_story_likes": 0,
        "total_story_accounts_liked": 0,
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
            "total_story_likes",
            "total_story_accounts_liked",
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


def _fmt_duration(minutes) -> str:
    """Turn minute totals into '2h 45m' / '45m' / '0m'."""
    try:
        total = max(0, int(minutes or 0))
    except (TypeError, ValueError):
        total = 0
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _as_int(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt_capped(label: str, count, cap) -> Optional[str]:
    """e.g. 'Liked Stories 12/20' — omit when count is 0 and no cap context needed."""
    n = int(count or 0)
    limit = _as_int(cap)
    if n <= 0 and not limit:
        return None
    if limit is not None and limit > 0:
        return f"{label} {n}/{limit}"
    if n > 0:
        return f"{label} {n}"
    return None


def _session_limits(session: dict) -> dict:
    args = session.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    likes_cap = args.get("current_likes_limit")
    follows_cap = args.get("current_follow_limit")
    # Story likes share the watch cap; daily-story batch may be lower.
    watches_cap = args.get("current_watch_limit")
    daily_cap = session.get("daily_story_likes_limit")
    story_cap = watches_cap
    daily_n = _as_int(daily_cap)
    watch_n = _as_int(watches_cap)
    if daily_n and daily_n > 0:
        if watch_n and watch_n > 0:
            story_cap = min(daily_n, watch_n)
        else:
            story_cap = daily_n
    return {
        "likes": likes_cap,
        "follows": follows_cap,
        "stories": story_cap,
        "crashes": args.get("current_crashes_limit"),
    }


def _fmt_session_bits(session: dict) -> list[str]:
    bits: list[str] = [_fmt_duration(session.get("duration"))]
    caps = _session_limits(session)
    for label, count, cap in (
        ("Liked Posts", session.get("total_likes"), caps["likes"]),
        ("Liked Stories", session.get("total_story_likes") or session.get("total_watched"), caps["stories"]),
        ("Story Accounts", session.get("total_story_accounts_liked"), None),
        ("Followed", session.get("total_followed"), caps["follows"]),
        ("Unfollowed", session.get("total_unfollowed"), None),
        ("Comments", session.get("total_comments"), None),
    ):
        # Always show the main three with caps when a cap exists.
        if label in ("Liked Posts", "Liked Stories", "Followed"):
            line = _fmt_capped(label, count, cap)
            if line:
                bits.append(line)
            elif _as_int(cap):
                bits.append(f"{label} 0/{int(cap)}")
            continue
        line = _fmt_capped(label, count, cap)
        if line:
            bits.append(line)
    crashes = int(session.get("total_crashes", 0) or 0)
    crash_cap = _as_int(caps.get("crashes"))
    if crashes > 0:
        bits.append(
            f"Crashes {crashes}/{crash_cap}" if crash_cap else f"Crashes {crashes}"
        )
    return bits


def _fmt_today_bits(data: dict, goals: Optional[dict] = None) -> list[str]:
    """Today totals with optional display-only goals (never stop the bot)."""
    goals = goals or {}
    bits: list[str] = [_fmt_duration(data.get("duration"))]
    for label, key, goal_key in (
        ("Liked Posts", "total_likes", "likes"),
        ("Liked Stories", "total_story_likes", "stories"),
        ("Story Accounts", "total_story_accounts_liked", None),
        ("Followed", "total_followed", "follows"),
        ("Unfollowed", "total_unfollowed", None),
        ("Comments", "total_comments", None),
    ):
        n = int(data.get(key, 0) or 0)
        if not n and key == "total_story_likes":
            n = int(data.get("total_watched", 0) or 0)
        goal = _as_int(goals.get(goal_key)) if goal_key else None
        if goal:
            bits.append(f"{label} {n}/{goal}")
        elif n:
            bits.append(f"{label} {n}")
    return bits


def generate_report(
    username,
    last_session,
    daily_aggregated_data,
    weekly_average_data,
    followers_now,
    following_now,
    next_session_at: Optional[datetime] = None,
    status_line: Optional[str] = None,
    daily_goals: Optional[dict] = None,
):
    prev_followers = last_session.get("profile", {}).get("followers", 0) or 0
    delta = int(followers_now) - int(prev_followers)
    if delta:
        follow_line = (
            f"{followers_now} followers ({delta:+}) · {following_now} following"
        )
    else:
        follow_line = f"{followers_now} followers · {following_now} following"

    if daily_goals is None:
        args = last_session.get("args") or {}
        daily_goals = {
            "likes": args.get("daily_liked_posts_goal")
            or args.get("daily-liked-posts-goal"),
            "stories": args.get("daily_liked_stories_goal")
            or args.get("daily-liked-stories-goal"),
            "follows": args.get("daily_follows_goal")
            or args.get("daily-follows-goal"),
        }

    session_bits = _fmt_session_bits(last_session)
    today_bits = _fmt_today_bits(daily_aggregated_data, daily_goals)
    gained = int(daily_aggregated_data.get("followers_gained", 0) or 0)
    today_line = " · ".join(today_bits) if today_bits else "no activity yet"
    if gained:
        today_line = f"{today_line} · {gained:+} followers"

    if status_line:
        status = status_line.replace("Status: ", "") if status_line.startswith("Status: ") else status_line
    elif next_session_at is not None:
        when = next_session_at.strftime("%I:%M %p").lstrip("0")
        status = f"Waiting — starts again {when}"
    else:
        status = "Stopped"

    # Plain text (no Markdown) — Telegram markdown breaks on * and looks messy.
    return "\n".join(
        [
            f"@{username}",
            follow_line,
            f"Session: {' · '.join(session_bits) if session_bits else '—'}",
            f"Today: {today_line}",
            f"Status: {status}",
        ]
    )


def weekly_average(daily_aggregated_data, today) -> dict:
    weekly_average_data = _initialize_aggregated_data()

    for date in daily_aggregated_data:
        if (today - datetime.strptime(date, "%Y-%m-%d")).days > 7:
            continue
        for key in [
            "total_likes",
            "total_watched",
            "total_story_likes",
            "total_story_accounts_liked",
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
        next_session_at = None
        status_line = None
        if time_left is not None:
            try:
                seconds = float(time_left)
            except (TypeError, ValueError):
                seconds = None
            if seconds is not None and seconds > 0:
                from datetime import timedelta

                next_session_at = datetime.now() + timedelta(seconds=seconds)
            else:
                status_line = "Status: Stopped"
        report = generate_report(
            username,
            last_session,
            today_data,
            weekly_average_data,
            followers_now,
            following_now,
            next_session_at=next_session_at,
            status_line=status_line,
            daily_goals={
                "likes": getattr(config.args, "daily_liked_posts_goal", None),
                "stories": getattr(config.args, "daily_liked_stories_goal", None),
                "follows": getattr(config.args, "daily_follows_goal", None),
            },
        )
        response = telegram_bot_send_text(
            telegram_config.get("telegram-api-token"),
            telegram_config.get("telegram-chat-id"),
            report,
            parse_mode=None,
        )
        if response and response.get("ok"):
            logger.info(
                "Telegram message sent successfully.",
                extra={"color": f"{Style.BRIGHT}{Fore.BLUE}"},
            )
        else:
            error = response.get("description") if response else "Unknown error"
            logger.error(f"Failed to send Telegram message: {error}")
