"""Persist Instagram 'Try Again Later' events with session counts at detection time."""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Tuple

from atomicwrites import atomic_write

from GramAddict.core.live_progress import load_live_progress
from GramAddict.core.storage import ACCOUNTS

if TYPE_CHECKING:
    from GramAddict.core.session_state import SessionState

logger = logging.getLogger(__name__)

HISTORY_FILENAME = "rate_limit_history.json"
MAX_EVENTS = 50

# Consecutive action-limit pauses ("in a row"). Step 1 is a random range.
# After a successful stretch past the prior break, the streak resets to 1.
RATE_LIMIT_BREAK_LADDER_MINUTES = (
    (60, 90),  # 1st: 1–1.5 hours
    180,  # 2nd: 3 hours
    480,  # 3rd: 8 hours
    720,  # 4th: 12 hours
    1440,  # 5th+: 24 hours
)
# If we get limited again within this many minutes after the previous break
# would have ended, count it as the next step in the ladder.
_STREAK_GRACE_AFTER_BREAK_MINUTES = 6 * 60


def _parse_event_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _minutes_for_streak(streak: int) -> int:
    """Map 1-based consecutive streak → pause minutes."""
    ladder = RATE_LIMIT_BREAK_LADDER_MINUTES
    idx = max(0, min(int(streak) - 1, len(ladder) - 1))
    step = ladder[idx]
    if isinstance(step, tuple):
        low, high = int(step[0]), int(step[1])
        if high < low:
            low, high = high, low
        return random.randint(low, high)
    return int(step)


def _load_events(username: str) -> list[dict[str, Any]]:
    path = _history_path(username)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict)]


def next_rate_limit_break_minutes(username: str) -> Tuple[int, int]:
    """Return ``(break_minutes, streak)`` for the next action-limit pause.

    Streak escalates when limits keep happening in a row (hit again soon after
    the previous break ends). Otherwise it resets to 1 (1–1.5h).
    """
    safe = (username or "").strip().lstrip("@")
    events = _load_events(safe)
    streak = 1
    if events:
        last = events[0]
        # Events from before progressive pauses have no streak — start fresh.
        if last.get("streak") is not None:
            last_at = _parse_event_time(last.get("at"))
            last_break = int(last.get("break_minutes") or 0)
            last_streak = max(1, int(last.get("streak") or 1))
            if last_at is not None and last_break >= 0:
                elapsed_min = (datetime.now() - last_at).total_seconds() / 60.0
                # Still "in a row" if within prior break + grace of last hit.
                if elapsed_min <= last_break + _STREAK_GRACE_AFTER_BREAK_MINUTES:
                    streak = last_streak + 1
    minutes = _minutes_for_streak(streak)
    return minutes, streak


def _history_path(username: str) -> Path:
    safe = (username or "").strip().lstrip("@")
    return Path(ACCOUNTS) / safe / HISTORY_FILENAME


