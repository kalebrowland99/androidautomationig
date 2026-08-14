"""Open Instagram Direct on bot start, reply to unreplied leads, skip low-confidence."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

from colorama import Fore

from GramAddict.core.device_facade import Mode, Timeout
from GramAddict.core.dm_conversation_store import (
    CONF_LOW,
    STATUS_ACTIVE,
    STATUS_GOT_PHONE,
    STATUS_SKIPPED_BUSINESS,
    STATUS_SKIPPED_LOW,
    append_messages,
    get_conversation,
    is_cold_outbound_waiting,
    is_lead_followup_due,
    list_due_followup_leads,
    should_skip_lead,
    upsert_conversation,
)
from GramAddict.core.dm_reply_ai import (
    decide_dm_reply,
    extract_phone,
    summarize_lead_for_sales,
)
from GramAddict.core.resources import ClassName
from GramAddict.core.resources import ResourceID as ResourceIDFactory
from GramAddict.core.utils import random_sleep
from GramAddict.core.views import HomeView, TabBarView, UniversalActions, case_insensitive_re

logger = logging.getLogger(__name__)

APP_ID = "com.instagram.android"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")

# UI chrome / toast / story counters that are NOT people
_JUNK_THREAD_LABELS = {
    "primary",
    "general",
    "requests",
    "from ads",
    "filter",
    "your note",
    "messages",
    "message",
    "instagram",
    "search or ask meta ai",
    "new message",
    "lead",
    "ad inquiry",
    "location off",
    "tap again to exit",
    "home",
    "reels",
    "search",
    "profile",
    "shop",
    "spam",
    "message requests",
    "other",
    "channels",
    "notes",
    "accounts to follow",
    "suggested for you",
    "suggested",
}


def _normalize_handle(value: Optional[str]) -> str:
    return (value or "").strip().replace("\xa0", " ").lstrip("@").strip()


def _is_valid_ig_username(value: Optional[str]) -> bool:
    """True only for a plausible Instagram @ handle (not a display name)."""
    s = _normalize_handle(value)
    if not s:
        return False
    low = s.lower()
    if low in _JUNK_THREAD_LABELS:
        return False
    if " " in s or "|" in s or "/" in s:
        return False
    if re.fullmatch(r"\d+[smhd]", low):
        return False
    return bool(_USERNAME_RE.fullmatch(s))


def _is_junk_inbox_label(value: Optional[str]) -> bool:
    """Skip home-feed / toast / story UI mistaken for inbox rows."""
    s = _normalize_handle(value)
    if not s:
        return True
    low = s.lower()
    if low in _JUNK_THREAD_LABELS:
        return True
    if low.startswith("active ") or low.startswith("seen ") or low.startswith("sent "):
        return True
    if re.fullmatch(r"\d+[smhd]|yesterday|\d+m|\d+h|\d+d", low):
        return True
    if re.fullmatch(r"\d{1,2}:\d{2}", low):
        return True
    # Story / carousel progress like "2/5"
    if re.fullmatch(r"\d+\s*/\s*\d+", s):
        return True
    if "tap again" in low:
        return True
    return False


def _username_from_text_blob(blob: str) -> Optional[str]:
    """Pull an @handle out of content-desc / title text when present."""
    if not blob:
        return None
    # Explicit @handle
    m = re.search(r"@([A-Za-z0-9._]{2,30})", blob)
    if m and _is_valid_ig_username(m.group(1)):
        return m.group(1)
    # "Conversation with username" / "Message username"
    m = re.search(
        r"(?:conversation with|message(?:s)?(?: to)?|chat with)\s+@?([A-Za-z0-9._]{2,30})",
        blob,
        flags=re.I,
    )
    if m and _is_valid_ig_username(m.group(1)):
        return m.group(1)
    cleaned = _normalize_handle(blob)
    if _is_valid_ig_username(cleaned):
        return cleaned
    return None


def _in_open_dm_thread(device) -> bool:
    rid = ResourceIDFactory(APP_ID)
    try:
        if device.find(resourceId=rid.ROW_THREAD_COMPOSER_EDITTEXT).exists(Timeout.TINY):
            return True
    except Exception:
        pass
    try:
        if device.find(resourceId=rid.DIRECT_THREAD_TITLE).exists(Timeout.TINY):
            # Title alone can false-positive; require composer OR send area nearby
            return device.find(
                resourceIdMatches=case_insensitive_re(
                    ".*:id/(row_thread_composer_edittext|row_thread_composer_button_send|"
                    "message_list|thread_recycler)"
                )
            ).exists(Timeout.TINY)
    except Exception:
        pass
    return False


def _is_dm_inbox_list(device) -> bool:
    """Strict: thread list chrome, not an open chat and not home feed."""
    if _in_open_dm_thread(device):
        return False
    home = HomeView(device)
    # Prefer dedicated inbox markers (search row / inbox list / Primary tab)
    checks = [
        device.find(
            resourceIdMatches=case_insensitive_re(
                ".*:id/(row_inbox_search|inbox_refreshable_thread_list|inbox_directory|"
                "direct_inbox|direct_empty_view)"
            )
        ),
        device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            textMatches=case_insensitive_re("^(Primary|General|Messages|Message requests)$"),
        ),
    ]
    for view in checks:
        try:
            if view is not None and view.exists(Timeout.TINY):
                return True
        except Exception:
            continue
    # Fall back to HomeView helper only if it agrees and we aren't on a thread
    try:
        return bool(home.is_inbox_open()) and not _in_open_dm_thread(device)
    except Exception:
        return False


def _ensure_dm_inbox(device) -> bool:
    """Get back to the Direct thread list (not home feed)."""
    if _is_dm_inbox_list(device):
        return True
    # Back out of thread / profile / half-open sheet (do NOT go Home first)
    for _ in range(3):
        if _is_dm_inbox_list(device):
            return True
        if _in_open_dm_thread(device):
            try:
                device.back()
            except Exception:
                pass
            random_sleep(0.5, 0.9, modulable=False, log=False)
            continue
        # Might be on profile opened from thread header
        try:
            device.back()
        except Exception:
            pass
        random_sleep(0.5, 0.9, modulable=False, log=False)
        if _is_dm_inbox_list(device):
            return True
        break

    # Try the paper-plane / inbox control from whatever screen we're on
    try:
        home = HomeView(device)
        if home.navigateToInbox():
            random_sleep(1.0, 1.6, modulable=False, log=False)
            if _is_dm_inbox_list(device):
                return True
    except Exception:
        pass

    # Deep-link straight to inbox (avoids Home "tap again to exit" loops)
    try:
        import shutil
        import subprocess

        serial = getattr(device, "device_id", None) or getattr(device, "serial", None)
        if not serial and hasattr(device, "deviceV2"):
            serial = getattr(device.deviceV2, "serial", None)
        adb = shutil.which("adb") or "adb"
        try:
            from tools.android_devices import resolve_adb

            adb = resolve_adb()
        except Exception:
            pass
        if serial:
            subprocess.run(
                [
                    adb,
                    "-s",
                    str(serial),
                    "shell",
                    "am",
                    "start",
                    "-a",
                    "android.intent.action.VIEW",
                    "-d",
                    "https://www.instagram.com/direct/inbox/",
                    APP_ID,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            random_sleep(2.0, 3.0, modulable=False, log=False)
            if _is_dm_inbox_list(device):
                return True
    except Exception as exc:
        logger.debug("Inbox deep-link (ensure) failed: %s", exc)

    # Last resort: Home → inbox (only if everything else failed)
    return open_dm_inbox(device)


def _notify_sales_lead_telegram(
    my_username: str,
    lead: str,
    *,
    phone: Optional[str],
    messages: list[dict],
    location: Optional[str] = None,
    convo: Optional[dict] = None,
) -> bool:
    """
    Send Instagram @ + phone + sales summary to Telegram once per lead.
    Uses accounts/<mule>/telegram.yml (optional telegram-sales-chat-id).
    """
    convo = convo or get_conversation(my_username, lead) or {}
    if convo.get("sales_notified"):
        return False
    if not phone:
        return False

    try:
        from GramAddict.plugins.telegram import (
            load_telegram_config,
            telegram_alerts_enabled,
            telegram_bot_send_text,
        )
    except Exception as exc:
        logger.warning("Telegram import failed for sales lead: %s", exc)
        return False

    telegram_config = load_telegram_config(my_username)
    if not telegram_config or not telegram_alerts_enabled(telegram_config):
        logger.info("Telegram alerts off — skip sales lead notify for @%s", lead)
        return False

    token = telegram_config.get("telegram-api-token")
    chat_id = (
        telegram_config.get("telegram-sales-chat-id")
        or telegram_config.get("telegram-chat-id")
    )
    if not token or not chat_id:
        logger.warning("Missing Telegram token/chat for sales lead @%s", lead)
        return False

    try:
        summary = summarize_lead_for_sales(
            my_username,
            lead=lead,
            phone=phone,
            messages=messages,
            location=location or convo.get("location"),
        )
    except Exception as exc:
        logger.warning("Sales summary failed for @%s: %s", lead, exc)
        summary = f"Phone captured. Open DM with @{lead.lstrip('@')} for full context."

    handle = lead.lstrip("@")
    mule = my_username.lstrip("@")
    text = (
        f"New wedding lead\n"
        f"Instagram: @{handle}\n"
        f"Phone: {phone}\n"
        f"Mule: @{mule}\n"
        f"\n"
        f"Summary for sales:\n"
        f"{summary}"
    )
    # Telegram hard limit ~4096; keep headroom
    if len(text) > 3900:
        text = text[:3890] + "…"

    response = telegram_bot_send_text(token, chat_id, text, parse_mode=None)
    if response and response.get("ok"):
        upsert_conversation(my_username, lead, {"sales_notified": True})
        logger.info(
            "Telegram sales lead sent for @%s (%s)",
            handle,
            phone,
            extra={"color": f"{Fore.GREEN}"},
        )
        return True

    error = response.get("description") if response else "Unknown error"
    logger.error("Failed to send sales lead Telegram for @%s: %s", handle, error)
    return False


def dm_inbox_replies_enabled(account_key: str) -> bool:
    try:
        from GramAddict.core.follow_vision_account import get_account_follow_vision

        settings = get_account_follow_vision(account_key)
    except FileNotFoundError:
        return False
    return bool(settings.get("dm-inbox-reply-enabled"))


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


def _business_name_keyword(
    profile_filter,
    username: Optional[str],
    display_name: Optional[str] = None,
) -> Optional[str]:
    """Return matched filters.yml blacklist_words_name hit, if any."""
    if profile_filter is None:
        return None
    try:
        return profile_filter.should_skip_dm_by_name(username, display_name)
    except Exception:
        return None


def _mark_skipped_business(
    account: str, lead: str, keyword: str, display_name: Optional[str] = None
) -> None:
    lead_key = (lead or "").strip().lstrip("@")
    if not lead_key or not _is_valid_ig_username(lead_key):
        return
    try:
        upsert_conversation(
            account,
            lead_key,
            {
                **get_conversation(account, lead_key),
                "status": STATUS_SKIPPED_BUSINESS,
                "skip_keyword": keyword,
                "display_name": display_name,
            },
        )
    except Exception:
        pass


def _wait_for_manychat_then_refresh(
    device,
    my_username: str,
    lead: str,
    wait_seconds: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """
    Pause so ManyChat (or similar) can fire, then re-scrape the thread.
    Returns (messages, merged_convo). wait_seconds=0 skips the pause.
    """
    if wait_seconds > 0:
        logger.info(
            "Waiting %ss for ManyChat before replying to @%s…",
            wait_seconds,
            lead,
            extra={"color": f"{Fore.CYAN}"},
        )
        # Slight jitter so every thread isn't identical timing
        lo = max(1.0, float(wait_seconds) - 2.0)
        hi = float(wait_seconds) + 2.0
        random_sleep(lo, hi, modulable=False, log=False)
    raw = _read_thread_messages(device, my_username)
    merged = append_messages(my_username, lead, new_messages=raw)
    messages = list(merged.get("messages") or [])
    return messages, merged


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
        nodes.append({k: (el.attrib.get(k) or "") for k in (
            "text",
            "content-desc",
            "resource-id",
            "class",
            "clickable",
            "bounds",
        )})
    return nodes


def _bounds_center(bounds: str) -> Optional[tuple[int, int]]:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def open_dm_inbox(device) -> bool:
    """Navigate to Direct inbox from Home."""
    try:
        TabBarView(device).navigateToHome()
        random_sleep(0.6, 1.0, modulable=False, log=False)
    except Exception:
        pass
    home = HomeView(device)
    if _is_dm_inbox_list(device):
        return True
    if home.navigateToInbox():
        random_sleep(1.0, 1.8, modulable=False, log=False)
        if _is_dm_inbox_list(device):
            return True
    # Deep-link fallback
    try:
        import shutil
        import subprocess

        serial = getattr(device, "device_id", None) or getattr(device, "serial", None)
        if not serial and hasattr(device, "deviceV2"):
            serial = getattr(device.deviceV2, "serial", None)
        adb = shutil.which("adb") or "adb"
        try:
            from tools.android_devices import resolve_adb

            adb = resolve_adb()
        except Exception:
            pass
        if serial:
            subprocess.run(
                [
                    adb,
                    "-s",
                    str(serial),
                    "shell",
                    "am",
                    "start",
                    "-a",
                    "android.intent.action.VIEW",
                    "-d",
                    "https://www.instagram.com/direct/inbox/",
                    APP_ID,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            random_sleep(2.0, 3.0, modulable=False, log=False)
            return _is_dm_inbox_list(device)
    except Exception as exc:
        logger.debug("Inbox deep-link failed: %s", exc)
    return _is_dm_inbox_list(device)


def _accounts_to_follow_cutoff_y(root, height: int) -> Optional[int]:
    """Y of the 'Accounts to follow' / suggestions header, if visible."""
    cutoff_y = None
    for el in root.iter("node"):
        blob = f"{el.attrib.get('text') or ''} {el.attrib.get('content-desc') or ''}".strip().lower()
        if not blob:
            continue
        if (
            "accounts to follow" in blob
            or blob == "suggested for you"
            or blob.startswith("suggested for you")
        ):
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.attrib.get("bounds") or "")
            if m:
                y1 = int(m.group(2))
                cutoff_y = y1 if cutoff_y is None else min(cutoff_y, y1)
    return cutoff_y


def _row_is_follow_suggestion(row) -> bool:
    """True for Instagram 'Accounts to follow' cards (Follow / Follow back button)."""
    if row is None:
        return False
    for el in row.iter("node"):
        text = (el.attrib.get("text") or "").strip().lower()
        desc = (el.attrib.get("content-desc") or "").strip().lower()
        if text in ("follow back", "follow", "requested") or desc in (
            "follow back",
            "follow",
            "requested",
        ):
            return True
        if "follow back" in text or "follow back" in desc:
            return True
    return False


def _list_inbox_threads(device) -> tuple[list[dict[str, Any]], bool]:
    """Visible inbox DM rows (top→bottom) and whether Accounts-to-follow is on screen.

    Dedupes by row bounds. Anything under 'Accounts to follow' or with a Follow
    button is ignored — that section means end of real DMs.
    """
    if not _is_dm_inbox_list(device):
        logger.warning("Not on DM inbox list — refusing to scrape threads.")
        return [], False

    xml = _dump_xml(device)
    if not xml:
        return [], False
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return [], False

    try:
        height = int(device.get_info().get("displayHeight") or 2000)
    except Exception:
        height = 2000

    parent: dict[Any, Any] = {}
    for p in root.iter("node"):
        for c in list(p):
            parent[c] = p

    cutoff_y = _accounts_to_follow_cutoff_y(root, height)
    suggestions_visible = cutoff_y is not None
    if suggestions_visible:
        logger.info(
            "Inbox end marker 'Accounts to follow' at y=%s — ignoring rows below.",
            cutoff_y,
            extra={"color": f"{Fore.CYAN}"},
        )

    # row_id -> thread dict (one entry per visible row)
    by_row: dict[str, dict[str, Any]] = {}
    skipped_suggestions = 0

    for el in root.iter("node"):
        text = (el.attrib.get("text") or "").strip()
        desc = (el.attrib.get("content-desc") or "").strip()
        label = text or desc
        if not label or len(label) > 60:
            continue
        if _is_junk_inbox_label(label) or _is_junk_inbox_label(text):
            continue
        if " · " in text or text.endswith("·"):
            continue
        low_label = label.lower()
        if low_label in ("accounts to follow", "see all", "suggested for you"):
            continue
        if low_label in ("follow back", "follow", "requested"):
            continue

        # Prefer a real handle from content-desc / @mention when available
        handle = _username_from_text_blob(desc) or _username_from_text_blob(text)
        display = _normalize_handle(text) or _normalize_handle(desc)
        if not handle and _is_junk_inbox_label(display):
            continue
        if not handle and not display:
            continue

        # Climb to clickable row
        cur = el
        row = None
        for _ in range(10):
            p = parent.get(cur)
            if p is None:
                break
            if p.attrib.get("clickable") == "true":
                b = _bounds_center(p.attrib.get("bounds", ""))
                m = re.match(
                    r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", p.attrib.get("bounds") or ""
                )
                if m and b:
                    x1, y1, x2, y2 = map(int, m.groups())
                    if (x2 - x1) > 400 and (y2 - y1) > 80:
                        row = p
                        break
            cur = p
        if row is None:
            continue

        # Never engage Accounts-to-follow / suggested cards
        if _row_is_follow_suggestion(row):
            skipped_suggestions += 1
            suggestions_visible = True
            continue

        row_desc = (row.attrib.get("content-desc") or "").strip()
        handle = handle or _username_from_text_blob(row_desc)

        center = _bounds_center(row.attrib.get("bounds", ""))
        if not center:
            continue
        if cutoff_y is not None and center[1] >= cutoff_y - 8:
            skipped_suggestions += 1
            continue
        row_bounds = re.match(
            r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", row.attrib.get("bounds") or ""
        )
        if not row_bounds:
            continue
        _rx1, ry1, _rx2, ry2 = map(int, row_bounds.groups())
        list_top = 220
        list_bottom = int(height * 0.90)
        if ry1 < list_top or ry2 > list_bottom:
            continue

        # Same physical row → one thread (snap Y so tiny layout jitter doesn't duplicate)
        row_id = f"{ry1 // 10}:{ry2 // 10}"
        unread = "unread" in row_desc.lower() or "unread" in desc.lower()
        existing = by_row.get(row_id)
        if existing is None:
            by_row[row_id] = {
                "username": handle or display,
                "display_name": display,
                "needs_resolve": not bool(handle),
                "unread": unread,
                "x": center[0],
                "y": center[1],
                "y1": ry1,
                "y2": ry2,
                "row_id": row_id,
            }
            continue

        # Merge: upgrade display-only row when we find a real @handle
        if handle and existing.get("needs_resolve"):
            existing["username"] = handle
            existing["needs_resolve"] = False
        if display and not existing.get("display_name"):
            existing["display_name"] = display
        elif (
            display
            and existing.get("display_name")
            and _is_valid_ig_username(existing.get("display_name"))
            and not _is_valid_ig_username(display)
        ):
            existing["display_name"] = display
        if unread:
            existing["unread"] = True

    threads = list(by_row.values())
    # Always top → bottom so we walk the inbox in visual order (no jumping)
    threads.sort(
        key=lambda t: (
            int(t.get("y1") or t.get("y") or 0),
            str(t.get("username") or "").lower(),
        )
    )
    if skipped_suggestions and not threads:
        suggestions_visible = True
    return threads, suggestions_visible


def _thread_aliases(thread: dict[str, Any], *extra: Optional[str]) -> set[str]:
    """All keys that identify this inbox row (avoid re-opening after resolve)."""
    aliases: set[str] = set()
    for raw in (thread.get("username"), thread.get("display_name"), *extra):
        s = str(raw or "").strip().lstrip("@").lower()
        if s:
            aliases.add(s)
    rid = thread.get("row_id")
    if rid:
        aliases.add(str(rid).lower())
        aliases.add(f"row:{rid}")
    return aliases


def _mark_processed(processed: set[str], thread: dict[str, Any], *extra: Optional[str]) -> None:
    processed.update(_thread_aliases(thread, *extra))


def open_thread_via_search(device, query: str) -> Optional[str]:
    """Use inbox search to open a thread by username/name. Returns opened title."""
    q = (query or "").strip().lstrip("@")
    if not q:
        return None
    # Tap search field
    search = device.find(textMatches=case_insensitive_re("^Search"))
    if not search.exists(Timeout.SHORT):
        search = device.find(
            resourceIdMatches=case_insensitive_re(".*:id/(row_inbox_search|ig_text|action_bar_search_edit_text)")
        )
    try:
        if search is not None and search.exists(Timeout.SHORT):
            search.click()
            random_sleep(0.6, 1.0, modulable=False, log=False)
    except Exception:
        pass

    # Type into focused edit text
    edit = device.find(className=ClassName.EDIT_TEXT)
    if not edit.exists(Timeout.MEDIUM):
        logger.warning("Inbox search field not found for @%s", q)
        return None
    edit.set_text(q, Mode.PASTE)
    random_sleep(1.2, 2.0, modulable=False, log=False)

    # Tap a result that matches
    xml = _dump_xml(device)
    nodes = _parse_nodes(xml)
    target = None
    for node in nodes:
        text = (node.get("text") or "").strip().lstrip("@")
        desc = (node.get("content-desc") or "").strip()
        if text.lower() == q.lower() or q.lower() in text.lower() or q.lower() in desc.lower():
            if node.get("clickable") == "true":
                target = node
                break
            # use bounds of text node
            target = node
            break
    if target is None:
        logger.info("No inbox search result for %s", q)
        device.back()
        random_sleep(0.4, 0.7, modulable=False, log=False)
        return None

    center = _bounds_center(target.get("bounds", ""))
    if not center:
        device.back()
        return None
    try:
        device.deviceV2.click(center[0], center[1])
        random_sleep(1.2, 2.0, modulable=False, log=False)
    except Exception as exc:
        logger.debug("search result tap failed: %s", exc)
        return None
    return _resolve_thread_username(device, fallback=q) or q


def _open_thread(device, thread: dict[str, Any]) -> bool:
    try:
        device.deviceV2.click(thread["x"], thread["y"])
        random_sleep(1.0, 1.8, modulable=False, log=False)
        return True
    except Exception as exc:
        logger.debug("Open thread tap failed: %s", exc)
        return False


def _thread_title_raw(device) -> Optional[str]:
    rid = ResourceIDFactory(APP_ID)
    title = device.find(resourceId=rid.DIRECT_THREAD_TITLE)
    try:
        if title.exists(Timeout.SHORT):
            text = _normalize_handle(title.get_text() or "")
            if text:
                return text
    except Exception:
        pass
    xml = _dump_xml(device)
    for node in _parse_nodes(xml):
        rid_s = node.get("resource-id") or ""
        if "header_title" in rid_s or "thread_title" in rid_s or "action_bar_title" in rid_s:
            text = _normalize_handle(
                node.get("text") or node.get("content-desc") or ""
            )
            if text:
                return text
            handle = _username_from_text_blob(
                node.get("content-desc") or node.get("text") or ""
            )
            if handle:
                return handle
    return None


def _handle_from_thread_screen(
    device, *, my_username: Optional[str] = None
) -> Optional[str]:
    """
    Prefer an @handle / bare username on the open-thread profile card
    (under the display name, above "View profile").
    """
    xml = _dump_xml(device)
    if not xml:
        return None
    try:
        height = int(device.get_info().get("displayHeight") or 2000)
    except Exception:
        height = 2000

    # Find "View profile" button Y — username sits just above it on the card
    view_profile_y: Optional[int] = None
    for node in _parse_nodes(xml):
        blob = f"{node.get('text') or ''} {node.get('content-desc') or ''}".strip().lower()
        if blob == "view profile" or blob.startswith("view profile"):
            bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
            if bm:
                view_profile_y = int(bm.group(2))
                break

    candidates: list[tuple[int, str]] = []
    for node in _parse_nodes(xml):
        bounds = node.get("bounds") or ""
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not bm:
            continue
        y1 = int(bm.group(2))
        # Status bar / "spam filter" notification lives up here — never use it
        if y1 < 80:
            continue
        if y1 > height * 0.72:
            continue
        if view_profile_y is not None and y1 >= view_profile_y:
            continue

        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        rid_s = (node.get("resource-id") or "").lower()
        blob = f"{text} {desc}"

        # Explicit @handle
        for m in re.finditer(r"@([A-Za-z0-9._]{2,30})", blob):
            handle = _normalize_handle(m.group(1))
            if not _is_forbidden_lead_username(handle, my_username):
                # Prefer closest above View profile
                score = abs((view_profile_y or height) - y1)
                candidates.append((score, handle))

        # Bare username under display name (IG often shows "lenakilburn" without @)
        bare = _normalize_handle(text)
        if bare and _is_valid_ig_username(bare) and not _is_forbidden_lead_username(
            bare, my_username
        ):
            # Skip if it looks like a display-name first token only when spaces were present
            if " " in (text or ""):
                continue
            # Prefer username-ish rids, or any bare handle sitting above View profile
            if (
                "username" in rid_s
                or "subtitle" in rid_s
                or view_profile_y is not None
            ):
                score = abs((view_profile_y or height) - y1)
                candidates.append((score, bare))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _tap_view_profile(device) -> bool:
    """Tap the thread profile-card 'View profile' button."""
    candidates = [
        device.find(resourceId=f"{APP_ID}:id/view_profile_button"),
        device.find(textMatches=case_insensitive_re("^View profile$")),
        device.find(descriptionMatches=case_insensitive_re("^View profile$")),
        device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            textMatches=case_insensitive_re("^View profile$"),
        ),
    ]
    for btn in candidates:
        try:
            if btn is not None and btn.exists(Timeout.SHORT):
                btn.click()
                random_sleep(1.2, 2.0, modulable=False, log=False)
                return True
        except Exception:
            continue
    # Bounds fallback from hierarchy
    xml = _dump_xml(device)
    for node in _parse_nodes(xml):
        blob = f"{node.get('text') or ''} {node.get('content-desc') or ''}".strip().lower()
        if blob != "view profile":
            continue
        center = _bounds_center(node.get("bounds") or "")
        if not center:
            continue
        try:
            device.deviceV2.click(center[0], center[1])
            random_sleep(1.2, 2.0, modulable=False, log=False)
            return True
        except Exception:
            return False
    return False


def _username_from_open_profile(
    device, *, my_username: Optional[str] = None
) -> Optional[str]:
    """Read @ from a profile screen without noisy ProfileView action-bar errors."""
    rid = ResourceIDFactory(APP_ID)

    # Quiet resource lookups first (avoid ProfileView ERROR spam)
    for res in (
        rid.USERNAME_TEXTVIEW,
        getattr(rid, "ACTION_BAR_TITLE", f"{APP_ID}:id/action_bar_title"),
        getattr(rid, "TITLE_VIEW", f"{APP_ID}:id/title_view"),
    ):
        try:
            uv = device.find(resourceId=res)
            if uv.exists(Timeout.TINY):
                uname = _normalize_handle(uv.get_text() or "")
                if uname and not _is_forbidden_lead_username(uname, my_username):
                    return uname
        except Exception:
            continue

    try:
        uv = device.find(
            resourceIdMatches=case_insensitive_re(
                ".*:id/(username_textview|action_bar_title|title_view|"
                "action_bar_large_title|profile_header_full_name_and_badge)"
            )
        )
        if uv.exists(Timeout.SHORT):
            # May be display name — also scan siblings via hierarchy below
            uname = _normalize_handle(uv.get_text() or "")
            if (
                uname
                and " " not in uname
                and not _is_forbidden_lead_username(uname, my_username)
            ):
                return uname
    except Exception:
        pass

    xml = _dump_xml(device)
    try:
        height = int(device.get_info().get("displayHeight") or 2000)
    except Exception:
        height = 2000

    candidates: list[tuple[int, str]] = []
    for node in _parse_nodes(xml):
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
        if not bm:
            continue
        y1 = int(bm.group(2))
        if y1 < 60 or y1 > height * 0.45:
            continue
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        rid_s = (node.get("resource-id") or "").lower()
        blob = f"{text} {desc}"
        for m in re.finditer(r"@([A-Za-z0-9._]{2,30})", blob):
            handle = _normalize_handle(m.group(1))
            if not _is_forbidden_lead_username(handle, my_username):
                candidates.append((y1, handle))
        bare = _normalize_handle(text)
        if (
            bare
            and " " not in bare
            and _is_valid_ig_username(bare)
            and not _is_forbidden_lead_username(bare, my_username)
        ):
            # Prefer title / username rids, else top-of-profile bare handles
            weight = y1
            if "username" in rid_s or "action_bar_title" in rid_s or "title_view" in rid_s:
                weight = y1 - 1000
            candidates.append((weight, bare))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _resolve_thread_username(
    device,
    fallback: Optional[str] = None,
    *,
    my_username: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve the real Instagram @ for the open DM thread.

    Preferred flow (reliable on current IG):
      tap "View profile" → read @ on profile → back to thread.
    """
    def _ok(handle: Optional[str]) -> Optional[str]:
        h = _normalize_handle(handle)
        if not h or _is_forbidden_lead_username(h, my_username):
            return None
        return h

    if not _in_open_dm_thread(device):
        return _ok(fallback) if fallback and _is_valid_ig_username(fallback) else None

    fb = _ok(fallback) if fallback and _is_valid_ig_username(_normalize_handle(fallback)) else None

    # Only reliable path: View profile → read @ → back
    if _tap_view_profile(device):
        handle = _ok(_username_from_open_profile(device, my_username=my_username))
        try:
            device.back()
            random_sleep(0.8, 1.3, modulable=False, log=False)
        except Exception:
            pass
        if not _in_open_dm_thread(device):
            try:
                device.back()
                random_sleep(0.6, 1.0, modulable=False, log=False)
            except Exception:
                pass
        if handle:
            logger.info("Resolved DM lead @%s via View profile.", handle)
            return handle
        logger.warning("View profile opened but @ was not readable.")
    else:
        logger.warning("View profile button not found in DM thread.")

    return fb


