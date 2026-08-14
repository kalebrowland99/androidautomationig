from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta

from colorama import Fore

from GramAddict.core.device_facade import Timeout
from GramAddict.core.resources import ClassName
from GramAddict.core.utils import random_sleep
from GramAddict.core.views import (
    HashTagView,
    OpenedPostView,
    PlacesView,
    PostsGridView,
    PostsViewList,
    ProfileView,
    TabBarView,
    UniversalActions,
    case_insensitive_re,
)

logger = logging.getLogger(__name__)

# Fallback coordinates for the first post of the SECOND profile-grid row, as
# screen-proportional coordinates (measured on a 1080x2094 device with the
# profile scrolled to the top). Only used when Instagram doesn't expose the
# thumbnail's position in the a11y tree — see `open_second_row_first_post`.
SECOND_ROW_FIRST_POST_FRACTION = (181 / 1080, 1862 / 2094)

# First 4 grid cells: 3 pin slots + the next chronological post.
_FIRST_FOUR_CELLS = ((0, 0), (0, 1), (0, 2), (1, 0))
_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_RELATIVE_UNITS = {
    "second": "seconds",
    "seconds": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "minute": "minutes",
    "minutes": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "hour": "hours",
    "hours": "hours",
    "hr": "hours",
    "hrs": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
    "wk": "weeks",
    "wks": "weeks",
    "month": "days",
    "months": "days",
    "mo": "days",
    "mos": "days",
    "year": "days",
    "years": "days",
    "yr": "days",
    "yrs": "days",
}


def parse_ig_post_when(text: str, now: datetime | None = None) -> datetime | None:
    """Parse any Instagram timestamp (relative or calendar)."""
    if not text:
        return None
    now = now or datetime.now()
    low = " ".join(str(text).lower().replace("·", " ").replace("•", " ").split())
    low = re.sub(r"^edited\s+", "", low)

    if re.search(r"\bjust now\b", low):
        return now
    if re.search(r"\byesterday\b", low):
        return now - timedelta(days=1)
    if re.search(r"\btoday\b", low):
        return now

    rel = re.search(
        r"\b(?:(\d+)|an?)\s*"
        r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|"
        r"months?|mos?|years?|yrs?)\s+ago\b",
        low,
    )
    if rel:
        n = 1 if rel.group(1) is None else int(rel.group(1))
        raw_unit = rel.group(2)
        unit = _RELATIVE_UNITS.get(raw_unit, "hours")
        if raw_unit.startswith("month") or raw_unit.startswith("mo"):
            n *= 30
            unit = "days"
        elif raw_unit.startswith("year") or raw_unit.startswith("yr"):
            n *= 365
            unit = "days"
        elif raw_unit.startswith("wk"):
            unit = "weeks"
        elif raw_unit.startswith("hr"):
            unit = "hours"
        elif raw_unit.startswith("min") or raw_unit.startswith("sec"):
            unit = "minutes" if raw_unit.startswith("min") else "seconds"
        return now - timedelta(**{unit: n})

    compact = re.search(r"\b(\d+)\s*([smhdwy])\b", low)
    compact_only = bool(
        compact and re.fullmatch(r"\d+\s*[smhdwy]", low.strip(" .,;:"))
    )
    if compact and (re.search(r"\bposted a\b", low) or compact_only):
        n = int(compact.group(1))
        unit = {
            "s": "seconds",
            "m": "minutes",
            "h": "hours",
            "d": "days",
            "w": "weeks",
            "y": "days",
        }[compact.group(2)]
        if compact.group(2) == "y":
            n *= 365
        return now - timedelta(**{unit: n})

    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", low)
    if iso:
        try:
            return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), 12, 0)
        except ValueError:
            pass

    abs_date = re.search(
        r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|"
        r"july|jul|august|aug|september|sept|sep|october|oct|november|nov|"
        r"december|dec)\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?\b",
        low,
    )
    if abs_date:
        month = _MONTHS[abs_date.group(1)]
        day = int(abs_date.group(2))
        year = int(abs_date.group(3)) if abs_date.group(3) else now.year
        try:
            parsed = datetime(year, month, day, 12, 0, 0)
        except ValueError:
            return None
        if not abs_date.group(3) and parsed > now + timedelta(days=1):
            parsed = parsed.replace(year=now.year - 1)
        return parsed

    dmy = re.search(
        r"\b(\d{1,2})\s+(january|jan|february|feb|march|mar|april|apr|may|"
        r"june|jun|july|jul|august|aug|september|sept|sep|october|oct|"
        r"november|nov|december|dec)\.?(?:,?\s*(\d{4}))?\b",
        low,
    )
    if dmy:
        month = _MONTHS[dmy.group(2)]
        day = int(dmy.group(1))
        year = int(dmy.group(3)) if dmy.group(3) else now.year
        try:
            parsed = datetime(year, month, day, 12, 0, 0)
        except ValueError:
            return None
        if not dmy.group(3) and parsed > now + timedelta(days=1):
            parsed = parsed.replace(year=now.year - 1)
        return parsed
    return None


