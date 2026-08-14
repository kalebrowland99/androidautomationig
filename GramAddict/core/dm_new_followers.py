"""Session-start pass: DM follow-backs who never got a cold DM.

When the daily PM cap hits, the bot still follows private likers. Those people
often follow back later. This pass opens Notifications → Follows, skips anyone
already marked pm_sent (this mule or the brand pool), and sends the same cold
videographer DM used in outreach — once, before jobs / new cold DMs.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

from colorama import Fore

from GramAddict.core.device_facade import Direction, Timeout
from GramAddict.core.resources import ClassName
from GramAddict.core.resources import ResourceID as ResourceIDFactory
from GramAddict.core.utils import random_sleep
from GramAddict.core.views import ProfileView, TabBarView, UniversalActions, case_insensitive_re

logger = logging.getLogger(__name__)

APP_ID = "com.instagram.android"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")
_FOLLOW_YOU_RE = re.compile(
    r"^\s*@?([A-Za-z0-9._]{2,30})[^\w]*(started following you|followed you)\b",
    re.I,
)
_AGGREGATE_RE = re.compile(r"\band\s+\d+\s+others?\b", re.I)
_SKIP_PHRASES = (
    "requested to follow you",
    "accepted your follow",
    "follow request",
    "suggested for you",
    "suggestions for you",
)


def _normalize_handle(value: Optional[str]) -> str:
    return (value or "").strip().replace("\xa0", " ").lstrip("@").strip()


def _is_valid_ig_username(value: Optional[str]) -> bool:
    s = _normalize_handle(value)
    return bool(s) and bool(_USERNAME_RE.fullmatch(s))


def _settings(account_key: str) -> dict[str, Any]:
    from GramAddict.core.follow_vision_account import get_account_follow_vision

    try:
        return get_account_follow_vision(account_key)
    except FileNotFoundError:
        return {}


def _int_setting(settings: dict[str, Any], key: str, default: int) -> int:
    try:
        return max(0, int(settings.get(key, default)))
    except (TypeError, ValueError):
        return default


def dm_new_followers_enabled(account_key: str, brand_pool: Optional[str] = None) -> bool:
    settings = _settings(account_key)
    if settings.get("dm-new-followers-enabled") is not None:
        return bool(settings.get("dm-new-followers-enabled"))
    return (brand_pool or "").strip().lower() == "ylf"


def _dump_xml(device) -> str:
    try:
        return device.deviceV2.dump_hierarchy() or ""
    except Exception as exc:
        logger.debug("hierarchy dump failed: %s", exc)
        return ""


def _parse_nodes(xml: str) -> list[dict[str, str]]:
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    nodes = []
    for el in root.iter("node"):
        nodes.append(
            {
                k: (el.attrib.get(k) or "")
                for k in (
                    "text",
                    "content-desc",
                    "resource-id",
                    "class",
                    "clickable",
                    "bounds",
                )
            }
        )
    return nodes


def _parse_bounds(bounds: str) -> Optional[tuple[int, int, int, int]]:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    return tuple(int(v) for v in match.groups())  # type: ignore[return-value]


def _blob_of(node: dict[str, str]) -> str:
    return f"{node.get('text') or ''} {node.get('content-desc') or ''}".strip()


def _username_from_follow_blob(blob: str) -> Optional[str]:
    text = (blob or "").replace("\xa0", " ").strip()
    if not text:
        return None
    low = text.lower()
    if "started following you" not in low and not re.search(
        r"\bfollowed you\b", low
    ):
        return None
    if any(phrase in low for phrase in _SKIP_PHRASES):
        return None
    if _AGGREGATE_RE.search(low):
        return None
    prefix = re.split(
        r"started following you|\bfollowed you\b", text, maxsplit=1, flags=re.I
    )[0]
    if "," in prefix:
        return None
    match = _FOLLOW_YOU_RE.search(text)
    if not match:
        return None
    handle = match.group(1)
    if _is_valid_ig_username(handle):
        return handle
    return None


def _on_notifications(device) -> bool:
    xml = _dump_xml(device)
    if not xml:
        return False
    nodes = _parse_nodes(xml)
    follow_rows = 0
    has_chrome = False
    for node in nodes:
        blob = _blob_of(node)
        low = blob.lower()
        rid = (node.get("resource-id") or "").lower()
        if "newsfeed" in rid or rid.endswith("/notification_list"):
            has_chrome = True
        if low in {"notifications", "activity", "follows"} or low.startswith(
            "notifications"
        ):
            has_chrome = True
        if _username_from_follow_blob(blob):
            follow_rows += 1
    if has_chrome:
        return True
    return follow_rows >= 1 and not _on_foreign_profile(device)


def _on_foreign_profile(device) -> bool:
    try:
        avatar = device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceIDFactory(APP_ID).ROW_PROFILE_HEADER_IMAGEVIEW
            )
        )
        if avatar.exists(Timeout.ZERO):
            username = ProfileView(device).getUsername()
            return bool(username)
    except Exception:
        pass
    return False


def _tap_follows_chip(device) -> None:
    chip = device.find(
        classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
        textMatches=case_insensitive_re("^Follows$"),
    )
    try:
        if chip is not None and chip.exists(Timeout.SHORT):
            chip.click()
            random_sleep(0.6, 1.0, modulable=False, log=False)
            logger.info("Notifications filter: Follows.")
            return
    except Exception:
        pass
    # Some builds put Follows in content-desc only.
    chip = device.find(descriptionMatches=case_insensitive_re("^Follows$"))
    try:
        if chip is not None and chip.exists(Timeout.TINY):
            chip.click()
            random_sleep(0.6, 1.0, modulable=False, log=False)
    except Exception:
        pass


def _tap_home_notifications_heart(device) -> bool:
    """Heart on Home's action bar (this IG build has no Activity tab)."""
    try:
        TabBarView(device).navigateToHome()
        random_sleep(0.6, 1.0, modulable=False, log=False)
    except Exception:
        pass
    # Prefer content-desc when present.
    heart = device.find(
        descriptionMatches=case_insensitive_re(
            r"^(Activity|Notifications|News)(,.+)?$"
        )
    )
    try:
        if heart is not None and heart.exists(Timeout.TINY):
            heart.click()
            random_sleep(1.0, 1.6, modulable=False)
            return _on_notifications(device)
    except Exception:
        pass
    # Current IG: top-right FrameLayout resource-id `notification`, empty desc.
    # Tab-bar Message badge also uses id/notification — only tap the top one.
    for node in _parse_nodes(_dump_xml(device)):
        rid = (node.get("resource-id") or "").lower()
        if not rid.endswith("/notification"):
            continue
        if (node.get("clickable") or "").lower() != "true":
            continue
        box = _parse_bounds(node.get("bounds") or "")
        if not box:
            continue
        x1, y1, x2, y2 = box
        if y1 > 280:
            continue
        tap_x, tap_y = (x1 + x2) // 2, (y1 + y2) // 2
        logger.info("Opening Notifications via Home heart at (%s, %s).", tap_x, tap_y)
        try:
            device.deviceV2.click(tap_x, tap_y)
        except Exception as exc:
            logger.debug("heart tap failed: %s", exc)
            return False
        random_sleep(1.2, 1.8, modulable=False)
        return _on_notifications(device)
    return False