def _is_dm_chrome_text(text: str) -> bool:
    """UI labels / cards that are not real chat bubbles."""
    low = (text or "").lower().replace("\xa0", " ").strip()
    if not low:
        return True
    if low in _JUNK_THREAD_LABELS or low in {
        "message…",
        "message...",
        "tap and hold to react",
        "today",
        "yesterday",
        "camera",
        "gallery",
        "stickers",
        "more",
        "view profile",
        "mute",
        "obsessed with...",
        "you're now friends. say hi!",
        "you follow each other on instagram",
        "say hi!",
        "active now",
        "send message",
        "report",
        "block",
        "delete",
    }:
        return True
    if "search or ask meta" in low:
        return True
    if "you both follow" in low or "you follow each other" in low:
        return True
    if "you're now friends" in low:
        return True
    if "disappearing messages" in low or low.startswith("swipe up"):
        return True
    if "view likes" in low or "like number" in low:
        return True
    if re.match(r"^active\b", low) or re.match(r"^seen\b", low):
        return True
    if re.search(r"\b\d[\d,.]*\s*followers?\b", low):
        return True
    if re.search(r"\b\d[\d,.]*\s*posts?\b", low):
        return True
    if "followers" in low and "posts" in low:
        return True
    if re.fullmatch(r"@[A-Za-z0-9._]{2,30}", (text or "").strip()):
        return True
    if re.fullmatch(r"(mon|tue|wed|thu|fri|sat|sun)\s+\d{1,2}:\d{2}", low):
        return True
    return False


