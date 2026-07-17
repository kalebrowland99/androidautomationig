"""Persist Instagram 'Try Again Later' events with session counts at detection time."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from atomicwrites import atomic_write

from GramAddict.core.live_progress import load_live_progress
from GramAddict.core.storage import ACCOUNTS

if TYPE_CHECKING:
    from GramAddict.core.session_state import SessionState

logger = logging.getLogger(__name__)

HISTORY_FILENAME = "rate_limit_history.json"
MAX_EVENTS = 50


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
            "Recorded Instagram rate limit snapshot for @%s (job=%s, daily_story_today=%s).",
            safe,
            job or "?",
            daily_today if daily_today is not None else counts.get("daily_story_accounts_session"),
        )
    except OSError as exc:
        logger.warning("Could not write rate limit history for @%s: %s", safe, exc)


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
