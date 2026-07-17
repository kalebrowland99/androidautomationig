"""Append-only daily story likes log per account (logs/<username>_story_likes.log)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from GramAddict.core.storage import Storage

logger = logging.getLogger(__name__)

_LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_STORY_LOG_LINE = re.compile(
    r"^\[(\d{2}/\d{2} \d{1,2}:\d{2}:\d{2} [AP]M)\] @([^:]+): (liked|checked)",
    re.IGNORECASE,
)


def story_likes_log_path(username: str) -> Path:
    safe = (username or "").strip().lstrip("@")
    return _LOGS_DIR / f"{safe}_story_likes.log"


def append_story_likes_log(username: str, message: str) -> None:
    """Record a story-likes event for the dashboard farm log."""
    safe = (username or "").strip().lstrip("@")
    if not safe or not message:
        return
    text = message.rstrip()
    # Echo to the main bot log so the dashboard websocket can show live updates.
    logger.info("Story likes | %s", text)
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%m/%d %I:%M:%S %p")
        line = f"[{stamp}] {text}\n"
        with open(story_likes_log_path(safe), "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        logger.debug("Could not write story likes log: %s", exc)


def backfill_story_checks_from_todays_log(
    storage: "Storage",
    username: str,
    session_id: str,
    *,
    list_usernames: Optional[set[str]] = None,
) -> int:
    """Restore today's daily story-like visits from the dedicated log after a restart."""
    log_path = story_likes_log_path(username)
    if not log_path.is_file():
        return 0
    today = datetime.now().date()
    year = today.year
    latest: dict[str, datetime] = {}
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        match = _STORY_LOG_LINE.match(line.strip())
        if not match:
            continue
        stamp_raw, uname, _action = match.groups()
        uname = uname.strip().lstrip("@")
        if not uname:
            continue
        if list_usernames is not None and uname not in list_usernames:
            continue
        try:
            when = datetime.strptime(f"{stamp_raw} {year}", "%m/%d %I:%M:%S %p %Y")
        except ValueError:
            continue
        if when.date() != today:
            continue
        prev = latest.get(uname)
        if prev is None or when > prev:
            latest[uname] = when
    backfilled = 0
    for uname, when in latest.items():
        if storage.story_checked_today(uname):
            continue
        storage.record_story_check(uname, session_id, when=when)
        backfilled += 1
    return backfilled