def _filter_real_messages(messages: Optional[list[dict[str, Any]]]) -> list[dict[str, str]]:
    """Drop UI chrome that was mistakenly saved as chat bubbles."""
    out: list[dict[str, str]] = []
    for msg in messages or []:
        text = (msg.get("text") or "").strip()
        role = msg.get("role")
        if role not in ("them", "us") or not text or _is_dm_chrome_text(text):
            continue
        item = {"role": role, "text": text}
        if msg.get("at"):
            item["at"] = msg.get("at")
        out.append(item)
    return out


def _thread_shows_friends_say_hi(device) -> bool:
    """True for empty mutual threads: 'You're now friends. Say hi!'"""
    xml = _dump_xml(device)
    if not xml:
        return False
    blob = " ".join(
        f"{n.get('text') or ''} {n.get('content-desc') or ''}"
        for n in _parse_nodes(xml)
    ).lower()
    if "you're now friends" in blob:
        return True
    if "you follow each other" in blob and "say hi" in blob:
        return True
    return False


def _compose_cold_dm_text(my_username: str) -> Optional[str]:
    try:
        from GramAddict.core.interaction import load_random_message

        return load_random_message(my_username)
    except Exception as exc:
        logger.warning("Cold DM compose failed: %s", exc)
        return None


def _send_empty_friends_cold_dm(
    device,
    session_state,
    my_username: str,
    lead: str,
    convo: dict[str, Any],
) -> str:
    """Mutual follow-back with empty thread → send the same cold DM as outreach."""
    # Daily / smart PM cap — don't keep hitting Instagram's request-limit screen.
    try:
        if session_state is not None and session_state.check_limit(
            limit_type=session_state.Limit.PM, output=False
        ):
            logger.info(
                "PM limit reached — skipping empty-friends cold DM to @%s.",
                lead,
                extra={"color": f"{Fore.CYAN}"},
            )
            _leave_thread(device)
            return "pm_limited"
        from GramAddict.core.dm_limit_history import active_smart_pm_cap

        cap = active_smart_pm_cap(my_username)
        if cap is not None and int(getattr(session_state, "totalPm", 0) or 0) >= cap:
            logger.info(
                "Smart PM cap (%s) hit — skipping empty-friends cold DM to @%s.",
                cap,
                lead,
                extra={"color": f"{Fore.CYAN}"},
            )
            _leave_thread(device)
            return "pm_limited"
    except Exception:
        pass

    stored_real = _filter_real_messages(convo.get("messages"))
    if stored_real and stored_real[-1].get("role") == "us":
        logger.info(
            "Friends/say-hi thread for @%s but we already sent last — skip.",
            lead,
            extra={"color": f"{Fore.CYAN}"},
        )
        _leave_thread(device)
        return "no_reply_needed"

    try:
        storage = getattr(session_state, "storage", None)
        if storage is not None:
            from GramAddict.core.interaction import _already_pm_sent

            if _already_pm_sent(storage, lead):
                logger.info(
                    "Friends/say-hi @%s already DMed earlier — skip.",
                    lead,
                    extra={"color": f"{Fore.CYAN}"},
                )
                _leave_thread(device)
                return "no_reply_needed"
    except Exception:
        pass

    text = _compose_cold_dm_text(my_username)
    if not text:
        logger.warning("No cold DM text available for empty friends @%s.", lead)
        _leave_thread(device)
        return "send_failed"

    logger.info(
        "Empty friends thread @%s — sending cold DM.",
        lead,
        extra={"color": f"{Fore.GREEN}"},
    )
    if not _send_thread_reply(
        device, text, session_state=session_state, my_username=my_username
    ):
        _leave_thread(device)
        # If Instagram just hit message-request / community limit, stop further DMs.
        try:
            if session_state is not None and session_state.check_limit(
                limit_type=session_state.Limit.PM, output=False
            ):
                return "pm_limited"
        except Exception:
            pass
        return "send_failed"

    append_messages(
        my_username,
        lead,
        new_messages=[{"role": "us", "text": text}],
        status=STATUS_ACTIVE,
    )
    if session_state is not None:
        try:
            session_state.register_pm()
        except Exception:
            session_state.totalPm = int(getattr(session_state, "totalPm", 0) or 0) + 1
        try:
            storage = getattr(session_state, "storage", None)
            if storage is not None and hasattr(storage, "add_interacted_user"):
                storage.add_interacted_user(
                    lead,
                    session_id=getattr(session_state, "session_id", None)
                    or getattr(session_state, "id", None),
                    pm_sent=True,
                    pm_sent_by=my_username,
                )
        except Exception:
            pass
    _leave_thread(device)
    return "cold_dm_sent"