_IMAGEVIEW_RID = case_insensitive_re(r".*:id/row_feed_photo_imageview")


def _read_opened_post_when(device) -> tuple[datetime | None, str]:
    """Read whatever timestamp Instagram shows on the opened post. Do not tap."""
    for _ in range(4):
        when, raw = _best_on_screen_date(device)
        if when is not None:
            return when, raw
        random_sleep(0.3, 0.5, modulable=False)
    return None, ""


def _node_texts(node) -> list[str]:
    chunks = []
    try:
        chunks.append(node.get_text(error=False) or "")
    except Exception:
        pass
    try:
        chunks.append(node.get_desc() or "")
    except Exception:
        pass
    out = []
    for chunk in chunks:
        text = " ".join(str(chunk).split())
        if text:
            out.append(text)
    return out


def _consider_date(best_top, best, top: int, text: str):
    when = parse_ig_post_when(text)
    if when is None:
        return best_top, best
    if best_top is None or top < best_top:
        return top, (when, text)
    return best_top, best


def _best_on_screen_date(device) -> tuple[datetime | None, str]:
    """Any parseable timestamp on screen; prefer the highest (current post)."""
    best_top = None
    best: tuple[datetime, str] | None = None
    selectors = (
        {"className": ClassName.TEXT_VIEW, "clickable": True},
        {"className": ClassName.TEXT_VIEW},
        {"resourceIdMatches": _IMAGEVIEW_RID},
    )
    seen = set()
    for selector in selectors:
        for node in _iter_matches(device, **selector):
            try:
                bounds = node.get_bounds()
                top = int(bounds.get("top") or 0)
            except Exception:
                continue
            if top < 80:
                continue
            for text in _node_texts(node):
                key = (top, text)
                if key in seen:
                    continue
                seen.add(key)
                best_top, best = _consider_date(best_top, best, top, text)
    if best is None:
        return None, ""
    return best[0], best[1]


def _iter_matches(device, **selector):
    nodes = device.find(**selector)
    if not nodes.exists(Timeout.ZERO):
        return
    try:
        count = nodes.count_items()
    except Exception:
        count = 1
    for index in range(max(count, 1)):
        node = nodes if count <= 1 else device.find(index=index, **selector)
        if node.exists(Timeout.ZERO):
            yield node


def _on_opened_post(device) -> bool:
    """True when a profile post/reel is open, not the thumbnail grid."""
    if device.find(resourceIdMatches=_IMAGEVIEW_RID).exists(Timeout.ZERO):
        return True
    if OpenedPostView(device)._get_focused_post_media() is not None:
        return True
    return PostsViewList(device)._is_feed_clips_viewer()


def _tap_xy_once(device, x: int, y: int) -> None:
    """Raw screen tap — no element wait, no vision retry."""
    device.deviceV2.click(int(x), int(y))


def _profile_feed_open(device) -> bool:
    return _on_opened_post(device) or PostsViewList(device)._is_feed_clips_viewer()


def _reveal_date_under_post(device) -> None:
    """Scroll the opened post so the grey date is visible. Never tap."""
    if PostsViewList(device)._is_feed_clips_viewer():
        return
    try:
        info = device.get_info()
        w = int(info.get("displayWidth") or 1080)
        h = int(info.get("displayHeight") or 2000)
    except Exception:
        w, h = 1080, 2000
    # Swipe on the media, not the grey date row (that row is clickable).
    start_y = int(h * 0.42)
    end_y = int(h * 0.22)
    device.swipe_points(w / 2, start_y, w / 2, end_y)
    random_sleep(0.35, 0.65, modulable=False)