def _open_notifications(device) -> bool:
    if _on_notifications(device):
        _tap_follows_chip(device)
        return True
    # This farm's IG puts the heart on Home, not in the tab bar.
    if _tap_home_notifications_heart(device):
        _tap_follows_chip(device)
        return True
    TabBarView(device).navigateToActivity()
    random_sleep(1.0, 1.6, modulable=False)
    if not _on_notifications(device):
        logger.warning("Could not open Instagram Notifications / Activity.")
        return False
    _tap_follows_chip(device)
    return True


def _ensure_notifications(device, max_backs: int = 5) -> bool:
    if _on_notifications(device):
        return True
    for _ in range(max_backs):
        try:
            device.back()
        except Exception:
            break
        random_sleep(0.4, 0.8, modulable=False, log=False)
        if _on_notifications(device):
            return True
    return _open_notifications(device)


def _list_follow_rows(device) -> list[dict[str, Any]]:
    info = device.get_info() or {}
    height = int(info.get("displayHeight") or 0) or 2000
    width = int(info.get("displayWidth") or 0) or 1080
    top_cut = int(height * 0.12)
    bottom_cut = int(height * 0.90)
    rows: dict[str, dict[str, Any]] = {}
    for node in _parse_nodes(_dump_xml(device)):
        blob = _blob_of(node)
        handle = _username_from_follow_blob(blob)
        if not handle:
            continue
        box = _parse_bounds(node.get("bounds") or "")
        if not box:
            continue
        x1, y1, x2, y2 = box
        if y2 < top_cut or y1 > bottom_cut:
            continue
        area = max(1, (x2 - x1) * (y2 - y1))
        existing = rows.get(handle.lower())
        if existing and existing["area"] >= area:
            continue
        # Tap left side of the row so we hit avatar/username, not Follow back.
        tap_x = max(24, min(x1 + int((x2 - x1) * 0.18), int(width * 0.42)))
        tap_y = (y1 + y2) // 2
        rows[handle.lower()] = {
            "username": handle,
            "y1": y1,
            "tap": (tap_x, tap_y),
            "area": area,
        }
    return sorted(rows.values(), key=lambda row: row["y1"])