def _is_forbidden_lead_username(value: Optional[str], my_username: Optional[str] = None) -> bool:
    s = _normalize_handle(value).lower()
    if not s or _is_junk_inbox_label(s) or not _is_valid_ig_username(s):
        return True
    mine = _normalize_handle(my_username).lower()
    if mine and s == mine:
        return True
    return False


def _read_thread_messages(device, my_username: str) -> list[dict[str, str]]:
    """Extract visible bubbles as them/us via hierarchy + left/right position."""
    xml = _dump_xml(device)
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    try:
        width = int(device.get_info().get("displayWidth") or 1080)
        height = int(device.get_info().get("displayHeight") or 2000)
    except Exception:
        width = 1080
        height = 2000

    my = (my_username or "").lstrip("@").lower()
    # Use title text only — never call full resolve here (it can navigate away).
    header = (_normalize_handle(_thread_title_raw(device) or "")).lower()

    def _is_bubble_rid(rid: str) -> bool:
        r = (rid or "").lower()
        return any(
            x in r
            for x in (
                "direct_text_message",
                "message_text",
                "row_message",
                "message_content",
                "direct_message",
            )
        )

    def _role_for_bounds(x1: int, x2: int, desc: str) -> Optional[str]:
        """Classify bubble side. Full-width system rows return None (skip)."""
        d = (desc or "").lower()
        if re.search(r"\byou sent\b", d):
            return "us"
        span = x2 - x1
        # System banners / cards span almost the full width
        if span >= width * 0.85 and x1 <= width * 0.08:
            return None
        # Our bubbles hug the right edge (dump: hey Lena bubble x1=402 x2=1080)
        if x2 >= int(width * 0.90) or x1 >= int(width * 0.33):
            return "us"
        # Their bubbles start on the left
        if x1 <= int(width * 0.12) or x2 <= int(width * 0.70):
            return "them"
        mid = (x1 + x2) / 2.0
        if mid > width * 0.55:
            return "us"
        return "them"

    messages: list[dict[str, Any]] = []
    for el in root.iter("node"):
        text = (el.attrib.get("text") or "").strip()
        if not text or len(text) > 500:
            continue
        low = text.lower().replace("\xa0", " ").strip()
        if _is_dm_chrome_text(text):
            continue
        if header and low.rstrip() == header:
            continue
        if my and low.lstrip("@") == my:
            continue
        # Timestamps / dates / clock
        if re.fullmatch(
            r"(\d{1,2}:\d{2}(\s*[ap]m)?|"
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}.*|"
            r"\d+[smhd])",
            low,
        ):
            continue
        rid = el.attrib.get("resource-id") or ""
        rid_l = rid.lower()
        if any(
            x in rid_l
            for x in (
                "composer",
                "action_bar",
                "tab_bar",
                "clock",
                "header",
                "avatar",
                "title_text",
                "legibility",
                "profile",
                "follow_list",
                "button",
                "message_action_log",
                "seen_state",
                "network_attribution",
                "view_profile",
                "thread_context",
            )
        ):
            continue
        if rid.endswith("title_text") or "xma" in rid_l:
            continue
        bounds = el.attrib.get("bounds") or ""
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if y2 < 200:
            continue
        if y1 > height * 0.88:
            continue
        bubble = _is_bubble_rid(rid)
        if not bubble and y2 < height * 0.55:
            if x1 > width * 0.08 and x2 < width * 0.92 and (x2 - x1) > width * 0.55:
                continue
        if (x2 - x1) < 40 and len(text) <= 2:
            continue
        desc = el.attrib.get("content-desc") or ""
        role = _role_for_bounds(x1, x2, desc)
        if role is None:
            continue
        messages.append({"role": role, "text": text, "y": y1})

    # Sort top→bottom so "last" is visually last bubble (not hierarchy quirks)
    messages.sort(key=lambda m: int(m.get("y") or 0))
    return [{"role": m["role"], "text": m["text"]} for m in messages]