def _open_grid_cell_once(grid_view: PostsGridView, row: int, col: int) -> bool:
    """Center-tap the thumbnail once from the profile grid. Do not tap again."""
    if _on_opened_post(grid_view.device):
        logger.info("Post already open — not tapping again.")
        return True
    post_view = grid_view.find_post_by_grid_position(row + 1, col + 1)
    if not post_view.exists(Timeout.MEDIUM):
        return False
    if not grid_view._ensure_grid_cell_tappable(post_view):
        post_view = grid_view.find_post_by_grid_position(row + 1, col + 1)
        if not post_view.exists(Timeout.SHORT):
            return False
    try:
        bounds = post_view.get_bounds()
    except Exception:
        return False
    x = int((bounds["left"] + bounds["right"]) / 2)
    y = int((bounds["top"] + bounds["bottom"]) / 2)
    logger.info(f"Grid tap r{row + 1}c{col + 1} at ({x}, {y}).")
    _tap_xy_once(grid_view.device, x, y)
    device = grid_view.device
    for _ in range(12):
        random_sleep(0.4, 0.6, modulable=False)
        if _profile_feed_open(device):
            return True
    return False


def _on_profile_grid(device) -> bool:
    if _on_opened_post(device):
        return False
    grid = PostsGridView(device)
    return grid.find_post_by_grid_position(1, 1).exists(Timeout.ZERO)


def _tap_top_left_back(device) -> bool:
    """Tap Instagram's top-left Back arrow. Does not use the system back key."""
    btn = device.find(
        resourceIdMatches=case_insensitive_re(r".*:id/action_bar_button_back")
    )
    if not btn.exists(Timeout.SHORT):
        return False
    try:
        bounds = btn.get_bounds()
    except Exception:
        return False
    x = int((bounds["left"] + bounds["right"]) / 2)
    y = int((bounds["top"] + bounds["bottom"]) / 2)
    _tap_xy_once(device, x, y)
    return True


def _back_to_profile_grid(device) -> bool:
    for _ in range(4):
        if _on_profile_grid(device):
            return True
        if not _tap_top_left_back(device):
            logger.debug("Top-left Back arrow not on screen.")
            return _on_profile_grid(device)
        random_sleep(0.5, 0.9, modulable=False)
    return _on_profile_grid(device)


def open_newest_of_first_four_posts(device) -> bool:
    """Open the most recently dated post among the first 4 grid cells.

    For each of the first 4 thumbnails: tap once, scroll down to read the date
    under that post (``July 21``, ``5 days ago``), back to the profile grid,
    then the next cell. Do not tap the opened post.
    """
    grid_view = PostsGridView(device)
    grid_view.ensure_grid_rows_visible(rows=2)

    scored: list[tuple[tuple[int, int], datetime | None]] = []
    for row, col in _FIRST_FOUR_CELLS:
        if _on_opened_post(device):
            if not _back_to_profile_grid(device):
                logger.warning("Lost profile grid while sampling post dates.")
                return False
            grid_view.ensure_grid_rows_visible(rows=2)
        cell = grid_view.find_post_by_grid_position(row + 1, col + 1)
        if not cell.exists(Timeout.SHORT):
            logger.info(
                f"Skip date-check — grid r{row + 1}c{col + 1} not on screen.",
                extra={"color": f"{Fore.CYAN}"},
            )
            continue
        if not _open_grid_cell_once(grid_view, row, col):
            logger.info(
                f"Could not open grid r{row + 1}c{col + 1} for date check.",
                extra={"color": f"{Fore.CYAN}"},
            )
            continue
        _reveal_date_under_post(device)
        when, raw = _read_opened_post_when(device)
        label = when.strftime("%Y-%m-%d %H:%M") if when else "unknown"
        logger.info(
            f"Grid r{row + 1}c{col + 1} posted {label}"
            + (f" ({raw[:80]})" if raw else ""),
            extra={"color": f"{Fore.CYAN}"},
        )
        scored.append(((row, col), when))
        if not _back_to_profile_grid(device):
            logger.warning("Lost profile grid while sampling post dates.")
            return False
        grid_view.ensure_grid_rows_visible(rows=2)

    if not scored:
        logger.warning("Could not sample any of the first 4 grid posts.")
        return False

    dated = [(cell, ts) for cell, ts in scored if ts is not None]
    if dated:
        dated.sort(key=lambda item: item[1], reverse=True)
        best_cell, best_ts = dated[0]
    else:
        best_cell = next((cell for cell, _ in scored if cell == (1, 0)), scored[0][0])
        best_ts = None

    when_label = best_ts.strftime("%Y-%m-%d %H:%M") if best_ts else "unknown date"
    logger.info(
        f"Most recent of first 4: row {best_cell[0] + 1}, col {best_cell[1] + 1} ({when_label}).",
        extra={"color": f"{Fore.GREEN}"},
    )
    opened_ok = _open_grid_cell_once(grid_view, *best_cell)
    return opened_ok or _profile_feed_open(device)