def _daily_story_accounts_today(username: str) -> Optional[int]:
    """Count list entries with last_story_check today (cross-session daily batch)."""
    try:
        from GramAddict.core.brand_pool import resolve_brand_pool
        from GramAddict.core.storage import Storage

        account_path = Path(ACCOUNTS) / username
        list_path = account_path / "story_likes.txt"
        if not list_path.is_file():
            return None
        usernames = [
            line.strip().lstrip("@")
            for line in list_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not usernames:
            return 0
        brand_pool = resolve_brand_pool(
            config_path=str(account_path / "config.yml"),
            ig_username=username,
        )
        storage = Storage(username, brand_pool=brand_pool)
        return storage.count_story_checks_today_in_list(usernames)
    except Exception as exc:
        logger.debug("Could not count daily story accounts today: %s", exc)
        return None


def snapshot_session_counts(session_state: "SessionState") -> dict[str, Any]:
    return {
        "likes": int(session_state.totalLikes or 0),
        "follows": int(sum(session_state.totalFollowed.values()) or 0),
        "unfollows": int(session_state.totalUnfollowed or 0),
        "comments": int(session_state.totalComments or 0),
        "pm": int(session_state.totalPm or 0),
        "watched": int(session_state.totalWatched or 0),
        "story_likes": int(session_state.totalStoryLikes or 0),
        "daily_story_accounts_session": int(session_state.totalDailyStoryAccounts or 0),
        "interactions": int(sum(session_state.totalInteractions.values()) or 0),
    }


def record_rate_limit_event(
    username: str,
    session_state: "SessionState",
    *,
    break_minutes: int,
    streak: int = 1,
    current_job: Optional[str] = None,
) -> None:
    """Append one rate-limit hit with counters at detection time."""
    safe = (username or "").strip().lstrip("@")
    if not safe or session_state is None:
        return

    live = load_live_progress(safe) or {}
    job = current_job or live.get("current_job")
    counts = snapshot_session_counts(session_state)
    if live.get("total_daily_story_accounts") is not None:
        counts["daily_story_accounts_live"] = int(live["total_daily_story_accounts"])
    daily_today = _daily_story_accounts_today(safe)
    if daily_today is not None:
        counts["daily_story_accounts_today"] = daily_today

    event = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "job": job,
        "break_minutes": int(break_minutes),
        "streak": max(1, int(streak)),
        "counts": counts,
    }

    path = _history_path(safe)
    path.parent.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                events = list(raw.get("events") or [])
            elif isinstance(raw, list):
                events = list(raw)
        except (OSError, json.JSONDecodeError):
            events = []

    events.insert(0, event)
    events = events[:MAX_EVENTS]
    payload = {"events": events, "updated_at": datetime.now().isoformat(timespec="seconds")}

    try:
        with atomic_write(path, overwrite=True, encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        logger.info(
            "Recorded Instagram rate limit snapshot for @%s "
            "(job=%s, streak=%s, break=%sm, daily_story_today=%s).",
            safe,
            job or "?",
            event["streak"],
            break_minutes,
            daily_today if daily_today is not None else counts.get("daily_story_accounts_session"),
        )
    except OSError as exc:
        logger.warning("Could not write rate limit history for @%s: %s", safe, exc)


def active_rate_limit_resume_at(username: str) -> Optional[str]:
    """ISO resume time if the latest recorded action-limit break is still active."""
    safe = (username or "").strip().lstrip("@")
    events = _load_events(safe)
    if not events:
        return None
    last = events[0]
    at = _parse_event_time(last.get("at"))
    break_minutes = int(last.get("break_minutes") or 0)
    if at is None or break_minutes <= 0:
        return None
    resume = at + timedelta(minutes=break_minutes)
    if resume <= datetime.now():
        return None
    return resume.isoformat(timespec="seconds")


def load_rate_limit_history(username: str, *, max_events: int = 20) -> dict[str, Any]:
    safe = (username or "").strip().lstrip("@")
    path = _history_path(safe)
    if not path.is_file():
        return {"events": [], "summary": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": [], "summary": None}

    events = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(events, list):
        events = []
    events = events[: max(1, min(max_events, MAX_EVENTS))]
    latest = events[0] if events else None
    summary = _summarize_event(latest) if latest else None
    return {"events": events, "summary": summary, "updated_at": raw.get("updated_at")}


def _summarize_event(event: dict[str, Any]) -> dict[str, Any]:
    counts = event.get("counts") or {}
    daily = (
        counts.get("daily_story_accounts_today")
        or counts.get("daily_story_accounts_live")
        or counts.get("daily_story_accounts_session")
    )
    return {
        "at": event.get("at"),
        "job": event.get("job"),
        "daily_story_accounts": daily,
        "story_likes": counts.get("story_likes"),
        "follows": counts.get("follows"),
        "likes": counts.get("likes"),
        "comments": counts.get("comments"),
    }
