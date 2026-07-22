"""Calendar-day action totals + display-only daily goals.

Daily goals are for Farm/Telegram progress (x/cap). They never stop the bot —
session limits and total-follows-limit-daily still control stopping.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from GramAddict.core.storage import ACCOUNTS

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        n = int(float(str(value).strip().split("-")[0]))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def daily_goals_from_args(args: Any) -> dict[str, Optional[int]]:
    """Display-only day targets from config (0 / missing = no cap shown)."""
    if args is None:
        return {"likes": None, "stories": None, "follows": None}
    return {
        "likes": _as_int(getattr(args, "daily_liked_posts_goal", None)),
        "stories": _as_int(getattr(args, "daily_liked_stories_goal", None)),
        "follows": _as_int(getattr(args, "daily_follows_goal", None)),
    }


def daily_goals_from_config(config: dict[str, Any]) -> dict[str, Optional[int]]:
    return {
        "likes": _as_int(config.get("daily-liked-posts-goal")),
        "stories": _as_int(config.get("daily-liked-stories-goal")),
        "follows": _as_int(config.get("daily-follows-goal")),
    }


def _sessions_path(username: str) -> Path:
    return Path(ACCOUNTS) / username.lstrip("@") / "sessions.json"


def _load_sessions(username: str) -> list[dict[str, Any]]:
    path = _sessions_path(username)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _empty_totals() -> dict[str, int]:
    return {
        "likes": 0,
        "story_likes": 0,
        "story_accounts_liked": 0,
        "follows": 0,
        "unfollows": 0,
        "comments": 0,
        "watched": 0,
    }


def _add_session_counts(totals: dict[str, int], session: dict[str, Any]) -> None:
    totals["likes"] += int(session.get("total_likes", 0) or 0)
    story = int(session.get("total_story_likes", 0) or 0)
    watched = int(session.get("total_watched", 0) or 0)
    totals["story_likes"] += story if story else watched
    totals["story_accounts_liked"] += int(
        session.get("total_story_accounts_liked", 0) or 0
    )
    totals["watched"] += watched
    totals["follows"] += int(session.get("total_followed", 0) or 0)
    totals["unfollows"] += int(session.get("total_unfollowed", 0) or 0)
    totals["comments"] += int(session.get("total_comments", 0) or 0)


def today_action_totals(
    username: str,
    *,
    current_session: Any = None,
    day: Optional[date] = None,
) -> dict[str, int]:
    """Sum finished sessions for today, plus the in-progress session if any."""
    if not username:
        return _empty_totals()
    day_key = (day or date.today()).isoformat()
    totals = _empty_totals()
    current_start = None
    if current_session is not None and getattr(current_session, "startTime", None):
        current_start = current_session.startTime.isoformat(timespec="seconds")

    for session in _load_sessions(username):
        start = str(session.get("start_time") or "")
        if not start.startswith(day_key):
            continue
        # Skip the in-progress session if it was somehow already persisted.
        if current_start and start.startswith(current_start[:19]):
            continue
        _add_session_counts(totals, session)

    if current_session is not None and getattr(current_session, "startTime", None):
        if current_session.startTime.date().isoformat() == day_key:
            totals["likes"] += int(getattr(current_session, "totalLikes", 0) or 0)
            story = int(getattr(current_session, "totalStoryLikes", 0) or 0)
            watched = int(getattr(current_session, "totalWatched", 0) or 0)
            totals["story_likes"] += story if story else watched
            totals["story_accounts_liked"] += int(
                getattr(current_session, "totalStoryAccountsLiked", 0) or 0
            )
            totals["watched"] += watched
            followed = getattr(current_session, "totalFollowed", {}) or {}
            totals["follows"] += (
                sum(followed.values()) if isinstance(followed, dict) else int(followed or 0)
            )
            totals["unfollows"] += int(
                getattr(current_session, "totalUnfollowed", 0) or 0
            )
            totals["comments"] += int(getattr(current_session, "totalComments", 0) or 0)

    return totals


def today_totals_from_disk(username: str) -> dict[str, int]:
    """Farm/dashboard helper — finished sessions today only (no live session object)."""
    return today_action_totals(username, current_session=None)
