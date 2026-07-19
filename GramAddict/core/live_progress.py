"""Write lightweight session progress snapshots for on-demand status (e.g. Telegram)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from atomicwrites import atomic_write

from GramAddict.core.storage import ACCOUNTS

if TYPE_CHECKING:
    from GramAddict.core.session_state import SessionState

logger = logging.getLogger(__name__)


def _progress_path(username: str) -> Path:
    return Path(ACCOUNTS) / username / "live_progress.json"


def write_live_progress(
    username: str,
    session_state: SessionState,
    *,
    running: bool = True,
    current_job: Optional[str] = None,
    sleeping: bool = False,
    next_session_at: Optional[str] = None,
    rate_limited: bool = False,
) -> None:
    if not username:
        return
    path = _progress_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-action writes don't know the job name; keep the last one we recorded
    # so the label doesn't blank out between the coarse per-job writes.
    if current_job is None:
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                current_job = prev.get("current_job")
        except (OSError, json.JSONDecodeError):
            pass
    payload: dict[str, Any] = {
        "username": username,
        "running": running,
        "current_job": current_job,
        "sleeping": bool(sleeping),
        "next_session_at": next_session_at,
        # True while paused for Instagram "Try Again Later" / action limit.
        "rate_limited": bool(rate_limited),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "session_started_at": (
            session_state.startTime.isoformat(timespec="seconds")
            if session_state.startTime
            else None
        ),
        "total_likes": session_state.totalLikes,
        "total_followed": sum(session_state.totalFollowed.values()),
        "total_unfollowed": session_state.totalUnfollowed,
        "total_watched": session_state.totalWatched,
        "total_story_likes": session_state.totalStoryLikes,
        "total_daily_story_accounts": session_state.totalDailyStoryAccounts,
        "daily_story_likes_limit": session_state.daily_story_likes_limit,
        "total_comments": session_state.totalComments,
        "total_pm": session_state.totalPm,
        "total_interactions": sum(session_state.totalInteractions.values()),
        "limits": {
            "likes": getattr(session_state.args, "current_likes_limit", None),
            "follows": getattr(session_state.args, "current_follow_limit", None),
            "watches": getattr(session_state.args, "current_watch_limit", None),
            "comments": getattr(session_state.args, "current_comments_limit", None),
        },
    }
    try:
        with atomic_write(path, overwrite=True, encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except OSError as exc:
        logger.debug("Could not write live progress for %s: %s", username, exc)


def load_live_progress(username: str) -> Optional[dict[str, Any]]:
    path = _progress_path(username)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
