"""Track Instagram DM / Community Standards messaging blocks + smart PM caps.

When IG shows "You can't send messages at this time" (Community Standards),
we record the hit and auto-lower the account's effective PM limit for 24 hours.
Repeat hits inside that window escalate (lower the cap further), similar to the
action-limit streak ladder. After 24h with no new hits the smart cap clears.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Tuple

from atomicwrites import atomic_write

from GramAddict.core.storage import ACCOUNTS

if TYPE_CHECKING:
    from GramAddict.core.session_state import SessionState

logger = logging.getLogger(__name__)

HISTORY_FILENAME = "dm_limit_history.json"
MAX_EVENTS = 50
# Smart PM caps last this long after the latest Community Standards hit.
SMART_CAP_WINDOW = timedelta(hours=24)
# Hits within this window after the previous one count as the next streak step.
_STREAK_WINDOW = timedelta(hours=24)


def _history_path(username: str) -> Path:
    safe = (username or "").strip().lstrip("@")
    return Path(ACCOUNTS) / safe / HISTORY_FILENAME


def _parse_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_raw(username: str) -> dict[str, Any]:
    path = _history_path(username)
    if not path.is_file():
        return {"events": [], "smart_pm_cap": None, "smart_pm_cap_until": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": [], "smart_pm_cap": None, "smart_pm_cap_until": None}
    if not isinstance(raw, dict):
        return {"events": [], "smart_pm_cap": None, "smart_pm_cap_until": None}
    events = raw.get("events")
    if not isinstance(events, list):
        events = []
    return {
        "events": [e for e in events if isinstance(e, dict)],
        "smart_pm_cap": raw.get("smart_pm_cap"),
        "smart_pm_cap_until": raw.get("smart_pm_cap_until"),
        "updated_at": raw.get("updated_at"),
    }


def _save_raw(username: str, payload: dict[str, Any]) -> None:
    path = _history_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        with atomic_write(path, overwrite=True, encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except OSError as exc:
        logger.warning("Could not write DM limit history for @%s: %s", username, exc)


def _pm_today(username: str, session_state: Optional["SessionState"]) -> int:
    try:
        from GramAddict.core.day_stats import today_action_totals

        totals = today_action_totals(username, current_session=session_state)
        return int(totals.get("pm") or 0)
    except Exception:
        if session_state is None:
            return 0
        return int(getattr(session_state, "totalPm", 0) or 0)


def _next_streak(events: list[dict[str, Any]]) -> int:
    if not events:
        return 1
    last = events[0]
    last_at = _parse_time(last.get("at"))
    if last_at is None:
        return 1
    if datetime.now() - last_at <= _STREAK_WINDOW:
        return max(1, int(last.get("streak") or 1)) + 1
    return 1


def _cap_for_hit(*, streak: int, pm_today: int, prev_cap: Optional[int]) -> int:
    """Lower the effective session/day PM ceiling after a Community Standards hit.

    streak 1: freeze at today's successful count (no more for the window)
    streak 2: half of previous / today's count
    streak 3+: 0 DMs for the rest of the 24h window
    """
    baseline = max(0, int(pm_today))
    if streak <= 1:
        return baseline
    if streak == 2:
        half_of = prev_cap if prev_cap is not None else baseline
        return max(0, int(half_of) // 2)
    return 0


def latest_smart_cap_kind(username: str) -> Optional[str]:
    """Kind of the newest event that still holds the 24h smart cap, if any."""
    if active_smart_pm_cap(username) is None:
        return None
    safe = (username or "").strip().lstrip("@")
    events = _load_raw(safe).get("events") or []
    if not events:
        return None
    kind = str(events[0].get("kind") or "").strip()
    return kind or None


def smart_cap_is_message_request_limit(username: str) -> bool:
    """True when the 24h cap is IG's 'message request limit' (not Community Standards).

    DMs to people who already follow us are not message requests, so follow-back
    sends can still go out under this cap.
    """
    kind = latest_smart_cap_kind(username) or ""
    return kind in ("message_request_limit", "request_limit")


def active_smart_pm_cap(username: str) -> Optional[int]:
    """Return the active smart PM cap, or None if expired / unset."""
    safe = (username or "").strip().lstrip("@")
    if not safe:
        return None
    raw = _load_raw(safe)
    until = _parse_time(raw.get("smart_pm_cap_until"))
    cap = raw.get("smart_pm_cap")
    if until is None or cap is None:
        return None
    if until <= datetime.now():
        # Expired — clear so Farm / next session start clean.
        if raw.get("smart_pm_cap") is not None or raw.get("smart_pm_cap_until"):
            raw["smart_pm_cap"] = None
            raw["smart_pm_cap_until"] = None
            _save_raw(safe, raw)
            logger.info(
                "Smart PM cap for @%s expired after 24h — restored to config limit.",
                safe,
            )
        return None
    try:
        return max(0, int(cap))
    except (TypeError, ValueError):
        return None


def apply_smart_pm_limit_to_session(username: str, session_state: "SessionState") -> None:
    """Clamp ``current_pm_limit`` to the active 24h smart cap (if any)."""
    if session_state is None or getattr(session_state, "args", None) is None:
        return
    cap = active_smart_pm_cap(username)
    if cap is None:
        return
    try:
        current = int(getattr(session_state.args, "current_pm_limit", 0) or 0)
    except (TypeError, ValueError):
        current = 0
    new_limit = min(current, cap) if current > 0 else cap
    # Never raise above what was already sent this session.
    already = int(getattr(session_state, "totalPm", 0) or 0)
    new_limit = max(already, int(new_limit))
    if new_limit != current:
        session_state.args.current_pm_limit = new_limit
        logger.info(
            "Smart PM limit active for @%s: session cap %s → %s (24h Community Standards).",
            username.lstrip("@"),
            current,
            new_limit,
        )


def freeze_session_pm_limit(session_state: "SessionState") -> None:
    """Stop further PMs this session (cap = already sent)."""
    if session_state is None or getattr(session_state, "args", None) is None:
        return
    already = int(getattr(session_state, "totalPm", 0) or 0)
    session_state.args.current_pm_limit = already
    logger.info(
        "Session PM limit frozen at %s after messaging restriction.",
        already,
    )


def record_dm_limit_event(
    username: str,
    session_state: "SessionState",
    *,
    kind: str = "community_standards",
    current_job: Optional[str] = None,
) -> Tuple[int, int]:
    """Record a hard DM block and refresh the 24h smart PM cap.

    Returns ``(smart_pm_cap, streak)``.
    """
    safe = (username or "").strip().lstrip("@")
    if not safe or session_state is None:
        return 0, 1

    raw = _load_raw(safe)
    events = list(raw.get("events") or [])
    streak = _next_streak(events)
    pm_session = int(getattr(session_state, "totalPm", 0) or 0)
    pm_today = _pm_today(safe, session_state)
    prev_cap: Optional[int] = None
    try:
        if raw.get("smart_pm_cap") is not None:
            prev_cap = int(raw["smart_pm_cap"])
    except (TypeError, ValueError):
        prev_cap = None

    smart_cap = _cap_for_hit(streak=streak, pm_today=pm_today, prev_cap=prev_cap)
    until = datetime.now() + SMART_CAP_WINDOW

    try:
        config_pm = int(getattr(session_state.args, "current_pm_limit", 0) or 0)
    except (TypeError, ValueError):
        config_pm = 0

    job = current_job
    if not job:
        try:
            from GramAddict.core.live_progress import load_live_progress

            live = load_live_progress(safe) or {}
            job = live.get("current_job")
        except Exception:
            job = None

    event = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "job": job,
        "streak": streak,
        "pm_sent_session": pm_session,
        "pm_sent_today": pm_today,
        "config_pm_limit": config_pm,
        "smart_pm_cap": smart_cap,
        "smart_pm_cap_until": until.isoformat(timespec="seconds"),
    }
    events.insert(0, event)
    events = events[:MAX_EVENTS]
    payload = {
        "events": events,
        "smart_pm_cap": smart_cap,
        "smart_pm_cap_until": until.isoformat(timespec="seconds"),
    }
    _save_raw(safe, payload)

    # Apply immediately to the live session.
    freeze_session_pm_limit(session_state)
    try:
        session_state.args.current_pm_limit = min(
            int(session_state.args.current_pm_limit), smart_cap
        )
        # Keep freeze semantics: cannot send more this session either.
        session_state.args.current_pm_limit = min(
            int(session_state.args.current_pm_limit),
            int(session_state.totalPm or 0),
        )
    except Exception:
        pass

    logger.warning(
        "DM Community Standards limit for @%s — streak=%s, smart PM cap=%s for 24h "
        "(session PMs=%s, today=%s).",
        safe,
        streak,
        smart_cap,
        pm_session,
        pm_today,
    )

    try:
        from GramAddict.plugins.telegram import send_telegram_alert

        send_telegram_alert(
            safe,
            "DM limit",
            f"smart cap {smart_cap}/24h (streak {streak})",
        )
    except Exception:
        pass

    return smart_cap, streak


def load_dm_limit_history(username: str, *, max_events: int = 20) -> dict[str, Any]:
    safe = (username or "").strip().lstrip("@")
    raw = _load_raw(safe)
    events = list(raw.get("events") or [])[: max(1, min(max_events, MAX_EVENTS))]
    cap = active_smart_pm_cap(safe)
    return {
        "events": events,
        "smart_pm_cap": cap,
        "smart_pm_cap_until": raw.get("smart_pm_cap_until") if cap is not None else None,
        "updated_at": raw.get("updated_at"),
    }


def active_dm_limit_until(username: str) -> Optional[str]:
    """ISO end time of the active smart PM cap window, if any."""
    safe = (username or "").strip().lstrip("@")
    if active_smart_pm_cap(safe) is None:
        return None
    raw = _load_raw(safe)
    until = raw.get("smart_pm_cap_until")
    return until if isinstance(until, str) else None