def _tap_second_row_first_post(device) -> None:
    """Coordinate-tap the first post of the second grid row (skip first row)."""
    info = device.get_info()
    fx, fy = SECOND_ROW_FIRST_POST_FRACTION
    x = int(info["displayWidth"] * fx)
    y = int(info["displayHeight"] * fy)
    logger.info(f"Skip first row: tapping first post of second row at ({x}, {y}).")
    device.deviceV2.click(x, y)
    random_sleep(1.0, 2.0, modulable=False)


def open_second_row_first_post(device) -> bool:
    """Open the first (most recent) post of the SECOND profile-grid row.

    Scrolls until two grid rows are fully on screen, then opens row 2 col 1.
    Profiles differ across Instagram builds, so try both reliable openers:
      1) a11y content-desc (``at row 2, column 1``)
      2) hierarchy child indexes (row=1, col=0)
    Coordinate tap is only the last resort.
    """
    grid_view = PostsGridView(device)
    grid_view.ensure_grid_rows_visible(rows=2)

    # navigateToPost already tries content-desc then hierarchy index.
    opened, _, _ = grid_view.navigateToPost(1, 0)
    if opened is not None:
        logger.info("Opened first post of the second row.")
        return True

    logger.debug(
        "Content-desc + hierarchy open failed; falling back to coordinate tap."
    )
    _tap_second_row_first_post(device)
    return OpenedPostView(device)._get_focused_post_media() is not None


def check_if_english(device):
    """check if app is in English"""
    logger.debug("Checking if app is in English..")
    # Accept singular label variants: a profile with a count of 1 renders the
    # English labels as "1 post" / "1 follower" ("following" has no plural).
    english_posts = {"posts", "post"}
    english_followers = {"followers", "follower"}
    english_following = {"following"}

    # The profile header is often still rendering at session start (especially on
    # fresh accounts showing the "get started" onboarding layout), so a single
    # read can come back empty. Retry a few times before concluding anything —
    # exiting on a transient misread here would stop the whole bot.
    post = follower = following = None
    attempts = 3
    for attempt in range(attempts):
        post, follower, following = ProfileView(device)._getSomeText()
        if None not in {post, follower, following}:
            break
        if attempt < attempts - 1:
            logger.debug(
                "Profile header not fully readable yet "
                f"(posts={post!r}, followers={follower!r}, following={following!r}); "
                f"retrying ({attempt + 1}/{attempts - 1})."
            )
            random_sleep(2.0, 3.0, modulable=False)

    if None in {post, follower, following}:
        # Couldn't read the labels at all — don't kill the bot over a header that
        # never rendered; warn and let the session continue.
        logger.warning(
            "Couldn't read the profile header to verify the app language "
            f"(posts={post!r}, followers={follower!r}, following={following!r}). "
            "Be sure Instagram is set to English or the bot won't work!"
        )
    elif (
        post in english_posts
        and follower in english_followers
        and following in english_following
    ):
        logger.debug("Instagram in English.")
    else:
        # Genuine non-English labels were read consistently — log what we saw so a
        # real language problem is distinguishable from a transient misread.
        logger.error(
            "Please change the language manually to English! "
            f"(read labels: posts={post!r}, followers={follower!r}, following={following!r})"
        )
        sys.exit(1)
    return ProfileView(device, is_own_profile=True)


def nav_to_blogger(device, username, current_job):
    """navigate to blogger (followers list or posts)"""
    _to_followers = bool(current_job.endswith("followers"))
    _to_following = bool(current_job.endswith("following"))
    if username is None:
        profile_view = TabBarView(device).navigateToProfile()
        if _to_followers:
            logger.info("Open your followers.")
            profile_view.navigateToFollowers()
        elif _to_following:
            logger.info("Open your following.")
            profile_view.navigateToFollowing()
    else:
        search_view = TabBarView(device).navigateToSearch()
        if not search_view.navigate_to_target(username, current_job):
            return False

        profile_view = ProfileView(device, is_own_profile=False)
        if _to_followers:
            logger.info(f"Open @{username} followers.")
            profile_view.navigateToFollowers()
        elif _to_following:
            logger.info(f"Open @{username} following.")
            profile_view.navigateToFollowing()

    return True


