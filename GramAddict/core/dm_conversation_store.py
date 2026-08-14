"""Persist per-lead Instagram DM conversation state for mule accounts."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from atomicwrites import atomic_write

from GramAddict.core.storage import ACCOUNTS

logger = logging.getLogger(__name__)

FILENAME = "dm_conversations.json"
MAX_MESSAGES = 80

STATUS_ACTIVE = "active"
STATUS_SKIPPED_LOW = "skipped_low_confidence"
STATUS_SKIPPED_BUSINESS = "skipped_business_name"
STATUS_GOT_PHONE = "got_phone"
STATUS_CLOSED = "closed"

CONF_UNKNOWN = "unknown"
CONF_HIGH = "high"
CONF_LOW = "low"

# One bump after they go quiet mid-thread — never for unanswered cold DMs.
FOLLOWUP_AFTER_HOURS = 24
MAX_FOLLOWUPS_PER_LEAD = 1

_WEAK_INBOUND_RE = re.compile(
    r"^(like|liked a message|❤|❤️|♥|♥️"
    r"|follow request accepted"
    r"|.+ accepted your follow request\.?"
    r"|\d+\s+new followers?"
    r"|you're now friends\.? say hi!?"
    r")$",
    re.I,
)

# Them-text that means this is not a couple we should bump.
_FOLLOWUP_DISQUALIFY_RE = re.compile(
    r"("
    r"already got married"
    r"|i got married in\s+\d{4}"
    r"|i've been married"
    r"|been married for"
    r"|my anniversary"
    r"|i am a .{0,60}(photographer|videographer|filmmaker|planner|stylist|teacher)"
    r"|i['’]?m a .{0,60}(photographer|videographer|filmmaker|planner|stylist|teacher)"
    r"|i['’]?m .{0,50}photographer"
    r"|fellow creative"
    r"|book a (private )?tour"
    r"|our (gorgeous )?venue"
    r"|opening day"
    r"|my (niece|nephew|friend|sister|brother|son|daughter).{0,50}"
    r"(wedding|married|getting married)"
    r"|not (my|our) wedding"
    r"|i didn't get married"
    r"|not getting married"
    r"|not even in a relationship"
    r")",
    re.I,
)

# If they also clearly said THEY are planning, keep them (e.g. elopement after "I didn't get married").
_FOLLOWUP_STILL_LEAD_RE = re.compile(
    r"("
    r"i am getting married"
    r"|i'm getting married"
    r"|we('re| are) getting married"
    r"|planning a wedding"
    r"|wedding is in the works"
    r"|elop(e|ing|ement)"
    r"|my fiance"
    r"|my fiancé"
    r"|haven't chosen one yet"
    r"|locking down a venue"
    r")",
    re.I,
)


def _path(username: str) -> Path:
    safe = (username or "").strip().lstrip("@")
    return Path(ACCOUNTS) / safe / FILENAME


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_store(username: str) -> dict[str, Any]:
    path = _path(username)
    if not path.is_file():
        return {"conversations": {}, "updated_at": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"conversations": {}, "updated_at": None}
    if not isinstance(raw, dict):
        return {"conversations": {}, "updated_at": None}
    convos = raw.get("conversations")
    if not isinstance(convos, dict):
        convos = {}
    return {"conversations": convos, "updated_at": raw.get("updated_at")}


def save_store(username: str, store: dict[str, Any]) -> None:
    path = _path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "conversations": store.get("conversations") or {},
        "updated_at": _now(),
    }
    try:
        with atomic_write(path, overwrite=True, encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.warning("Could not write dm_conversations for @%s: %s", username, exc)


def get_conversation(username: str, lead: str) -> dict[str, Any]:
    lead_key = (lead or "").strip().lstrip("@").lower()
    store = load_store(username)
    existing = store["conversations"].get(lead_key)
    if isinstance(existing, dict):
        return existing
    return {
        "username": lead_key,
        "messages": [],
        "wedding_confidence": CONF_UNKNOWN,
        "location": None,
        "phone": None,
        "status": STATUS_ACTIVE,
        "updated_at": None,
    }


def upsert_conversation(username: str, lead: str, convo: dict[str, Any]) -> None:
    lead_key = (lead or "").strip().lstrip("@").lower()
    store = load_store(username)
    convo = dict(convo)
    convo["username"] = lead_key
    convo["updated_at"] = _now()
    msgs = convo.get("messages") or []
    if isinstance(msgs, list) and len(msgs) > MAX_MESSAGES:
        convo["messages"] = msgs[-MAX_MESSAGES:]
    store["conversations"][lead_key] = convo
    save_store(username, store)


def append_messages(
    username: str,
    lead: str,
    *,
    new_messages: list[dict[str, str]],
    **fields: Any,
) -> dict[str, Any]:
    """Merge new transcript lines (dedupe by role+text tail) and optional fields."""
    convo = get_conversation(username, lead)
    existing = list(convo.get("messages") or [])
    seen = {(m.get("role"), (m.get("text") or "").strip()) for m in existing}
    for msg in new_messages:
        role = msg.get("role")
        text = (msg.get("text") or "").strip()
        if not text or role not in ("them", "us"):
            continue
        key = (role, text)
        if key in seen:
            continue
        existing.append({"role": role, "text": text, "at": msg.get("at") or _now()})
        seen.add(key)
    convo["messages"] = existing
    for key, value in fields.items():
        if value is not None:
            convo[key] = value
    upsert_conversation(username, lead, convo)
    return convo


def should_skip_lead(convo: dict[str, Any]) -> bool:
    status = str(convo.get("status") or STATUS_ACTIVE)
    if status in (
        STATUS_SKIPPED_LOW,
        STATUS_SKIPPED_BUSINESS,
        STATUS_GOT_PHONE,
        STATUS_CLOSED,
    ):
        return True
    if str(convo.get("wedding_confidence") or "") == CONF_LOW:
        return True
    if convo.get("phone"):
        return True
    return False


def last_substantive_role(convo: Optional[dict[str, Any]]) -> Optional[str]:
    """Last real chat role, ignoring Like / follow-accept chrome."""
    if not convo:
        return None
    for msg in reversed(list(convo.get("messages") or [])):
        role = msg.get("role")
        text = str(msg.get("text") or "").strip()
        if role not in ("them", "us") or not text:
            continue
        if role == "them" and not _is_substantive_inbound(text):
            continue
        return role
    return None


def last_message_role(convo: Optional[dict[str, Any]]) -> Optional[str]:
    if not convo:
        return None
    msgs = convo.get("messages") or []
    if not msgs:
        return None
    return msgs[-1].get("role")


def is_cold_outbound_waiting(convo: Optional[dict[str, Any]], *, unread: bool) -> bool:
    """
    True when we already sent the last message and they haven't come back.
    Unread rows are never treated as cold (they may have replied).
    """
    if unread:
        return False
    return last_message_role(convo) == "us"


def _parse_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _is_substantive_inbound(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 2:
        return False
    return _WEAK_INBOUND_RE.match(t) is None


def _them_blob(convo: Optional[dict[str, Any]]) -> str:
    if not convo:
        return ""
    parts = []
    for msg in convo.get("messages") or []:
        if msg.get("role") == "them":
            parts.append(str(msg.get("text") or ""))
    return "\n".join(parts)


def is_followup_disqualified(convo: Optional[dict[str, Any]]) -> bool:
    """True when their own messages show they're not a couple lead."""
    blob = _them_blob(convo)
    if not blob.strip():
        return True
    if _FOLLOWUP_STILL_LEAD_RE.search(blob):
        return False
    return _FOLLOWUP_DISQUALIFY_RE.search(blob) is not None