def _needs_reply(messages: list[dict[str, str]], convo: dict[str, Any]) -> bool:
    if should_skip_lead(convo):
        return False
    real = _filter_real_messages(messages)
    if not real:
        return False
    # If last message is from them → we should reply (unless low confidence).
    return real[-1].get("role") == "them"


def _send_thread_reply(
    device,
    text: str,
    *,
    session_state=None,
    my_username: Optional[str] = None,
) -> bool:
    rid = ResourceIDFactory(APP_ID)
    ua = UniversalActions(device)
    normalize = lambda value: (value or "").strip()
    try:
        from GramAddict.core.follow_vision_account import _normalize_cold_dm_text

        normalize = _normalize_cold_dm_text
    except Exception:
        pass
    text = normalize(text)
    if not text:
        logger.warning("Refusing to send empty DM reply.")
        return False

    # Account-level limits replace the composer entirely.
    try:
        from GramAddict.core.interaction import _handle_pm_restriction, _pm_block_kind

        kind = _pm_block_kind(device)
        if kind in ("hard", "request_limit"):
            logger.warning("Cannot reply in thread — messaging blocked (%s).", kind)
            if session_state is not None and my_username:
                _handle_pm_restriction(
                    device,
                    ua,
                    session_state,
                    my_username,
                    kind=kind,
                )
            return False
    except Exception:
        pass

    box = device.find(
        resourceId=rid.ROW_THREAD_COMPOSER_EDITTEXT,
        className=ClassName.EDIT_TEXT,
    )
    if not box.exists(Timeout.MEDIUM):
        box = device.find(resourceId=rid.ROW_THREAD_COMPOSER_EDITTEXT)
    if not box.exists(Timeout.SHORT):
        # No textbox — often the daily message-request limit screen.
        try:
            from GramAddict.core.interaction import _handle_pm_restriction, _pm_block_kind

            kind = _pm_block_kind(device)
            if kind in ("hard", "request_limit", "soft"):
                logger.warning(
                    "DM composer missing — Instagram restriction (%s).", kind
                )
                if session_state is not None and my_username:
                    _handle_pm_restriction(
                        device,
                        ua,
                        session_state,
                        my_username,
                        kind=kind,
                    )
                return False
        except Exception:
            pass
        logger.warning("DM composer not found in thread.")
        return False

    try:
        box.click()
        random_sleep(0.3, 0.5, modulable=False, log=False)
    except Exception:
        pass

    # Always clear first — Instagram's composer often APPENDs on set_text/paste,
    # which produced 2–3x duplicated cold DMs in one bubble.
    try:
        device.deviceV2.clear_text()
    except Exception:
        try:
            box.set_text("", Mode.PASTE)
        except Exception:
            pass
    random_sleep(0.2, 0.4, modulable=False, log=False)

    # Prefer human-like typing (clears again inside TYPE mode).
    try:
        from GramAddict.core import filter as filter_mod

        dont_type = bool(getattr(getattr(filter_mod, "args", None), "dont_type", False))
    except Exception:
        dont_type = False
    mode = Mode.PASTE if dont_type else Mode.TYPE
    logger.info(
        "Write DM reply: %s",
        text.replace("\n", "\\n"),
        extra={"color": f"{Fore.CYAN}"},
    )
    box.set_text(text, mode)
    random_sleep(0.4, 0.8, modulable=False, log=False)

    # If composer still shows an exact 2x/3x duplicate, clear and set once more.
    try:
        typed = (box.get_text() or "").strip()
        fixed = normalize(typed) if typed else text
        if typed and fixed != typed and fixed == text:
            logger.warning(
                "Composer had duplicated text — clearing and rewriting once."
            )
            try:
                device.deviceV2.clear_text()
            except Exception:
                pass
            box.set_text(text, Mode.PASTE)
            random_sleep(0.3, 0.5, modulable=False, log=False)
    except Exception:
        pass

    send = device.find(resourceIdMatches=rid.ROW_THREAD_COMPOSER_BUTTON_SEND)
    if not send.exists(Timeout.SHORT):
        # Sometimes send appears as content-desc Send after typing
        send = device.find(descriptionMatches=case_insensitive_re("^Send$"))
    if not send.exists(Timeout.SHORT):
        logger.warning("DM send button not found.")
        ua.close_keyboard(device)
        return False
    send.click()
    random_sleep(1.0, 1.6, modulable=False, log=False)
    try:
        ua.detect_block(device)
        ua.close_keyboard(device)
    except Exception:
        pass
    return True