def _storage_key(storage, username: str) -> Optional[str]:
    handle = _normalize_handle(username)
    if not handle or storage is None:
        return None
    users = getattr(storage, "interacted_users", None) or {}
    if handle in users:
        return handle
    low = handle.lower()
    for key in users:
        if str(key).lower() == low:
            return str(key)
    return None


def _already_dmed(storage, username: str) -> tuple[bool, str]:
    if storage is None:
        return False, ""
    try:
        storage.refresh_interacted_users_from_disk()
    except Exception:
        pass
    key = _storage_key(storage, username) or _normalize_handle(username)
    try:
        if storage.user_was_pm_sent(key):
            reason = storage.pm_already_sent_reason(key) or "already DMed earlier"
            return True, reason
    except Exception:
        pass
    return False, ""


def _in_outreach_history(storage, username: str) -> bool:
    """True when we already followed/interacted with them (not a random follower)."""
    return _storage_key(storage, username) is not None


def _pm_cap_reached(session_state, my_username: str) -> bool:
    # Follow-backs already follow us — IG's daily *message request* cap does not
    # apply. Community Standards / session PM freeze still stop the pass.
    try:
        from GramAddict.core.dm_limit_history import smart_cap_is_message_request_limit

        if smart_cap_is_message_request_limit(my_username):
            logger.info(
                "Message-request cap is active — still DMing follow-backs "
                "(they already follow us)."
            )
            return False
    except Exception:
        pass
    try:
        if session_state.check_limit(
            limit_type=session_state.Limit.PM, output=False
        ):
            return True
    except Exception:
        pass
    try:
        from GramAddict.core.dm_limit_history import active_smart_pm_cap

        cap = active_smart_pm_cap(my_username)
        if cap is not None and int(getattr(session_state, "totalPm", 0) or 0) >= cap:
            return True
    except Exception:
        pass
    return False