def nav_to_hashtag_or_place(device, target, current_job):
    """navigate to hashtag/place/feed list"""
    search_view = TabBarView(device).navigateToSearch()
    if not search_view.navigate_to_target(target, current_job):
        return False

    TargetView = HashTagView if current_job.startswith("hashtag") else PlacesView

    if current_job.endswith("recent"):
        logger.info("Switching to Recent tab.")
        recent_tab = TargetView(device)._getRecentTab()
        if recent_tab.exists(Timeout.MEDIUM):
            recent_tab.click()
        else:
            return False

        if UniversalActions(device)._check_if_no_posts():
            UniversalActions(device)._reload_page()
            if UniversalActions(device)._check_if_no_posts():
                return False

    result_view = TargetView(device)._getRecyclerView()
    FistImageInView = TargetView(device)._getFistImageView(result_view)
    if FistImageInView.exists():
        logger.info(f"Opening the first result for {target}.")
        FistImageInView.click()
        return True
    else:
        logger.info(
            f"There is any result for {target} (not exists or doesn't load). Skip."
        )
        return False


def nav_to_post_likers(device, username, my_username, skip_first_row: bool = False):
    """navigate to blogger post likers"""
    if username == my_username:
        TabBarView(device).navigateToProfile()
    else:
        search_view = TabBarView(device).navigateToSearch()
        if not search_view.navigate_to_target(username, "account"):
            return False
    profile_view = ProfileView(device)
    is_private = profile_view.isPrivateAccount()
    posts_count = profile_view.getPostsCount()
    is_empty = posts_count == 0
    if is_private or is_empty:
        private_empty = "Private" if is_private else "Empty"
        logger.info(f"{private_empty} account.", extra={"color": f"{Fore.GREEN}"})
        return False
    grid_view = PostsGridView(device)
    if skip_first_row:
        logger.info(
            f"Sampling first 4 grid posts to find the newest (skip pins) for {username}."
        )
        if not open_newest_of_first_four_posts(device):
            logger.warning(
                f"Date sample failed for {username} — falling back to second-row post."
            )
            if not open_second_row_first_post(device):
                logger.warning(f"Could not open a post of {username} (skip first row).")
                return False
        return True
    logger.info(f"Opening the first post of {username}.")
    if not grid_view.is_post_tappable(0, 0):
        ProfileView(device).swipe_to_fit_posts()
    opened, _, _ = grid_view.navigateToPost(0, 0)
    if opened is None:
        logger.warning(f"Could not open a post of {username}.")
        return False
    return True


TAB_BAR_RESOURCE_ID = "com.instagram.android:id/tab_bar"


def _tap_tab_right_of_home(device) -> bool:
    """Tap the bottom-nav tab immediately to the right of Home (Reels/Clips).

    Located positionally (Home button + the next tab to its right) so it works
    regardless of the exact content-description on this IG build. Returns False
    if the tab bar / Home button can't be resolved.
    """
    d = device.deviceV2
    try:
        elements = d.xpath(
            f'//*[@resource-id="{TAB_BAR_RESOURCE_ID}"]'
            '//*[@clickable="true"]'
        ).all()
    except Exception as e:
        logger.debug(f"Could not enumerate tab bar buttons: {e}")
        return False

    tabs = []
    for el in elements:
        try:
            left, top, right, bottom = el.bounds
        except Exception:
            continue
        center_x = (left + right) / 2
        desc = (el.attrib.get("content-desc") or "").strip()
        tabs.append((center_x, desc, el))

    if not tabs:
        return False
    tabs.sort(key=lambda t: t[0])
    home_index = next(
        (i for i, (_, desc, _) in enumerate(tabs) if desc.lower() == "home"),
        None,
    )
    if home_index is None or home_index + 1 >= len(tabs):
        return False
    tabs[home_index + 1][2].click()
    logger.info("Opened the tab to the right of Home (Reels/Clips feed).")
    return True


def nav_to_feed(device):
    # For the feed job, scroll the Reels/Clips feed (the tab to the right of
    # Home) instead of the home feed. Land on Home first so the tab bar is
    # present, then tap the tab to its right; fall back to the Reels tab.
    TabBarView(device).navigateToHome()
    if not _tap_tab_right_of_home(device):
        logger.debug(
            "Could not tap the tab right of Home positionally — using the Reels tab."
        )
        TabBarView(device).navigateToReels()