def _leave_thread(device) -> None:
    try:
        device.back()
    except Exception:
        pass
    random_sleep(0.5, 0.9, modulable=False, log=False)
    if not _ensure_dm_inbox(device):
        logger.warning("Could not return to DM inbox after leaving thread.")


def _scroll_inbox(device) -> None:
    """Scroll the inbox list downward to reveal older / lower threads.

    Direction.DOWN = finger swipe up = content moves up = see rows below.
    (We previously used Direction.UP, which scrolled the wrong way.)
    """
    try:
        from GramAddict.core.views import Direction

        UniversalActions(device)._swipe_points(direction=Direction.DOWN, delta_y=700)
    except Exception:
        try:
            h = int(device.get_info().get("displayHeight") or 1600)
            w = int(device.get_info().get("displayWidth") or 900)
            # Finger up the screen → reveal more DMs below
            device.deviceV2.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.32), 0.25)
        except Exception:
            pass
    random_sleep(0.6, 1.0, modulable=False, log=False)


def _send_thread_ai_reply(
    device,
    session_state,
    my_username: str,
    lead: str,
    messages: list[dict[str, str]],
    merged: dict[str, Any],
    *,
    followup: bool = False,
) -> str:
    """AI decide → send (or leave on read). Used for inbound replies and day-later bumps."""
    try:
        decision = decide_dm_reply(
            my_username,
            lead=lead,
            messages=messages,
            location=merged.get("location"),
            phone=merged.get("phone"),
            confidence=str(merged.get("wedding_confidence") or "unknown"),
            followup=followup,
        )
    except Exception as exc:
        logger.warning("AI DM decision failed for @%s: %s", lead, exc)
        _leave_thread(device)
        return "ai_error"

    conf = decision.get("wedding_confidence") or "unknown"
    action = decision.get("action") or "reply"
    loc = decision.get("location") or merged.get("location")
    phone = decision.get("phone") or merged.get("phone")
    reply = (decision.get("reply") or "").strip()

    fields: dict[str, Any] = {
        "wedding_confidence": conf,
        "location": loc,
        "phone": phone,
    }

    if followup and conf == CONF_LOW:
        fields["status"] = STATUS_SKIPPED_LOW
        append_messages(my_username, lead, new_messages=[], **fields)
        logger.info(
            "Follow-up cancelled for @%s — not a lead (confidence=low).",
            lead,
            extra={"color": f"{Fore.CYAN}"},
        )
        _leave_thread(device)
        return "read_only"

    if conf == CONF_LOW or action == "read_only":
        fields["status"] = STATUS_SKIPPED_LOW if conf == CONF_LOW else STATUS_ACTIVE
        fields["wedding_confidence"] = conf
        append_messages(my_username, lead, new_messages=[], **fields)
        logger.info(
            "Leaving @%s on read (confidence=%s).",
            lead,
            conf,
            extra={"color": f"{Fore.CYAN}"},
        )
        _leave_thread(device)
        return "read_only"

    if action == "done" or phone:
        fields["status"] = STATUS_GOT_PHONE
        fields["phone"] = phone
        if reply:
            ok = _send_thread_reply(
                device,
                reply,
                session_state=session_state,
                my_username=my_username,
            )
            if ok:
                if followup:
                    fields["followups_sent"] = int(merged.get("followups_sent") or 0) + 1
                append_messages(
                    my_username,
                    lead,
                    new_messages=[{"role": "us", "text": reply}],
                    **fields,
                )
                if session_state is not None:
                    try:
                        session_state.register_pm()
                    except Exception:
                        session_state.totalPm = (
                            int(getattr(session_state, "totalPm", 0) or 0) + 1
                        )
            else:
                append_messages(my_username, lead, new_messages=[], **fields)
        else:
            append_messages(my_username, lead, new_messages=[], **fields)
        sales_msgs = list(messages)
        if reply:
            sales_msgs = sales_msgs + [{"role": "us", "text": reply}]
        _notify_sales_lead_telegram(
            my_username,
            lead,
            phone=phone,
            messages=sales_msgs,
            location=loc,
            convo={**merged, **fields},
        )
        _leave_thread(device)
        return "done"

    if not reply:
        if followup:
            fields["followups_sent"] = int(merged.get("followups_sent") or 0) + 1
        append_messages(my_username, lead, new_messages=[], **fields)
        _leave_thread(device)
        return "empty_reply"

    ok = _send_thread_reply(
        device,
        reply,
        session_state=session_state,
        my_username=my_username,
    )
    if ok:
        fields["status"] = STATUS_ACTIVE
        if followup:
            fields["followups_sent"] = int(merged.get("followups_sent") or 0) + 1
        append_messages(
            my_username,
            lead,
            new_messages=[{"role": "us", "text": reply}],
            **fields,
        )
        if session_state is not None:
            try:
                session_state.register_pm()
            except Exception:
                session_state.totalPm = int(getattr(session_state, "totalPm", 0) or 0) + 1
        logger.info(
            "%s @%s: %s",
            "Followed up with" if followup else "Replied to",
            lead,
            reply[:80],
            extra={"color": f"{Fore.GREEN}"},
        )
        _leave_thread(device)
        return "followup" if followup else "replied"

    logger.warning("Failed to send reply to @%s.", lead)
    _leave_thread(device)
    return "send_failed"


