"""Dashboard debug tests — isolated bot flow steps for the device lab.

Add a new debug test
--------------------
1. Add an entry to ``DEBUG_TESTS``.
2. Add its id to the right list in ``DEBUG_GROUP_ORDER`` (order matters).
3. Implement the ``kind`` handler in ``run_debug_test``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Literal, Optional, TypedDict

from fastapi import HTTPException

from dashboard import debug_log
from dashboard.gramaddict_bootstrap import ensure_gramaddict_globals
from GramAddict.core.device_facade import DeviceFacade, Direction, Mode, Timeout
from GramAddict.core.utils import random_sleep
from GramAddict.core.views import (
    DEFAULT_LIKERS_TAP_OFFSET_X,
    DEFAULT_LIKERS_TAP_OFFSET_Y,
    LIKES_COUNT_HIDDEN,
    case_insensitive_re,
)
from GramAddict.core.vpn import (
    DEFAULT_APP_NAME,
    VPN_CONNECT_DESC,
    VPN_STOP_DESC,
    _find_by_description,
    ensure_shadowrocket_vpn,
)

DEFAULT_APP_ID = "com.instagram.android"
_DEBUG_VPN_WAIT_SECONDS = 10
_facade_cache: dict[str, DeviceFacade] = {}
_cancel_events: dict[str, threading.Event] = {}
_cancel_guard = threading.Lock()
_debug_tls = threading.local()


class DebugCancelled(Exception):
    """Raised when the user hits Stop on a running dashboard debug test."""


PRODUCTION_DEBUG_GROUPS = frozenset({"blogger-post-likers"})
BLOGGER_POST_LIKERS_JOB = "blogger-post-likers"


def uses_production_debug_mode(test_id: str) -> bool:
    for group in PRODUCTION_DEBUG_GROUPS:
        if test_id in DEBUG_GROUP_ORDER.get(group, []):
            return True
    return False


def begin_debug_run(serial: str, *, production_mode: bool = False) -> None:
    import importlib

    import GramAddict.core.device_facade as ga_device_facade
    import GramAddict.core.interaction as ga_interaction
    import GramAddict.core.navigation as ga_navigation
    import GramAddict.core.resources as ga_resources
    import GramAddict.core.utils as ga_utils
    import GramAddict.core.views as ga_views

    importlib.reload(ga_device_facade)
    importlib.reload(ga_resources)
    importlib.reload(ga_views)
    importlib.reload(ga_navigation)

    global DeviceFacade
    DeviceFacade = ga_device_facade.DeviceFacade
    _facade_cache.clear()

    # Reloading views/resources drops the ResourceID instance the modules cached,
    # so rebuild it from the freshly-reloaded resources module.
    app_id = getattr(getattr(ga_utils, "args", None), "app_id", "com.instagram.android")
    resource_ids = ga_resources.ResourceID(app_id)
    ga_views.FEED_DEBUG_FAST = not production_mode
    ga_views.ResourceID = resource_ids
    ga_utils.ResourceID = resource_ids
    ga_interaction.ResourceID = resource_ids

    with _cancel_guard:
        event = _cancel_events.get(serial)
        if event is None:
            event = threading.Event()
            _cancel_events[serial] = event
        event.clear()
    _debug_tls.serial = serial
    _debug_tls.production_mode = production_mode
    debug_log.start_session(serial)

    if production_mode:
        return

    if not hasattr(begin_debug_run, "_orig_random_sleep"):
        begin_debug_run._orig_random_sleep = ga_utils.random_sleep  # type: ignore[attr-defined]

    def cancellable_sleep(lo=0.5, hi=3.0, modulable=True, log=True):
        deadline = time.time() + (lo if lo >= hi else lo + (hi - lo) * 0.5)
        while time.time() < deadline:
            raise_if_cancelled(serial)
            time.sleep(min(0.05, max(0.0, deadline - time.time())))

    ga_utils.random_sleep = cancellable_sleep  # type: ignore[method-assign]

    if not hasattr(begin_debug_run, "_orig_get_ui_timeout"):
        begin_debug_run._orig_get_ui_timeout = ga_device_facade.DeviceFacade.View.get_ui_timeout  # type: ignore[attr-defined]

    orig_get_ui_timeout = begin_debug_run._orig_get_ui_timeout  # type: ignore[attr-defined]

    def _debug_ui_timeout(*args):  # type: ignore[no-untyped-def]
        ui_timeout = args[-1]
        seconds = orig_get_ui_timeout(ui_timeout)
        return min(int(seconds), 1)

    ga_device_facade.DeviceFacade.View.get_ui_timeout = _debug_ui_timeout  # type: ignore[method-assign]


def request_debug_cancel(serial: str) -> None:
    with _cancel_guard:
        event = _cancel_events.get(serial)
        if event is not None:
            event.set()


def clear_debug_cancel(serial: str) -> None:
    import GramAddict.core.utils as ga_utils

    with _cancel_guard:
        event = _cancel_events.get(serial)
        if event is not None:
            event.clear()
    orig = getattr(begin_debug_run, "_orig_random_sleep", None)
    if orig is not None:
        ga_utils.random_sleep = orig  # type: ignore[method-assign]
    orig_ui = getattr(begin_debug_run, "_orig_get_ui_timeout", None)
    if orig_ui is not None:
        DeviceFacade.View.get_ui_timeout = staticmethod(orig_ui)  # type: ignore[method-assign]
    try:
        import GramAddict.core.views as ga_views

        ga_views.FEED_DEBUG_FAST = False
    except ImportError:
        pass
    _debug_tls.serial = None
    _debug_tls.production_mode = False
    debug_log.stop_session(serial)


def debug_cancelled(serial: str) -> bool:
    with _cancel_guard:
        event = _cancel_events.get(serial)
        return bool(event and event.is_set())


def raise_if_cancelled(serial: str) -> None:
    if debug_cancelled(serial):
        raise DebugCancelled()


def _pause(lo: float = 0.15, hi: float = 0.3) -> None:
    """Short pause for dashboard debug taps (no multi-second bot delays)."""
    serial = getattr(_debug_tls, "serial", None)
    deadline = time.time() + (lo if lo >= hi else lo + (hi - lo) * 0.5)
    while time.time() < deadline:
        if serial:
            raise_if_cancelled(serial)
        time.sleep(min(0.05, max(0.0, deadline - time.time())))

DebugKind = Literal[
    "home",
    "wake_unlock",
    "vpn_ensure",
    "vpn_then_instagram",
    "open_shadowrocket",
    "wait_vpn_ui",
    "tap_vpn_connect_if_needed",
    "wait_vpn_connected",
    "detect_vpn",
    "open_instagram",
    "close_instagram",
    "screenshot",
    "ig_close_keyboard",
    "ig_tab_home",
    "ig_tab_search",
    "ig_tab_profile",
    "ig_profile_read_labels",
    "ig_search_open_user",
    "ig_profile_tap_followers",
    "ig_followers_open_first",
    "ig_back",
    "ig_profile_swipe_posts",
    "ig_profile_open_post",
    "ig_post_detect_like",
    "ig_post_tap_like",
    "ig_profile_detect_follow",
    "ig_profile_follow_vision",
    "ig_profile_tap_follow",
    "ig_nav_blogger_followers",
    "ig_export_crash",
    "ig_dismiss_popups",
    "ig_tab_reels",
    "ig_tab_activity",
    "ig_check_english",
    "ig_profile_detect_private",
    "ig_profile_tap_following",
    "ig_following_open_first",
    "ig_followers_scroll",
    "ig_profile_open_account_menu",
    "ig_account_switch_user",
    "ig_profile_detect_story_ring",
    "ig_profile_open_stories",
    "ig_story_detect_like",
    "ig_search_open_hashtag",
    "ig_nav_hashtag_top",
    "ig_nav_hashtag_recent",
    "ig_feed_refresh",
    "ig_feed_inspect_post",
    "ig_feed_detect_owner",
    "ig_feed_detect_caption",
    "ig_feed_likers_status",
    "ig_feed_already_liked",
    "ig_feed_media_type",
    "ig_feed_check_last_post",
    "ig_feed_detect_like",
    "ig_feed_tap_like",
    "ig_feed_scroll_down",
    "ig_feed_swipe",
    "ig_post_detect_comment",
    "ig_post_open_comments",
    "ig_profile_detect_message",
    "ig_profile_open_message",
    "ig_nav_blogger_following",
    "ig_nav_post_likers",
    "ig_post_detect_likers",
    "ig_post_tap_likers",
    "ig_post_open_likers",
    "ig_likers_open_first",
    "ig_post_detect_carousel",
    "ig_post_swipe_carousel",
    "ig_post_detect_video",
    "ig_post_detect_photo_or_video",
    "ig_post_start_video",
    "ig_post_open_video",
    "ig_post_tap_like_video",
    "ig_post_fullscreen_like_stay",
    "ig_post_fullscreen_detect_comment",
    "ig_post_fullscreen_open_comments",
    "ig_post_fullscreen_send_comment",
    "ig_post_fullscreen_detect",
    "ig_post_inline_detect",
    "ig_post_inline_like",
    "ig_post_inline_open_comments",
    "ig_post_inline_send_comment",
    "ig_post_fullscreen_reveal_likes",
    "ig_post_fullscreen_likers",
    "ig_profile_tap_mutual",
    "ig_profile_tab_posts",
    "ig_profile_tab_reels",
    "ig_nav_hashtag_likers_top",
    "ig_own_following_list",
    "ig_unfollow_detect_row",
    "ig_unfollow_tap_first_row",
    "ig_unfollow_detect_profile",
    "ig_unfollow_tap_profile",
    "ig_unfollow_confirm",
    "ig_own_followers_list",
    "ig_remove_follower_search",
    "ig_remove_follower_detect",
    "ig_remove_follower_tap",
    "ig_story_tap_like",
    "ig_open_post_url",
    "ig_post_send_comment",
    "ig_profile_send_pm",
    "telegram_test_send",
    "ig_post_reel_tap_create",
    "ig_post_reel_clear_gallery",
    "ig_post_reel_push_media",
    "ig_post_reel_select_media",
    "ig_post_reel_tap_next_top",
    "ig_post_reel_dismiss_popups",
    "ig_post_reel_tap_next_clips",
    "ig_post_reel_write_caption",
    "ig_post_reel_share",
    "ig_post_reel_full_session",
    "ig_bpl_production_nav",
    "ig_bpl_production_check_post",
    "ig_bpl_production_likers_status",
    "ig_bpl_production_open_likers",
    "ig_bpl_production_verify_list",
    "ig_bpl_production_full",
]


class DebugTest(TypedDict, total=False):
    label: str
    kind: DebugKind
    state: str
    detects: str
    needs_username: bool
    needs_search: bool
    needs_post_url: bool
    needs_test_message: bool


# Explicit execution order per debug tab (sequential A→Z).
DEBUG_GROUP_ORDER: dict[str, list[str]] = {
    "flow": [
        "wake-unlock",
        "home",
        "open-shadowrocket",
        "wait-vpn-ui",
        "tap-vpn-connect",
        "home-after-vpn",
        "open-instagram",
        "ig-tab-home",
        "ig-feed-inspect-post",
        "ig-feed-detect-like",
        "ig-feed-tap-like",
        "ig-feed-scroll-down",
    ],
    "vpn": [
        "home",
        "open-shadowrocket",
        "wait-vpn-ui",
        "tap-vpn-connect",
        "home-after-vpn",
    ],
    "feed": [
        "open-instagram",
        "ig-tab-home",
        "ig-feed-refresh",
        "ig-feed-inspect-post",
        "ig-feed-detect-owner",
        "ig-feed-detect-caption",
        "ig-feed-likers-status",
        "ig-feed-already-liked",
        "ig-feed-media-type",
        "ig-feed-check-last-post",
        "ig-feed-detect-like",
        "ig-feed-tap-like",
        "ig-feed-scroll-down",
        "ig-feed-swipe",
    ],
    "blogger-post-likers": [
        "open-instagram",
        "ig-bpl-production-nav",
        "ig-bpl-production-check-post",
        "ig-bpl-production-likers-status",
        "ig-bpl-production-open-likers",
        "ig-bpl-production-verify-list",
        "ig-bpl-production-full",
    ],
    "reel-comment": [
        "ig-post-fullscreen-like-stay",
        "ig-post-fullscreen-detect-comment",
        "ig-post-fullscreen-open-comments",
        "ig-post-fullscreen-send-comment",
    ],
    "profile-post-video": [
        "ig-post-inline-detect",
        "ig-post-inline-like",
        "ig-post-inline-open-comments",
        "ig-post-inline-send-comment",
    ],
    "instagram": [
        "open-instagram",
        "ig-dismiss-popups",
        "ig-post-reel-tap-create",
        "ig-post-reel-clear-gallery",
        "ig-post-reel-push-media",
        "ig-post-reel-select-media",
        "ig-post-reel-tap-next-top",
        "ig-post-reel-dismiss-popups",
        "ig-post-reel-tap-next-clips",
        "ig-post-reel-write-caption",
        "ig-post-reel-share",
        "ig-post-reel-full-session",
        "ig-close-keyboard",
        "ig-tab-home",
        "ig-feed-refresh",
        "ig-feed-inspect-post",
        "ig-feed-detect-owner",
        "ig-feed-detect-caption",
        "ig-feed-likers-status",
        "ig-feed-already-liked",
        "ig-feed-media-type",
        "ig-feed-check-last-post",
        "ig-feed-detect-like",
        "ig-feed-tap-like",
        "ig-feed-scroll-down",
        "ig-feed-swipe",
        "ig-tab-search",
        "ig-tab-reels",
        "ig-tab-activity",
        "ig-tab-profile",
        "ig-profile-read-labels",
        "ig-check-english",
        "ig-profile-detect-private",
        "ig-profile-open-account-menu",
        "ig-account-switch-user",
        "ig-profile-detect-story-ring",
        "ig-profile-open-stories",
        "ig-story-detect-like",
        "ig-back-from-stories",
        "ig-search-open-user",
        "ig-profile-detect-follow",
        "ig-profile-follow-vision",
        "ig-profile-tap-follow",
        "ig-profile-detect-message",
        "ig-profile-open-message",
        "ig-profile-send-pm",
        "ig-back-to-profile",
        "ig-profile-tap-followers",
        "ig-followers-open-first",
        "ig-back-to-followers",
        "ig-followers-scroll",
        "ig-back-to-profile-2",
        "ig-profile-tap-following",
        "ig-following-open-first",
        "ig-back-from-following",
        "ig-profile-swipe-posts",
        "ig-profile-open-post",
        "ig-post-detect-like",
        "ig-post-tap-like",
        "ig-post-detect-comment",
        "ig-post-open-comments",
        "ig-post-send-comment",
        "ig-back-from-comments",
        "ig-back-from-post",
        "ig-search-open-hashtag",
        "ig-nav-hashtag-top",
        "ig-back-to-search",
        "ig-nav-blogger-followers",
        "ig-nav-blogger-following",
        "ig-nav-post-likers",
        "ig-post-detect-likers",
        "ig-post-tap-likers",
        "ig-post-open-likers",
        "ig-likers-open-first",
        "ig-back-from-liker-profile",
        "ig-post-detect-carousel",
        "ig-post-swipe-carousel",
        "ig-post-detect-video",
        "ig-post-detect-photo-or-video",
        "ig-post-start-video",
        "ig-post-open-video",
        "ig-post-tap-like-video",
        "ig-post-fullscreen-like-stay",
        "ig-post-fullscreen-detect-comment",
        "ig-post-fullscreen-open-comments",
        "ig-post-fullscreen-send-comment",
        "ig-post-fullscreen-detect",
        "ig-post-fullscreen-reveal-likes",
        "ig-post-fullscreen-likers",
        "ig-profile-tap-mutual",
        "ig-profile-tab-posts",
        "ig-profile-tab-reels",
        "ig-nav-hashtag-likers-top",
        "ig-back-after-hashtag-likers",
        "ig-own-following-list",
        "ig-unfollow-detect-row",
        "ig-unfollow-tap-first-row",
        "ig-unfollow-detect-profile",
        "ig-unfollow-tap-profile",
        "ig-unfollow-confirm",
        "ig-own-followers-list",
        "ig-remove-follower-search",
        "ig-remove-follower-detect",
        "ig-remove-follower-tap",
        "ig-story-tap-like",
        "ig-open-post-url",
        "telegram-test-send",
        "screenshot",
        "ig-export-crash",
        "close-instagram",
    ],
}

DEBUG_TESTS: dict[str, DebugTest] = {
    "wake-unlock": {
        "label": "Wake & unlock screen",
        "kind": "wake_unlock",
    },
    "home": {
        "label": "Press home",
        "kind": "home",
    },
    "home-after-vpn": {
        "label": "Press home (after VPN)",
        "kind": "home",
    },
    "open-shadowrocket": {
        "label": "Tap Shadowrocket",
        "kind": "open_shadowrocket",
    },
    "wait-vpn-ui": {
        "label": "Wait for Connect or Stop",
        "kind": "wait_vpn_ui",
    },
    "tap-vpn-connect": {
        "label": "Tap Connect (if VPN off)",
        "kind": "tap_vpn_connect_if_needed",
    },
    "wait-vpn-connected": {
        "label": "Wait for VPN connected (Stop)",
        "kind": "wait_vpn_connected",
    },
    "open-instagram": {
        "label": "Open Instagram",
        "kind": "open_instagram",
    },
    "vpn-ensure": {
        "label": "Ensure VPN (full)",
        "kind": "vpn_ensure",
    },
    "vpn-then-instagram": {
        "label": "VPN on → open Instagram",
        "kind": "vpn_then_instagram",
    },
    "detect-vpn-connect": {
        "label": "Detect Connect button",
        "kind": "detect_vpn",
        "state": "connect",
    },
    "detect-vpn-stop": {
        "label": "Detect Stop (VPN on)",
        "kind": "detect_vpn",
        "state": "stop",
    },
    "close-instagram": {
        "label": "Close Instagram",
        "kind": "close_instagram",
    },
    "screenshot": {
        "label": "Capture screenshot",
        "kind": "screenshot",
    },
    "ig-close-keyboard": {
        "label": "Dismiss keyboard",
        "kind": "ig_close_keyboard",
    },
    "ig-tab-home": {
        "label": "Tab bar → Home",
        "kind": "ig_tab_home",
    },
    "ig-tab-search": {
        "label": "Tab bar → Search",
        "kind": "ig_tab_search",
    },
    "ig-tab-profile": {
        "label": "Tab bar → Profile",
        "kind": "ig_tab_profile",
    },
    "ig-profile-read-labels": {
        "label": "Read profile labels (posts / followers / following)",
        "kind": "ig_profile_read_labels",
    },
    "ig-search-open-user": {
        "label": "Search → open user profile",
        "kind": "ig_search_open_user",
        "needs_username": True,
    },
    "ig-profile-tap-followers": {
        "label": "Profile → tap Followers",
        "kind": "ig_profile_tap_followers",
    },
    "ig-followers-open-first": {
        "label": "Followers list → open first user",
        "kind": "ig_followers_open_first",
    },
    "ig-back": {
        "label": "Press back",
        "kind": "ig_back",
    },
    "ig-back-from-post": {
        "label": "Press back (from post)",
        "kind": "ig_back",
    },
    "ig-back-to-search": {
        "label": "Press back (to search)",
        "kind": "ig_back",
    },
    "ig-profile-swipe-posts": {
        "label": "Profile → swipe to posts grid",
        "kind": "ig_profile_swipe_posts",
    },
    "ig-profile-open-post": {
        "label": "Profile → open first post",
        "kind": "ig_profile_open_post",
    },
    "ig-post-detect-like": {
        "label": "Post → detect like button",
        "kind": "ig_post_detect_like",
    },
    "ig-post-tap-like": {
        "label": "Post → double-tap center to like",
        "kind": "ig_post_tap_like",
    },
    "ig-profile-detect-follow": {
        "label": "Profile → detect Follow / Following",
        "kind": "ig_profile_detect_follow",
    },
    "ig-profile-follow-vision": {
        "label": "Profile → follow vision (2 screenshots + OpenAI)",
        "kind": "ig_profile_follow_vision",
    },
    "ig-profile-tap-follow": {
        "label": "Profile → tap Follow",
        "kind": "ig_profile_tap_follow",
    },
    "ig-nav-blogger-followers": {
        "label": "Full nav → user's followers list",
        "kind": "ig_nav_blogger_followers",
        "needs_username": True,
    },
    "ig-export-crash": {
        "label": "Export UI crash dump",
        "kind": "ig_export_crash",
    },
    "ig-dismiss-popups": {
        "label": "Dismiss popups (crash, find people, Not Now)",
        "kind": "ig_dismiss_popups",
    },
    "ig-tab-reels": {
        "label": "Tab bar → Reels",
        "kind": "ig_tab_reels",
    },
    "ig-tab-activity": {
        "label": "Tab bar → Activity",
        "kind": "ig_tab_activity",
    },
    "ig-check-english": {
        "label": "Check profile labels are English",
        "kind": "ig_check_english",
    },
    "ig-profile-detect-private": {
        "label": "Profile → detect private account",
        "kind": "ig_profile_detect_private",
    },
    "ig-profile-tap-following": {
        "label": "Profile → tap Following",
        "kind": "ig_profile_tap_following",
    },
    "ig-following-open-first": {
        "label": "Following list → open first user",
        "kind": "ig_following_open_first",
    },
    "ig-followers-scroll": {
        "label": "Followers list → scroll down",
        "kind": "ig_followers_scroll",
    },
    "ig-profile-open-account-menu": {
        "label": "Profile → open account switcher menu",
        "kind": "ig_profile_open_account_menu",
    },
    "ig-account-switch-user": {
        "label": "Account menu → switch to phone's linked @username",
        "kind": "ig_account_switch_user",
    },
    "ig-profile-detect-story-ring": {
        "label": "Profile → detect story ring",
        "kind": "ig_profile_detect_story_ring",
    },
    "ig-profile-open-stories": {
        "label": "Profile → open stories",
        "kind": "ig_profile_open_stories",
    },
    "ig-story-detect-like": {
        "label": "Story → detect like button",
        "kind": "ig_story_detect_like",
    },
    "ig-back-from-stories": {
        "label": "Press back (from stories)",
        "kind": "ig_back",
    },
    "ig-back-to-profile": {
        "label": "Press back (to profile)",
        "kind": "ig_back",
    },
    "ig-back-to-followers": {
        "label": "Press back (to followers list)",
        "kind": "ig_back",
    },
    "ig-back-to-profile-2": {
        "label": "Press back (to profile)",
        "kind": "ig_back",
    },
    "ig-back-from-following": {
        "label": "Press back (from following)",
        "kind": "ig_back",
    },
    "ig-back-from-comments": {
        "label": "Press back (from comments)",
        "kind": "ig_back",
    },
    "ig-back-to-search": {
        "label": "Press back (to search)",
        "kind": "ig_back",
    },
    "ig-profile-detect-message": {
        "label": "Profile → detect Message button",
        "kind": "ig_profile_detect_message",
    },
    "ig-profile-open-message": {
        "label": "Profile → open message compose",
        "kind": "ig_profile_open_message",
    },
    "ig-search-open-hashtag": {
        "label": "Search → open hashtag",
        "kind": "ig_search_open_hashtag",
        "needs_search": True,
    },
    "ig-nav-hashtag-top": {
        "label": "Hashtag → open first top post",
        "kind": "ig_nav_hashtag_top",
        "needs_search": True,
    },
    "ig-nav-hashtag-recent": {
        "label": "Hashtag → open first recent post",
        "kind": "ig_nav_hashtag_recent",
        "needs_search": True,
    },
    "ig-feed-refresh": {
        "label": "Home feed → refresh (new-posts pill or pull-to-refresh)",
        "kind": "ig_feed_refresh",
    },
    "ig-feed-inspect-post": {
        "label": "Home feed → inspect current post (full diagnosis)",
        "kind": "ig_feed_inspect_post",
    },
    "ig-feed-detect-owner": {
        "label": "Home feed → detect post owner @username",
        "kind": "ig_feed_detect_owner",
    },
    "ig-feed-detect-caption": {
        "label": "Home feed → detect caption row (ROW_FEED_TEXT)",
        "kind": "ig_feed_detect_caption",
    },
    "ig-feed-likers-status": {
        "label": "Home feed → likers count / hidden likes?",
        "kind": "ig_feed_likers_status",
    },
    "ig-feed-already-liked": {
        "label": "Home feed → already liked this post?",
        "kind": "ig_feed_already_liked",
    },
    "ig-feed-media-type": {
        "label": "Home feed → photo / video / carousel?",
        "kind": "ig_feed_media_type",
    },
    "ig-feed-check-last-post": {
        "label": "Home feed → _check_if_last_post (caption dedup logic)",
        "kind": "ig_feed_check_last_post",
    },
    "ig-feed-detect-like": {
        "label": "Home feed → detect like button",
        "kind": "ig_feed_detect_like",
    },
    "ig-feed-tap-like": {
        "label": "Home feed → double-tap center to like",
        "kind": "ig_feed_tap_like",
    },
    "ig-feed-scroll-down": {
        "label": "Home feed → scroll down to next post (feed flick)",
        "kind": "ig_feed_scroll_down",
    },
    "ig-feed-swipe": {
        "label": "Home feed → pull-to-refresh swipe up",
        "kind": "ig_feed_swipe",
    },
    "ig-bpl-production-nav": {
        "label": "[Production] nav_to_post_likers → open first grid post",
        "kind": "ig_bpl_production_nav",
        "needs_username": True,
    },
    "ig-bpl-production-check-post": {
        "label": "[Production] _check_if_last_post (blogger-post-likers)",
        "kind": "ig_bpl_production_check_post",
        "needs_username": True,
    },
    "ig-bpl-production-likers-status": {
        "label": "[Production] likers_open_status (owner + current_job)",
        "kind": "ig_bpl_production_likers_status",
        "needs_username": True,
    },
    "ig-bpl-production-open-likers": {
        "label": "[Production] open_likers_container",
        "kind": "ig_bpl_production_open_likers",
        "needs_username": True,
    },
    "ig-bpl-production-verify-list": {
        "label": "[Production] _getListViewLikers (list loaded?)",
        "kind": "ig_bpl_production_verify_list",
    },
    "ig-bpl-production-full": {
        "label": "[Production] Full preflight (nav → check → likers → open → verify)",
        "kind": "ig_bpl_production_full",
        "needs_username": True,
    },
    "ig-post-detect-comment": {
        "label": "Post → detect comment button",
        "kind": "ig_post_detect_comment",
    },
    "ig-post-open-comments": {
        "label": "Post → open comments",
        "kind": "ig_post_open_comments",
    },
    "ig-nav-blogger-following": {
        "label": "Full nav → user's following list",
        "kind": "ig_nav_blogger_following",
        "needs_username": True,
    },
    "ig-nav-post-likers": {
        "label": "Full nav → first post → open likers list",
        "kind": "ig_nav_post_likers",
        "needs_username": True,
    },
    "ig-post-detect-likers": {
        "label": "Post → detect like button",
        "kind": "ig_post_detect_likers",
    },
    "ig-post-tap-likers": {
        "label": "Post → tap likers (like btn + offset)",
        "kind": "ig_post_tap_likers",
    },
    "ig-post-open-likers": {
        "label": "Post → open likers list",
        "kind": "ig_post_open_likers",
    },
    "ig-likers-open-first": {
        "label": "Likers list → open first user",
        "kind": "ig_likers_open_first",
    },
    "ig-back-from-liker-profile": {
        "label": "Press back (from liker profile)",
        "kind": "ig_back",
    },
    "ig-post-detect-carousel": {
        "label": "Post → detect carousel",
        "kind": "ig_post_detect_carousel",
    },
    "ig-post-swipe-carousel": {
        "label": "Post → swipe carousel next",
        "kind": "ig_post_swipe_carousel",
    },
    "ig-post-detect-video": {
        "label": "Post → detect video / reel",
        "kind": "ig_post_detect_video",
    },
    "ig-post-detect-photo-or-video": {
        "label": "Post → photo or video?",
        "kind": "ig_post_detect_photo_or_video",
    },
    "ig-post-start-video": {
        "label": "Post → tap play on video",
        "kind": "ig_post_start_video",
    },
    "ig-post-open-video": {
        "label": "Post → open video fullscreen",
        "kind": "ig_post_open_video",
    },
    "ig-post-tap-like-video": {
        "label": "Post → tap like on video",
        "kind": "ig_post_tap_like_video",
    },
    "ig-post-fullscreen-like-stay": {
        "label": "Video → like and stay in reel (no back)",
        "kind": "ig_post_fullscreen_like_stay",
    },
    "ig-post-fullscreen-detect-comment": {
        "label": "Fullscreen reel → detect comment button",
        "kind": "ig_post_fullscreen_detect_comment",
    },
    "ig-post-fullscreen-open-comments": {
        "label": "Fullscreen reel → open comments",
        "kind": "ig_post_fullscreen_open_comments",
    },
    "ig-post-fullscreen-send-comment": {
        "label": "Fullscreen reel → send comment",
        "kind": "ig_post_fullscreen_send_comment",
        "needs_test_message": True,
    },
    "ig-post-inline-detect": {
        "label": "Profile Posts → detect inline video",
        "kind": "ig_post_inline_detect",
    },
    "ig-post-inline-like": {
        "label": "Profile Posts → like inline video",
        "kind": "ig_post_inline_like",
    },
    "ig-post-inline-open-comments": {
        "label": "Profile Posts → open comments",
        "kind": "ig_post_inline_open_comments",
    },
    "ig-post-inline-send-comment": {
        "label": "Profile Posts → send comment",
        "kind": "ig_post_inline_send_comment",
        "needs_test_message": True,
    },
    "ig-post-fullscreen-detect": {
        "label": "Video → fullscreen? (like btn on right)",
        "kind": "ig_post_fullscreen_detect",
    },
    "ig-post-fullscreen-reveal-likes": {
        "label": "Fullscreen video → reveal hidden likes (tap 'likes')",
        "kind": "ig_post_fullscreen_reveal_likes",
    },
    "ig-post-fullscreen-likers": {
        "label": "Fullscreen video → open likers (below like btn)",
        "kind": "ig_post_fullscreen_likers",
    },
    "ig-profile-tap-mutual": {
        "label": "Profile → tap mutual friends",
        "kind": "ig_profile_tap_mutual",
    },
    "ig-profile-tab-posts": {
        "label": "Profile → Posts tab",
        "kind": "ig_profile_tab_posts",
    },
    "ig-profile-tab-reels": {
        "label": "Profile → Reels tab",
        "kind": "ig_profile_tab_reels",
    },
    "ig-nav-hashtag-likers-top": {
        "label": "Hashtag → open likers (top)",
        "kind": "ig_nav_hashtag_likers_top",
        "needs_search": True,
    },
    "ig-back-after-hashtag-likers": {
        "label": "Press back (after hashtag likers)",
        "kind": "ig_back",
    },
    "ig-own-following-list": {
        "label": "Own profile → open Following list",
        "kind": "ig_own_following_list",
    },
    "ig-unfollow-detect-row": {
        "label": "Following list → detect Following on list user",
        "kind": "ig_unfollow_detect_row",
    },
    "ig-unfollow-tap-first-row": {
        "label": "Following list → unfollow user from unfollow_list.txt",
        "kind": "ig_unfollow_tap_first_row",
    },
    "ig-unfollow-detect-profile": {
        "label": "Profile → detect Following (unfollow)",
        "kind": "ig_unfollow_detect_profile",
    },
    "ig-unfollow-tap-profile": {
        "label": "Profile → tap Following",
        "kind": "ig_unfollow_tap_profile",
    },
    "ig-unfollow-confirm": {
        "label": "Unfollow sheet → tap Unfollow",
        "kind": "ig_unfollow_confirm",
    },
    "ig-own-followers-list": {
        "label": "Own profile → open Followers list",
        "kind": "ig_own_followers_list",
    },
    "ig-remove-follower-search": {
        "label": "Followers list → search remove_list user",
        "kind": "ig_remove_follower_search",
    },
    "ig-remove-follower-detect": {
        "label": "Followers list → detect Remove (X if needed)",
        "kind": "ig_remove_follower_detect",
    },
    "ig-remove-follower-tap": {
        "label": "Followers list → remove user from remove_list.txt",
        "kind": "ig_remove_follower_tap",
    },
    "ig-story-tap-like": {
        "label": "Story → tap like",
        "kind": "ig_story_tap_like",
    },
    "ig-open-post-url": {
        "label": "Open Instagram post URL",
        "kind": "ig_open_post_url",
        "needs_post_url": True,
    },
    "telegram-test-send": {
        "label": "Send Telegram test message",
        "kind": "telegram_test_send",
    },
    "ig-post-reel-tap-create": {
        "label": "Reel → tap + create",
        "kind": "ig_post_reel_tap_create",
    },
    "ig-post-reel-clear-gallery": {
        "label": "Reel → clear phone gallery (ADB)",
        "kind": "ig_post_reel_clear_gallery",
    },
    "ig-post-reel-push-media": {
        "label": "Reel → push next video via ADB",
        "kind": "ig_post_reel_push_media",
    },
    "ig-post-reel-select-media": {
        "label": "Reel → select recent media (counter)",
        "kind": "ig_post_reel_select_media",
    },
    "ig-post-reel-tap-next-top": {
        "label": "Reel → tap Next (top right)",
        "kind": "ig_post_reel_tap_next_top",
    },
    "ig-post-reel-dismiss-popups": {
        "label": "Reel → dismiss popups (center ×3)",
        "kind": "ig_post_reel_dismiss_popups",
    },
    "ig-post-reel-tap-next-clips": {
        "label": "Reel → tap Next (clips bottom right)",
        "kind": "ig_post_reel_tap_next_clips",
    },
    "ig-post-reel-write-caption": {
        "label": "Reel → OpenAI caption + type",
        "kind": "ig_post_reel_write_caption",
    },
    "ig-post-reel-share": {
        "label": "Reel → tap Share",
        "kind": "ig_post_reel_share",
    },
    "ig-post-reel-full-session": {
        "label": "Reel → full post loop (posts-per-session)",
        "kind": "ig_post_reel_full_session",
    },
    "ig-post-send-comment": {
        "label": "Post → send comment",
        "kind": "ig_post_send_comment",
        "needs_test_message": True,
    },
    "ig-profile-send-pm": {
        "label": "Profile → send PM",
        "kind": "ig_profile_send_pm",
        "needs_test_message": True,
    },
}


IG = "com.instagram.android:id"

# What each debug step looks for on screen (resource IDs, text, content-desc).
DEBUG_KIND_DETECTS: dict[str, str] = {
    "wake_unlock": "Device screen lock state (wake + swipe unlock via uiautomator2)",
    "home": "Android HOME key press (no UI element)",
    "open_shadowrocket": "Home-screen app icon: textMatches VPN app name (default Shadowrocket)",
    "wait_vpn_ui": "contentDescriptionMatches ^Connect$ OR ^Stop$ (Shadowrocket VPN UI)",
    "tap_vpn_connect_if_needed": "contentDescriptionMatches ^Connect$ — skipped if ^Stop$ already visible",
    "wait_vpn_connected": "contentDescriptionMatches ^Stop$ (VPN connected indicator)",
    "detect_vpn:connect": "contentDescriptionMatches ^Connect$",
    "detect_vpn:stop": "contentDescriptionMatches ^Stop$",
    "vpn_ensure": "VPN app icon text → Connect/Stop desc → tap Connect if needed",
    "vpn_then_instagram": "Full VPN ensure flow, then app_start com.instagram.android",
    "open_instagram": "app_start com.instagram.android (monkey launch)",
    "close_instagram": "app_stop com.instagram.android",
    "screenshot": "Full device screenshot (no element lookup)",
    "ig_close_keyboard": "Dismiss soft keyboard (UniversalActions.close_keyboard)",
    "ig_tab_home": "Tab bar: descriptionMatches Home (Button/FrameLayout)",
    "ig_tab_search": "Tab bar: descriptionMatches Search and Explore — or Home action bar search",
    "ig_tab_profile": "Tab bar: descriptionMatches Profile — fallback desc Profile on BUTTON",
    "ig_tab_reels": "Tab bar: descriptionMatches Reels",
    "ig_tab_activity": "Tab bar: descriptionMatches Activity",
    "ig_profile_read_labels": (
        f"{IG}/row_profile_header_post_count_container → posts label\n"
        f"{IG}/row_profile_header_followers_container → followers label\n"
        f"{IG}/row_profile_header_following_container → following label"
    ),
    "ig_check_english": "Same profile header labels — must read posts / followers / following (English)",
    "ig_search_open_user": (
        f"Tab Search → {IG}/row_search_edit_text\n"
        f"Search result: {IG}/row_search_user_username textMatches @username"
    ),
    "ig_profile_tap_followers": (
        f"{IG}/row_profile_header_followers_container (tap)\n"
        f"OR {IG}/unified_follow_list_tab_layout child textContains Followers"
    ),
    "ig_profile_tap_following": (
        f"{IG}/row_profile_header_following_container (tap)\n"
        f"OR {IG}/unified_follow_list_tab_layout child textContains Following"
    ),
    "ig_followers_open_first": (
        f"{IG}/user_list_container rows\n"
        "Per row: child(1) → child(0) → child() username TextView (click_retry)"
    ),
    "ig_following_open_first": (
        f"{IG}/user_list_container rows\n"
        "Per row: child(1) → child(0) → child() username TextView (click_retry)"
    ),
    "ig_back": "Android BACK key (device.back())",
    "ig_profile_swipe_posts": "ProfileView.swipe_to_fit_posts() — scrolls to posts grid",
    "ig_profile_open_post": "PostsGridView.navigateToPost(0, 0) — first grid thumbnail in LIST",
    "ig_post_detect_like": (
        f"{IG}/media_container → {IG}/row_feed_button_like\n"
        "If hidden, scroll down ~20% and retry (up to 3 times)"
    ),
    "ig_post_tap_like": "double-tap random point near screen center (±8% jitter)",
    "ig_profile_detect_follow": (
        "clickable textMatches ^Follow$\n"
        "clickable textMatches ^Following|^Requested\n"
        "clickable textMatches ^Follow Back$"
    ),
    "ig_profile_follow_vision": (
        "Current screen must be an Instagram profile.\n"
        "1) Screenshot top of profile\n"
        "2) Scroll down ~40% → screenshot\n"
        "3) Scroll back up\n"
        "4) OpenAI Vision (follow_vision.yml + follow_vision_prompts.yml)\n"
        "Pass phrases: potential musician (615FILMS) or potential couple (YourLoveFilms)"
    ),
    "ig_profile_tap_follow": "clickable textMatches ^Follow$",
    "ig_nav_blogger_followers": "TabBar → Search → open @user profile → navigateToFollowers()",
    "ig_nav_blogger_following": "TabBar → Search → open @user profile → navigateToFollowing()",
    "ig_nav_post_likers": (
        "Search or Profile → first post → tap likers row → username list\n"
        f"{IG}/row_feed_textview_likes or {IG}/row_feed_like_count_facepile_stub"
    ),
    "ig_export_crash": "save_crash() — dumps UI hierarchy to GramAddict crashes folder",
    "ig_dismiss_popups": (
        "Crash dialog (check_if_crash_popup_is_there)\n"
        f"{IG}/find_people_dismiss_button\n"
        "textMatches Not [Nn]ow\n"
        f"{IG}/negative_button"
    ),
    "ig_profile_detect_private": (
        f"{IG}/private_profile_empty_state\n"
        f"OR {IG}/row_profile_header_empty_profile_notice_title\n"
        f"OR {IG}/row_profile_header_empty_profile_notice_container"
    ),
    "ig_followers_scroll": (
        f"{IG}/see_more_button (tap to load +25 real followers)\n"
        f"Stop at text 'Suggested for you' — rows below are not followers\n"
        f"{IG}/list className android.widget.ListView — fling DOWN if no See more"
    ),
    "ig_profile_open_account_menu": f"{IG}/action_bar_title_chevron (account switcher)",
    "ig_account_switch_user": (
        f"Farm → phones table @handle for this device (not Tools → Debug target)\n"
        f"{IG}/action_bar_title_chevron → {IG}/list\n"
        f"{IG}/row_user_textview textMatches @username (with or without @)"
    ),
    "ig_profile_detect_story_ring": f"{IG}/reel_ring on profile header",
    "ig_profile_open_stories": (
        f"{IG}/reel_ring (tap)\n"
        f"Then {IG}/reel_viewer_media_container OR {IG}/reel_viewer_title"
    ),
    "ig_story_detect_like": f"{IG}/toolbar_like_button",
    "ig_story_tap_like": f"{IG}/toolbar_like_button (tap)",
    "ig_search_open_hashtag": (
        f"SearchView.navigate_to_target(#tag, hashtag-posts-top)\n"
        f"{IG}/row_hashtag_textview_tag_name"
    ),
    "ig_nav_hashtag_top": f"Search → hashtag tab → {IG}/recycler_view first {IG}/image_button",
    "ig_nav_hashtag_recent": f"Search → hashtag Recent tab → {IG}/recycler_view first post",
    "ig_nav_hashtag_likers_top": "Hashtag top post → likers count > 1 → open likers",
    "ig_feed_refresh": (
        f"Home tab → {IG}/new_feed_pill (tap if shown)\n"
        "Else pull-to-refresh swipe up (_reload_page)"
    ),
    "ig_feed_inspect_post": (
        "All feed-job checks on the visible post:\n"
        f"  · {IG}/row_feed_photo_profile_name\n"
        f"  · {IG}/row_feed_text (caption)\n"
        f"  · {IG}/row_feed_button_like + likers\n"
        "  · media type, already liked, tags"
    ),
    "ig_feed_detect_owner": f"Home tab → {IG}/row_feed_photo_profile_name (PostsViewList._post_owner)",
    "ig_feed_detect_caption": (
        f"Home tab → IgTextLayoutView @text='username caption… more'\n"
        f"  · {IG}/clips_caption_component child content-desc (clips overlay)\n"
        f"  · IgTextLayoutView @text or 2nd Button child (classic feed)\n"
        f"  · fallback: {IG}/row_feed_text textStartsWith owner"
    ),
    "ig_feed_likers_status": (
        f"PostsViewList.likers_open_status()\n"
        f"  · {IG}/row_feed_button_like\n"
        "  · like count / facepile beside heart (hidden = skip in bot)"
    ),
    "ig_feed_already_liked": f"OpenedPostView._is_post_liked() on {IG}/row_feed_button_like",
    "ig_feed_media_type": (
        "PostsViewList.get_home_feed_media_type() after tap — "
        "clips overlay = REEL, else PHOTO"
    ),
    "ig_feed_check_last_post": (
        "PostsViewList._check_if_last_post('', feed)\n"
        "Returns same-post flag + caption used for dedup (may micro-scroll)"
    ),
    "ig_feed_detect_like": (
        f"Home tab → {IG}/row_feed_button_like\n"
        "If hidden below the image, scroll feed down ~20% and retry (up to 3 times)"
    ),
    "ig_feed_tap_like": (
        "Home tab → scroll down ~20% if like row hidden → double-tap near screen center (±8% jitter)"
    ),
    "ig_feed_scroll_down": (
        "In clips overlay: swipe up to next reel; otherwise scroll home feed down ~50%"
    ),
    "ig_feed_swipe": "Pull-to-refresh swipe UP (UniversalActions._swipe_points)",
    "ig_bpl_production_nav": (
        "Production nav_to_post_likers(device, @user, my_username):\n"
        "  · Search → profile (or own profile tab)\n"
        "  · PostsGridView.is_post_tappable → swipe profile only if needed\n"
        "  · PostsGridView.navigateToPost(0, 0)"
    ),
    "ig_bpl_production_check_post": (
        "Production PostsViewList._check_if_last_post('', blogger-post-likers)\n"
        "  · _post_owner(GET_NAME) — no home-feed prepare_home_feed_post"
    ),
    "ig_bpl_production_likers_status": (
        "Production PostsViewList.likers_open_status(\n"
        "  owner=@target, current_job='blogger-post-likers')"
    ),
    "ig_bpl_production_open_likers": (
        "Production PostsViewList.open_likers_container(tap_offset_x)"
    ),
    "ig_bpl_production_verify_list": (
        "Production OpenedPostView._getListViewLikers() after open_likers_container"
    ),
    "ig_bpl_production_full": (
        "Runs production preflight in one shot:\n"
        "nav_to_post_likers → _check_if_last_post → likers_open_status\n"
        "→ open_likers_container → _getListViewLikers"
    ),
    "ig_post_detect_comment": f"{IG}/row_feed_button_comment on open post",
    "ig_post_open_comments": (
        f"{IG}/row_feed_button_comment (tap)\n"
        f"Then {IG}/layout_comment_thread_edittext_multiline inside {IG}/edittext_container"
    ),
    "ig_post_detect_likers": (
        f"{IG}/row_feed_button_like below post media\n"
        "Number / facepile / “Liked by…” beside heart required (hidden likes = skip)"
    ),
    "ig_post_tap_likers": (
        f"{IG}/row_feed_button_like → tap N px to the right\n"
        f"Then {IG}/list with username rows"
    ),
    "ig_post_open_likers": (
        f"{IG}/row_feed_button_like → tap N px to the right\n"
        f"Then {IG}/list (OpenedPostView._getListViewLikers)"
    ),
    "ig_likers_open_first": (
        f"{IG}/user_list_container row\n"
        f"{IG}/row_user_primary_name username (click_retry)"
    ),
    "ig_post_detect_carousel": f"{IG}/carousel_media_group",
    "ig_post_swipe_carousel": f"{IG}/carousel_media_group — swipe to next slide",
    "ig_post_detect_video": f"{IG}/video_container present → video (or contentDescription VIDEO / REEL / IGTV)",
    "ig_post_detect_photo_or_video": (
        f"{IG}/video_container → video\n"
        f"{IG}/carousel_media_group → carousel\n"
        f"{IG}/media_container (no video_container) → photo"
    ),
    "ig_post_start_video": f"{IG}/view_play_button if present (modern IG autoplays — passes on {IG}/video_container)",
    "ig_post_open_video": f"{IG}/video_container present or tap {IG}/media_container to play (modern IG autoplays inline)",
    "ig_post_tap_like_video": "Video UFI stack like button (OpenedPostView.like_video)",
    "ig_post_fullscreen_like_stay": (
        "Production-like flow: open video → like_video → verify still fullscreen\n"
        f"and {IG}/comment_button (or Comment content-desc) is visible — no device.back()"
    ),
    "ig_post_fullscreen_detect_comment": (
        f"Fullscreen reel: {IG}/comment_button or contentDescription Comment\n"
        f"(OpenedPostView.find_comment_button)"
    ),
    "ig_post_fullscreen_open_comments": (
        f"Fullscreen reel: tap comment button → {IG}/layout_comment_thread_edittext"
    ),
    "ig_post_fullscreen_send_comment": (
        f"Fullscreen reel: comment button → compose → {IG}/layout_comment_thread_post_button_icon\n"
        f"Confirm {IG}/row_comment_textview_comment"
    ),
    "ig_post_inline_detect": (
        f"Profile Posts feed (not fullscreen): action bar Posts + {IG}/carousel_video_media_group\n"
        f"Scoped {IG}/row_feed_button_like in {IG}/row_feed_view_group_buttons below media"
    ),
    "ig_post_inline_like": (
        "Production inline path: OpenedPostView.like_post()\n"
        f"Heart in {IG}/row_feed_view_group_buttons below focused {IG}/carousel_video_media_group"
    ),
    "ig_post_inline_open_comments": (
        f"{IG}/row_feed_button_comment → {IG}/layout_comment_thread_edittext"
    ),
    "ig_post_inline_send_comment": (
        f"Send comment → back to Posts view → confirm inline IgTextLayoutView\n"
        f"(textContains username + message, not comment-thread time_ago row)"
    ),
    "ig_post_fullscreen_detect": (
        f"{IG}/like_button on the right half of the screen → fullscreen video\n"
        f"(or {IG}/clips_video_container / {IG}/video_container present)"
    ),
    "ig_post_fullscreen_reveal_likes": (
        f"Fullscreen video with the word 'likes' under {IG}/like_button → owner hid count\n"
        "Tap the 'likes' label to reveal the like count"
    ),
    "ig_post_fullscreen_likers": (
        f"{IG}/clips_video_container or {IG}/video_container → fullscreen video\n"
        f"{IG}/like_button → tap N px *below* the heart to open likers"
    ),
    "ig_profile_detect_message": "className Button|TextView text Message",
    "ig_profile_open_message": (
        "Message button (tap)\n"
        f"Then {IG}/row_thread_composer_edittext className EditText"
    ),
    "ig_profile_tap_mutual": f"{IG}/profile_header_follow_context_text (mutual friends link)",
    "ig_profile_tab_posts": f"{IG}/profile_tab_layout — contentDescription Grid View",
    "ig_profile_tab_reels": f"{IG}/profile_tab_layout — contentDescription Reels",
    "ig_own_following_list": "Own profile → navigateToFollowing()",
    "ig_own_followers_list": "Own profile → navigateToFollowers()",
    "ig_unfollow_detect_row": (
        f"Search Following for first username in unfollow_list.txt\n"
        f"Find {IG}/follow_list_username on that row"
    ),
    "ig_unfollow_tap_first_row": (
        "Search Following for unfollow_list.txt user → tap the name's first "
        "characters to open the profile (not the avatar/story) → unfollow "
        "(skips whitelist users)"
    ),
    "ig_unfollow_detect_profile": "clickable textMatches ^Following|^Requested",
    "ig_unfollow_tap_profile": "clickable textMatches ^Following|^Requested (tap)",
    "ig_unfollow_confirm": (
        f"{IG}/primary_button OR {IG}/follow_sheet_unfollow_row\n"
        "textMatches ^Unfollow$"
    ),
    "ig_remove_follower_search": (
        f"Followers list → {IG}/row_search_edit_text\n"
        "Search first username in remove_list.txt (partial query)"
    ),
    "ig_remove_follower_detect": (
        f"Find {IG}/follow_list_username row for remove_list.txt user\n"
        "Remove visible, or X (description ^Dismiss$) to reveal it"
    ),
    "ig_remove_follower_tap": (
        "remove_list.txt user → X (Dismiss) → action sheet Remove → confirm if shown"
    ),
    "telegram_test_send": (
        "Read telegram.yml for this phone's account → sendMessage test ping\n"
        "Requires telegram-api-token + telegram-chat-id in Reports tab"
    ),
    "ig_post_reel_tap_create": (
        f"Tap + create: {IG}/action_bar_buttons_container_left → ImageView\n"
        "Fallback: coordinate tap top-left"
    ),
    "ig_post_reel_clear_gallery": "ADB rm DCIM/Camera, Pictures, Movies, Download + media scan",
    "ig_post_reel_push_media": (
        "Push next video from account post_media/ (by media_selection_counter) via adb push"
    ),
    "ig_post_reel_select_media": (
        f"{IG}/gallery_grid_item_thumbnail — 1=newest, 2=second newest (post_reel_state.json counter)"
    ),
    "ig_post_reel_tap_next_top": f"{IG}/next_button_textview",
    "ig_post_reel_dismiss_popups": "Tap screen center 3× to dismiss overlays",
    "ig_post_reel_tap_next_clips": f"{IG}/clips_right_action_button",
    "ig_post_reel_write_caption": (
        f"{IG}/caption_input_text_view + OpenAI from post_reel_prompts.yml (615FILMS or YourLoveFilms batch)"
    ),
    "ig_post_reel_share": f"{IG}/share_button → wait for composer to close",
    "ig_post_reel_full_session": (
        "Loop posts-per-session from post_reel.yml: clear → push → + → select → next → caption → share; "
        "increment counter only after confirmed post"
    ),
    "ig_open_post_url": "adb am start VIEW instagram.com/p/… URL",
    "ig_post_send_comment": (
        f"{IG}/row_feed_button_comment → {IG}/layout_comment_thread_edittext\n"
        f"Type message → {IG}/layout_comment_thread_post_button_icon\n"
        f"Confirm {IG}/row_comment_textview_comment"
    ),
    "ig_profile_send_pm": (
        "Profile Message button\n"
        f"{IG}/row_thread_composer_edittext → {IG}/row_thread_composer_button_send or row_thread_composer_send_button_icon\n"
        "Confirm thread bubble with sent text"
    ),
}


def _detects_for_test(spec: DebugTest, test_id: str) -> str:
    if spec.get("detects"):
        return spec["detects"]
    kind = spec["kind"]
    if kind == "detect_vpn":
        state = spec.get("state", "connect")
        return DEBUG_KIND_DETECTS.get(f"detect_vpn:{state}", DEBUG_KIND_DETECTS.get(kind, ""))
    return DEBUG_KIND_DETECTS.get(kind, "")


def _device(serial: str) -> DeviceFacade:
    facade = _facade_cache.get(serial)
    if facade is not None:
        return facade
    from dashboard import device_service

    facade = DeviceFacade.__new__(DeviceFacade)
    facade.device_id = serial
    facade.app_id = DEFAULT_APP_ID
    facade.deviceV2 = device_service.connect(serial)
    try:
        facade.disable_auto_rotate()
    except Exception:
        pass
    _facade_cache[serial] = facade
    return facade


def release_device_facade(serial: str) -> None:
    _facade_cache.pop(serial, None)


def warmup_device_facade(serial: str) -> None:
    _device(serial)


def _normalize_username(value: Optional[str]) -> str:
    return (value or "").strip().lstrip("@")


def _require_target(target_username: Optional[str]) -> str:
    username = _normalize_username(target_username)
    if not username:
        raise HTTPException(
            status_code=400,
            detail="Set a target @username in Tools → Debug (needed for search / followers steps).",
        )
    return username


def _require_device_username(serial: str) -> str:
    """Instagram @handle linked to this phone in Farm (ignores debug target field)."""
    from dashboard.gramaddict_config import username_for_device

    username = _normalize_username(username_for_device(serial))
    if not username:
        raise HTTPException(
            status_code=400,
            detail=(
                "Set the Instagram @handle on this phone in Farm → phones table "
                "(click Set account on the device row)."
            ),
        )
    return username


def _require_search(target_search: Optional[str]) -> str:
    value = (target_search or "").strip().lstrip("#")
    if not value:
        raise HTTPException(
            status_code=400,
            detail="Set a hashtag in Tools → Debug (needed for hashtag search steps).",
        )
    return value


def _require_post_url(target_post_url: Optional[str]) -> str:
    url = (target_post_url or "").strip()
    if not url or "instagram.com" not in url:
        raise HTTPException(
            status_code=400,
            detail="Set an Instagram post URL in Tools → Debug (e.g. https://www.instagram.com/p/…).",
        )
    return url


def _require_test_message(test_message: Optional[str]) -> str:
    message = (test_message or "").strip()
    if not message:
        raise HTTPException(
            status_code=400,
            detail="Set a test comment / PM message in Tools → Debug.",
        )
    return message


def _first_user_list_row(device: DeviceFacade, ga_views) -> tuple[Any, str] | tuple[None, None]:
    ResourceID = ga_views.ResourceID
    user_list = device.find(
        resourceIdMatches=case_insensitive_re(ResourceID.USER_LIST_CONTAINER),
    )
    if not user_list.exists(Timeout.LONG):
        return None, None
    for item in user_list:
        user_info_view = item.child(index=1)
        user_name_view = user_info_view.child(index=0).child()
        if user_name_view.exists():
            return item, user_name_view.get_text()
    return None, None


def _device_username_optional(serial: str) -> str:
    from dashboard.gramaddict_config import username_for_device

    return _normalize_username(username_for_device(serial))


def _unfollow_practice_username_or_fail(
    serial: str,
    device: DeviceFacade,
    test_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    from dashboard.gramaddict_config import (
        UNFOLLOW_LIST_FILENAME,
        account_id_for_device,
        is_username_whitelisted_for_device,
        unfollow_practice_username_for_device,
    )

    account_id = account_id_for_device(serial)
    if not account_id:
        return None, _fail(
            serial,
            device,
            test_id,
            "No account linked to this device",
            target=(
                "Trying to detect:\n"
                "  · Assign this phone to an account on the Farm tab"
            ),
        )
    username = unfollow_practice_username_for_device(serial)
    if not username:
        return None, _fail(
            serial,
            device,
            test_id,
            f"No usernames in {UNFOLLOW_LIST_FILENAME}",
            target=(
                "Trying to detect:\n"
                f"  · Add at least one username to {UNFOLLOW_LIST_FILENAME}\n"
                "  · Lists tab in account settings (one per line, # for comments)"
            ),
        )
    normalized = _normalize_username(username)
    if is_username_whitelisted_for_device(serial, normalized):
        return None, _fail(
            serial,
            device,
            test_id,
            f"@{normalized} is in whitelist.txt — cannot unfollow",
            target=(
                "Trying to detect:\n"
                f"  · @{normalized} must not be in whitelist.txt for unfollow practice"
            ),
        )
    return normalized, None


def _remove_practice_username_or_fail(
    serial: str,
    device: DeviceFacade,
    test_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    from dashboard.gramaddict_config import (
        REMOVE_LIST_FILENAME,
        account_id_for_device,
        remove_practice_username_for_device,
    )

    account_id = account_id_for_device(serial)
    if not account_id:
        return None, _fail(
            serial,
            device,
            test_id,
            "No account linked to this device",
            target=(
                "Trying to detect:\n"
                "  · Assign this phone to an account on the Farm tab"
            ),
        )
    username = remove_practice_username_for_device(serial)
    if not username:
        return None, _fail(
            serial,
            device,
            test_id,
            f"No usernames in {REMOVE_LIST_FILENAME}",
            target=(
                "Trying to detect:\n"
                f"  · Add at least one follower to {REMOVE_LIST_FILENAME}\n"
                "  · Lists tab in account settings (one per line, # for comments)"
            ),
        )
    return _normalize_username(username), None


def _account_for_device_or_fail(
    serial: str,
    device: DeviceFacade,
    test_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    from dashboard.gramaddict_config import account_id_for_device

    account_id = account_id_for_device(serial)
    if not account_id:
        return None, _fail(
            serial,
            device,
            test_id,
            "No account linked to this device",
            target="Trying to detect:\n  · Assign this phone to an account on the Farm tab",
        )
    return account_id, None


def _search_followers_list(device: DeviceFacade, username: str) -> bool:
    from GramAddict.core.views import UniversalActions

    if not UniversalActions(device).search_text(username):
        return False
    UniversalActions.close_keyboard(device)
    random_sleep(1, 2, modulable=False)
    return True


def _find_following_row_for_username(
    device: DeviceFacade, ga_views, username: str
):
    ResourceID = ga_views.ResourceID
    target = _normalize_username(username)
    user_list = device.find(
        resourceIdMatches=case_insensitive_re(ResourceID.USER_LIST_CONTAINER),
    )
    if not user_list.exists(Timeout.LONG):
        return None
    for item in user_list:
        user_info_view = item.child(index=1)
        user_name_view = user_info_view.child(index=0).child()
        if not user_name_view.exists():
            continue
        row_username = _normalize_username(user_name_view.get_text())
        if row_username == target:
            return item
    return None


def _search_following_list(device: DeviceFacade, username: str) -> bool:
    from GramAddict.core.views import UniversalActions

    if not UniversalActions(device).search_text(username):
        return False
    UniversalActions.close_keyboard(device)
    random_sleep(1, 2, modulable=False)
    return True


def _open_first_user_in_list(
    device: DeviceFacade,
    serial: str,
    test_id: str,
    *,
    list_label: str,
    skip_username: Optional[str] = None,
    expand_followers: bool = False,
) -> dict[str, Any]:
    from GramAddict.core import views as ga_views
    from GramAddict.core.handle_sources import (
        expand_truncated_followers_list,
        item_at_or_below_y,
        suggested_for_you_top,
    )

    ResourceID = ga_views.ResourceID
    if expand_followers:
        expand_truncated_followers_list(device, ResourceID)
    list_rid = ResourceID.USER_LIST_CONTAINER
    user_list = device.find(
        resourceIdMatches=case_insensitive_re(list_rid),
    )
    if not user_list.exists(Timeout.LONG):
        return _fail(
            serial,
            device,
            test_id,
            f"{list_label} list not visible (USER_LIST_CONTAINER missing)",
            label=f"{list_label} list container",
            resourceIdMatches=case_insensitive_re(list_rid),
            flow=[
                f"resourceIdMatches USER_LIST_CONTAINER (`{list_rid}`)",
                "Per row: child(1) → child(0) → child() username TextView",
            ],
        )
    skip = _normalize_username(skip_username)
    suggested_top = suggested_for_you_top(device) if expand_followers else None
    for item in user_list:
        if item_at_or_below_y(item, suggested_top):
            break
        user_info_view = item.child(index=1)
        user_name_view = user_info_view.child(index=0).child()
        if not user_name_view.exists():
            continue
        username = user_name_view.get_text()
        if skip and _normalize_username(username).upper() == skip.upper():
            continue
        if user_name_view.click_retry():
            random_sleep(1, 2, modulable=False)
            return {
                "success": True,
                "message": f"Opened @{username}",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            f"Found @{username} but tap did not open profile",
            flow=[
                f"Username row for @{username} in USER_LIST_CONTAINER",
                "click_retry() on username TextView",
            ],
        )
    return _fail(
        serial,
        device,
        test_id,
        f"No {list_label.lower()} rows visible on screen",
        label=f"{list_label} list rows",
        resourceIdMatches=case_insensitive_re(list_rid),
        flow=["At least one USER_LIST_CONTAINER row with visible username"],
    )


def _find_feed_like_button(device: DeviceFacade, ga_views) -> Any:
    return ga_views.PostsViewList(device).find_feed_like_button()


def _prepare_home_feed(device: DeviceFacade, ga_views) -> bool:
    return ga_views.PostsViewList(device).prepare_home_feed_post()


def _feed_post_diagnosis(device: DeviceFacade, ga_views) -> tuple[bool, str, str]:
    """Snapshot of what the feed job sees on the current home-feed post."""
    from GramAddict.core.resources import ClassName

    ResourceID = ga_views.ResourceID
    Owner = ga_views.Owner
    pvl = ga_views.PostsViewList(device)
    opv = ga_views.OpenedPostView(device)
    lines: list[str] = []
    detect: list[str] = []

    if not pvl.prepare_home_feed_post():
        return (
            False,
            "Like button not visible — tap feed video did not open clips overlay.",
            "Feed job taps media → clips overlay (like_button / clips_author_username)",
        )

    if pvl._is_feed_clips_viewer():
        lines.append("ui_mode: clips/reel overlay (home-feed video after tap)")

    suggested = device.find(
        resourceIdMatches=ResourceID.NETEGO_CAROUSEL_HEADER,
    )
    if suggested.exists(Timeout.SHORT):
        lines.append(
            "feed_section: Suggested for you (not a followed-account post — bot usually scrolls past)"
        )

    username, is_ad, is_hashtag = pvl._post_owner("feed", Owner.GET_NAME)
    detect.append(
        f"{ResourceID.CLIPS_AUTHOR_USERNAME} or {ResourceID.ROW_FEED_PHOTO_PROFILE_NAME} → owner"
    )
    if not username:
        lines.append("owner: MISSING — bot cannot identify this post")
    else:
        flags = []
        if is_ad:
            flags.append("sponsored/ad")
        if is_hashtag:
            flags.append("hashtag row")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"owner: @{username}{suffix}")

    if username:
        caption_text = pvl._find_feed_caption_text(username)
        detect.append(
            f"{ResourceID.CLIPS_CAPTION_COMPONENT} child desc or IgTextLayoutView / {ResourceID.ROW_FEED_TEXT} → caption"
        )
        if caption_text:
            preview = caption_text.replace("\n", " ")
            preview = preview[:100] + ("…" if len(preview) > 100 else "")
            word_preview = ga_views.PostsViewList._feed_caption_preview(caption_text, 3)
            lines.append(f"caption: {preview!r} (first words: {word_preview!r})")
        else:
            lines.append(
                "caption: not found "
                f"({ResourceID.CLIPS_CAPTION_COMPONENT} nested desc or IgTextLayoutView / row_feed_text)"
            )

    header = device.find(resourceId=ResourceID.ROW_FEED_PROFILE_HEADER)
    detect.append(f"{ResourceID.ROW_FEED_PROFILE_HEADER} → profile header row")
    lines.append(
        f"profile_headers: {header.count_items() if header.exists(Timeout.SHORT) else 0} visible"
    )

    media = pvl._feed_media_view()
    gap = device.find(resourceIdMatches=ResourceID.GAP_VIEW_AND_FOOTER_SPACE)
    detect.append(f"{ResourceID.MEDIA_CONTAINER} → post media")
    detect.append(f"{ResourceID.GAP_VIEW} / footer → post boundary")
    lines.append(f"media_container: {'yes' if media.exists(Timeout.SHORT) else 'NO'}")
    lines.append(f"gap/footer: {'yes' if gap.exists(Timeout.SHORT) else 'NO'}")

    likers_line: Optional[str] = None
    try:
        can_open, liker_count = pvl.likers_open_status(username)
    except DeviceFacade.JsonRpcError as exc:
        can_open, liker_count = False, 0
        likers_line = f"likers: ERROR — {exc}"
    detect.append(
        f"{ResourceID.ROW_FEED_BUTTON_LIKE} + Button count / "
        f"{ResourceID.ROW_FEED_TEXTVIEW_LIKES} / facepile"
    )
    if likers_line:
        lines.append(likers_line)
    elif liker_count == LIKES_COUNT_HIDDEN:
        lines.append("likers: HIDDEN — bot will skip (no like count beside heart)")
    else:
        lines.append(f"likers: count={liker_count}, can_open_likers={can_open}")

    try:
        liked, _ = opv._is_post_liked()
    except DeviceFacade.JsonRpcError:
        liked = False
    lines.append(f"already_liked: {liked}")

    try:
        media_type = pvl.get_media_type()
    except DeviceFacade.JsonRpcError:
        media_type = None
    lines.append(f"media_type: {media_type.name if media_type else 'UNKNOWN'}")

    like_btn = (
        pvl._find_post_like_button_scoped(username)
        if username
        else pvl._find_post_like_button()
    )
    detect.append(f"{ResourceID.ROW_FEED_BUTTON_LIKE} → heart button")
    if like_btn.exists(Timeout.SHORT):
        lines.append(
            f"like_button: found, selected={like_btn.get_selected()}"
        )
    else:
        lines.append("like_button: NOT FOUND (scroll may reveal it)")

    has_tags = pvl._has_tags()
    lines.append(f"product_tags: {has_tags}")

    ok = bool(username) and not is_ad and not is_hashtag
    target = "Feed job checks on current home post:\n  · " + "\n  · ".join(detect)
    message = "\n".join(lines)
    return ok, message, target


def _likers_tap_offset(value: Optional[int]) -> int:
    if value is None:
        return DEFAULT_LIKERS_TAP_OFFSET_X
    try:
        return max(0, min(int(value), 500))
    except (TypeError, ValueError):
        return DEFAULT_LIKERS_TAP_OFFSET_X


def _likers_tap_offset_y(value: Optional[int]) -> int:
    if value is None:
        return DEFAULT_LIKERS_TAP_OFFSET_Y
    try:
        return max(0, min(int(value), 500))
    except (TypeError, ValueError):
        return DEFAULT_LIKERS_TAP_OFFSET_Y


LIKERS_HIDDEN_MSG = "Owner hid likes — no count beside the heart, likers list cannot be opened"
LIKERS_HIDDEN_TARGET = (
    "Trying to detect:\n"
    "  · row_feed_button_like visible\n"
    "  · No number / facepile / “Liked by…” beside the heart → likes hidden"
)


def _fail_likers_hidden(serial: str, device: DeviceFacade, test_id: str):
    return _fail(
        serial,
        device,
        test_id,
        LIKERS_HIDDEN_MSG,
        target=LIKERS_HIDDEN_TARGET,
    )


def _open_post_likers_list(
    device: DeviceFacade,
    ga_views,
    serial: str,
    test_id: str,
    *,
    require_multi_like: bool = False,
    tap_offset_x: int = DEFAULT_LIKERS_TAP_OFFSET_X,
):
    """Reveal like button, tap right of it, verify username list loaded. Returns (count, error_response)."""
    from GramAddict.core.views import OpenedPostView, PostsViewList

    post_view = PostsViewList(device)
    can_open, count = post_view.likers_open_status()
    if count == LIKES_COUNT_HIDDEN:
        return count, _fail_likers_hidden(serial, device, test_id)
    if not post_view._find_post_like_button().exists(Timeout.MEDIUM):
        return 0, _fail(
            serial,
            device,
            test_id,
            "Post like button not found",
            target=f"Trying to detect:\n  · {IG}/row_feed_button_like below post",
        )
    if require_multi_like and count == 1:
        return count, _fail(
            serial,
            device,
            test_id,
            "Post has only 1 like — Instagram does not open a likers list",
            target="Trying to detect:\n  · Post with 2+ likes (or “Liked by X and N others”)",
        )
    if not can_open:
        return count, _fail(
            serial,
            device,
            test_id,
            "Cannot open likers for this post",
            target=f"Trying to detect:\n  · Like count visible beside {IG}/row_feed_button_like",
        )
    coords = post_view.open_likers_container(tap_offset_x=tap_offset_x)
    random_sleep(1, 2, modulable=False)
    if OpenedPostView(device)._getListViewLikers() is None:
        coord_msg = f" ({coords[0]}, {coords[1]})" if coords else ""
        return count, _fail(
            serial,
            device,
            test_id,
            f"Likers list did not load after tap{coord_msg} (offset={tap_offset_x}px)",
            target=f"Trying to detect:\n  · Tap {IG}/row_feed_button_like + {tap_offset_x}px → android:id/list",
        )
    return count, None


def _describe_target(label: str, **kwargs: Any) -> str:
    """Human-readable description of a UIAutomator find() attempt."""
    lines = [f"Trying to detect: {label}"]
    for key in (
        "resourceId",
        "resourceIdMatches",
        "text",
        "textMatches",
        "textContains",
        "className",
        "classNameMatches",
        "description",
        "descriptionMatches",
        "clickable",
        "enabled",
        "index",
    ):
        if key in kwargs and kwargs[key] is not None:
            lines.append(f"  · {key} = {kwargs[key]!r}")
    return "\n".join(lines)


def _describe_flow(*steps: str) -> str:
    return "Trying to detect:\n" + "\n".join(f"  · {step}" for step in steps)


def _capture_failure(
    serial: str,
    device: DeviceFacade,
    message: str,
    test_id: str,
    *,
    target: str | None = None,
    save_dump: bool = True,
) -> dict[str, Any]:
    if save_dump:
        from GramAddict.core.utils import save_crash

        save_crash(device)
    image = None
    try:
        from dashboard import device_service

        image = device_service.get_screenshot(serial)
    except Exception:
        pass
    full_message = f"{message}\n\n{target}" if target else message
    return {
        "success": False,
        "message": full_message,
        "target": target,
        "test_id": test_id,
        "image": image,
    }


def _fail(
    serial: str,
    device: DeviceFacade,
    test_id: str,
    message: str,
    *,
    target: str | None = None,
    label: str | None = None,
    flow: list[str] | None = None,
    **find_kwargs: Any,
) -> dict[str, Any]:
    if target is None and flow:
        target = _describe_flow(*flow)
    elif target is None and find_kwargs:
        target = _describe_target(label or message, **find_kwargs)
    return _capture_failure(serial, device, message, test_id, target=target)


def _inline_profile_post_video_target() -> str:
    return (
        "Trying to detect:\n"
        f"  · action_bar_title Posts (profile Posts feed)\n"
        f"  · {IG}/carousel_video_media_group (inline video, not fullscreen)\n"
        f"  · {IG}/row_feed_button_like in {IG}/row_feed_view_group_buttons below media"
    )


def _require_inline_profile_post_video(
    serial: str,
    device: DeviceFacade,
    test_id: str,
    opened: Any,
    resource_id: Any,
) -> Optional[dict[str, Any]]:
    in_fullscreen, _ = opened._is_video_in_fullscreen()
    if in_fullscreen:
        return _fail(
            serial,
            device,
            test_id,
            "Video is fullscreen reel — use Reel comment debug flow instead",
            target=_inline_profile_post_video_target(),
        )
    posts_title = device.find(
        resourceId=resource_id.ACTION_BAR_TITLE,
        text="Posts",
    )
    if not posts_title.exists(Timeout.SHORT):
        posts_title = device.find(
            resourceId=resource_id.ACTION_BAR_TITLE,
            textMatches=case_insensitive_re(r"^Posts$"),
        )
    media = opened._get_focused_post_media()
    if media is None or not media.exists(Timeout.SHORT):
        return _fail(
            serial,
            device,
            test_id,
            "No focused inline video media — open a profile grid video first",
            target=_inline_profile_post_video_target(),
        )
    like_btn = opened._get_post_like_button()
    if like_btn is None or not like_btn.exists(Timeout.SHORT):
        return _fail(
            serial,
            device,
            test_id,
            "Scoped like button not found below focused media",
            target=_inline_profile_post_video_target(),
        )
    return None


def _open_instagram(device: DeviceFacade, serial: str) -> bool:
    pkg = device.app_id
    device.deviceV2.app_start(pkg, use_monkey=True)
    for _ in range(2):
        raise_if_cancelled(serial)
        if device.deviceV2.app_current().get("package") == pkg:
            return True
        time.sleep(0.35)
    return False


def _wait_for_vpn_ui_cancellable(
    device: DeviceFacade,
    serial: str,
    timeout: int = _DEBUG_VPN_WAIT_SECONDS,
) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(serial)
        if _find_by_description(device, VPN_STOP_DESC).exists(Timeout.SHORT):
            return "stop"
        if _find_by_description(device, VPN_CONNECT_DESC).exists(Timeout.SHORT):
            return "connect"
        time.sleep(0.2)
    return None


def _vpn_then_instagram(device: DeviceFacade, serial: str, vpn_app_name: str) -> tuple[bool, str]:
    if not ensure_shadowrocket_vpn(device, vpn_app_name):
        return False, "VPN check failed"
    if not _open_instagram(device, serial):
        return False, "VPN on but Instagram did not open"
    return True, "VPN on and Instagram opened"


def _open_shadowrocket(device: DeviceFacade, app_name: str) -> bool:
    import re

    app_icon = device.find_any(textMatches=case_insensitive_re(f"^{re.escape(app_name)}$"))
    if not app_icon.exists(Timeout.MEDIUM):
        app_icon = device.find_any(textMatches=case_insensitive_re(app_name))
    if not app_icon.exists(Timeout.MEDIUM):
        return False
    app_icon.click()
    _pause(0.25, 0.45)
    return True


def _detect_vpn_state(device: DeviceFacade, state: str) -> bool:
    pattern = VPN_CONNECT_DESC if state == "connect" else VPN_STOP_DESC
    return _find_by_description(device, pattern).exists(Timeout.SHORT)


def _ig_views():
    from GramAddict.core import views as ga_views
    from GramAddict.core.navigation import (
        nav_to_blogger,
        nav_to_hashtag_or_place,
        nav_to_post_likers,
    )

    return ga_views, nav_to_blogger, nav_to_hashtag_or_place, nav_to_post_likers


def list_debug_tests(group: Optional[str] = None) -> list[dict[str, Any]]:
    flow_group = (group or "flow").strip().lower()
    if flow_group not in DEBUG_GROUP_ORDER:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown debug group {flow_group!r}. Restart Device Lab — dashboard server is outdated.",
        )
    ordered_ids = DEBUG_GROUP_ORDER[flow_group]
    items: list[dict[str, Any]] = []
    for index, test_id in enumerate(ordered_ids):
        spec = DEBUG_TESTS.get(test_id)
        if not spec:
            continue
        item: dict[str, Any] = {
            "id": test_id,
            "label": f"{index + 1}. {spec['label']}",
            "step": index + 1,
            "detects": _detects_for_test(spec, test_id),
        }
        if spec.get("needs_username"):
            item["needs_username"] = "1"
        if spec.get("needs_search"):
            item["needs_search"] = "1"
        if spec.get("needs_post_url"):
            item["needs_post_url"] = "1"
        if spec.get("needs_test_message"):
            item["needs_test_message"] = "1"
        items.append(item)
    return items


def run_debug_test(
    serial: str,
    test_id: str,
    *,
    vpn_app_name: str = DEFAULT_APP_NAME,
    target_username: Optional[str] = None,
    target_search: Optional[str] = None,
    target_post_url: Optional[str] = None,
    test_message: Optional[str] = None,
    likers_tap_offset_x: Optional[int] = None,
    likers_tap_offset_y: Optional[int] = None,
    post_reel_posts_count: Optional[int] = None,
) -> dict[str, Any]:
    ensure_gramaddict_globals()
    spec = DEBUG_TESTS.get(test_id)
    if not spec:
        raise HTTPException(status_code=404, detail=f"Unknown debug test: {test_id}")

    device = _device(serial)
    kind = spec["kind"]
    likers_offset = _likers_tap_offset(likers_tap_offset_x)
    likers_offset_y = _likers_tap_offset_y(likers_tap_offset_y)
    raise_if_cancelled(serial)

    if kind == "home":
        device.deviceV2.press("home")
        _pause(0.1, 0.2)
        return {"success": True, "message": "Pressed home", "test_id": test_id}

    if kind == "wake_unlock":
        device.wake_up()
        if device.is_screen_locked():
            device.unlock()
        locked = device.is_screen_locked()
        return {
            "success": not locked,
            "message": "Screen woken and unlocked" if not locked else "Screen still locked",
            "test_id": test_id,
        }

    if kind == "vpn_ensure":
        ok = ensure_shadowrocket_vpn(device, vpn_app_name)
        return {
            "success": ok,
            "message": "VPN connected" if ok else "VPN check failed",
            "test_id": test_id,
        }

    if kind == "vpn_then_instagram":
        ok, message = _vpn_then_instagram(device, serial, vpn_app_name)
        return {"success": ok, "message": message, "test_id": test_id}

    if kind == "open_shadowrocket":
        ok = _open_shadowrocket(device, vpn_app_name)
        return {
            "success": ok,
            "message": f"Opened {vpn_app_name}" if ok else f"Could not find {vpn_app_name}",
            "test_id": test_id,
        }

    if kind == "wait_vpn_ui":
        state = _wait_for_vpn_ui_cancellable(device, serial)
        if state is None:
            return {
                "success": False,
                "message": "Timed out waiting for Connect or Stop",
                "test_id": test_id,
            }
        label = "Stop" if state == "stop" else "Connect"
        return {
            "success": True,
            "message": f"Found {label}",
            "test_id": test_id,
            "vpn_state": state,
        }

    if kind == "tap_vpn_connect_if_needed":
        if _detect_vpn_state(device, "stop"):
            return {
                "success": True,
                "message": "VPN already on — skipped Connect",
                "test_id": test_id,
                "skipped": True,
            }
        if not _detect_vpn_state(device, "connect"):
            return {
                "success": False,
                "message": "Connect not on screen",
                "test_id": test_id,
            }
        _find_by_description(device, VPN_CONNECT_DESC).click()
        _pause(0.35, 0.55)
        return {"success": True, "message": "Tapped Connect", "test_id": test_id}

    if kind == "wait_vpn_connected":
        if _detect_vpn_state(device, "stop"):
            return {"success": True, "message": "VPN connected (Stop visible)", "test_id": test_id}
        if _wait_for_vpn_ui_cancellable(device, serial) == "stop":
            return {"success": True, "message": "VPN connected (Stop appeared)", "test_id": test_id}
        return {"success": False, "message": "Stop never appeared", "test_id": test_id}

    if kind == "detect_vpn":
        state = spec.get("state", "connect")
        found = _detect_vpn_state(device, state)
        label = "Connect" if state == "connect" else "Stop"
        return {
            "success": found,
            "message": f"Found {label}" if found else f"{label} not on screen",
            "test_id": test_id,
        }

    if kind == "open_instagram":
        ok = _open_instagram(device, serial)
        return {
            "success": ok,
            "message": "Instagram opened" if ok else "Instagram did not open",
            "test_id": test_id,
        }

    if kind == "close_instagram":
        device.deviceV2.app_stop(device.app_id)
        random_sleep(2, 3, modulable=False)
        return {"success": True, "message": "Instagram closed", "test_id": test_id}

    if kind == "screenshot":
        from dashboard import device_service

        image = device_service.get_screenshot(serial)
        return {
            "success": True,
            "message": "Screenshot captured",
            "test_id": test_id,
            "image": image,
        }

    ga_views, nav_to_blogger, nav_to_hashtag_or_place, nav_to_post_likers = _ig_views()
    TabBarView = ga_views.TabBarView
    ProfileView = ga_views.ProfileView
    PostsGridView = ga_views.PostsGridView
    UniversalActions = ga_views.UniversalActions
    ResourceID = ga_views.ResourceID

    if kind == "ig_close_keyboard":
        UniversalActions.close_keyboard(device)
        random_sleep(0.5, 1, modulable=False)
        return {"success": True, "message": "Keyboard dismissed", "test_id": test_id}

    if kind == "ig_tab_home":
        TabBarView(device).navigateToHome()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Tapped Home tab", "test_id": test_id}

    if kind == "ig_tab_search":
        TabBarView(device).navigateToSearch()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Tapped Search tab", "test_id": test_id}

    if kind == "ig_tab_profile":
        TabBarView(device).navigateToProfile()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Tapped Profile tab", "test_id": test_id}

    if kind == "ig_profile_read_labels":
        post, followers, following = ProfileView(device, is_own_profile=True)._getSomeText()
        if None in {post, followers, following}:
            return _fail(serial, device, test_id, "Could not read posts / followers / following labels — UI may have changed", target="Trying to detect:\n  · profile_header_familiar_*_value / *_label (new header)\n  · ROW_PROFILE_HEADER_POST_COUNT_CONTAINER → post label\n  · ROW_PROFILE_HEADER_FOLLOWERS_CONTAINER → 'followers' label\n  · ROW_PROFILE_HEADER_FOLLOWING_CONTAINER → 'following' label")
        return {
            "success": True,
            "message": f"Read labels: {post!r}, {followers!r}, {following!r}",
            "test_id": test_id,
        }

    if kind == "ig_search_open_user":
        username = _require_target(target_username)
        search = TabBarView(device).navigateToSearch()
        if not search.navigate_to_target(username, "account"):
            return _fail(serial, device, test_id, f"Could not open @{username} from search", target="Trying to detect:\n  · SearchView.navigate_to_target(username, 'account')\n  · SEARCH_ROW_ITEM or ROW_SEARCH_USER_USERNAME")
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"Opened @{username} profile",
            "test_id": test_id,
        }

    if kind == "ig_profile_tap_followers":
        ok = ProfileView(device, is_own_profile=False).navigateToFollowers()
        if not ok:
            return _fail(serial, device, test_id, "Followers button or tab not found on profile", target="Trying to detect:\n  · ROW_PROFILE_HEADER_FOLLOWERS_CONTAINER (tap)\n  · UNIFIED_FOLLOW_LIST_TAB_LAYOUT child textContains 'Followers'")
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Opened followers list", "test_id": test_id}

    if kind == "ig_followers_open_first":
        return _open_first_user_in_list(
            device,
            serial,
            test_id,
            list_label="Followers",
            skip_username=_device_username_optional(serial),
            expand_followers=True,
        )

    if kind == "ig_back":
        device.back()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Pressed back", "test_id": test_id}

    if kind == "ig_profile_swipe_posts":
        ProfileView(device, is_own_profile=False).swipe_to_fit_posts()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Swiped profile to posts grid", "test_id": test_id}

    if kind == "ig_profile_open_post":
        opened, media_type, _ = PostsGridView(device).navigateToPost(0, 0)
        if opened is None:
            return _fail(serial, device, test_id, "Could not open first post on profile grid", target='Trying to detect:\n  · PostsGridView.navigateToPost(0, 0)\n  · LIST resource row 1 col 0 post thumbnail')
        random_sleep(1, 2, modulable=False)
        media = getattr(media_type, "name", str(media_type))
        return {
            "success": True,
            "message": f"Opened first post ({media})",
            "test_id": test_id,
        }

    if kind == "ig_post_detect_like":
        opened = ga_views.OpenedPostView(device)
        liked, like_btn = opened._is_post_liked()
        if like_btn is None:
            return _fail(serial, device, test_id, "Like button not found on open post", target='Trying to detect:\n  · MEDIA_CONTAINER → ROW_FEED_BUTTON_LIKE (OpenedPostView._get_post_like_button)\n  · Or double-tap MEDIA_CONTAINER for like')
        state = "liked" if liked else "not liked"
        return {
            "success": True,
            "message": f"Like button found ({state})",
            "test_id": test_id,
        }

    if kind == "ig_post_tap_like":
        opened = ga_views.OpenedPostView(device)
        x, y = device.double_tap_screen_center()
        random_sleep(0.5, 1, modulable=False)
        liked, _ = opened._is_post_liked()
        if liked:
            return {
                "success": True,
                "message": f"Post liked (double-tap at {x}, {y})",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            f"Double-tap at ({x}, {y}) did not register as liked",
            target="Trying to detect:\n  · ROW_FEED_BUTTON_LIKE selected state after screen-center double-tap",
        )

    if kind == "ig_profile_detect_follow":
        follow = device.find(
            clickable=True,
            textMatches=case_insensitive_re("^Follow$"),
        )
        following = device.find(
            clickable=True,
            textMatches=case_insensitive_re("^Following|^Requested"),
        )
        follow_back = device.find(
            clickable=True,
            textMatches=case_insensitive_re("^Follow Back$"),
        )
        if follow.exists(Timeout.SHORT):
            label = "Follow"
        elif follow_back.exists(Timeout.SHORT):
            label = "Follow Back"
        elif following.exists(Timeout.SHORT):
            label = following.get_text() or "Following/Requested"
        else:
            return _fail(serial, device, test_id, "No Follow / Following / Follow Back button on profile", target="Trying to detect:\n  · clickable textMatches '^Follow$'\n  · clickable textMatches '^Following|^Requested'\n  · clickable textMatches '^Follow Back$'")
        return {
            "success": True,
            "message": f"Found button: {label}",
            "test_id": test_id,
        }

    if kind == "ig_profile_follow_vision":
        from dashboard import device_service
        from GramAddict.core.follow_vision_account import (
            _openai_model,
            analyze_profile_images,
            capture_profile_vision_screenshots,
            get_account_follow_vision,
        )

        account_id, err = _account_for_device_or_fail(serial, device, test_id)
        if err:
            return err
        settings = get_account_follow_vision(account_id)
        batch = str(settings.get("prompt-batch") or "615FILMS")
        model = _openai_model(account_id)
        enabled = bool(settings.get("enabled"))
        try:
            images = capture_profile_vision_screenshots(device)
            passed, raw = analyze_profile_images(account_id, images, force=True)
        except Exception as exc:
            return _fail(
                serial,
                device,
                test_id,
                f"Follow vision check failed: {exc}",
                target=(
                    "Trying to detect:\n"
                    "  · Instagram profile on screen\n"
                    "  · follow_vision.yml / follow_vision_prompts.yml\n"
                    "  · openai-api-key in follow_vision.yml or post_reel.yml"
                ),
            )
        image = None
        try:
            image = device_service.get_screenshot(serial)
        except Exception:
            pass
        status = "PASS" if passed else "NO"
        enabled_note = "" if enabled else " (production filter is off — debug ran anyway)"
        return {
            "success": True,
            "message": (
                f"Follow vision {status}{enabled_note}\n"
                f"Batch: {batch} · Model: {model}\n"
                f"Response: {raw or '(empty)'}"
            ),
            "test_id": test_id,
            "image": image,
        }

    if kind == "ig_profile_tap_follow":
        follow = device.find(
            clickable=True,
            textMatches=case_insensitive_re("^Follow$"),
        )
        if not follow.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Follow button not found — already following or UI changed", target="Trying to detect:\n  · clickable textMatches '^Follow$'")
        follow.click()
        random_sleep(1, 2, modulable=False)
        following = device.find(
            clickable=True,
            textMatches=case_insensitive_re("^Following|^Requested"),
        )
        if following.exists(Timeout.SHORT):
            return {"success": True, "message": "Tapped Follow", "test_id": test_id}
        return _fail(serial, device, test_id, "Tapped Follow but state did not change to Following/Requested", target="Trying to detect:\n  · After tap Follow → textMatches '^Following|^Requested'")

    if kind == "ig_nav_blogger_followers":
        username = _require_target(target_username)
        if not nav_to_blogger(device, username, "blogger-followers"):
            return _fail(serial, device, test_id, f"nav_to_blogger failed for @{username} followers", target='Trying to detect:\n  · TabBar → Search → profile → navigateToFollowers/Following')
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"On @{username} followers list",
            "test_id": test_id,
        }

    if kind == "ig_nav_blogger_following":
        username = _require_target(target_username)
        if not nav_to_blogger(device, username, "blogger-following"):
            return _fail(serial, device, test_id, f"nav_to_blogger failed for @{username} following", target='Trying to detect:\n  · TabBar → Search → profile → navigateToFollowers/Following')
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"On @{username} following list",
            "test_id": test_id,
        }

    if kind == "ig_nav_post_likers":
        username = _require_target(target_username)
        my_username = _require_device_username(serial)
        if not nav_to_post_likers(device, username, my_username):
            return _fail(
                serial,
                device,
                test_id,
                f"Could not open first post for @{username}",
                target="Trying to detect:\n  · Search or Profile → posts grid → first post",
            )
        count, err = _open_post_likers_list(
            device,
            ga_views,
            serial,
            test_id,
            require_multi_like=True,
            tap_offset_x=likers_offset,
        )
        if err:
            return err
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"Opened likers list for @{username} first post (count={count})",
            "test_id": test_id,
        }

    if kind == "ig_dismiss_popups":
        from GramAddict.core.utils import check_if_crash_popup_is_there

        dismissed: list[str] = []
        if check_if_crash_popup_is_there(device):
            dismissed.append("crash dialog")
        find_people = device.find(resourceId=ResourceID.FIND_PEOPLE_DISMISS_BUTTON)
        if find_people.exists(Timeout.SHORT):
            find_people.click()
            dismissed.append("find people")
            random_sleep(0.5, 1, modulable=False)
        not_now = device.find(textMatches=case_insensitive_re("Not [Nn]ow"))
        if not_now.exists(Timeout.SHORT):
            not_now.click()
            dismissed.append("Not Now")
            random_sleep(0.5, 1, modulable=False)
        ok_btn = device.find(resourceIdMatches=case_insensitive_re(ResourceID.NEGATIVE_BUTTON))
        if ok_btn.exists(Timeout.SHORT):
            ok_btn.click()
            dismissed.append("dialog OK")
            random_sleep(0.5, 1, modulable=False)
        if dismissed:
            return {
                "success": True,
                "message": f"Dismissed: {', '.join(dismissed)}",
                "test_id": test_id,
            }
        return {
            "success": True,
            "message": "No known popups on screen",
            "test_id": test_id,
        }

    if kind == "ig_tab_reels":
        TabBarView(device).navigateToReels()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Tapped Reels tab", "test_id": test_id}

    if kind == "ig_tab_activity":
        TabBarView(device).navigateToActivity()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Tapped Activity tab", "test_id": test_id}

    if kind == "ig_check_english":
        post, followers, following = ProfileView(device, is_own_profile=True)._getSomeText()
        if None in {post, followers, following}:
            return _fail(serial, device, test_id, "Could not read profile labels for language check", target='Trying to detect:\n  · profile_header_familiar_*_value / *_label\n  · ROW_PROFILE_HEADER_POST_COUNT_CONTAINER / FOLLOWERS / FOLLOWING labels')
        if post == "posts" and followers == "followers" and following == "following":
            return {
                "success": True,
                "message": "Instagram profile labels are English",
                "test_id": test_id,
            }
        return _fail(serial, device, test_id, f"Labels not English: {post!r}, {followers!r}, {following!r}", target='Trying to detect:\n  · Profile labels must be posts / followers / following (English)')

    if kind == "ig_profile_detect_private":
        is_private = ProfileView(device, is_own_profile=False).isPrivateAccount()
        return {
            "success": True,
            "message": "Private account" if is_private else "Public account",
            "test_id": test_id,
        }

    if kind == "ig_profile_tap_following":
        ok = ProfileView(device, is_own_profile=False).navigateToFollowing()
        if not ok:
            return _fail(serial, device, test_id, "Following button or tab not found on profile", target="Trying to detect:\n  · ROW_PROFILE_HEADER_FOLLOWING_CONTAINER (tap)\n  · UNIFIED_FOLLOW_LIST_TAB_LAYOUT child textContains 'Following'")
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Opened following list", "test_id": test_id}

    if kind == "ig_following_open_first":
        return _open_first_user_in_list(
            device,
            serial,
            test_id,
            list_label="Following",
            skip_username=_device_username_optional(serial),
        )

    if kind == "ig_followers_scroll":
        from GramAddict.core.handle_sources import (
            advance_foreign_followers_list,
            seek_and_tap_see_more,
            truncated_followers_fully_shown,
        )
        from GramAddict.core.resources import ClassName

        if seek_and_tap_see_more(device, ResourceID, max_scroll_attempts=15):
            return {
                "success": True,
                "message": 'Tapped "See more" to load additional followers',
                "test_id": test_id,
            }

        if truncated_followers_fully_shown(device, ResourceID):
            return {
                "success": True,
                "message": 'At "Suggested for you" — no more See more button',
                "test_id": test_id,
            }

        list_view = device.find(
            resourceId=ResourceID.LIST,
            className=ClassName.LIST_VIEW,
        )
        if not list_view.exists(Timeout.MEDIUM):
            list_view = device.find(resourceIdMatches=case_insensitive_re(ResourceID.LIST))
        if not list_view.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Followers list not found",
                target="Trying to detect:\n  · see_more_button or text 'See more'\n  · resourceId LIST + className ListView",
            )

        advance = advance_foreign_followers_list(device, ResourceID, list_view)
        if advance == "expanded":
            return {
                "success": True,
                "message": 'Tapped "See more" after scrolling',
                "test_id": test_id,
            }
        if advance == "done":
            return {
                "success": True,
                "message": 'Reached "Suggested for you" — no See more',
                "test_id": test_id,
            }
        return {"success": True, "message": "Scrolled followers list one step", "test_id": test_id}

    if kind == "ig_profile_open_account_menu":
        selector = device.find(resourceId=ResourceID.ACTION_BAR_TITLE_CHEVRON)
        if not selector.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Account switcher chevron not found on profile", target='Trying to detect:\n  · ACTION_BAR_TITLE_CHEVRON')
        selector.click()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Opened account switcher", "test_id": test_id}

    if kind == "ig_account_switch_user":
        username = _require_device_username(serial)
        ok = ga_views.AccountView(device).changeToUsername(username)
        if not ok:
            return _fail(
                serial,
                device,
                test_id,
                f"Could not switch to @{username} (phone-linked account)",
                target=(
                    "Trying to detect:\n"
                    f"  · Farm → phones table @handle for this device → @{username}\n"
                    "  · ACTION_BAR_TITLE_CHEVRON → LIST → ROW_USER_TEXTVIEW username"
                ),
            )
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"Switched to @{username}",
            "test_id": test_id,
        }

    if kind == "ig_profile_detect_story_ring":
        profile = ProfileView(device, is_own_profile=False)
        if profile.live_marker().exists(Timeout.SHORT):
            return {
                "success": True,
                "message": "Live badge found (not stories)",
                "test_id": test_id,
            }
        ring = profile.StoryRing()
        if ring.exists(Timeout.MEDIUM):
            return {"success": True, "message": "Story ring found", "test_id": test_id}
        return _fail(serial, device, test_id, "No story ring on profile", target='Trying to detect:\n  · REEL_RING on profile header')

    if kind == "ig_profile_open_stories":
        profile = ProfileView(device, is_own_profile=False)
        ring = profile.StoryRing()
        if not ring.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Story ring not found — cannot open stories", target='Trying to detect:\n  · REEL_RING (tap to open story viewer)')
        ring.click()
        random_sleep(1, 2, modulable=False)
        story_view = ga_views.CurrentStoryView(device)
        frame = story_view.getStoryFrame()
        if not frame.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Story viewer did not open", target='Trying to detect:\n  · REEL_VIEWER_MEDIA_CONTAINER after story ring tap\n  · REEL_VIEWER_TITLE username')
        story_user = story_view.getUsername() or "unknown"
        return {
            "success": True,
            "message": f"Opened stories ({story_user})",
            "test_id": test_id,
        }

    if kind == "ig_story_detect_like":
        like_btn = device.find(resourceIdMatches=case_insensitive_re(ResourceID.TOOLBAR_LIKE_BUTTON))
        if not like_btn.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Story like button not found", target='Trying to detect:\n  · TOOLBAR_LIKE_BUTTON')
        state = "liked" if like_btn.get_selected() else "not liked"
        return {
            "success": True,
            "message": f"Story like button found ({state})",
            "test_id": test_id,
        }

    if kind == "ig_search_open_hashtag":
        tag = _require_search(target_search)
        search = TabBarView(device).navigateToSearch()
        if not search.navigate_to_target(tag, "hashtag-posts-top"):
            return _fail(serial, device, test_id, f"Could not open #{tag} from search", target="Trying to detect:\n  · SearchView.navigate_to_target(hashtag, 'hashtag-posts-top')")
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"Opened #{tag}",
            "test_id": test_id,
        }

    if kind == "ig_nav_hashtag_top":
        tag = _require_search(target_search)
        if not nav_to_hashtag_or_place(device, tag, "hashtag-posts-top"):
            return _fail(serial, device, test_id, f"nav_to_hashtag_or_place failed for #{tag} (top)", target='Trying to detect:\n  · Search → hashtag tab → RECYCLER_VIEW first IMAGE_BUTTON')
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"Opened first top post for #{tag}",
            "test_id": test_id,
        }

    if kind == "ig_nav_hashtag_recent":
        tag = _require_search(target_search)
        if not nav_to_hashtag_or_place(device, tag, "hashtag-posts-recent"):
            return _fail(serial, device, test_id, f"nav_to_hashtag_or_place failed for #{tag} (recent)", target='Trying to detect:\n  · Search → hashtag tab → RECYCLER_VIEW first IMAGE_BUTTON')
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"Opened first recent post for #{tag}",
            "test_id": test_id,
        }

    if kind == "ig_feed_refresh":
        TabBarView(device).navigateToHome()
        random_sleep(1, 2, modulable=False)
        ga_views.PostsViewList(device)._refresh_feed()
        return {
            "success": True,
            "message": "Feed refresh completed (pill tap or pull-to-refresh)",
            "test_id": test_id,
        }

    if kind == "ig_feed_inspect_post":
        try:
            ok, message, target = _feed_post_diagnosis(device, ga_views)
        except Exception as exc:
            return _fail(
                serial,
                device,
                test_id,
                str(exc),
                target="Feed inspect failed — see error above",
            )
        return {
            "success": True,
            "message": message + ("\n\n⚠ Some checks failed — see lines above." if not ok else ""),
            "test_id": test_id,
            "target": target,
        }

    if kind == "ig_feed_detect_owner":
        username, is_ad, is_hashtag = ga_views.PostsViewList(device)._post_owner(
            "feed", ga_views.Owner.GET_NAME
        )
        if not username:
            return _fail(
                serial,
                device,
                test_id,
                "Post owner not found on home feed",
                target=(
                    f"Trying to detect:\n"
                    f"  · {ResourceID.CLIPS_AUTHOR_USERNAME} (clips overlay after tap)\n"
                    f"  · {ResourceID.ROW_FEED_PHOTO_PROFILE_NAME} (classic photo post)"
                ),
            )
        flags = []
        if is_ad:
            flags.append("ad/sponsored")
        if is_hashtag:
            flags.append("hashtag row")
        extra = f" ({', '.join(flags)})" if flags else ""
        return {
            "success": True,
            "message": f"Owner @{username}{extra}",
            "test_id": test_id,
        }

    if kind == "ig_feed_detect_caption":
        from GramAddict.core.resources import ClassName

        pvl = ga_views.PostsViewList(device)
        username, is_ad, is_hashtag = pvl._post_owner("feed", ga_views.Owner.GET_NAME)
        if not username:
            return _fail(
                serial,
                device,
                test_id,
                "Need post owner first — owner not found",
                target=(
                    f"Trying to detect:\n"
                    f"  · {ResourceID.CLIPS_AUTHOR_USERNAME} (clips overlay after tap)\n"
                    f"  · {ResourceID.ROW_FEED_PHOTO_PROFILE_NAME} (classic feed)"
                ),
            )
        caption_text = pvl._find_feed_caption_text(username)
        if caption_text:
            preview = caption_text.replace("\n", " ")
            preview = preview[:120] + ("…" if len(preview) > 120 else "")
            dedup_key = ga_views.PostsViewList._feed_dedup_key(username, caption_text)
            return {
                "success": True,
                "message": f"Caption: {preview!r} (dedup key: {dedup_key!r})",
                "test_id": test_id,
                "target": (
                    f"Trying to detect:\n"
                    f"  · {ResourceID.CLIPS_CAPTION_COMPONENT} child content-desc (clips overlay)\n"
                    f"  · IgTextLayoutView @text starts with @{username}\n"
                    f"  · {ResourceID.ROW_FEED_TEXT} textStartsWith @{username}"
                ),
            }
        ig_layout = pvl._find_post_caption_layout(username)
        if ig_layout.exists(Timeout.SHORT):
            raw = pvl._combined_text_from_ig_layout(ig_layout)
            detail = (
                f"IgTextLayoutView found (@text={raw!r}) but caption not parsed"
            )
        else:
            detail = "No IgTextLayoutView with text starting with owner username"
        return {
            "success": True,
            "message": (
                f"No caption for @{username}. {detail} "
                "(bot falls back to owner + media for reels)"
            ),
            "test_id": test_id,
            "target": (
                f"Trying to detect:\n"
                f"  · {ResourceID.CLIPS_CAPTION_COMPONENT} child content-desc (clips overlay)\n"
                f"  · IgTextLayoutView @text starts with @{username}\n"
                f"  · {ResourceID.ROW_FEED_TEXT} textStartsWith @{username}"
            ),
        }

    if kind == "ig_feed_likers_status":
        pvl = ga_views.PostsViewList(device)
        username, _, _ = pvl._post_owner("feed", ga_views.Owner.GET_NAME)
        can_open, count = pvl.likers_open_status(username if username else None)
        if count == LIKES_COUNT_HIDDEN:
            return {
                "success": True,
                "message": "Likes HIDDEN — bot will skip this post (Scroll down to see next post)",
                "test_id": test_id,
                "target": LIKERS_HIDDEN_TARGET,
            }
        return {
            "success": True,
            "message": f"likers count={count}, can_open_likers={can_open}",
            "test_id": test_id,
        }

    if kind == "ig_feed_already_liked":
        if not _prepare_home_feed(device, ga_views):
            return _fail(
                serial,
                device,
                test_id,
                "Like button not visible after tapping feed media — cannot check liked state",
                target=f"Trying to detect:\n  · {ResourceID.ROW_FEED_BUTTON_LIKE} on home feed",
            )
        liked, _ = ga_views.OpenedPostView(device)._is_post_liked()
        return {
            "success": True,
            "message": "Post already liked" if liked else "Post not liked yet",
            "test_id": test_id,
        }

    if kind == "ig_feed_media_type":
        if not _prepare_home_feed(device, ga_views):
            return _fail(
                serial,
                device,
                test_id,
                "Like button not visible after tapping feed media — cannot detect media type",
                target=f"Trying to detect:\n  · {ResourceID.MEDIA_CONTAINER} after like row is visible",
            )
        media_type = ga_views.PostsViewList(device).get_home_feed_media_type()
        if media_type is None:
            return _fail(
                serial,
                device,
                test_id,
                "Could not detect media type on current feed post",
                target=(
                    f"Trying to detect:\n"
                    f"  · {ResourceID.CLIPS_ROOT_LAYOUT} / {ResourceID.CLIPS_AUTHOR_USERNAME} → REEL\n"
                    f"  · otherwise → PHOTO (classic feed row after tap)"
                ),
            )
        return {
            "success": True,
            "message": (
                f"Media type: {media_type.name} "
                f"({'clips overlay' if media_type == ga_views.MediaType.REEL else 'classic feed row'})"
            ),
            "test_id": test_id,
        }

    if kind == "ig_feed_check_last_post":
        (
            is_same,
            description,
            username,
            is_ad,
            is_hashtag,
            has_tags,
        ) = ga_views.PostsViewList(device)._check_if_last_post("", "feed")
        desc_preview = (
            (description[:80] + "…") if len(description) > 80 else description or "(empty)"
        )
        return {
            "success": True,
            "message": (
                f"same_as_last={is_same}, owner=@{username or '?'}, "
                f"ad={is_ad}, hashtag={is_hashtag}, tags={has_tags}, "
                f"caption={desc_preview!r}"
            ),
            "test_id": test_id,
        }

    if kind == "ig_feed_detect_like":
        pvl = ga_views.PostsViewList(device)
        username, _, _ = pvl._post_owner("feed", ga_views.Owner.GET_NAME)
        like = _find_feed_like_button(device, ga_views)
        if not like.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Feed like button not found on home", target='Trying to detect:\n  · ROW_FEED_BUTTON_LIKE on home feed (TabBar → Home)')
        state = "liked" if like.get_selected() else "not liked"
        return {
            "success": True,
            "message": f"Feed like button found ({state})",
            "test_id": test_id,
        }

    if kind == "ig_feed_tap_like":
        pvl = ga_views.PostsViewList(device)
        username, _, _ = pvl._post_owner("feed", ga_views.Owner.GET_NAME)
        like = _find_feed_like_button(device, ga_views)
        if like.exists(Timeout.SHORT) and like.get_selected():
            return {
                "success": True,
                "message": "Feed post already liked",
                "test_id": test_id,
            }
        x, y = device.double_tap_screen_center()
        random_sleep(0.5, 1, modulable=False)
        like = _find_feed_like_button(device, ga_views)
        if like.exists(Timeout.MEDIUM) and like.get_selected():
            return {
                "success": True,
                "message": f"Feed post liked (double-tap at {x}, {y})",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            f"Double-tap at ({x}, {y}) did not register as liked on feed",
            target="Trying to detect:\n  · ROW_FEED_BUTTON_LIKE selected state after screen-center double-tap",
        )

    if kind == "ig_feed_scroll_down":
        pvl = ga_views.PostsViewList(device)
        was_clips = pvl._is_feed_clips_viewer()
        pvl.swipe_to_fit_posts(ga_views.SwipeTo.NEXT_POST, home_feed=True)
        random_sleep(0.5, 1, modulable=False)
        msg = "Scrolled to next post (GramAddict NEXT_POST swipe)"
        if was_clips:
            msg = "Swiped to next reel in clips overlay"
        return {
            "success": True,
            "message": msg,
            "test_id": test_id,
        }

    if kind == "ig_feed_swipe":
        UniversalActions(device)._swipe_points(
            direction=Direction.UP,
            delta_y=450,
        )
        random_sleep(0.5, 1, modulable=False)
        return {"success": True, "message": "Swiped home feed up", "test_id": test_id}

    if kind == "ig_post_detect_comment":
        comment_btn = device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.ROW_FEED_BUTTON_COMMENT),
        )
        if not comment_btn.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Comment button not found on open post", target='Trying to detect:\n  · ROW_FEED_BUTTON_COMMENT on open post')
        return {
            "success": True,
            "message": "Comment button found",
            "test_id": test_id,
        }

    if kind == "ig_post_open_comments":
        from GramAddict.core.interaction import find_comment_thread_edittext

        comment_btn = device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.ROW_FEED_BUTTON_COMMENT),
        )
        if not comment_btn.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Comment button not found on open post", target='Trying to detect:\n  · ROW_FEED_BUTTON_COMMENT on open post')
        comment_btn.click()
        random_sleep(1, 2, modulable=False)
        comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Comments opened but compose box not found", target='Trying to detect:\n  · layout_comment_thread_edittext / edittext_container (enabled true or false)')
        return {
            "success": True,
            "message": "Comments opened (compose box visible)",
            "test_id": test_id,
        }

    if kind == "ig_profile_detect_message":
        from GramAddict.core.resources import ClassName

        message_button = device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            enabled=True,
            textMatches="Message",
        )
        if not message_button.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Message button not found on profile", target="Trying to detect:\n  · className BUTTON_OR_TEXTVIEW text 'Message'")
        return {
            "success": True,
            "message": "Message button found",
            "test_id": test_id,
        }

    if kind == "ig_profile_open_message":
        from GramAddict.core.resources import ClassName

        message_button = device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            enabled=True,
            textMatches="Message",
        )
        if not message_button.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Message button not found on profile", target="Trying to detect:\n  · className BUTTON_OR_TEXTVIEW text 'Message'")
        message_button.click()
        random_sleep(1, 2, modulable=False)
        message_box = device.find(
            resourceId=ResourceID.ROW_THREAD_COMPOSER_EDITTEXT,
            className=ClassName.EDIT_TEXT,
            enabled="true",
        )
        if not message_box.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Message compose box not found", target='Trying to detect:\n  · ROW_THREAD_COMPOSER_EDITTEXT className EditText')
        return {
            "success": True,
            "message": "Message compose opened (not sent)",
            "test_id": test_id,
        }

    if kind == "ig_post_detect_likers":
        post_view = ga_views.PostsViewList(device)
        like_btn = post_view._find_post_like_button()
        if not like_btn.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Post like button not found",
                target=f"Trying to detect:\n  · {IG}/row_feed_button_like below post media",
            )
        if post_view._owner_hid_likes_on_post():
            return _fail_likers_hidden(serial, device, test_id)
        has_likers, count = post_view._find_likers_container()
        count_msg = f" (likers count={count})" if has_likers else ""
        return {
            "success": True,
            "message": f"Like button and likers count visible{count_msg}",
            "test_id": test_id,
        }

    if kind == "ig_post_tap_likers":
        post_view = ga_views.PostsViewList(device)
        if post_view._owner_hid_likes_on_post():
            return _fail_likers_hidden(serial, device, test_id)
        coords = post_view._tap_likers_right_of_like_button(likers_offset)
        if coords is None:
            return _fail(
                serial,
                device,
                test_id,
                "Post like button not found",
                target=f"Trying to detect:\n  · {IG}/row_feed_button_like",
            )
        random_sleep(1, 2, modulable=False)
        likes_list = ga_views.OpenedPostView(device)._getListViewLikers()
        if likes_list is None:
            return _fail(
                serial,
                device,
                test_id,
                f"Tapped ({coords[0]}, {coords[1]}) with offset={likers_offset}px but likers list did not open",
                target=f"Trying to detect:\n  · Adjust “Likers tap offset” and retry",
            )
        return {
            "success": True,
            "message": f"Likers list opened — tapped ({coords[0]}, {coords[1]}) offset={likers_offset}px",
            "test_id": test_id,
        }

    if kind == "ig_post_open_likers":
        count, err = _open_post_likers_list(
            device,
            ga_views,
            serial,
            test_id,
            tap_offset_x=likers_offset,
        )
        if err:
            return err
        return {"success": True, "message": f"Likers list opened (count={count})", "test_id": test_id}

    if kind == "ig_likers_open_first":
        container = ga_views.OpenedPostView(device)._getUserContainer()
        if container is None:
            return _fail(serial, device, test_id, "Likers list not visible", target='Trying to detect:\n  · USER_LIST_CONTAINER in likers sheet')
        for item in container:
            username_view = ga_views.OpenedPostView(device)._getUserName(item)
            if not username_view.exists(Timeout.MEDIUM):
                continue
            username = username_view.get_text()
            if username_view.click_retry():
                random_sleep(1, 2, modulable=False)
                return {
                    "success": True,
                    "message": f"Opened liker @{username}",
                    "test_id": test_id,
                }
            return _fail(serial, device, test_id, f"Found @{username} but tap failed", target='Trying to detect:\n  · USER_LIST_CONTAINER username row click_retry')
        return _fail(serial, device, test_id, "No liker rows on screen", target='Trying to detect:\n  · USER_LIST_CONTAINER rows with ROW_USER_PRIMARY_NAME')

    if kind == "ig_post_detect_carousel":
        media_obj = device.find(resourceIdMatches=case_insensitive_re(ResourceID.CAROUSEL_MEDIA_GROUP))
        if media_obj.exists(Timeout.MEDIUM):
            return {"success": True, "message": "Carousel detected", "test_id": test_id}
        post_list = ga_views.PostsViewList(device)
        if post_list.detect_media_type_by_container() == ga_views.MediaType.CAROUSEL:
            return {"success": True, "message": "Carousel detected", "test_id": test_id}
        return _fail(serial, device, test_id, "No carousel on open post", target=f'Trying to detect:\n  · {IG}/carousel_media_group')

    if kind == "ig_post_swipe_carousel":
        media_obj = device.find(resourceIdMatches=case_insensitive_re(ResourceID.CAROUSEL_MEDIA_GROUP))
        if not media_obj.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Carousel not found to swipe", target='Trying to detect:\n  · CAROUSEL_MEDIA_GROUP')
        bounds = media_obj.get_bounds()
        mid_y = (bounds["bottom"] + bounds["top"]) / 2
        start_x = bounds["right"] * 5 / 6
        end_x = bounds["left"] + (bounds["right"] - bounds["left"]) * 0.25
        device.swipe_points(start_x, mid_y, end_x, mid_y)
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Swiped carousel", "test_id": test_id}

    if kind == "ig_post_detect_video":
        post_list = ga_views.PostsViewList(device)
        media_type = post_list.get_media_type()
        if media_type is None:
            return _fail(serial, device, test_id, "No media on open post", target=f'Trying to detect:\n  · {IG}/media_container contentDescription\n  · or {IG}/video_container')
        name = getattr(media_type, "name", str(media_type))
        if media_type in (
            ga_views.MediaType.VIDEO,
            ga_views.MediaType.REEL,
            ga_views.MediaType.IGTV,
        ):
            return {"success": True, "message": f"Video media: {name}", "test_id": test_id}
        return _fail(serial, device, test_id, f"Not a video post (type={name})", target=f'Trying to detect:\n  · MediaType VIDEO / REEL / IGTV\n  · via contentDescription or {IG}/video_container')

    if kind == "ig_post_detect_photo_or_video":
        post_list = ga_views.PostsViewList(device)
        media_type = post_list.get_media_type()
        if media_type is None:
            return _fail(
                serial,
                device,
                test_id,
                "No media container on open post",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/video_container → video\n"
                    f"  · {IG}/carousel_media_group → carousel\n"
                    f"  · {IG}/media_container → photo"
                ),
            )
        name = getattr(media_type, "name", str(media_type))
        is_video = media_type in (
            ga_views.MediaType.VIDEO,
            ga_views.MediaType.REEL,
            ga_views.MediaType.IGTV,
        )
        label = "VIDEO" if is_video else name
        return {
            "success": True,
            "message": f"This post is a {label}",
            "test_id": test_id,
        }

    if kind == "ig_post_start_video":
        opened = ga_views.OpenedPostView(device)
        if opened.start_video():
            return {"success": True, "message": "Play button tapped", "test_id": test_id}
        # Modern Instagram autoplays videos — no play button. Confirm it's a video and pass.
        media_type = ga_views.PostsViewList(device).get_media_type()
        if media_type in (
            ga_views.MediaType.VIDEO,
            ga_views.MediaType.REEL,
            ga_views.MediaType.IGTV,
        ):
            return {
                "success": True,
                "message": "Video autoplays (no play button in this Instagram version)",
                "test_id": test_id,
            }
        return _fail(serial, device, test_id, f"Not a video post (type={getattr(media_type, 'name', media_type)})", target=f'Trying to detect:\n  · {IG}/video_container (video autoplays, no play button)')

    if kind == "ig_post_open_video":
        opened = ga_views.OpenedPostView(device)
        if opened.open_video():
            return {"success": True, "message": "Video is playing", "test_id": test_id}
        media_type = ga_views.PostsViewList(device).get_media_type()
        if media_type in (
            ga_views.MediaType.VIDEO,
            ga_views.MediaType.REEL,
            ga_views.MediaType.IGTV,
        ):
            return {
                "success": True,
                "message": "Video present (autoplays inline)",
                "test_id": test_id,
            }
        return _fail(serial, device, test_id, f"No video to open (type={getattr(media_type, 'name', media_type)})", target=f'Trying to detect:\n  · {IG}/video_container (tap to play; modern IG autoplays inline)')

    if kind == "ig_post_tap_like_video":
        opened = ga_views.OpenedPostView(device)
        if opened.like_video():
            return {"success": True, "message": "Video liked", "test_id": test_id}
        return _fail(serial, device, test_id, "Video like failed", target='Trying to detect:\n  · UFI_STACK / video like button (OpenedPostView.like_video)')

    if kind == "ig_post_fullscreen_like_stay":
        from GramAddict.core.interaction import find_post_comment_button

        opened = ga_views.OpenedPostView(device)
        in_fullscreen, _ = opened._is_video_in_fullscreen()
        if not in_fullscreen:
            if not opened.open_video():
                return _fail(
                    serial,
                    device,
                    test_id,
                    "Video is not fullscreen — open a video post first",
                    target=(
                        "Trying to detect:\n"
                        f"  · {IG}/like_button on right half OR {IG}/video_container"
                    ),
                )
        if not opened.like_video():
            return _fail(
                serial,
                device,
                test_id,
                "Video like failed",
                target="Trying to detect:\n  · UFI_STACK / video like button",
            )
        still_fullscreen, _ = opened._is_video_in_fullscreen()
        if not still_fullscreen:
            return _fail(
                serial,
                device,
                test_id,
                "Left fullscreen after like — production should stay in reel view",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/like_button still on right half after like (no back())"
                ),
            )
        comment_btn = find_post_comment_button(device)
        if not comment_btn.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Still fullscreen but comment button not found",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/comment_button or contentDescription Comment"
                ),
            )
        return {
            "success": True,
            "message": "Liked video and stayed in reel view (comment button visible)",
            "test_id": test_id,
        }

    if kind == "ig_post_fullscreen_detect_comment":
        from GramAddict.core.interaction import find_post_comment_button

        opened = ga_views.OpenedPostView(device)
        in_fullscreen, _ = opened._is_video_in_fullscreen()
        if not in_fullscreen:
            return _fail(
                serial,
                device,
                test_id,
                "Not in fullscreen reel — open a video post first",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/like_button on right half (fullscreen video)"
                ),
            )
        comment_btn = find_post_comment_button(device)
        if not comment_btn.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Fullscreen comment button not found",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/comment_button or contentDescription Comment"
                ),
            )
        return {
            "success": True,
            "message": "Fullscreen comment button found",
            "test_id": test_id,
        }

    if kind == "ig_post_fullscreen_open_comments":
        from GramAddict.core.interaction import (
            find_comment_thread_edittext,
            find_post_comment_button,
        )

        opened = ga_views.OpenedPostView(device)
        in_fullscreen, media = opened._is_video_in_fullscreen()
        if not in_fullscreen:
            return _fail(
                serial,
                device,
                test_id,
                "Not in fullscreen reel — open a video post first",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/like_button on right half (fullscreen video)"
                ),
            )
        comment_btn = find_post_comment_button(device)
        if not comment_btn.exists(Timeout.MEDIUM) and media.exists():
            media.click()
            random_sleep(0.3, 0.6, modulable=False)
            comment_btn = find_post_comment_button(device)
        if not comment_btn.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Fullscreen comment button not found",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/comment_button or contentDescription Comment"
                ),
            )
        comment_btn.click()
        random_sleep(1, 2, modulable=False)
        comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Comments opened but compose box not found",
                target=(
                    "Trying to detect:\n"
                    "  · layout_comment_thread_edittext / edittext_container"
                ),
            )
        return {
            "success": True,
            "message": "Fullscreen comments opened (compose box visible)",
            "test_id": test_id,
        }

    if kind == "ig_post_fullscreen_send_comment":
        from GramAddict.core import utils as ga_utils
        from GramAddict.core.interaction import (
            find_comment_thread_edittext,
            find_post_comment_button,
        )

        message = _require_test_message(test_message)
        opened = ga_views.OpenedPostView(device)
        in_fullscreen, media = opened._is_video_in_fullscreen()
        if not in_fullscreen:
            return _fail(
                serial,
                device,
                test_id,
                "Not in fullscreen reel — open a video post first",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/like_button on right half (fullscreen video)"
                ),
            )
        comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.SHORT):
            comment_btn = find_post_comment_button(device)
            if not comment_btn.exists(Timeout.MEDIUM) and media.exists():
                media.click()
                random_sleep(0.3, 0.6, modulable=False)
                comment_btn = find_post_comment_button(device)
            if not comment_btn.exists(Timeout.MEDIUM):
                return _fail(
                    serial,
                    device,
                    test_id,
                    "Fullscreen comment button not found",
                    target=(
                        "Trying to detect:\n"
                        f"  · {IG}/comment_button or contentDescription Comment"
                    ),
                )
            comment_btn.click()
            random_sleep(1, 2, modulable=False)
            comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Comment compose box not found or comments limited",
                target=(
                    "Trying to detect:\n"
                    "  · layout_comment_thread_edittext or edittext_container"
                ),
            )
        comment_box.set_text(
            message,
            Mode.PASTE if ga_utils.args.dont_type else Mode.TYPE,
        )
        post_button = device.find(
            resourceId=ResourceID.LAYOUT_COMMENT_THREAD_POST_BUTTON_ICON,
        )
        if not post_button.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Comment Post button not found",
                target="Trying to detect:\n  · layout_comment_thread_post_button_icon",
            )
        post_button.click()
        random_sleep(1, 2, modulable=False)
        UniversalActions.detect_block(device)
        UniversalActions.close_keyboard(device)
        posted = device.find(textContains=message[: min(len(message), 40)])
        if not posted.exists(Timeout.MEDIUM):
            posted = device.find(
                resourceId=ResourceID.ROW_COMMENT_TEXTVIEW_COMMENT,
                textContains=message[: min(len(message), 40)],
            )
        if posted.exists(Timeout.SHORT):
            return {
                "success": True,
                "message": "Fullscreen reel comment sent",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            "Tapped Post but could not confirm comment appeared",
            target="Trying to detect:\n  · ROW_COMMENT_TEXTVIEW_COMMENT with posted text",
        )

    if kind == "ig_post_inline_detect":
        opened = ga_views.OpenedPostView(device)
        fail = _require_inline_profile_post_video(
            serial, device, test_id, opened, ResourceID
        )
        if fail:
            return fail
        media = opened._get_focused_post_media()
        like_btn = opened._get_post_like_button()
        media_desc = ""
        if media is not None and media.exists(Timeout.ZERO):
            try:
                media_desc = (media.get_desc() or "")[:80]
            except Exception:
                media_desc = ""
        return {
            "success": True,
            "message": (
                "Inline profile Posts video detected"
                + (f" ({media_desc})" if media_desc else "")
            ),
            "test_id": test_id,
            "details": {
                "like_bounds": like_btn.get_bounds() if like_btn else None,
            },
        }

    if kind == "ig_post_inline_like":
        opened = ga_views.OpenedPostView(device)
        fail = _require_inline_profile_post_video(
            serial, device, test_id, opened, ResourceID
        )
        if fail:
            return fail
        already_liked, _ = opened._is_post_liked()
        if already_liked:
            return {
                "success": True,
                "message": "Post already liked",
                "test_id": test_id,
            }
        if opened.like_post():
            return {
                "success": True,
                "message": "Inline video liked (OpenedPostView.like_post)",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            "Inline like failed",
            target=_inline_profile_post_video_target(),
        )

    if kind == "ig_post_inline_open_comments":
        from GramAddict.core.interaction import find_comment_thread_edittext

        opened = ga_views.OpenedPostView(device)
        fail = _require_inline_profile_post_video(
            serial, device, test_id, opened, ResourceID
        )
        if fail:
            return fail
        comment_btn = device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.ROW_FEED_BUTTON_COMMENT),
        )
        if not comment_btn.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "row_feed_button_comment not found on inline post",
                target=f"Trying to detect:\n  · {IG}/row_feed_button_comment",
            )
        comment_btn.click()
        random_sleep(1, 2, modulable=False)
        comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Comments opened but compose box not found",
                target=(
                    "Trying to detect:\n"
                    "  · layout_comment_thread_edittext / edittext_container"
                ),
            )
        return {
            "success": True,
            "message": "Inline post comments opened (compose box visible)",
            "test_id": test_id,
        }

    if kind == "ig_post_inline_send_comment":
        from GramAddict.core import utils as ga_utils
        from GramAddict.core.interaction import (
            _confirm_comment_posted,
            find_comment_thread_edittext,
        )

        message = _require_test_message(test_message)
        opened = ga_views.OpenedPostView(device)
        fail = _require_inline_profile_post_video(
            serial, device, test_id, opened, ResourceID
        )
        if fail:
            return fail
        username = _require_device_username(serial)
        comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.SHORT):
            comment_btn = device.find(
                resourceIdMatches=case_insensitive_re(ResourceID.ROW_FEED_BUTTON_COMMENT),
            )
            if not comment_btn.exists(Timeout.MEDIUM):
                return _fail(
                    serial,
                    device,
                    test_id,
                    "Comment button not found on inline post",
                    target=f"Trying to detect:\n  · {IG}/row_feed_button_comment",
                )
            comment_btn.click()
            random_sleep(1, 2, modulable=False)
            comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Comment compose box not found or comments limited",
                target=(
                    "Trying to detect:\n"
                    "  · layout_comment_thread_edittext or edittext_container"
                ),
            )
        comment_box.set_text(
            message,
            Mode.PASTE if ga_utils.args.dont_type else Mode.TYPE,
        )
        post_button = device.find(
            resourceId=ResourceID.LAYOUT_COMMENT_THREAD_POST_BUTTON_ICON,
        )
        if not post_button.exists(Timeout.MEDIUM):
            return _fail(
                serial,
                device,
                test_id,
                "Comment Post button not found",
                target="Trying to detect:\n  · layout_comment_thread_post_button_icon",
            )
        post_button.click()
        random_sleep(1, 2, modulable=False)
        UniversalActions.detect_block(device)
        UniversalActions.close_keyboard(device)
        device.back()
        random_sleep(0.5, 1, modulable=False)
        if _confirm_comment_posted(device, username, message):
            return {
                "success": True,
                "message": "Inline profile post comment sent and verified",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            "Comment posted but inline verification failed",
            target=(
                "Trying to detect:\n"
                f"  · IgTextLayoutView textContains '{username} {message[:40]}'"
            ),
        )

    if kind == "ig_post_fullscreen_detect":
        opened = ga_views.OpenedPostView(device)
        on_right = opened._like_button_on_right_side()
        in_fullscreen, _ = opened._is_video_in_fullscreen()
        if in_fullscreen:
            how = "like button on right side" if on_right else "video container present"
            return {
                "success": True,
                "message": f"Video is fullscreen ({how})",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            "Video is not fullscreen (like button not on right side)",
            target=(
                "Trying to detect:\n"
                f"  · {IG}/like_button center on right half of screen → fullscreen\n"
                f"  · or {IG}/clips_video_container / {IG}/video_container"
            ),
        )

    if kind == "ig_post_fullscreen_reveal_likes":
        opened = ga_views.OpenedPostView(device)
        in_fullscreen, _ = opened._is_video_in_fullscreen()
        if not in_fullscreen:
            return _fail(
                serial,
                device,
                test_id,
                "Video is not fullscreen — reveal-likes only works fullscreen",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/like_button on right side (fullscreen video)"
                ),
            )
        if not opened.fullscreen_likes_hidden():
            return {
                "success": True,
                "message": "Likes are not hidden (no 'likes' word under the heart)",
                "test_id": test_id,
            }
        if opened.reveal_fullscreen_hidden_likes():
            random_sleep(1, 2, modulable=False)
            return {
                "success": True,
                "message": "Tapped 'likes' to reveal hidden like count",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            "Could not tap 'likes' label to reveal count",
            target=(
                "Trying to detect:\n"
                f"  · 'likes' text under {IG}/like_button (fullscreen hidden likes)"
            ),
        )

    if kind == "ig_post_fullscreen_likers":
        opened = ga_views.OpenedPostView(device)
        in_fullscreen, _ = opened._is_video_in_fullscreen()
        if not in_fullscreen:
            return _fail(
                serial,
                device,
                test_id,
                "Video is not in fullscreen — likers are beside the heart (use feed likers flow)",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/clips_video_container or {IG}/video_container (fullscreen video)"
                ),
            )
        coords = opened.open_likers_fullscreen(tap_offset_y=likers_offset_y)
        if coords is None:
            return _fail(
                serial,
                device,
                test_id,
                "Fullscreen like button not found — could not open likers",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/like_button, then tap {likers_offset_y}px below it"
                ),
            )
        random_sleep(1, 2, modulable=False)
        x, y = coords
        return {
            "success": True,
            "message": f"Tapped likers {likers_offset_y}px below fullscreen like button ({x}, {y})",
            "test_id": test_id,
        }

    if kind == "ig_profile_tap_mutual":
        if ProfileView(device, is_own_profile=False).navigateToMutual():
            random_sleep(1, 2, modulable=False)
            return {"success": True, "message": "Opened mutual friends", "test_id": test_id}
        return _fail(serial, device, test_id, "Mutual friends link not found on profile", target='Trying to detect:\n  · PROFILE_HEADER_FOLLOW_CONTEXT_TEXT (navigateToMutual)')

    if kind == "ig_profile_tab_posts":
        ProfileView(device, is_own_profile=False).navigateToPostsTab()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Tapped Posts tab", "test_id": test_id}

    if kind == "ig_profile_tab_reels":
        from GramAddict.core.resources import TabBarText

        ProfileView(device, is_own_profile=False)._navigateToTab(TabBarText.REELS_CONTENT_DESC)
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Tapped Reels tab on profile", "test_id": test_id}

    if kind == "ig_nav_hashtag_likers_top":
        tag = _require_search(target_search)
        if not nav_to_hashtag_or_place(device, tag, "hashtag-likers-top"):
            return _fail(serial, device, test_id, f"nav_to_hashtag_or_place failed for #{tag} likers", target='Trying to detect:\n  · Search → hashtag tab → RECYCLER_VIEW first IMAGE_BUTTON')
        has_likers, count = ga_views.PostsViewList(device)._find_likers_container()
        if count == LIKES_COUNT_HIDDEN:
            return _fail_likers_hidden(serial, device, test_id)
        if not has_likers or count == 1:
            return _fail(serial, device, test_id, f"No likers to open on hashtag post (count={count})", target='Trying to detect:\n  · Hashtag post with likers count > 1')
        ga_views.PostsViewList(device).open_likers_container(tap_offset_x=likers_offset)
        random_sleep(1, 2, modulable=False)
        return {
            "success": True,
            "message": f"Opened likers for #{tag} (count={count})",
            "test_id": test_id,
        }

    if kind == "ig_own_following_list":
        TabBarView(device).navigateToProfile()
        ProfileView(device, is_own_profile=True).navigateToFollowing()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "On own Following list", "test_id": test_id}

    if kind == "ig_unfollow_detect_row":
        username, err = _unfollow_practice_username_or_fail(serial, device, test_id)
        if err:
            return err
        if not _search_following_list(device, username):
            return _fail(
                serial,
                device,
                test_id,
                f"Could not search Following list for @{username}",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/row_search_edit_text (UniversalActions.search_text)\n"
                    f"  · Username from unfollow_list.txt: @{username}"
                ),
            )
        row = _find_following_row_for_username(device, ga_views, username)
        if row is None:
            return _fail(
                serial,
                device,
                test_id,
                f"@{username} not found in Following list",
                target=(
                    "Trying to detect:\n"
                    f"  · USER_LIST_CONTAINER row for @{username}\n"
                    "  · Add someone you follow to unfollow_list.txt"
                ),
            )
        name_view = ga_views.FollowingView(device)._find_row_username_view(row)
        if name_view is not None:
            return {
                "success": True,
                "message": f"Found @{username} row (will tap name to open profile)",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            f"Username text not found on row for @{username}",
            target=f"Trying to detect:\n  · {IG}/follow_list_username on the @{username} row",
        )

    if kind == "ig_unfollow_tap_first_row":
        username, err = _unfollow_practice_username_or_fail(serial, device, test_id)
        if err:
            return err
        if not _search_following_list(device, username):
            return _fail(
                serial,
                device,
                test_id,
                f"Could not search Following list for @{username}",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/row_search_edit_text\n"
                    f"  · Username from unfollow_list.txt: @{username}"
                ),
            )
        row = _find_following_row_for_username(device, ga_views, username)
        if row is None:
            return _fail(
                serial,
                device,
                test_id,
                f"@{username} not found in Following list",
                target=(
                    "Trying to detect:\n"
                    f"  · USER_LIST_CONTAINER row for @{username}"
                ),
            )
        if ga_views.FollowingView(device).do_unfollow_from_list(username, user_row=row):
            return {
                "success": True,
                "message": f"Unfollowed @{username} from list",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            f"Unfollow failed for @{username}",
            target="Trying to detect:\n  · FollowingView.do_unfollow_from_list",
        )

    if kind == "ig_unfollow_detect_profile":
        from GramAddict.core.resources import ClassName

        btn = device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            clickable=True,
            textMatches=case_insensitive_re("^Following|^Requested"),
        )
        if btn.exists(Timeout.MEDIUM):
            return {
                "success": True,
                "message": f"Found {btn.get_text() or 'Following'} button",
                "test_id": test_id,
            }
        return _fail(serial, device, test_id, "Following / Requested button not on profile", target="Trying to detect:\n  · textMatches '^Following|^Requested' clickable")

    if kind == "ig_unfollow_tap_profile":
        from GramAddict.core.resources import ClassName

        btn = device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            clickable=True,
            textMatches=case_insensitive_re("^Following|^Requested"),
        )
        if not btn.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Following button not found", target="Trying to detect:\n  · textMatches '^Following|^Requested' (unfollow tap)")
        btn.click()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Tapped Following", "test_id": test_id}

    if kind == "ig_unfollow_confirm":
        confirm = device.find(
            resourceIdMatches=case_insensitive_re(
                f"{ResourceID.PRIMARY_BUTTON}|{ResourceID.FOLLOW_SHEET_UNFOLLOW_ROW}"
            ),
            textMatches=case_insensitive_re("^Unfollow$"),
        )
        if not confirm.exists(Timeout.MEDIUM):
            confirm = device.find(
                resourceIdMatches=case_insensitive_re(ResourceID.FOLLOW_SHEET_UNFOLLOW_ROW)
            )
        if not confirm.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Unfollow confirm button not found", target="Trying to detect:\n  · PRIMARY_BUTTON or FOLLOW_SHEET_UNFOLLOW_ROW textMatches '^Unfollow$'")
        confirm.click()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Confirmed unfollow", "test_id": test_id}

    if kind == "ig_own_followers_list":
        TabBarView(device).navigateToProfile()
        ProfileView(device, is_own_profile=True).navigateToFollowers()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "On own Followers list", "test_id": test_id}

    if kind == "ig_remove_follower_search":
        username, err = _remove_practice_username_or_fail(serial, device, test_id)
        if err:
            return err
        if not _search_followers_list(device, username):
            return _fail(
                serial,
                device,
                test_id,
                f"Could not search followers list for @{username}",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/row_search_edit_text\n"
                    f"  · Username from remove_list.txt: @{username}"
                ),
            )
        return {
            "success": True,
            "message": f"Searched followers for @{username}",
            "test_id": test_id,
        }

    if kind == "ig_remove_follower_detect":
        username, err = _remove_practice_username_or_fail(serial, device, test_id)
        if err:
            return err
        if not _search_followers_list(device, username):
            return _fail(
                serial,
                device,
                test_id,
                f"Could not search followers list for @{username}",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/row_search_edit_text\n"
                    f"  · Username from remove_list.txt: @{username}"
                ),
            )
        followers = ga_views.FollowersView(device)
        row = followers._find_user_to_remove(username)
        if row is None:
            return _fail(
                serial,
                device,
                test_id,
                f"@{username} not found in followers list",
                target=_describe_flow(
                    f"FOLLOW_LIST_CONTAINER row for @{username}",
                    "FollowersView._find_user_to_remove",
                ),
            )
        remove_btn = followers._get_remove_button(row)
        if remove_btn.exists(Timeout.SHORT):
            return {
                "success": True,
                "message": f"Remove button found for @{username}",
                "test_id": test_id,
            }
        dismiss = followers._get_dismiss_button(row)
        if dismiss is not None and dismiss.exists(Timeout.SHORT):
            return {
                "success": True,
                "message": f"X (Dismiss) found for @{username} — tap it to reveal Remove",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            f"Remove button not found for @{username}",
            target=(
                "Trying to detect:\n"
                "  · BUTTON/TextView textMatches '^Remove$'\n"
                "  · or X icon descriptionMatches '^Dismiss$' to reveal Remove"
            ),
        )

    if kind == "ig_remove_follower_tap":
        username, err = _remove_practice_username_or_fail(serial, device, test_id)
        if err:
            return err
        if not _search_followers_list(device, username):
            return _fail(
                serial,
                device,
                test_id,
                f"Could not search followers list for @{username}",
                target=(
                    "Trying to detect:\n"
                    f"  · {IG}/row_search_edit_text\n"
                    f"  · Username from remove_list.txt: @{username}"
                ),
            )
        if ga_views.FollowersView(device).remove_follower(username):
            return {
                "success": True,
                "message": f"Removed @{username} from followers",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            f"Remove follower failed for @{username}",
            target=_describe_flow(
                "FOLLOW_LIST_CONTAINER row for username",
                "X (description ^Dismiss$) → ACTION_SHEET_ROW_TEXT_VIEW ^Remove$",
            ),
        )

    if kind == "telegram_test_send":
        from dashboard.gramaddict_config import (
            account_id_for_device,
            get_account_telegram_for_device,
        )
        from GramAddict.plugins.telegram import telegram_bot_send_text

        account_id = account_id_for_device(serial)
        if not account_id:
            return _fail(
                serial,
                device,
                test_id,
                "No account linked to this device",
                target="Trying to detect:\n  · Assign this phone to an account on the Farm tab",
            )
        tg = get_account_telegram_for_device(serial)
        token = str(tg.get("telegram-api-token") or "").strip()
        chat_id = str(tg.get("telegram-chat-id") or "").strip()
        if not token or "your-api-token" in token.lower():
            return _fail(
                serial,
                device,
                test_id,
                "telegram-api-token not set",
                target=(
                    "Trying to detect:\n"
                    "  · Account → Reports tab → Bot token from @BotFather"
                ),
            )
        if not chat_id or "your-chat-id" in chat_id.lower():
            return _fail(
                serial,
                device,
                test_id,
                "telegram-chat-id not set",
                target=(
                    "Trying to detect:\n"
                    "  · Account → Reports tab → Chat ID from @myidbot"
                ),
            )
        handle = _device_username_optional(serial) or account_id
        message = (
            f"*GramAddict test* — dashboard ping from @{handle} "
            f"(device …{serial[-4:]})"
        )
        response = telegram_bot_send_text(token, chat_id, message)
        if response and response.get("ok"):
            return {
                "success": True,
                "message": "Telegram test message sent — check your chat",
                "test_id": test_id,
            }
        detail = (response or {}).get("description") or "Unknown Telegram API error"
        return _fail(
            serial,
            device,
            test_id,
            f"Telegram send failed: {detail}",
            target=(
                "Trying to detect:\n"
                "  · Valid telegram-api-token + telegram-chat-id\n"
                "  · You must /start the bot in Telegram first"
            ),
        )

    if kind == "ig_post_reel_tap_create":
        from GramAddict.core.post_reel import tap_create_button

        if tap_create_button(device):
            return {"success": True, "message": "Tapped + create button", "test_id": test_id}
        return _fail(
            serial,
            device,
            test_id,
            "Could not tap + create button",
            target=f"Trying to detect:\n  · {IG}/action_bar_buttons_container_left → ImageView",
        )

    if kind == "ig_post_reel_clear_gallery":
        from GramAddict.core.post_reel import (
            clear_device_gallery,
            count_mediastore_videos,
        )

        account_id, err = _account_for_device_or_fail(serial, device, test_id)
        if err:
            return err
        before = count_mediastore_videos(serial)
        clear_device_gallery(serial)
        after = count_mediastore_videos(serial)
        return {
            "success": True,
            "message": (
                f"Gallery cleared for {account_id}: "
                f"videos in MediaStore {before} → {after}"
            ),
            "test_id": test_id,
        }

    if kind == "ig_post_reel_push_media":
        from dashboard.post_reel_config import get_media_selection_number, media_dir_for_account
        from GramAddict.core.post_reel import list_local_media, prepare_gallery_with_media

        account_id, err = _account_for_device_or_fail(serial, device, test_id)
        if err:
            return err
        media_dir = media_dir_for_account(account_id)
        files = list_local_media(media_dir)
        if not files:
            return _fail(
                serial,
                device,
                test_id,
                f"No videos in post_media/ for {account_id}",
                target="Trying to detect:\n  · Add .mp4 files to accounts/…/post_media/",
            )
        counter = get_media_selection_number(account_id)
        media_index = (counter - 1) % len(files)
        local = prepare_gallery_with_media(
            serial, media_dir, media_index, clear_first=True
        )
        if local is None:
            return _fail(serial, device, test_id, "ADB push failed", target="Trying to detect:\n  · adb push to /sdcard/DCIM/Camera")
        return {
            "success": True,
            "message": f"Pushed {local.name} (counter={counter}, index={media_index})",
            "test_id": test_id,
        }

    if kind == "ig_post_reel_select_media":
        from dashboard.post_reel_config import get_media_selection_number
        from GramAddict.core.post_reel import select_recent_media

        account_id, err = _account_for_device_or_fail(serial, device, test_id)
        if err:
            return err
        counter = get_media_selection_number(account_id)
        from dashboard.post_reel_config import get_account_post_reel

        settings = get_account_post_reel(account_id)
        select_num = 1 if settings.get("clear-gallery-before-each", True) else counter
        if select_recent_media(device, select_num):
            return {
                "success": True,
                "message": f"Selected gallery item #{select_num} (state counter={counter})",
                "test_id": test_id,
            }
        return _fail(
            serial,
            device,
            test_id,
            f"Could not select gallery item #{select_num}",
            target=f"Trying to detect:\n  · {IG}/gallery_grid_item_thumbnail[{select_num - 1}]",
        )

    if kind == "ig_post_reel_tap_next_top":
        from GramAddict.core.post_reel import tap_next_top

        if tap_next_top(device):
            return {"success": True, "message": "Tapped top Next", "test_id": test_id}
        return _fail(
            serial,
            device,
            test_id,
            "Top Next not found",
            target=f"Trying to detect:\n  · {IG}/next_button_textview",
        )

    if kind == "ig_post_reel_dismiss_popups":
        from GramAddict.core.post_reel import dismiss_popups_center

        dismiss_popups_center(device, taps=3)
        return {"success": True, "message": "Tapped center 3×", "test_id": test_id}

    if kind == "ig_post_reel_tap_next_clips":
        from GramAddict.core.post_reel import tap_next_clips

        if tap_next_clips(device):
            return {"success": True, "message": "Tapped clips Next", "test_id": test_id}
        return _fail(
            serial,
            device,
            test_id,
            "Clips Next not found",
            target=f"Trying to detect:\n  · {IG}/clips_right_action_button",
        )

    if kind == "ig_post_reel_write_caption":
        from dashboard.post_reel_config import generate_caption
        from GramAddict.core.post_reel import enter_caption

        account_id, err = _account_for_device_or_fail(serial, device, test_id)
        if err:
            return err
        try:
            caption = generate_caption(account_id)
        except Exception as exc:
            return _fail(
                serial,
                device,
                test_id,
                f"Caption generation failed: {exc}",
                target="Trying to detect:\n  · post_reel.yml openai-api-key\n  · post_reel_prompts.yml batch prompt",
            )
        if not enter_caption(device, caption):
            return _fail(
                serial,
                device,
                test_id,
                "Could not enter caption",
                target=f"Trying to detect:\n  · {IG}/caption_input_text_view",
            )
        preview = caption[:80] + ("…" if len(caption) > 80 else "")
        return {
            "success": True,
            "message": f"Caption entered: {preview}",
            "test_id": test_id,
        }

    if kind == "ig_post_reel_share":
        from GramAddict.core.post_reel import tap_share, wait_for_post_success
        from dashboard.post_reel_config import get_media_selection_number, increment_media_counter

        account_id, err = _account_for_device_or_fail(serial, device, test_id)
        if err:
            return err
        if not tap_share(device):
            return _fail(
                serial,
                device,
                test_id,
                "Share button not found",
                target=f"Trying to detect:\n  · {IG}/share_button",
            )
        if not wait_for_post_success(device):
            return _fail(
                serial,
                device,
                test_id,
                "Share tapped but success not confirmed",
                target="Trying to detect:\n  · Composer closes after upload",
            )
        counter_before = get_media_selection_number(account_id)
        increment_media_counter(account_id)
        return {
            "success": True,
            "message": f"Reel shared — counter {counter_before} → {counter_before + 1}",
            "test_id": test_id,
        }

    if kind == "ig_post_reel_full_session":
        from dashboard.post_reel_config import get_account_post_reel, run_post_reel_session

        account_id, err = _account_for_device_or_fail(serial, device, test_id)
        if err:
            return err
        settings = get_account_post_reel(account_id)
        count = post_reel_posts_count if post_reel_posts_count is not None else int(
            settings.get("posts-per-session") or 1
        )
        result = run_post_reel_session(device, serial, account_id, posts_count=max(1, count))
        if result.get("success"):
            return {
                "success": True,
                "message": result.get("message", "Reels posted"),
                "test_id": test_id,
                "posted": result.get("posted", 0),
            }
        return _fail(
            serial,
            device,
            test_id,
            result.get("message", "Reel session failed"),
            target="Trying to detect:\n  · Full flow: gallery → + → select → next → caption → share",
        )

    if kind == "ig_story_tap_like":
        like_btn = device.find(resourceIdMatches=case_insensitive_re(ResourceID.TOOLBAR_LIKE_BUTTON))
        if not like_btn.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Story like button not found", target='Trying to detect:\n  · TOOLBAR_LIKE_BUTTON')
        if not like_btn.get_selected():
            like_btn.click()
        random_sleep(1, 2, modulable=False)
        return {"success": True, "message": "Story liked", "test_id": test_id}

    if kind == "ig_open_post_url":
        from GramAddict.core import utils as ga_utils
        from GramAddict.core.utils import open_instagram_with_url, validate_url

        url = _require_post_url(target_post_url)
        if not validate_url(url):
            raise HTTPException(status_code=400, detail="Invalid post URL")
        ga_utils.configs.device_id = serial
        if not open_instagram_with_url(url):
            return _fail(serial, device, test_id, f"Could not open URL: {url}", target='Trying to detect:\n  · adb am start VIEW instagram.com/p/ URL')
        random_sleep(2, 3, modulable=False)
        return {"success": True, "message": "Post URL opened", "test_id": test_id}

    if kind == "ig_post_send_comment":
        from GramAddict.core import utils as ga_utils
        from GramAddict.core.interaction import find_comment_thread_edittext
        from GramAddict.core.resources import ClassName

        message = _require_test_message(test_message)
        comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.SHORT):
            comment_button = device.find(
                resourceIdMatches=case_insensitive_re(ResourceID.ROW_FEED_BUTTON_COMMENT),
            )
            if not comment_button.exists(Timeout.MEDIUM):
                return _fail(serial, device, test_id, "Comment button not found — open a post first", target='Trying to detect:\n  · ROW_FEED_BUTTON_COMMENT')
            comment_button.click()
            random_sleep(1, 2, modulable=False)
            comment_box = find_comment_thread_edittext(device)
        if not comment_box.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Comment compose box not found or comments limited", target='Trying to detect:\n  · layout_comment_thread_edittext or edittext_container')
        comment_box.set_text(
            message,
            Mode.PASTE if ga_utils.args.dont_type else Mode.TYPE,
        )
        post_button = device.find(
            resourceId=ResourceID.LAYOUT_COMMENT_THREAD_POST_BUTTON_ICON,
        )
        if not post_button.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "Comment Post button not found", target='Trying to detect:\n  · layout_comment_thread_post_button_icon')
        post_button.click()
        random_sleep(1, 2, modulable=False)
        UniversalActions.detect_block(device)
        UniversalActions.close_keyboard(device)
        posted = device.find(textContains=message[: min(len(message), 40)])
        if not posted.exists(Timeout.MEDIUM):
            posted = device.find(
                resourceId=ResourceID.ROW_COMMENT_TEXTVIEW_COMMENT,
                textContains=message[: min(len(message), 40)],
            )
        if posted.exists(Timeout.SHORT):
            return {
                "success": True,
                "message": "Comment sent",
                "test_id": test_id,
            }
        return _fail(serial, device, test_id, "Tapped Post but could not confirm comment appeared", target='Trying to detect:\n  · ROW_COMMENT_TEXTVIEW_COMMENT with posted text')

    if kind == "ig_profile_send_pm":
        from GramAddict.core import utils as ga_utils
        from GramAddict.core.resources import ClassName

        message = _require_test_message(test_message)
        message_box = device.find(
            resourceId=ResourceID.ROW_THREAD_COMPOSER_EDITTEXT,
            className=ClassName.EDIT_TEXT,
            enabled="true",
        )
        if not message_box.exists(Timeout.SHORT):
            message_button = device.find(
                classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
                enabled=True,
                textMatches="Message",
            )
            if not message_button.exists(Timeout.MEDIUM):
                return _fail(serial, device, test_id, "Message button not found — open a profile first", target='Trying to detect:\n  · Message button on profile')
            message_button.click()
            random_sleep(1, 2, modulable=False)
            message_box = device.find(
                resourceId=ResourceID.ROW_THREAD_COMPOSER_EDITTEXT,
                className=ClassName.EDIT_TEXT,
                enabled="true",
            )
        if not message_box.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "PM compose box not found or DMs limited", target='Trying to detect:\n  · ROW_THREAD_COMPOSER_EDITTEXT')
        message_box.set_text(
            message,
            Mode.PASTE if ga_utils.args.dont_type else Mode.TYPE,
        )
        send_button = device.find(
            resourceIdMatches=ResourceID.ROW_THREAD_COMPOSER_BUTTON_SEND,
        )
        if not send_button.exists(Timeout.MEDIUM):
            return _fail(serial, device, test_id, "PM Send button not found", target='Trying to detect:\n  · row_thread_composer_button_send\n  · row_thread_composer_send_button_icon')
        send_button.click()
        random_sleep(1, 2, modulable=False)
        UniversalActions.detect_block(device)
        UniversalActions.close_keyboard(device)
        sending_icon = device.find(
            resourceId=ResourceID.ACTION_ICON,
            className=ClassName.IMAGE_VIEW,
        )
        if sending_icon.exists(Timeout.SHORT):
            random_sleep(1, 2, modulable=False)
        posted = device.find(text=message)
        if posted.exists(Timeout.MEDIUM):
            device.back()
            random_sleep(0.5, 1, modulable=False)
            return {"success": True, "message": "PM sent", "test_id": test_id}
        return _fail(serial, device, test_id, "Tapped Send but could not confirm message appeared", target='Trying to detect:\n  · Thread message bubble with sent text')

    if kind in (
        "ig_bpl_production_nav",
        "ig_bpl_production_check_post",
        "ig_bpl_production_likers_status",
        "ig_bpl_production_open_likers",
        "ig_bpl_production_verify_list",
        "ig_bpl_production_full",
    ):
        from GramAddict.core.navigation import nav_to_post_likers
        from GramAddict.core.views import LIKES_COUNT_HIDDEN, OpenedPostView, PostsViewList

        username = _require_target(target_username)
        my_username = _require_device_username(serial)
        post_view = PostsViewList(device)
        lines: list[str] = []

        def _step(msg: str) -> None:
            lines.append(msg)
            debug_log.trace(serial, f"▶ {msg}")
            raise_if_cancelled(serial)

        if kind in ("ig_bpl_production_nav", "ig_bpl_production_full"):
            _step("nav_to_post_likers…")
            if not nav_to_post_likers(device, username, my_username):
                return _fail(
                    serial,
                    device,
                    test_id,
                    f"nav_to_post_likers failed for @{username}",
                    target=DEBUG_KIND_DETECTS["ig_bpl_production_nav"],
                )
            lines.append("nav_to_post_likers: OK")

        if kind in ("ig_bpl_production_check_post", "ig_bpl_production_full"):
            _step("_check_if_last_post…")
            (
                is_same,
                post_desc,
                owner,
                is_ad,
                is_hashtag,
                has_tags,
            ) = post_view._check_if_last_post("", BLOGGER_POST_LIKERS_JOB)
            lines.append(
                f"_check_if_last_post: owner=@{owner or '?'}, same={is_same}, "
                f"ad={is_ad}, hashtag={is_hashtag}, tags={has_tags}"
            )
            if post_desc:
                lines.append(f"  caption key: {post_desc[:80]}{'…' if len(post_desc) > 80 else ''}")

        can_open = False
        count = 0
        if kind in (
            "ig_bpl_production_likers_status",
            "ig_bpl_production_open_likers",
            "ig_bpl_production_full",
        ):
            _step("likers_open_status…")
            can_open, count = post_view.likers_open_status(
                owner=username,
                current_job=BLOGGER_POST_LIKERS_JOB,
                tap_offset_x=likers_offset,
            )
            lines.append(f"likers_open_status: can_open={can_open}, count={count}")
            if count == LIKES_COUNT_HIDDEN:
                return {
                    "success": True,
                    "message": "\n".join(
                        lines
                        + ["→ production would skip (hidden likes — no 'Liked by' after tap)"]
                    ),
                    "test_id": test_id,
                }
            if can_open and count == -1:
                lines.append("→ 'Liked by' sheet opened (count unknown)")
            if kind == "ig_bpl_production_likers_status":
                return {
                    "success": True,
                    "message": "\n".join(lines),
                    "test_id": test_id,
                }
            if not can_open:
                return {
                    "success": True,
                    "message": "\n".join(
                        lines + ["→ production would swipe_to_fit_posts(NEXT_POST)"]
                    ),
                    "test_id": test_id,
                }

        if kind in ("ig_bpl_production_open_likers", "ig_bpl_production_full"):
            _step("open_likers_container…")
            coords = post_view.open_likers_container(tap_offset_x=likers_offset)
            lines.append(f"open_likers_container: coords={coords}")

        if kind in ("ig_bpl_production_verify_list", "ig_bpl_production_full"):
            _step("_getListViewLikers…")
            list_view = OpenedPostView(device)._getListViewLikers()
            if list_view is None:
                return _fail(
                    serial,
                    device,
                    test_id,
                    "Likers list did not load",
                    target=DEBUG_KIND_DETECTS["ig_bpl_production_verify_list"],
                )
            lines.append("_getListViewLikers: OK")

        return {
            "success": True,
            "message": "\n".join(lines),
            "test_id": test_id,
        }

    if kind == "ig_export_crash":
        from GramAddict.core.utils import save_crash

        save_crash(device)
        return {
            "success": True,
            "message": "Crash dump saved (check GramAddict crashes folder on device / logs)",
            "test_id": test_id,
        }

    raise HTTPException(status_code=500, detail=f"Unhandled debug kind: {kind}")