def _scroll_notifications(device) -> None:
    ua = UniversalActions(device)
    try:
        ua._swipe_points(direction=Direction.DOWN)
    except Exception:
        try:
            info = device.get_info()
            w = int(info["displayWidth"])
            h = int(info["displayHeight"])
            device.swipe_points(w // 2, int(h * 0.72), w // 2, int(h * 0.32))
        except Exception:
            pass
    random_sleep(0.7, 1.2, modulable=False, log=False)


def _open_follow_row(device, row: dict[str, Any]) -> bool:
    tap = row.get("tap")
    if not tap:
        return False
    x, y = tap
    try:
        device.deviceV2.click(int(x), int(y))
    except Exception as exc:
        logger.debug("tap follow row failed: %s", exc)
        return False
    for _ in range(6):
        random_sleep(0.45, 0.7, modulable=False, log=False)
        if _on_foreign_profile(device):
            return True
    return False


def _mark_pm_sent(storage, session_state, my_username: str, handle: str) -> None:
    if storage is None:
        return
    try:
        from GramAddict.core.storage import FollowingStatus

        key = _storage_key(storage, handle) or handle
        status = storage.get_following_status(key)
        storage.add_interacted_user(
            key,
            session_id=getattr(session_state, "session_id", None)
            or getattr(session_state, "id", None),
            followed=status
            in (FollowingStatus.FOLLOWED, FollowingStatus.REQUESTED),
            is_requested=status == FollowingStatus.REQUESTED,
            pm_sent=True,
            pm_sent_by=my_username,
        )
    except Exception:
        pass
    try:
        from GramAddict.core.dm_conversation_store import STATUS_ACTIVE, append_messages

        append_messages(
            my_username,
            handle,
            new_messages=[{"role": "us", "text": "(follow-back cold DM)"}],
            status=STATUS_ACTIVE,
        )
    except Exception:
        pass


def _dm_opened_profile(
    device,
    session_state,
    my_username: str,
    expected: str,
    profile_filter,
    storage,
) -> str:
    """On a profile opened from Notifications. Returns sent/skip/error."""
    profile = ProfileView(device)
    seen = _normalize_handle(profile.getUsername() or "")
    expected_n = _normalize_handle(expected)
    if seen and expected_n and seen.lower() != expected_n.lower():
        logger.info(
            "Opened @%s from follow row for @%s — skipping mismatch.",
            seen,
            expected_n,
        )
        return "skip"
    handle = seen or expected_n
    if not _is_valid_ig_username(handle):
        return "error"

    already, reason = _already_dmed(storage, handle)
    if already:
        logger.info(
            "@%s: %s. Skip follow-back DM.",
            handle,
            reason,
            extra={"color": f"{Fore.CYAN}"},
        )
        return "skip"

    fullname = ""
    try:
        fullname = profile.getFullName() or ""
    except Exception:
        fullname = ""
    biz_hit = None
    if profile_filter is not None:
        try:
            biz_hit = profile_filter.should_skip_dm_by_name(handle, fullname)
        except Exception:
            biz_hit = None
    if biz_hit:
        logger.info(
            "@%s: skip follow-back DM — business keyword '%s' in name/handle.",
            handle,
            biz_hit,
            extra={"color": f"{Fore.CYAN}"},
        )
        return "skip"

    skip_business = False
    try:
        skip_business = bool(
            profile_filter
            and profile_filter.conditions
            and profile_filter.conditions.get("skip_business")
        )
    except Exception:
        skip_business = False
    if skip_business:
        try:
            from GramAddict.core.filter import Filter

            if Filter._has_business_category(device, profile):
                logger.info(
                    "@%s: skip follow-back DM — business account.",
                    handle,
                    extra={"color": f"{Fore.CYAN}"},
                )
                return "skip"
        except Exception:
            pass

    is_private = False
    try:
        is_private = bool(profile.isPrivateAccount())
    except Exception:
        is_private = False

    from GramAddict.core.interaction import _send_PM

    logger.info(
        "Follow-back DM @%s (%s).",
        handle,
        "private" if is_private else "public",
        extra={"color": f"{Fore.GREEN}"},
    )
    sent = _send_PM(
        device,
        session_state,
        my_username,
        swipe_amount=0,
        private=is_private,
    )
    if sent:
        _mark_pm_sent(storage, session_state, my_username, handle)
        return "sent"
    return "error"


def run_dm_new_followers(
    device,
    session_state,
    my_username: str,
    *,
    profile_filter=None,
    brand_pool: Optional[str] = None,
) -> dict[str, int]:
    """Session-start hook: DM follow-backs who were never cold-DMed."""
    stats = {"seen": 0, "sent": 0, "skipped": 0, "errors": 0}
    account = (my_username or "").lstrip("@")
    if not account or not dm_new_followers_enabled(account, brand_pool=brand_pool):
        return stats

    storage = getattr(session_state, "storage", None)
    if profile_filter is None:
        try:
            profile_filter = getattr(storage, "profile_filter", None)
        except Exception:
            profile_filter = None

    if _pm_cap_reached(session_state, account):
        logger.info("Skipping follow-back DMs — PM cap already reached.")
        return stats

    settings = _settings(account)
    max_sends = _int_setting(settings, "dm-new-followers-max-per-session", 8)
    max_scrolls = _int_setting(settings, "dm-new-followers-max-scrolls", 12)
    if max_sends <= 0:
        return stats

    logger.info(
        "Checking Notifications for follow-backs to DM "
        "(skip already cold-DMed, max %s)…",
        max_sends,
        extra={"color": f"{Fore.CYAN}"},
    )
    if not _open_notifications(device):
        stats["errors"] += 1
        return stats

    processed: set[str] = set()
    idle_scrolls = 0
    for _scroll in range(max_scrolls + 1):
        if stats["sent"] >= max_sends or _pm_cap_reached(session_state, account):
            break
        if not _ensure_notifications(device):
            stats["errors"] += 1
            break
        rows = _list_follow_rows(device)
        progressed = False
        for row in rows:
            handle = _normalize_handle(row.get("username"))
            if not handle:
                continue
            key = handle.lower()
            if key in processed:
                continue
            processed.add(key)
            stats["seen"] += 1
            progressed = True

            already, reason = _already_dmed(storage, handle)
            if already:
                stats["skipped"] += 1
                logger.info(
                    "@%s: %s. Skip follow-back DM.",
                    handle,
                    reason,
                    extra={"color": f"{Fore.CYAN}"},
                )
                continue
            if not _in_outreach_history(storage, handle):
                stats["skipped"] += 1
                logger.info(
                    "@%s: skip — not in outreach history "
                    "(only DM follow-backs of people we already followed).",
                    handle,
                    extra={"color": f"{Fore.CYAN}"},
                )
                continue
            if _pm_cap_reached(session_state, account):
                break

            if not _open_follow_row(device, row):
                stats["errors"] += 1
                logger.warning("Could not open profile for follow-back @%s.", handle)
                _ensure_notifications(device)
                continue

            result = _dm_opened_profile(
                device,
                session_state,
                account,
                handle,
                profile_filter,
                storage,
            )
            if result == "sent":
                stats["sent"] += 1
            elif result == "skip":
                stats["skipped"] += 1
            else:
                stats["errors"] += 1
            if not _ensure_notifications(device):
                stats["errors"] += 1
                return stats
            if stats["sent"] >= max_sends:
                break

        if stats["sent"] >= max_sends:
            break
        if not progressed:
            idle_scrolls += 1
            if idle_scrolls >= 2:
                break
        else:
            idle_scrolls = 0
        _scroll_notifications(device)

    logger.info(
        "Follow-back DM pass done — sent %s, skipped %s, seen %s.",
        stats["sent"],
        stats["skipped"],
        stats["seen"],
        extra={"color": f"{Fore.GREEN}"},
    )
    try:
        TabBarView(device).navigateToProfile()
        random_sleep(0.6, 1.0, modulable=False, log=False)
    except Exception:
        pass
    return stats