def process_open_thread(
    device,
    session_state,
    my_username: str,
    lead: str,
) -> str:
    """Read → AI decide → reply or leave on read. Returns action taken."""
    if _is_forbidden_lead_username(lead, my_username):
        logger.warning("Refusing DM lead %r (self/junk) — back to inbox.", lead)
        _leave_thread(device)
        return "invalid_lead"

    if not _in_open_dm_thread(device):
        logger.warning(
            "Not inside a real DM thread (inbox/Spam/notes chrome) — skipping %r.",
            lead,
        )
        _ensure_dm_inbox(device)
        return "not_in_thread"

    convo = get_conversation(my_username, lead)
    if should_skip_lead(convo):
        logger.info(
            "Skipping @%s (status=%s confidence=%s).",
            lead,
            convo.get("status"),
            convo.get("wedding_confidence"),
        )
        _leave_thread(device)
        return "skipped_cached"

    raw_msgs = _filter_real_messages(_read_thread_messages(device, my_username))
    # Prefer stored history merged with fresh scrape
    merged = append_messages(my_username, lead, new_messages=raw_msgs)
    messages = _filter_real_messages(merged.get("messages") or [])

    # Phone already handled for this lead?
    existing_phone = None
    for msg in messages:
        if msg.get("role") == "them":
            found = extract_phone(msg.get("text") or "")
            if found:
                existing_phone = found
                break
    if existing_phone and (
        merged.get("status") == STATUS_GOT_PHONE or merged.get("sales_notified")
    ):
        merged["phone"] = existing_phone
        merged["status"] = STATUS_GOT_PHONE
        upsert_conversation(my_username, lead, merged)
        _notify_sales_lead_telegram(
            my_username,
            lead,
            phone=existing_phone,
            messages=messages,
            location=merged.get("location"),
            convo=merged,
        )
        logger.info(
            "Already have phone for @%s — leaving thread.",
            lead,
            extra={"color": f"{Fore.GREEN}"},
        )
        _leave_thread(device)
        return "got_phone"

    # Mutual follow-back: empty thread with "You're now friends. Say hi!"
    if not raw_msgs and _thread_shows_friends_say_hi(device):
        return _send_empty_friends_cold_dm(
            device, session_state, my_username, lead, merged
        )

    if not _needs_reply(raw_msgs or messages, merged):
        hours = _int_setting(
            _settings(my_username), "dm-inbox-followup-after-hours", 24
        )
        if hours > 0 and is_lead_followup_due(merged, after_hours=hours):
            logger.info(
                "Lead @%s went quiet — sending day-later follow-up.",
                lead,
                extra={"color": f"{Fore.GREEN}"},
            )
            return _send_thread_ai_reply(
                device,
                session_state,
                my_username,
                lead,
                messages,
                merged,
                followup=True,
            )
        logger.info("No unreplied inbound for @%s — back.", lead)
        _leave_thread(device)
        return "no_reply_needed"
    # Let ManyChat / other IG automations send first so we don't double-reply.
    wait_s = _int_setting(_settings(my_username), "dm-inbox-reply-manychat-wait-seconds", 10)
    if wait_s > 0:
        messages, merged = _wait_for_manychat_then_refresh(
            device, my_username, lead, wait_s
        )
        if not _needs_reply(messages, merged):
            logger.info(
                "ManyChat (or prior outbound) already covered @%s — skipping our reply.",
                lead,
                extra={"color": f"{Fore.CYAN}"},
            )
            _leave_thread(device)
            return "manychat_covered"

    return _send_thread_ai_reply(
        device,
        session_state,
        my_username,
        lead,
        messages,
        merged,
        followup=False,
    )


