"""Hard safety rules for production Instagram accounts."""

from __future__ import annotations

from typing import Any

# Accounts that must never auto-post reels from the bot.
AUTOPOST_LOCKED_USERNAMES = frozenset({"615films", "yourlovefilms"})


def normalize_account_key(key: str) -> str:
    return str(key or "").strip().lstrip("@").lower()


def is_autopost_locked(*keys: str) -> bool:
    for key in keys:
        if normalize_account_key(key) in AUTOPOST_LOCKED_USERNAMES:
            return True
    return False


def apply_autopost_lock(account_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Force post-reels off for locked accounts."""
    if not is_autopost_locked(account_id, str(data.get("username") or "")):
        return data
    out = dict(data)
    out["post-reels"] = "0"
    return out