def has_lead_reply(convo: Optional[dict[str, Any]]) -> bool:
    """True if they actually chatted back (not just Like / follow-accept chrome)."""
    if not convo:
        return False
    for msg in convo.get("messages") or []:
        if msg.get("role") != "them":
            continue
        if _is_substantive_inbound(str(msg.get("text") or "")):
            return True
    return False


def last_us_at(convo: Optional[dict[str, Any]]) -> Optional[datetime]:
    if not convo:
        return None
    for msg in reversed(list(convo.get("messages") or [])):
        if msg.get("role") != "us":
            continue
        parsed = _parse_at(msg.get("at"))
        if parsed:
            return parsed
    return _parse_at(convo.get("updated_at"))


def is_lead_followup_due(
    convo: Optional[dict[str, Any]],
    *,
    unread: bool = False,
    now: Optional[datetime] = None,
    after_hours: int = FOLLOWUP_AFTER_HOURS,
    max_followups: int = MAX_FOLLOWUPS_PER_LEAD,
) -> bool:
    """
    True when a potential wedding lead replied, then went quiet after our last
    message for at least `after_hours`. Never true for cold DMs they ignored.
    """
    if not convo or unread or after_hours <= 0:
        return False
    if should_skip_lead(convo):
        return False
    if str(convo.get("wedding_confidence") or "").lower() != CONF_HIGH:
        return False
    if not has_lead_reply(convo):
        return False
    if is_followup_disqualified(convo):
        return False
    if last_substantive_role(convo) != "us":
        return False
    sent = int(convo.get("followups_sent") or 0)
    if sent >= max(1, int(max_followups or 1)):
        return False
    last = last_us_at(convo)
    if not last:
        return False
    age = (now or datetime.now()) - last
    return age >= timedelta(hours=max(1, int(after_hours)))


def list_due_followup_leads(
    username: str,
    *,
    now: Optional[datetime] = None,
    after_hours: int = FOLLOWUP_AFTER_HOURS,
    max_followups: int = MAX_FOLLOWUPS_PER_LEAD,
) -> list[str]:
    """Leads in this account's store that are due a one-day bump, oldest first."""
    store = load_store(username)
    due: list[tuple[datetime, str]] = []
    for lead, convo in (store.get("conversations") or {}).items():
        if not isinstance(convo, dict):
            continue
        if not is_lead_followup_due(
            convo,
            now=now,
            after_hours=after_hours,
            max_followups=max_followups,
        ):
            continue
        due.append((last_us_at(convo) or datetime.min, lead))
    due.sort(key=lambda row: row[0])
    return [lead for _, lead in due]