def run_dm_inbox_replies(
    device,
    session_state,
    my_username: str,
    *,
    prioritize: Optional[list[str]] = None,
    profile_filter=None,
) -> dict[str, int]:
    """Session-start hook: check Direct and reply before other jobs."""
    stats = {
        "opened": 0,  # threads that needed work (reply / AI / read-only)
        "peeks": 0,  # includes cold outbound peeks that we skip past
        "replied": 0,
        "read_only": 0,
        "skipped": 0,
        "errors": 0,
    }
    account = (my_username or "").lstrip("@")
    if not account or not dm_inbox_replies_enabled(account):
        return stats

    if profile_filter is None:
        try:
            storage = getattr(session_state, "storage", None)
            profile_filter = getattr(storage, "profile_filter", None) if storage else None
        except Exception:
            profile_filter = None
    # Do not construct Filter(None) — needs storage.filter_path
    # Respect Community Standards smart PM cap.
    try:
        from GramAddict.core.dm_limit_history import active_smart_pm_cap

        cap = active_smart_pm_cap(account)
        if cap is not None and int(getattr(session_state, "totalPm", 0) or 0) >= cap:
            logger.info(
                "Skipping DM inbox replies — smart PM cap reached (%s).",
                cap,
            )
            return stats
    except Exception:
        pass

    settings = _settings(account)
    max_threads = _int_setting(settings, "dm-inbox-reply-max-threads", 25)
    max_replies = _int_setting(settings, "dm-inbox-reply-max-replies", 20)
    max_peeks = _int_setting(settings, "dm-inbox-reply-max-peeks", 80)
    max_scrolls = _int_setting(settings, "dm-inbox-reply-max-scrolls", 15)
    # Peeks must be at least as large as thread budget
    max_peeks = max(max_peeks, max_threads)

    logger.info(
        "Checking Instagram DMs before jobs "
        "(max work threads=%s, max replies=%s, max peeks=%s, max scrolls=%s)…",
        max_threads,
        max_replies,
        max_peeks,
        max_scrolls,
        extra={"color": f"{Fore.CYAN}"},
    )
    try:
        from GramAddict.core.live_progress import write_live_progress

        write_live_progress(
            account, session_state, running=True, current_job="dm-inbox-reply"
        )
    except Exception:
        pass

    if not open_dm_inbox(device):
        logger.warning("Could not open Direct inbox — continuing with jobs.")
        return stats

    processed: set[str] = set()

    def _budget_hit() -> bool:
        return (
            stats["opened"] >= max_threads
            or stats["replied"] >= max_replies
            or stats["peeks"] >= max_peeks
        )

    def _record_result(result: str) -> None:
        # Cold outbound / already-handled peeks should NOT burn the work-thread budget
        # (those sit on top of the inbox after mass cold DMs).
        cold_peek = result in (
            "no_reply_needed",
            "skipped_cached",
            "got_phone",
            "manychat_covered",
            "invalid_lead",
            "not_in_thread",
            "pm_limited",
        )
        if result == "pm_limited":
            stats["skipped"] += 1
        elif cold_peek:
            stats["skipped"] += 1
        elif result in ("replied", "cold_dm_sent", "followup"):
            stats["opened"] += 1
            stats["replied"] += 1
        elif result == "read_only":
            stats["opened"] += 1
            stats["read_only"] += 1
        elif result == "done":
            stats["opened"] += 1
            stats["skipped"] += 1
        else:
            stats["opened"] += 1
            stats["errors"] += 1

    # Quiet wedding leads due a day-later bump, then optional priority searches.
    followup_hours = _int_setting(settings, "dm-inbox-followup-after-hours", 24)
    followup_cap = _int_setting(settings, "dm-inbox-followup-max-per-session", 8)
    due_followups: list[str] = []
    if followup_hours > 0:
        due_followups = list_due_followup_leads(account, after_hours=followup_hours)
        if followup_cap > 0:
            due_followups = due_followups[:followup_cap]
        if due_followups:
            logger.info(
                "Day-later follow-ups due: %s",
                ", ".join(f"@{u}" for u in due_followups),
                extra={"color": f"{Fore.CYAN}"},
            )

    for raw in list(due_followups) + list(prioritize or []):
        if _budget_hit():
            break
        q = (raw or "").strip().lstrip("@")
        if not q or q.lower() in processed:
            continue
        logger.info("Priority inbox search: @%s", q)
        opened_as = open_thread_via_search(device, q)
        if not opened_as:
            continue
        lead = _resolve_thread_username(device, fallback=opened_as, my_username=account) or opened_as
        if not _is_valid_ig_username(lead):
            logger.warning("Priority open for %r did not resolve to an @ — skip.", q)
            _leave_thread(device)
            continue
        biz_hit = _business_name_keyword(profile_filter, lead, opened_as)
        if biz_hit:
            _mark_skipped_business(account, lead, biz_hit, opened_as)
            processed.add(q.lower())
            processed.add(lead.lower())
            stats["skipped"] += 1
            logger.info(
                "Skip priority DM @%s — business keyword '%s'.",
                lead,
                biz_hit,
                extra={"color": f"{Fore.CYAN}"},
            )
            _leave_thread(device)
            _ensure_dm_inbox(device)
            continue
        processed.add(q.lower())
        processed.add(lead.lower())
        stats["peeks"] += 1
        try:
            result = process_open_thread(device, session_state, account, lead)
        except Exception as exc:
            logger.warning("Priority thread @%s failed: %s", lead, exc)
            stats["errors"] += 1
            try:
                _leave_thread(device)
            except Exception:
                pass
            _ensure_dm_inbox(device)
            continue
        _record_result(result)
        _ensure_dm_inbox(device)
        if result == "pm_limited":
            logger.warning(
                "Message request / PM limit hit — stopping inbox DM pass."
            )
            break

    scrolls = 0
    while not _budget_hit() and scrolls < max_scrolls:
        if not _ensure_dm_inbox(device):
            logger.warning("Lost DM inbox — stopping inbox reply pass.")
            break
        threads, suggestions_visible = _list_inbox_threads(device)
        if not threads:
            if suggestions_visible:
                logger.info(
                    "Reached Accounts to follow (end of DMs) — stopping inbox pass.",
                    extra={"color": f"{Fore.CYAN}"},
                )
                break
            logger.info("No fully-visible inbox rows — scrolling down…")
            _scroll_inbox(device)
            scrolls += 1
            random_sleep(0.4, 0.7, modulable=False, log=False)
            continue

        # Pick the topmost row we haven't handled yet (strict visual order).
        target: Optional[dict[str, Any]] = None
        for idx, thread in enumerate(threads):
            user = thread["username"]
            aliases = _thread_aliases(thread)
            if aliases & processed:
                _mark_processed(processed, thread)
                continue
            if str(user).lower() == account.lower():
                _mark_processed(processed, thread)
                continue
            if _is_junk_inbox_label(user):
                _mark_processed(processed, thread)
                continue

            display = thread.get("display_name")
            biz_hit = _business_name_keyword(profile_filter, user, display)
            if biz_hit:
                _mark_processed(processed, thread)
                if _is_valid_ig_username(user):
                    _mark_skipped_business(account, user, biz_hit, display)
                stats["skipped"] += 1
                logger.info(
                    "Skip DM inbox @%s — business keyword '%s' in name/handle.",
                    user,
                    biz_hit,
                    extra={"color": f"{Fore.CYAN}"},
                )
                continue

            if _is_valid_ig_username(user) and not thread.get("needs_resolve"):
                known = get_conversation(account, user)
                known_msgs = _filter_real_messages(known.get("messages"))
                known_for_cold = {**known, "messages": known_msgs}
                if should_skip_lead(known) and not thread.get("unread"):
                    _mark_processed(processed, thread)
                    stats["skipped"] += 1
                    logger.info(
                        "Skip inbox @%s — already handled (status=%s).",
                        user,
                        known.get("status"),
                        extra={"color": f"{Fore.CYAN}"},
                    )
                    continue
                if is_cold_outbound_waiting(
                    known_for_cold, unread=bool(thread.get("unread"))
                ) and not is_lead_followup_due(
                    known,
                    unread=bool(thread.get("unread")),
                    after_hours=_int_setting(
                        settings, "dm-inbox-followup-after-hours", 24
                    ),
                ):
                    _mark_processed(processed, thread)
                    stats["skipped"] += 1
                    logger.info(
                        "Skip inbox row %s/%s @%s — already cold-DM'd, waiting on reply.",
                        idx + 1,
                        len(threads),
                        user,
                        extra={"color": f"{Fore.CYAN}"},
                    )
                    continue

            target = thread
            logger.info(
                "Inbox top→bottom: next row %s/%s @%s (y=%s)",
                idx + 1,
                len(threads),
                user,
                thread.get("y1") or thread.get("y"),
                extra={"color": f"{Fore.CYAN}"},
            )
            break

        if target is None:
            # Whole viewport already skipped/processed.
            # If Accounts-to-follow is visible, we are at the end of real DMs — stop.
            if suggestions_visible:
                logger.info(
                    "Inbox DMs above Accounts to follow are done — stopping.",
                    extra={"color": f"{Fore.CYAN}"},
                )
                break
            logger.info(
                "Inbox viewport cleared (%s rows) — scrolling down for more…",
                len(threads),
            )
            _scroll_inbox(device)
            scrolls += 1
            if not _ensure_dm_inbox(device):
                break
            random_sleep(0.4, 0.7, modulable=False, log=False)
            continue

        user = target["username"]
        display = target.get("display_name")
        if not _open_thread(device, target):
            # Don't thrash the same row — mark and keep walking down next pass.
            _mark_processed(processed, target)
            stats["errors"] += 1
            logger.warning("Could not open inbox row %r — continuing down.", user)
            _ensure_dm_inbox(device)
            continue

        lead = _resolve_thread_username(device, fallback=user, my_username=account)
        if not lead or _is_forbidden_lead_username(lead, account):
            logger.warning(
                "Could not resolve IG @ for inbox row %r — skipping.",
                display or user,
            )
            stats["errors"] += 1
            _leave_thread(device)
            _mark_processed(processed, target, user, display)
            _ensure_dm_inbox(device)
            continue

        if not _in_open_dm_thread(device):
            logger.warning(
                "Opened %r but not in a DM thread (likely clipped row) — "
                "scrolling and continuing.",
                display or user,
            )
            # Don't remount the same clipped row forever
            _mark_processed(processed, target, lead)
            _ensure_dm_inbox(device)
            _scroll_inbox(device)
            scrolls += 1
            continue

        _mark_processed(processed, target, lead, display)

        biz_hit = _business_name_keyword(profile_filter, lead, display)
        if biz_hit:
            _mark_skipped_business(account, lead, biz_hit, display)
            stats["peeks"] += 1
            stats["skipped"] += 1
            logger.info(
                "Skip DM inbox @%s — business keyword '%s' in name/handle.",
                lead,
                biz_hit,
                extra={"color": f"{Fore.CYAN}"},
            )
            _leave_thread(device)
            _ensure_dm_inbox(device)
            continue

        # Always open into process_open_thread so empty "friends / say hi"
        # threads can still get a cold DM (don't pre-skip on store alone).
        stats["peeks"] += 1
        try:
            result = process_open_thread(device, session_state, account, lead)
        except Exception as exc:
            logger.warning("Thread @%s failed: %s", lead, exc)
            stats["errors"] += 1
            try:
                _leave_thread(device)
            except Exception:
                pass
            _ensure_dm_inbox(device)
            continue

        _record_result(result)

        if result == "pm_limited":
            logger.warning(
                "Message request / PM limit hit — stopping inbox DM pass."
            )
            break

        try:
            from GramAddict.core.dm_limit_history import active_smart_pm_cap

            cap = active_smart_pm_cap(account)
            if cap is not None and int(session_state.totalPm or 0) >= cap:
                logger.info("Smart PM cap hit during inbox pass — stopping replies.")
                stats["replied"] = max_replies  # force budget hit
                break
        except Exception:
            pass

        if not _ensure_dm_inbox(device):
            logger.warning("Could not return to inbox after @%s — stopping.", lead)
            break

        # Re-list from current viewport (coords stale after open); do not scroll yet
        # unless the next pass finds nothing left on screen.
        random_sleep(0.3, 0.6, modulable=False, log=False)

    # Back to Home for the rest of the session.
    try:
        for _ in range(3):
            if HomeView(device).is_inbox_open():
                device.back()
                random_sleep(0.4, 0.7, modulable=False, log=False)
            else:
                break
        TabBarView(device).navigateToHome()
    except Exception:
        pass

    logger.info(
        "DM inbox pass done — work=%s peeks=%s replied=%s read_only=%s skipped=%s errors=%s",
        stats["opened"],
        stats["peeks"],
        stats["replied"],
        stats["read_only"],
        stats["skipped"],
        stats["errors"],
        extra={"color": f"{Fore.CYAN}"},
    )
    return stats
