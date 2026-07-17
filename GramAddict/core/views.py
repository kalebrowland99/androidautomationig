import datetime
import logging
import re
import platform
import time
from difflib import SequenceMatcher
from enum import Enum, auto
from random import choice, randint, uniform
from time import sleep
from typing import Optional, Tuple

import emoji
from colorama import Fore, Style

from GramAddict.core.device_facade import (
    DeviceFacade,
    Direction,
    Location,
    Mode,
    SleepTime,
    Timeout,
)
from GramAddict.core.resources import ClassName
from GramAddict.core.resources import ResourceID as resources
from GramAddict.core.resources import TabBarText
from GramAddict.core.utils import (
    ActionBlockedError,
    Square,
    get_value,
    random_sleep,
    save_crash,
)

logger = logging.getLogger(__name__)

# Dashboard debug runs set this for faster feed probes (no multi-second exists waits).
FEED_DEBUG_FAST = False

DEFAULT_LIKERS_TAP_OFFSET_X = 15
DEFAULT_LIKERS_TAP_OFFSET_Y = 15

LIKES_COUNT_HIDDEN = -2


def load_config(config):
    global args
    global configs
    global ResourceID
    args = config.args
    configs = config
    ResourceID = resources(config.args.app_id)


def case_insensitive_re(str_list):
    strings = str_list if isinstance(str_list, str) else "|".join(str_list)
    return f"(?i)({strings})"


def _recover_from_popup(device, reason: str) -> bool:
    """Best-effort: when an element can't be found, a popup may be covering it.
    Ask the vision model to locate and tap its dismiss button. Returns True only
    if a popup was detected and tapped (so the caller should retry). Never raises.
    """
    try:
        from GramAddict.core.vision_popup import dismiss_popup_with_vision

        return bool(dismiss_popup_with_vision(device, reason=reason))
    except Exception as exc:
        logger.debug(f"Vision popup recovery skipped for {reason}: {exc}")
        return False


class TabBarTabs(Enum):
    HOME = auto()
    SEARCH = auto()
    REELS = auto()
    ORDERS = auto()
    ACTIVITY = auto()
    PROFILE = auto()


class SearchTabs(Enum):
    TOP = auto()
    ACCOUNTS = auto()
    TAGS = auto()
    PLACES = auto()


class FollowStatus(Enum):
    FOLLOW = auto()
    FOLLOWING = auto()
    FOLLOW_BACK = auto()
    REQUESTED = auto()
    NONE = auto()


class SwipeTo(Enum):
    HALF_PHOTO = auto()
    NEXT_POST = auto()


class LikeMode(Enum):
    SINGLE_CLICK = auto()
    DOUBLE_CLICK = auto()


class MediaType(Enum):
    PHOTO = auto()
    VIDEO = auto()
    REEL = auto()
    IGTV = auto()
    CAROUSEL = auto()
    UNKNOWN = auto()


class Owner(Enum):
    OPEN = auto()
    GET_NAME = auto()
    GET_POSITION = auto()


class TabBarView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def _getTabBar(self):
        return self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.TAB_BAR),
            className=ClassName.LINEAR_LAYOUT,
        )

    def navigateToHome(self):
        self._navigateTo(TabBarTabs.HOME)
        return HomeView(self.device)

    def navigateToSearch(self):
        self._navigateTo(TabBarTabs.SEARCH)
        return SearchView(self.device)

    def navigateToReels(self):
        self._navigateTo(TabBarTabs.REELS)

    def navigateToOrders(self):
        self._navigateTo(TabBarTabs.ORDERS)

    def navigateToActivity(self):
        self._navigateTo(TabBarTabs.ACTIVITY)

    def navigateToProfile(self):
        self._navigateTo(TabBarTabs.PROFILE)
        return ProfileView(self.device, is_own_profile=True)

    def _get_new_profile_position(self) -> Optional[DeviceFacade.View]:
        buttons = self.device.find(className=ResourceID.BUTTON)
        for button in buttons:
            if button.get_desc() == "Profile":
                return button
        return None

    def _find_tab_button(self, tab: TabBarTabs) -> Optional[DeviceFacade.View]:
        button = None
        if tab == TabBarTabs.HOME:
            button = self.device.find(
                classNameMatches=ClassName.BUTTON_OR_FRAME_LAYOUT_REGEX,
                descriptionMatches=case_insensitive_re(TabBarText.HOME_CONTENT_DESC),
            )
        elif tab == TabBarTabs.SEARCH:
            button = self.device.find(
                classNameMatches=ClassName.BUTTON_OR_FRAME_LAYOUT_REGEX,
                descriptionMatches=case_insensitive_re(TabBarText.SEARCH_CONTENT_DESC),
            )
        elif tab == TabBarTabs.REELS:
            button = self.device.find(
                classNameMatches=ClassName.BUTTON_OR_FRAME_LAYOUT_REGEX,
                descriptionMatches=case_insensitive_re(TabBarText.REELS_CONTENT_DESC),
            )
        elif tab == TabBarTabs.ORDERS:
            button = self.device.find(
                classNameMatches=ClassName.BUTTON_OR_FRAME_LAYOUT_REGEX,
                descriptionMatches=case_insensitive_re(TabBarText.ORDERS_CONTENT_DESC),
            )
        elif tab == TabBarTabs.ACTIVITY:
            button = self.device.find(
                classNameMatches=ClassName.BUTTON_OR_FRAME_LAYOUT_REGEX,
                descriptionMatches=case_insensitive_re(
                    TabBarText.ACTIVITY_CONTENT_DESC
                ),
            )
        elif tab == TabBarTabs.PROFILE:
            button = self.device.find(
                classNameMatches=ClassName.BUTTON_OR_FRAME_LAYOUT_REGEX,
                descriptionMatches=case_insensitive_re(TabBarText.PROFILE_CONTENT_DESC),
            )
            if not button.exists():
                button = self._get_new_profile_position()
        return button

    def _dismiss_to_tab_bar(self, max_backs: int = 3) -> None:
        """Press Android back until the tab bar is visible (e.g. leaving clips overlay)."""
        posts = PostsViewList(self.device)
        for _ in range(max_backs):
            if self._getTabBar().exists(Timeout.ZERO):
                return
            posts.exit_home_feed_clips_overlay()
            logger.debug("Tab bar not visible — pressing Android back.")
            self.device.back(modulable=False)
            if not FEED_DEBUG_FAST:
                random_sleep(0.25, 0.4, modulable=False, log=False)

    def _navigateTo(self, tab: TabBarTabs):
        tab_name = tab.name
        logger.debug(f"Navigate to {tab_name}")
        UniversalActions.close_keyboard(self.device)

        for attempt in range(2):
            button = self._find_tab_button(tab)

            if tab == TabBarTabs.SEARCH and button is not None and not button.exists():
                # Some accounts display the search btn only in Home -> action bar
                logger.debug("Didn't find search in the tab bar...")
                if attempt == 0:
                    self._dismiss_to_tab_bar()
                    continue
                home_view = self.navigateToHome()
                home_view.navigateToSearch()
                return

            if button is not None and button.exists(Timeout.MEDIUM):
                # Two clicks to reset tab content
                button.click(sleep=SleepTime.SHORT)
                if tab is not TabBarTabs.PROFILE:
                    button.click(sleep=SleepTime.SHORT)
                return

            if attempt == 0:
                self._dismiss_to_tab_bar()
                continue

        logger.error(f"Didn't find tab {tab_name} in the tab bar...")


class ActionBarView:
    def __init__(self, device: DeviceFacade):
        self.device = device
        self.action_bar = self._getActionBar()

    def _getActionBar(self):
        return self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.ACTION_BAR_CONTAINER),
            className=ClassName.FRAME_LAYOUT,
        )


class HomeView(ActionBarView):
    def __init__(self, device: DeviceFacade):
        super().__init__(device)
        self.device = device

    def navigateToSearch(self):
        logger.debug("Navigate to Search")
        search_btn = self.action_bar.child(
            descriptionMatches=case_insensitive_re(TabBarText.SEARCH_CONTENT_DESC)
        )
        search_btn.click()

        return SearchView(self.device)


class HashTagView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def _getRecyclerView(self):
        obj = self.device.find(resourceIdMatches=ResourceID.RECYCLER_VIEW)
        if obj.exists(Timeout.LONG):
            logger.debug("RecyclerView exists.")
        else:
            logger.debug("RecyclerView doesn't exists.")
        return obj

    def _getFistImageView(self, recycler):
        obj = recycler.child(
            resourceIdMatches=ResourceID.IMAGE_BUTTON,
        )
        if obj.exists(Timeout.LONG):
            logger.debug("First image in view exists.")
        else:
            logger.debug("First image in view doesn't exists.")
        return obj

    def _getRecentTab(self):
        obj = self.device.find(
            className=ClassName.TEXT_VIEW,
            textMatches=case_insensitive_re(TabBarText.RECENT_CONTENT_DESC),
        )
        if obj.exists(Timeout.LONG):
            logger.debug("Recent Tab exists.")
        else:
            logger.debug("Recent Tab doesn't exists.")
        return obj


# The place view for the moment It's only a copy/paste of HashTagView
# Maybe we can add the com.instagram.android:id/category_name == "Country/Region" (or other obv)


class PlacesView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def _getRecyclerView(self):
        obj = self.device.find(resourceIdMatches=ResourceID.RECYCLER_VIEW)
        if obj.exists(Timeout.LONG):
            logger.debug("RecyclerView exists.")
        else:
            logger.debug("RecyclerView doesn't exists.")
        return obj

    def _getFistImageView(self, recycler):
        obj = recycler.child(
            resourceIdMatches=ResourceID.IMAGE_BUTTON,
        )
        if obj.exists(Timeout.LONG):
            logger.debug("First image in view exists.")
        else:
            logger.debug("First image in view doesn't exists.")
        return obj

    def _getRecentTab(self):
        return self.device.find(
            className=ClassName.TEXT_VIEW,
            textMatches=case_insensitive_re(TabBarText.RECENT_CONTENT_DESC),
        )

    def _getInformBody(self):
        return self.device.find(
            className=ClassName.TEXT_VIEW,
            resourceId=ResourceID.INFORM_BODY,
        )


class SearchView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def _getSearchEditText(self):
        for _ in range(2):
            obj = self.device.find(
                resourceIdMatches=case_insensitive_re(
                    ResourceID.ACTION_BAR_SEARCH_EDIT_TEXT
                ),
            )
            if obj.exists(Timeout.LONG):
                return obj
            logger.error(
                "Can't find the search bar! Refreshing it by pressing Home and Search again.."
            )
            UniversalActions.close_keyboard(self.device)
            TabBarView(self.device).navigateToHome()
            TabBarView(self.device).navigateToSearch()
        logger.error("Can't find the search bar!")
        return None

    def _getUsernameRow(self, username):
        return self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.ROW_SEARCH_USER_USERNAME),
            className=ClassName.TEXT_VIEW,
            textMatches=case_insensitive_re(username),
        )

    def _getHashtagRow(self, hashtag):
        return self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.ROW_HASHTAG_TEXTVIEW_TAG_NAME
            ),
            className=ClassName.TEXT_VIEW,
            text=f"#{hashtag}",
        )

    def _getPlaceRow(self):
        obj = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.ROW_PLACE_TITLE),
        )
        obj.wait(Timeout.MEDIUM)
        return obj

    def _getTabTextView(self, tab: SearchTabs):
        tab_layout = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.FIXED_TABBAR_TABS_CONTAINER
            ),
        )
        if tab_layout.exists():
            logger.debug("Tabs container exists!")
            tab_text_view = tab_layout.child(
                resourceIdMatches=case_insensitive_re(ResourceID.TAB_BUTTON_NAME_TEXT),
                textMatches=case_insensitive_re(tab.name),
            )
            if not tab_text_view.exists():
                logger.debug("Tabs container hasn't text! Let's try with description.")
                for obj in tab_layout.child():
                    if obj.ui_info()["contentDescription"].upper() == tab.name.upper():
                        tab_text_view = obj
                        break
            return tab_text_view
        return None

    def _searchTabWithTextPlaceholder(self, tab: SearchTabs):
        tab_layout = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.FIXED_TABBAR_TABS_CONTAINER
            ),
        )
        search_edit_text = self._getSearchEditText()

        fixed_text = "Search {}".format(tab.name if tab.name != "TAGS" else "hashtags")
        logger.debug(
            "Going to check if the search bar have as placeholder: {}".format(
                fixed_text
            )
        )

        for item in tab_layout.child(
            resourceId=ResourceID.TAB_BUTTON_FALLBACK_ICON,
            className=ClassName.IMAGE_VIEW,
        ):
            item.click()

            # Little trick for force-update the ui and placeholder text
            if search_edit_text is not None:
                search_edit_text.click()

            if self.device.find(
                className=ClassName.TEXT_VIEW,
                textMatches=case_insensitive_re(fixed_text),
            ).exists():
                return item
        return None

    def _search_query(self, target: str, job: str) -> str:
        if "hashtag" in job:
            return f"#{target.lstrip('#')}"
        return target

    def navigate_to_target(self, target: str, job: str) -> bool:
        target = emoji.emojize(target, use_aliases=True)
        logger.info(f"Navigate to {target}")
        search_edit_text = self._getSearchEditText()
        if search_edit_text is not None:
            logger.debug("Pressing on searchbar.")
            search_edit_text.click(sleep=SleepTime.SHORT)
        else:
            logger.debug("There is no searchbar!")
            return False
        if self._check_current_view(target, job):
            logger.info(f"{target} is in recent history.")
            return True
        search_query = self._search_query(target, job)
        if "hashtag" in job:
            logger.info(f"Typing hashtag search query: {search_query!r}")
            search_edit_text.set_text(search_query, Mode.PASTE)
        else:
            search_edit_text.set_text(
                search_query,
                Mode.PASTE if args.dont_type else Mode.TYPE,
            )
        if self._check_current_view(target, job):
            logger.info(f"{target} is in top view.")
            return True
        echo_text = self.device.find(resourceId=ResourceID.ECHO_TEXT)
        if echo_text.exists(Timeout.SHORT):
            logger.debug("Pressing on see all results.")
            echo_text.click()
        # at this point we have the tabs available
        self._switch_to_target_tag(job)
        if self._check_current_view(target, job, in_place_tab=True):
            return True
        return False

    def _switch_to_target_tag(self, job: str):
        if "place" in job:
            tab = SearchTabs.PLACES
        elif "hashtag" in job:
            tab = SearchTabs.TAGS
        else:
            tab = SearchTabs.ACCOUNTS

        obj = self._getTabTextView(tab)
        if obj is not None:
            logger.info(f"Switching to {tab.name}")
            obj.click()

    def _check_current_view(
        self, target: str, job: str, in_place_tab: bool = False
    ) -> bool:
        if "place" in job:
            if not in_place_tab:
                return False
            obj = self._getPlaceRow()
        elif "hashtag" in job:
            if not in_place_tab:
                return False
            obj = self._getHashtagRow(target)
        else:
            obj = self.device.find(
                text=target,
                resourceIdMatches=ResourceID.SEARCH_ROW_ITEM,
            )
        if obj.exists():
            obj.click()
            return True
        # Fallbacks below only apply to account searches.
        if "place" not in job and "hashtag" not in job:
            # 1) Account rendered as a search "suggestion/entity" row
            #    (row_search_keyword_title/subtitle) instead of a user row.
            if self._open_search_entity_row(target):
                return True
            # 2) Handle is slightly off (typo / renamed account) but Instagram
            #    surfaced the intended account as the top result.
            if self._open_closest_user_row(target):
                return True
        return False

    # Minimum handle similarity before we'll open a non-exact account match.
    _CLOSEST_USER_MATCH_RATIO = 0.90

    def _open_closest_user_row(self, target: str) -> bool:
        """Open the top account result when its handle is a very close match to
        ``target``.

        Instagram often surfaces the intended account even when the requested
        handle is slightly off (a typo, or a since-renamed handle). We only act
        on this when the displayed handle is highly similar to ``target`` so we
        never open an unrelated account.
        """
        username_view = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.ROW_SEARCH_USER_USERNAME
            ),
        )
        if not username_view.exists(Timeout.SHORT):
            return False
        shown = (username_view.get_text(error=False) or "").strip().lstrip("@")
        want = target.strip().lstrip("@")
        if not shown or not want:
            return False
        if shown.lower() == want.lower():
            # Exact (case-only) match — safe to open.
            ratio = 1.0
        else:
            ratio = SequenceMatcher(None, want.lower(), shown.lower()).ratio()
            if ratio < self._CLOSEST_USER_MATCH_RATIO:
                logger.debug(
                    f"Top result '{shown}' not similar enough to '{target}' "
                    f"(similarity {ratio:.2f}); leaving it."
                )
                return False
        logger.info(
            f"No exact match for '{target}'; opening closest account "
            f"'{shown}' (similarity {ratio:.2f})."
        )
        username_view.click()
        return True

    def _open_search_entity_row(self, target: str) -> bool:
        """Open a profile that appears as a search "entity" suggestion row.

        Some accounts show up under a keyword-style row instead of a normal
        user row. That row's subtitle reads ``"<username> • <N> followers"``
        and its avatar sits on the *right*. Tapping the row body can trigger a
        keyword search, but tapping the right-hand avatar opens the profile —
        so we match the subtitle by username and tap that avatar.
        """
        subtitle = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.ROW_SEARCH_KEYWORD_SUBTITLE
            ),
            textMatches=rf"(?i)^{re.escape(target)}(\s|$|[•·・]).*",
        )
        if not subtitle.exists(Timeout.SHORT):
            return False
        logger.info(
            f"'{target}' appeared as a search suggestion; opening its profile "
            "via the avatar on the right."
        )
        avatar = self._search_entity_avatar(subtitle)
        if avatar is None:
            logger.debug("Could not locate the entity row avatar to tap.")
            return False
        avatar.click()
        return True

    def _search_entity_avatar(self, subtitle):
        """Return the clickable avatar on the right of the entity row that owns
        ``subtitle``.

        Preference order:
          1. the ``row_search_avatar_with_ring`` sharing the subtitle's row
             (matched by vertical position, on the right half of the screen);
          2. the lone FrameLayout-classed avatar (standard user rows use a
             Button), as a build-independent fallback.
        """
        try:
            sb = subtitle.get_bounds()
        except DeviceFacade.JsonRpcError:
            sb = None
        width = self.device.get_info()["displayWidth"]
        if sb is not None:
            avatars = self.device.find(
                resourceIdMatches=case_insensitive_re(
                    ResourceID.ROW_SEARCH_AVATAR_WITH_RING
                ),
            )
            for av in avatars:
                try:
                    b = av.get_bounds()
                except DeviceFacade.JsonRpcError:
                    continue
                cy = (b["top"] + b["bottom"]) / 2
                cx = (b["left"] + b["right"]) / 2
                on_this_row = sb["top"] - 140 <= cy <= sb["bottom"] + 140
                on_right_half = cx > width / 2
                if on_this_row and on_right_half:
                    return av
        fallback = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.ROW_SEARCH_AVATAR_WITH_RING
            ),
            className=ClassName.FRAME_LAYOUT,
        )
        return fallback if fallback.exists(Timeout.SHORT) else None


class PostsViewList:
    _feed_align_keys: dict[int, Optional[str]] = {}

    def __init__(self, device: DeviceFacade):
        self.device = device
        self.has_tags = False

    @classmethod
    def _feed_device_key(cls, device: DeviceFacade) -> int:
        return id(device)

    def _get_feed_align_key(self) -> Optional[str]:
        return PostsViewList._feed_align_keys.get(
            self._feed_device_key(self.device)
        )

    def _set_feed_align_key(self, value: Optional[str]) -> None:
        key = self._feed_device_key(self.device)
        if value is None:
            PostsViewList._feed_align_keys.pop(key, None)
        else:
            PostsViewList._feed_align_keys[key] = value

    def prepare_home_feed_post(self, username: Optional[str] = None) -> bool:
        """Tap feed media to reveal actions, then verify like row is visible."""
        return self.ensure_feed_post_actions_visible(username)

    def _feed_find_timeout(self) -> Timeout:
        return Timeout.ZERO if FEED_DEBUG_FAST else Timeout.TINY

    def _feed_caption_find_timeout(self) -> Timeout:
        return Timeout.ZERO if FEED_DEBUG_FAST else Timeout.SHORT

    def _is_feed_clips_viewer(self) -> bool:
        """Home-feed video after tap — IG opens the clips/reel overlay (right-rail UFI)."""
        feed_like = self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
        )
        if feed_like.exists(Timeout.ZERO) or feed_like.exists(Timeout.TINY):
            try:
                bounds = feed_like.get_bounds()
                center_x = (bounds["left"] + bounds["right"]) / 2
                if center_x < self.device.get_info()["displayWidth"] / 2:
                    return False
            except DeviceFacade.JsonRpcError:
                return False
        author = self.device.find(resourceId=ResourceID.CLIPS_AUTHOR_USERNAME)
        if author.exists(Timeout.ZERO):
            return True
        clips_root = self.device.find(resourceId=ResourceID.CLIPS_ROOT_LAYOUT)
        if clips_root.exists(Timeout.ZERO):
            return OpenedPostView(self.device)._like_button_on_right_side()
        in_fullscreen, _ = OpenedPostView(self.device)._is_video_in_fullscreen()
        return in_fullscreen and OpenedPostView(self.device)._like_button_on_right_side()

    def _clips_feed_like_button(self):
        return self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.LIKE_BUTTON)
        )

    def _feed_like_row_ready(self, username: Optional[str] = None) -> bool:
        if self._is_feed_clips_viewer():
            return self._clips_feed_like_button().exists(Timeout.ZERO)
        return self._feed_like_button_usable(
            self._find_post_like_button_for_post(username)
        )

    def _read_clips_feed_owner(self) -> Optional[str]:
        author = self.device.find(resourceId=ResourceID.CLIPS_AUTHOR_USERNAME)
        if author.exists(Timeout.ZERO):
            label = (
                author.get_text(error=False) or author.get_desc() or ""
            ).strip().lstrip("@")
            if label:
                return label.split()[0].replace("•", "")
        for selector in (
            {"resourceId": ResourceID.CLIPS_VIDEO_CONTAINER},
            {"resourceId": ResourceID.CLIPS_VIEWER_CONTAINER},
        ):
            node = self.device.find(**selector)
            if not node.exists(Timeout.ZERO):
                continue
            desc = (node.get_desc() or "").strip()
            match = re.search(
                r"(?i)(?:reel|video)\s+by\s+(@?\S+)", desc
            )
            if match:
                return match.group(1).lstrip("@").rstrip(".,•").strip()
        return None

    def _read_clips_feed_caption(self) -> Optional[str]:
        """Clips overlay: caption content-desc is nested under clips_caption_component."""
        ui_timeout = self._feed_caption_find_timeout()
        caption_root = self.device.find(resourceId=ResourceID.CLIPS_CAPTION_COMPONENT)
        if not caption_root.exists(ui_timeout):
            return None

        skip_prefixes = (
            "follow ",
            "like number",
            "comment number",
            "reposted ",
            "reshare number",
            "save number",
            "profile picture",
            "reel by ",
            "video by ",
        )
        skip_exact = {"more", "like", "comment", "share", "save", "repost", "audio"}

        def walk(node, depth: int = 0) -> Optional[str]:
            if depth > 8:
                return None
            try:
                desc = (
                    node.get_desc() or node.get_text(error=False) or ""
                ).strip()
            except DeviceFacade.JsonRpcError:
                desc = ""
            if desc:
                dl = desc.lower()
                if dl not in skip_exact and not any(
                    dl.startswith(prefix) for prefix in skip_prefixes
                ):
                    cleaned = PostsViewList._clean_feed_caption_tail(desc)
                    if cleaned and len(cleaned) > 2:
                        return cleaned
            try:
                for child in node:
                    found = walk(child, depth + 1)
                    if found:
                        return found
            except DeviceFacade.JsonRpcError:
                pass
            return None

        found = walk(caption_root)
        if found:
            return found

        # Fallback: first clickable descendant with a content-desc under caption row
        try:
            clickable = caption_root.child(clickable=True)
            if clickable.exists(Timeout.ZERO):
                desc = (
                    clickable.get_desc() or clickable.get_text(error=False) or ""
                ).strip()
                return PostsViewList._clean_feed_caption_tail(desc)
        except DeviceFacade.JsonRpcError:
            pass
        return None

    @staticmethod
    def _feed_caption_preview(caption_text: str, word_count: int = 3) -> str:
        """First N caption words — matches dedup key and IG truncated captions."""
        words = (caption_text or "").split()[:word_count]
        return " ".join(words)

    def _read_clips_like_count(self) -> Optional[int]:
        like_count = self.device.find(resourceId=ResourceID.CLIPS_LIKE_COUNT)
        if like_count.exists(Timeout.ZERO):
            label = (
                like_count.get_text(error=False) or like_count.get_desc() or ""
            ).strip()
            match = re.search(
                r"(?i)like\s+number\s+is\s*([\d,]+)", label.replace(",", "")
            )
            if match:
                parsed = self._parse_like_count_text(match.group(1))
                if parsed is not None:
                    return parsed
            parsed = self._parse_like_count_text(label.replace(",", ""))
            if parsed is not None:
                return parsed
        if OpenedPostView(self.device).fullscreen_likes_hidden():
            return LIKES_COUNT_HIDDEN
        return None

    def _feed_like_button_usable(self, like_btn) -> bool:
        """True when the heart exists and at least part of it is in the viewport."""
        if not like_btn.exists(Timeout.ZERO):
            return False
        try:
            bounds = like_btn.get_bounds()
        except DeviceFacade.JsonRpcError:
            return False
        if bounds["bottom"] <= bounds["top"]:
            return False
        display_height = self.device.get_info()["displayHeight"]
        top_limit = 56
        bottom_limit = display_height - 72
        return bounds["bottom"] > top_limit and bounds["top"] < bottom_limit

    def exit_home_feed_clips_overlay(self) -> bool:
        """Leave home-feed clips/reel overlay via Android back → home feed."""
        if not self._is_feed_clips_viewer():
            return True
        logger.info("Leaving clips overlay — pressing Android back to home feed.")
        for _ in range(3):
            self.device.back(modulable=False)
            if not FEED_DEBUG_FAST:
                random_sleep(0.25, 0.4, modulable=False, log=False)
            if not self._is_feed_clips_viewer():
                self._set_feed_align_key(None)
                return True
        self._set_feed_align_key(None)
        if self._is_feed_clips_viewer():
            logger.warning("Could not exit home-feed clips overlay.")
            return False
        return True

    def _tap_feed_media_for_actions(self) -> bool:
        """One tap on home-feed video opens the clips overlay (right-rail UFI)."""
        if self._is_feed_clips_viewer():
            return True
        media = self._feed_media_view()
        if not media.exists(self._feed_find_timeout()):
            logger.debug("No feed media on screen to tap.")
            return False
        logger.debug("Tapping feed media to open clips/reel overlay.")
        try:
            media.click()
        except DeviceFacade.JsonRpcError:
            return False
        if not FEED_DEBUG_FAST:
            random_sleep(0.25, 0.45, modulable=False, log=False)
        return True

    def _find_feed_post_owner_view(self, username: Optional[str] = None):
        """Profile name for the current post — anchored from the visible like row."""
        uname = (username or "").lstrip("@").strip()
        ui_timeout = Timeout.ZERO
        like_btn = self._find_post_like_button_for_post(uname or None)
        if like_btn.exists(ui_timeout):
            profile = like_btn.up(
                resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME
            )
            if profile.exists(ui_timeout):
                if not uname or (profile.get_text(error=False) or "").lower().startswith(
                    uname.lower()
                ):
                    return profile
        if uname:
            profile = self.device.find(
                resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME,
                textStartsWith=uname,
            )
            if profile.exists(ui_timeout):
                return profile
        return self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME,
        )

    def _find_post_like_button_for_post(self, username: Optional[str] = None):
        """Like button scoped to post — classic feed row or clips overlay after tap."""
        if self._is_feed_clips_viewer():
            return self._clips_feed_like_button()
        ui_timeout = Timeout.ZERO
        uname = (username or "").lstrip("@").strip()
        if uname:
            profile = self.device.find(
                resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME,
                textStartsWith=uname,
            )
            if profile.exists(ui_timeout):
                like_btn = profile.down(
                    resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
                )
                if like_btn.exists(ui_timeout):
                    return like_btn
        return self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE,
        )

    def _combined_text_from_ig_layout(self, layout_view) -> str:
        """IgTextLayoutView @text is often empty; children Buttons hold username + caption."""
        try:
            combined = (layout_view.get_text(error=False) or "").strip()
        except DeviceFacade.JsonRpcError:
            combined = ""
        if combined:
            return combined
        try:
            desc = (layout_view.get_desc() or "").strip()
        except DeviceFacade.JsonRpcError:
            desc = ""
        if desc:
            return desc
        parts: list[str] = []
        try:
            for child in layout_view:
                try:
                    info = child.ui_info()
                except DeviceFacade.JsonRpcError:
                    continue
                if info.get("className") != ClassName.BUTTON:
                    continue
                try:
                    label = (
                        child.get_desc() or child.get_text(error=False) or ""
                    ).strip()
                except DeviceFacade.JsonRpcError:
                    continue
                if label:
                    parts.append(label)
        except DeviceFacade.JsonRpcError:
            pass
        return " ".join(parts).strip()

    def _layout_matches_feed_owner(self, layout_view, username: str) -> bool:
        uname = username.lstrip("@").strip().lower()
        if not uname:
            return False
        combined = self._combined_text_from_ig_layout(layout_view).lower()
        if combined and uname in combined:
            return True
        try:
            for child in layout_view:
                try:
                    info = child.ui_info()
                except DeviceFacade.JsonRpcError:
                    continue
                if info.get("className") != ClassName.BUTTON:
                    continue
                try:
                    label = (
                        child.get_desc() or child.get_text(error=False) or ""
                    ).strip()
                except DeviceFacade.JsonRpcError:
                    continue
                if not label:
                    continue
                ll = label.lower().lstrip("@")
                if ll == uname or ll.startswith(uname):
                    return True
        except DeviceFacade.JsonRpcError:
            pass
        return False

    def _find_caption_layout_from_owner_button(self, username: str):
        """Find IgTextLayoutView via username Button child (matches uiautomator xpath)."""
        uname = username.lstrip("@").strip()
        like_btn = self._find_post_like_button_for_post(username)
        like_top: Optional[int] = None
        if like_btn.exists(Timeout.ZERO):
            try:
                like_top = like_btn.get_bounds()["top"]
            except DeviceFacade.JsonRpcError:
                like_top = None
        for kwargs in (
            {"text": uname},
            {"textStartsWith": uname},
            {"description": uname},
            {"descriptionStartsWith": uname},
            {"textMatches": rf"(?i)^@?{re.escape(uname)}$"},
        ):
            owner_btn = self.device.find(className=ClassName.BUTTON, **kwargs)
            try:
                count = owner_btn.count_items()
            except DeviceFacade.JsonRpcError:
                continue
            if count <= 0:
                continue
            for index in range(min(count, 6)):
                btn = (
                    owner_btn
                    if count <= 1
                    else self.device.find(
                        className=ClassName.BUTTON, index=index, **kwargs
                    )
                )
                if not btn.exists(Timeout.ZERO):
                    continue
                if like_top is not None:
                    try:
                        if btn.get_bounds()["top"] < like_top - 80:
                            continue
                    except DeviceFacade.JsonRpcError:
                        pass
                parent = btn.up(className=ClassName.IG_TEXT_LAYOUT)
                if parent.exists(Timeout.ZERO) and self._layout_matches_feed_owner(
                    parent, username
                ):
                    return parent
        return None

    def _scan_ig_text_layouts_for_owner(self, username: str):
        uname = username.lstrip("@").strip()
        layouts = self.device.find(className=ClassName.IG_TEXT_LAYOUT)
        try:
            count = layouts.count_items()
        except DeviceFacade.JsonRpcError:
            return None
        if count <= 0:
            return None
        like_btn = self._find_post_like_button_for_post(username)
        like_top: Optional[int] = None
        if like_btn.exists(Timeout.ZERO):
            try:
                like_top = like_btn.get_bounds()["top"]
            except DeviceFacade.JsonRpcError:
                like_top = None
        for index in range(min(count, 25)):
            layout = self.device.find(
                className=ClassName.IG_TEXT_LAYOUT, index=index
            )
            if not layout.exists(Timeout.ZERO):
                continue
            if like_top is not None:
                try:
                    layout_top = layout.get_bounds()["top"]
                except DeviceFacade.JsonRpcError:
                    continue
                if layout_top < like_top - 80:
                    continue
            if self._layout_matches_feed_owner(layout, uname):
                return layout
        return None

    def _feed_media_view(self):
        """Primary feed media node — photos, carousels, and home-feed reels."""
        media = self.device.find(resourceIdMatches=ResourceID.MEDIA_CONTAINER)
        if media.exists(Timeout.SHORT):
            return media
        return self.device.find(
            resourceIdMatches=ResourceID.VIDEO_CONTAINER_AND_CLIPS_VIDEO_CONTAINER
        )

    def _feed_media_bottom(self) -> Optional[int]:
        media = self._feed_media_view()
        if not media.exists(Timeout.SHORT):
            return None
        try:
            return media.get_bounds()["bottom"]
        except DeviceFacade.JsonRpcError:
            return None

    def _swipe_to_next_clips_feed_post(self) -> None:
        """Swipe up in the home-feed clips overlay to advance to the next reel."""
        self._set_feed_align_key(None)
        logger.info(
            "Swipe to next reel in clips overlay.",
            extra={"color": f"{Fore.GREEN}"},
        )
        display_width = self.device.get_info()["displayWidth"]
        display_height = self.device.get_info()["displayHeight"]
        self.device.swipe_points(
            display_width / 2,
            display_height * 0.75,
            display_width / 2,
            display_height * 0.25,
        )
        if not FEED_DEBUG_FAST:
            random_sleep(0.5, 1.0, modulable=False, log=False)

    def _scroll_home_feed_to_next_post(self) -> None:
        """Scroll home feed down by 50% of screen height to reach the next post."""
        PostsViewList._feed_align_keys.pop(
            PostsViewList._feed_device_key(self.device), None
        )
        display_width = self.device.get_info()["displayWidth"]
        display_height = self.device.get_info()["displayHeight"]
        fraction = 0.5
        logger.info(
            "Scroll down to see next post.", extra={"color": f"{Fore.GREEN}"}
        )
        start_y = display_height * 0.65
        end_y = display_height * (0.65 - fraction)
        self.device.swipe_points(
            display_width / 2,
            start_y,
            display_width / 2,
            end_y,
        )

    def swipe_to_fit_posts(self, swipe: SwipeTo, *, home_feed: bool = False):
        """calculate the right swipe amount necessary to swipe to next post in hashtag post view
        in order to make it available to other plug-ins I cut it in two moves"""
        if self._is_feed_clips_viewer():
            if swipe == SwipeTo.NEXT_POST:
                self._swipe_to_next_clips_feed_post()
            return
        if home_feed:
            if swipe == SwipeTo.HALF_PHOTO:
                return
            if swipe == SwipeTo.NEXT_POST:
                self._scroll_home_feed_to_next_post()
                return
        displayWidth = self.device.get_info()["displayWidth"]
        containers_content = ResourceID.MEDIA_CONTAINER
        containers_gap = ResourceID.GAP_VIEW_AND_FOOTER_SPACE
        suggested_users = ResourceID.NETEGO_CAROUSEL_HEADER

        # move type: half photo
        if swipe == SwipeTo.HALF_PHOTO:
            zoomable_view_container = self._feed_media_bottom()
            if zoomable_view_container is None:
                logger.debug(
                    "Media container not on screen; using fallback feed scroll."
                )
                UniversalActions(self.device)._swipe_points(Direction.DOWN)
                return
            ac_exists, _, ac_bottom = PostsViewList(
                self.device
            )._get_action_bar_position()
            if ac_exists and zoomable_view_container < ac_bottom:
                zoomable_view_container += ac_bottom
            self.device.swipe_points(
                displayWidth / 2,
                zoomable_view_container - 5,
                displayWidth / 2,
                zoomable_view_container * 0.5,
            )
        elif swipe == SwipeTo.NEXT_POST:
            PostsViewList._feed_align_keys.pop(
                PostsViewList._feed_device_key(self.device), None
            )
            logger.info(
                "Scroll down to see next post.", extra={"color": f"{Fore.GREEN}"}
            )
            gap_view_obj = self.device.find(index=-1, resourceIdMatches=containers_gap)
            obj1 = None
            for _ in range(3):
                if not gap_view_obj.exists():
                    logger.debug("Can't find the gap obj, scroll down a little more.")
                    PostsViewList(self.device).swipe_to_fit_posts(SwipeTo.HALF_PHOTO)
                    gap_view_obj = self.device.find(resourceIdMatches=containers_gap)
                    if not gap_view_obj.exists():
                        continue
                    else:
                        break
                else:
                    media = self._feed_media_view()
                    if not media.exists(Timeout.SHORT):
                        UniversalActions(self.device)._swipe_points(Direction.DOWN)
                        return True
                    try:
                        gap_bottom = gap_view_obj.get_bounds()["bottom"]
                        media_bottom = media.get_bounds()["bottom"]
                    except DeviceFacade.JsonRpcError:
                        UniversalActions(self.device)._swipe_points(Direction.DOWN)
                        return True
                    if gap_bottom < media_bottom:
                        PostsViewList(self.device).swipe_to_fit_posts(
                            SwipeTo.HALF_PHOTO
                        )
                        continue
                    suggested = self.device.find(resourceIdMatches=suggested_users)
                    if suggested.exists():
                        for _ in range(2):
                            PostsViewList(self.device).swipe_to_fit_posts(
                                SwipeTo.HALF_PHOTO
                            )
                            footer_obj = self.device.find(
                                resourceIdMatches=ResourceID.FOOTER_SPACE
                            )
                            if footer_obj.exists():
                                obj1 = footer_obj.get_bounds()["bottom"]
                                break
                    break
            if not gap_view_obj.exists(Timeout.SHORT):
                logger.debug("Gap view missing; using fallback feed scroll.")
                UniversalActions(self.device)._swipe_points(Direction.DOWN)
                return True
            if obj1 is None:
                try:
                    obj1 = gap_view_obj.get_bounds()["bottom"]
                except DeviceFacade.JsonRpcError:
                    UniversalActions(self.device)._swipe_points(Direction.DOWN)
                    return True
            media = self._feed_media_view()
            if not media.exists(Timeout.SHORT):
                UniversalActions(self.device)._swipe_points(Direction.DOWN)
                return True
            try:
                media_bounds = media.get_bounds()
            except DeviceFacade.JsonRpcError:
                UniversalActions(self.device)._swipe_points(Direction.DOWN)
                return True

            obj2 = (media_bounds["bottom"] + media_bounds["top"]) / 3

            self.device.swipe_points(
                displayWidth / 2,
                obj1 - 5,
                displayWidth / 2,
                obj2 + 5,
            )
            return True

    @staticmethod
    def _parse_like_count_text(text: str) -> Optional[int]:
        raw = (text or "").strip().replace(",", "")
        if not raw:
            return None
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        match = re.fullmatch(r"([\d.]+)([KkMmBb])", raw)
        if match:
            val = float(match.group(1))
            suffix = match.group(2).upper()
            if suffix == "K":
                return int(val * 1000)
            if suffix == "M":
                return int(val * 1_000_000)
            if suffix == "B":
                return int(val * 1_000_000_000)
        return None

    def _feed_visible_bottom(self, bottom_margin: int = 220) -> int:
        return self.device.get_info()["displayHeight"] - bottom_margin

    def _view_on_screen(self, view, top_margin: int = 63, bottom_margin: int = 220) -> bool:
        if not view.exists(Timeout.ZERO):
            return False
        try:
            bounds = view.get_bounds()
        except DeviceFacade.JsonRpcError:
            return False
        visible_bottom = self._feed_visible_bottom(bottom_margin)
        return bounds["top"] >= top_margin and bounds["bottom"] <= visible_bottom

    def ensure_feed_post_actions_visible(
        self, username: Optional[str] = None, max_attempts: Optional[int] = None
    ) -> bool:
        """Tap feed media so like/caption/share appear; verify like row before reads."""
        if max_attempts is None:
            max_attempts = 2
        uname = (username or "").lstrip("@").strip()
        align_key = uname or "__feed__"
        if self._get_feed_align_key() == align_key:
            if self._feed_like_row_ready(uname or None):
                return True
            self._set_feed_align_key(None)
        for attempt in range(max_attempts):
            if self._feed_like_row_ready(uname or None):
                self._set_feed_align_key(align_key)
                return True
            if attempt + 1 >= max_attempts:
                break
            logger.debug(
                "Tapping feed media to reveal actions (attempt %s/%s)",
                attempt + 1,
                max_attempts,
            )
            if not self._tap_feed_media_for_actions():
                break
            if not FEED_DEBUG_FAST:
                random_sleep(0.15, 0.25, modulable=False, log=False)
        logger.info(
            "Like button not visible after tapping feed media — skipping feed reads."
        )
        self._set_feed_align_key(None)
        return False

    def _find_post_caption_layout(self, username: Optional[str] = None):
        if username:
            uname = username.lstrip("@")
            owner_layout = self._find_caption_layout_from_owner_button(username)
            if owner_layout is not None:
                return owner_layout
            scanned = self._scan_ig_text_layouts_for_owner(username)
            if scanned is not None:
                return scanned
            profile = self.device.find(
                resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME,
                textStartsWith=uname,
            )
            if profile.exists(Timeout.ZERO):
                like = profile.down(
                    resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
                )
                if like.exists(Timeout.ZERO):
                    below = like.down(className=ClassName.IG_TEXT_LAYOUT)
                    if below.exists(Timeout.ZERO) and self._layout_matches_feed_owner(
                        below, username
                    ):
                        return below
            like_btn = self._find_post_like_button_for_post(username)
            if like_btn.exists(Timeout.ZERO):
                below = like_btn.down(className=ClassName.IG_TEXT_LAYOUT)
                if below.exists(Timeout.ZERO) and self._layout_matches_feed_owner(
                    below, username
                ):
                    return below
            for selector in (
                {"textStartsWith": uname},
                {"textContains": uname},
                {"textMatches": rf"(?i)^{re.escape(uname)}\s+.+"},
                {"descriptionContains": uname},
            ):
                scoped = self.device.find(
                    className=ClassName.IG_TEXT_LAYOUT,
                    **selector,
                )
                try:
                    count = scoped.count_items()
                except DeviceFacade.JsonRpcError:
                    continue
                if count <= 0:
                    continue
                if count == 1:
                    layout = self.device.find(
                        className=ClassName.IG_TEXT_LAYOUT, **selector
                    )
                    if self._layout_matches_feed_owner(layout, username):
                        return layout
                    continue
                for index in range(min(count, 8)):
                    layout = self.device.find(
                        className=ClassName.IG_TEXT_LAYOUT,
                        index=index,
                        **selector,
                    )
                    if layout.exists(Timeout.ZERO) and self._layout_matches_feed_owner(
                        layout, username
                    ):
                        return layout
            return self.device.find(
                className=ClassName.IG_TEXT_LAYOUT,
                textStartsWith="__gramaddict_no_caption__",
            )
        return self.device.find(index=-1, className=ClassName.IG_TEXT_LAYOUT)

    def _like_count_beside_heart(
        self, like_bounds: dict, child_bounds: dict
    ) -> bool:
        """True when child looks like the numeric like count beside the heart."""
        return child_bounds["left"] >= like_bounds["right"] - 20 and abs(
            child_bounds["top"] - like_bounds["top"]
        ) < 120

    def _read_like_count_from_like_row(self, like_btn) -> Optional[int]:
        """Fast path: numeric count button beside the heart in the action row."""
        try:
            like_bounds = like_btn.get_bounds()
        except DeviceFacade.JsonRpcError:
            return None
        wrapper = like_btn.up(className=ClassName.VIEW_GROUP)
        if not wrapper.exists(Timeout.ZERO):
            return None
        for finder in (
            lambda: wrapper.sibling(className=ClassName.BUTTON),
            lambda: wrapper.right(className=ClassName.BUTTON),
        ):
            try:
                candidate = finder()
                if not candidate.exists(Timeout.ZERO):
                    continue
                count = self._parse_like_count_text(self._like_count_text(candidate))
                if count is None:
                    continue
                cb = candidate.get_bounds()
                if self._like_count_beside_heart(like_bounds, cb):
                    return count
            except DeviceFacade.JsonRpcError:
                continue
        return None

    def _scan_row_like_count_candidates(
        self, row, like_bounds: dict, seen: set[str], ui_timeout: Timeout
    ) -> list:
        """Collect numeric like-count views from row_feed_view_group_buttons."""
        candidates: list = []

        def add(view, *, checked: bool = False) -> None:
            if not checked:
                try:
                    if not view.exists(Timeout.ZERO):
                        return
                except DeviceFacade.JsonRpcError:
                    return
            try:
                key = str(view.get_bounds())
            except DeviceFacade.JsonRpcError:
                return
            if key in seen:
                return
            seen.add(key)
            candidates.append(view)

        def consider(view) -> None:
            try:
                if not view.exists(Timeout.ZERO):
                    return
                text = self._like_count_text(view)
                cb = view.get_bounds()
            except DeviceFacade.JsonRpcError:
                return
            if self._parse_like_count_text(text) is None:
                return
            if self._like_count_beside_heart(like_bounds, cb):
                add(view, checked=True)

        try:
            child_count = row.child().count_items()
        except DeviceFacade.JsonRpcError:
            child_count = 0
        for index in range(child_count):
            child = row.child(index=index)
            consider(child)
            try:
                nested_count = child.child(className=ClassName.BUTTON).count_items()
            except DeviceFacade.JsonRpcError:
                nested_count = 0
            for nested_index in range(nested_count):
                consider(child.child(className=ClassName.BUTTON, index=nested_index))
        return candidates

    def _like_count_candidates(self, like_btn):
        candidates = []
        seen: set[str] = set()
        ui_timeout = self._feed_find_timeout()

        def add(view) -> None:
            try:
                if not view.exists(ui_timeout):
                    return
                key = str(view.get_bounds())
            except DeviceFacade.JsonRpcError:
                return
            if key in seen:
                return
            seen.add(key)
            candidates.append(view)

        try:
            like_bounds = like_btn.get_bounds()
        except DeviceFacade.JsonRpcError:
            return candidates

        row = like_btn.up(resourceIdMatches=ResourceID.ROW_FEED_VIEW_GROUP_BUTTONS)
        if row.exists(Timeout.ZERO) and not candidates:
            candidates.extend(
                self._scan_row_like_count_candidates(row, like_bounds, seen, Timeout.ZERO)
            )

        wrapper = like_btn.up(className=ClassName.VIEW_GROUP)
        if wrapper.exists(Timeout.ZERO) and not candidates:
            for finder in (
                lambda: wrapper.sibling(className=ClassName.BUTTON),
                lambda: wrapper.right(className=ClassName.BUTTON),
            ):
                try:
                    add(finder())
                except DeviceFacade.JsonRpcError:
                    pass
        return candidates

    def _like_count_text(self, view) -> str:
        try:
            text = view.get_text(error=False) or ""
        except DeviceFacade.JsonRpcError:
            text = ""
        if text.strip():
            return text.strip().replace(",", "")
        try:
            return (view.get_desc() or "").strip().replace(",", "")
        except DeviceFacade.JsonRpcError:
            return ""

    def _find_post_like_count_view(self, username: Optional[str] = None):
        ui_timeout = self._feed_find_timeout()
        facepile = self.device.find(
            index=-1, resourceId=ResourceID.ROW_FEED_LIKE_COUNT_FACEPILE_STUB
        )
        if facepile.exists(ui_timeout):
            return facepile
        like_btn = self._find_post_like_button_scoped(username)
        if like_btn.exists(ui_timeout):
            for candidate in self._like_count_candidates(like_btn):
                if self._parse_like_count_text(self._like_count_text(candidate)) is not None:
                    return candidate
        legacy = self.device.find(
            index=-1,
            resourceId=ResourceID.ROW_FEED_TEXTVIEW_LIKES,
            className=ClassName.TEXT_VIEW,
        )
        return legacy

    def _read_like_count(
        self,
        username: Optional[str] = None,
        *,
        like_btn=None,
    ) -> Optional[int]:
        """Parse like count beside the heart without touching stale UiObject selectors."""
        if self._is_feed_clips_viewer():
            return self._read_clips_like_count()
        if like_btn is None:
            like_btn = self._find_post_like_button_scoped(username)
        if not like_btn.exists(Timeout.ZERO):
            return None
        fast = self._read_like_count_from_like_row(like_btn)
        if fast is not None:
            return fast
        for candidate in self._like_count_candidates(like_btn):
            count = self._parse_like_count_text(self._like_count_text(candidate))
            if count is not None:
                return count
        facepile = self.device.find(
            index=-1, resourceId=ResourceID.ROW_FEED_LIKE_COUNT_FACEPILE_STUB
        )
        if facepile.exists(Timeout.ZERO):
            return -1
        legacy = self.device.find(
            index=-1,
            resourceId=ResourceID.ROW_FEED_TEXTVIEW_LIKES,
            className=ClassName.TEXT_VIEW,
        )
        if legacy.exists(Timeout.ZERO):
            return self._get_number_of_likers(legacy)
        return None

    def _find_post_like_button_scoped(
        self, username: Optional[str] = None, max_scroll_attempts: int = 3
    ):
        if self._is_feed_clips_viewer():
            return self._clips_feed_like_button()
        ui_timeout = self._feed_find_timeout()
        fallback = self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE,
        )
        if fallback.exists(ui_timeout):
            return fallback
        if username:
            profile = self.device.find(
                resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME,
                textStartsWith=username.lstrip("@"),
            )
            if profile.exists(Timeout.ZERO):
                like_btn = profile.down(
                    resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
                )
                if like_btn.exists(Timeout.ZERO):
                    return like_btn
            logger.debug(
                "Caption layout scan for @%s (like row not found yet)", username
            )
            caption = self._find_post_caption_layout(username)
            if caption.exists(Timeout.ZERO):
                like_btn = caption.up(
                    resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
                )
                if like_btn.exists(Timeout.ZERO):
                    return like_btn
                # Profile/grid posts: caption is below the action row (heart + count).
                caption_block = caption.up(className=ClassName.VIEW_GROUP)
                if caption_block.exists(Timeout.ZERO):
                    row = caption_block.sibling(
                        resourceIdMatches=ResourceID.ROW_FEED_VIEW_GROUP_BUTTONS
                    )
                    if row.exists(Timeout.ZERO):
                        like_btn = row.child(
                            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
                        )
                        if like_btn.exists(Timeout.ZERO):
                            return like_btn
        return self._find_post_like_button(max_scroll_attempts=1)

    def _post_likers_count_visible(self, username: Optional[str] = None) -> bool:
        """True when a like count, facepile, or 'Liked by…' row is shown beside the heart."""
        count = self._read_like_count(username)
        return count is not None and count != 0

    def _owner_hid_likes_on_post(self, username: Optional[str] = None) -> bool:
        """True when the post shows a like button but no visible like count to tap."""
        like_btn = self._open_post_feed_like_button()
        if like_btn.exists(Timeout.ZERO):
            return False
        ui_timeout = self._feed_find_timeout()
        like_btn = self._find_post_like_button_scoped(username)
        if not like_btn.exists(ui_timeout):
            return False
        return self._read_like_count(username) is None

    def _find_likers_container(self, username: Optional[str] = None):
        universal_actions = UniversalActions(self.device)
        containers_gap = ResourceID.GAP_VIEW_AND_FOOTER_SPACE
        likes = 0
        for _ in range(2):
            gap_view_obj = self.device.find(resourceIdMatches=containers_gap)
            likes_view = self._find_post_like_count_view(username)
            description_view = self._find_post_caption_layout(username)
            if not description_view.exists(Timeout.SHORT):
                description_view = self.device.find(
                    resourceIdMatches=ResourceID.ROW_FEED_COMMENT_TEXTVIEW_LAYOUT
                )
            media = self._feed_media_view()
            media_exists = media.exists(Timeout.SHORT)
            media_count = media.count_items() if media_exists else 0
            logger.debug(f"I can see {media_count} media(s) in this view..")

            if (
                media_exists
                and media_count > 1
                and media.get_bounds()["bottom"]
                < self.device.get_info()["displayHeight"] / 3
            ):
                universal_actions._swipe_points(Direction.DOWN, delta_y=100)
                continue
            if not likes_view.exists():
                if description_view.exists() or gap_view_obj.exists():
                    if self._owner_hid_likes_on_post(username):
                        logger.info(
                            "Post owner hid likes — likers list is not available."
                        )
                        return False, LIKES_COUNT_HIDDEN
                    return False, likes
                else:
                    universal_actions._swipe_points(Direction.DOWN, delta_y=100)
                    continue
            elif media_exists and media.get_bounds()["bottom"] > likes_view.get_bounds()["bottom"]:
                universal_actions._swipe_points(Direction.DOWN, delta_y=100)
                continue
            logger.debug("Likers container exists!")
            likes = self._get_number_of_likers(likes_view)
            return likes_view.exists(), likes
        if self._owner_hid_likes_on_post(username):
            logger.info("Post owner hid likes — likers list is not available.")
            return False, LIKES_COUNT_HIDDEN
        return False, 0

    def _get_number_of_likers(self, likes_view):
        likes = 0
        if likes_view.exists():
            likes_view_text = self._like_count_text(likes_view)
            plain_count = self._parse_like_count_text(likes_view_text)
            if plain_count is not None:
                logger.info(f"This post has {plain_count} like(s).")
                return plain_count
            matches_likes = re.search(
                r"(?P<likes>\d+) (?:others|likes)", likes_view_text, re.IGNORECASE
            )
            matches_view = re.search(
                r"(?P<views>\d+) views", likes_view_text, re.IGNORECASE
            )
            if hasattr(matches_likes, "group"):
                likes = int(matches_likes.group("likes"))
                logger.info(
                    f"This post has {likes if 'likes' in likes_view_text else likes + 1} like(s)."
                )
                return likes
            elif hasattr(matches_view, "group"):
                views = int(matches_view.group("views"))
                logger.info(
                    f"I can see only that this post has {views} views(s). It may contain likes.."
                )
                return -1
            else:
                if likes_view_text.endswith("others"):
                    logger.info("This post has more than 1 like.")
                    return -1
                else:
                    logger.info("This post has only 1 like.")
                    likes = 1
                    return likes
        else:
            logger.info("This post has no likes, skip.")
            return likes

    def _find_post_like_button(self, max_scroll_attempts: int = 3):
        """Find the heart/like button on an open post (classic feed row or clips overlay)."""
        if self._is_feed_clips_viewer():
            return self._clips_feed_like_button()
        universal_actions = UniversalActions(self.device)
        for attempt in range(max_scroll_attempts + 1):
            like_btn = self.device.find(
                resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE,
            )
            if like_btn.exists(Timeout.SHORT):
                return like_btn
            if attempt < max_scroll_attempts:
                universal_actions.scroll_to_reveal_feed_actions()
                random_sleep(0.3, 0.6, modulable=False, log=False)
        return self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE,
        )

    def _tap_likers_right_of_like_button(
        self,
        tap_offset_x: int = DEFAULT_LIKERS_TAP_OFFSET_X,
        like_btn=None,
    ) -> Optional[Tuple[int, int]]:
        """Tap just right of the post like button to open the likers sheet."""
        if like_btn is None:
            like_btn = self._open_post_feed_like_button()
        if not like_btn.exists(Timeout.ZERO):
            like_btn = self._find_post_like_button()
        if not like_btn.exists(Timeout.MEDIUM):
            logger.debug("Post like button not found for likers tap.")
            return None
        bounds = like_btn.get_bounds()
        display = self.device.get_info()
        x = int(bounds["right"] + tap_offset_x)
        y = int((bounds["top"] + bounds["bottom"]) / 2)
        x = min(max(x, 0), display["displayWidth"] - 1)
        y = min(max(y, 0), display["displayHeight"] - 1)
        logger.info(
            "Tapping likers at (%s, %s) — %spx right of %s",
            x,
            y,
            tap_offset_x,
            ResourceID.ROW_FEED_BUTTON_LIKE,
        )
        self.device.deviceV2.click(x, y)
        return x, y

    def _open_likers_by_tap(
        self,
        tap_offset_x: int = DEFAULT_LIKERS_TAP_OFFSET_X,
        *,
        like_btn=None,
    ) -> Tuple[bool, Optional[Tuple[int, int]]]:
        """Tap beside the heart and wait for the 'Liked by' likers sheet."""
        opened_post_view = OpenedPostView(self.device)
        if opened_post_view.likers_sheet_visible():
            logger.info("Likers sheet already open.")
            return True, None
        if like_btn is None:
            like_btn = self._open_post_feed_like_button()
        if not like_btn.exists(Timeout.ZERO):
            logger.info("Cannot open likers — heart not found.")
            return False, None
        coords = self._tap_likers_right_of_like_button(
            tap_offset_x, like_btn=like_btn
        )
        if coords is None:
            return False, None
        if opened_post_view.wait_for_likers_sheet():
            return True, coords
        logger.info("No 'Liked by' after tap — owner hid likes.")
        self.device.back(modulable=False)
        return False, coords

    def open_likers_container(
        self,
        tap_offset_x: int = DEFAULT_LIKERS_TAP_OFFSET_X,
        tap_offset_y: int = DEFAULT_LIKERS_TAP_OFFSET_Y,
    ) -> Optional[Tuple[int, int]]:
        """Open the likers list for the current post.

        Full-screen videos put the like count *below* the heart, so we detect that
        case first and tap ``tap_offset_y`` px under the like button; otherwise we fall
        back to the feed layout where the count sits ``tap_offset_x`` px to the right.
        """
        started = time.monotonic()
        logger.info("open_likers_container: start")
        feed_like = self._open_post_feed_like_button()
        if feed_like.exists(Timeout.ZERO):
            logger.info("open_likers_container: feed/profile like row — tap beside heart")
            opened, coords = self._open_likers_by_tap(
                tap_offset_x, like_btn=feed_like
            )
            if opened:
                logger.info(
                    "open_likers_container: likers sheet open (%.1fs)",
                    time.monotonic() - started,
                )
                return coords
            logger.info(
                "open_likers_container: likers sheet did not open (%.1fs)",
                time.monotonic() - started,
            )
            return None
        opened_post_view = OpenedPostView(self.device)
        in_fullscreen, _ = opened_post_view._is_video_in_fullscreen()
        logger.info("open_likers_container: fullscreen=%s", in_fullscreen)
        if in_fullscreen:
            coords = opened_post_view.open_likers_fullscreen(tap_offset_y)
            if coords is not None:
                return coords
            logger.debug("Fullscreen likers tap failed; trying feed layout.")
        if self._owner_hid_likes_on_post():
            logger.info("Post owner hid likes — cannot open likers list.")
            return None
        logger.info("Opening post likers.")
        facepil_stub = self.device.find(
            index=-1, resourceId=ResourceID.ROW_FEED_LIKE_COUNT_FACEPILE_STUB
        )
        if facepil_stub.exists():
            logger.debug("Facepile present, pressing on it!")
            facepil_stub.click()
            return None

        coords = self._tap_likers_right_of_like_button(tap_offset_x)
        if coords is not None:
            logger.info(
                "open_likers_container: tapped feed layout at %s (%.1fs)",
                coords,
                time.monotonic() - started,
            )
            return coords

        logger.warning("Like-button likers tap failed; trying legacy text row.")
        self._open_likers_container_legacy()
        logger.info("open_likers_container: done via legacy (%.1fs)", time.monotonic() - started)
        return None

    def _open_likers_container_legacy(self):
        """Legacy tap targets when like-button offset tap is unavailable."""
        post_liked_by_a_following = False
        random_sleep(1, 2, modulable=False)
        likes_view = self.device.find(
            index=-1,
            resourceId=ResourceID.ROW_FEED_TEXTVIEW_LIKES,
            className=ClassName.TEXT_VIEW,
        )
        if not likes_view.exists():
            return
        if " Liked by" in likes_view.get_text():
            post_liked_by_a_following = True
        elif likes_view.child().count_items() < 2:
            likes_view.click()
            return
        if likes_view.child().exists():
            if post_liked_by_a_following:
                likes_view.child().click()
                return
            foil = likes_view.get_bounds()
            hole = likes_view.child().get_bounds()
            try:
                sq1 = Square(
                    foil["left"],
                    foil["top"],
                    hole["left"],
                    foil["bottom"],
                ).point()
                sq2 = Square(
                    hole["left"],
                    foil["top"],
                    hole["right"],
                    hole["top"],
                ).point()
                sq3 = Square(
                    hole["left"],
                    hole["bottom"],
                    hole["right"],
                    foil["bottom"],
                ).point()
                sq4 = Square(
                    hole["right"],
                    foil["top"],
                    foil["right"],
                    foil["bottom"],
                ).point()
            except ValueError:
                logger.debug(f"Point calculation fails: F:{foil} H:{hole}")
                likes_view.click(Location.RIGHT)
                return
            sq_list = [sq1, sq2, sq3, sq4]
            available_sq_list = [x for x in sq_list if x == x]
            if available_sq_list:
                likes_view.click(Location.CUSTOM, coord=choice(available_sq_list))
            else:
                likes_view.click(Location.RIGHT)
        elif not post_liked_by_a_following:
            likes_view.click(Location.RIGHT)
        else:
            likes_view.click(Location.LEFT)

    def _has_tags(self) -> bool:
        tags_icon = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.INDICATOR_ICON_VIEW)
        )
        self.has_tags = tags_icon.exists()
        return self.has_tags

    def _collect_view_text_parts(
        self, view, depth: int = 0, max_depth: int = 5
    ) -> list[str]:
        """Gather visible text from nested TextViews (reel captions use IgTextLayoutView)."""
        parts: list[str] = []
        if depth > max_depth:
            return parts
        try:
            for child in view:
                try:
                    info = child.ui_info()
                except DeviceFacade.JsonRpcError:
                    continue
                cls = info.get("className") or ""
                if cls == ClassName.TEXT_VIEW:
                    t = (child.get_text(error=False) or "").strip()
                    if t:
                        parts.append(t)
                elif cls == ClassName.BUTTON:
                    try:
                        desc = (child.get_desc() or "").strip()
                        if desc and desc.lower() not in {
                            "more",
                            "like",
                            "comment",
                            "send post",
                            "add to saved",
                        }:
                            parts.append(desc)
                    except DeviceFacade.JsonRpcError:
                        pass
                elif "ViewGroup" in cls or cls == ClassName.IG_TEXT_LAYOUT:
                    parts.extend(
                        self._collect_view_text_parts(child, depth + 1, max_depth)
                    )
        except DeviceFacade.JsonRpcError:
            pass
        return parts

    def _format_reel_caption_parts(
        self, parts: list[str], username: str
    ) -> Optional[str]:
        if not parts:
            return None
        username_l = username.lower().lstrip("@")
        cleaned: list[str] = []
        for part in parts:
            p = part.strip()
            if not p:
                continue
            pl = p.lower().rstrip("…").strip()
            if pl in {"more", username_l, f"@{username_l}"}:
                continue
            if p.startswith("@"):
                continue
            if re.match(
                r"^(january|february|march|april|may|june|july|august|"
                r"september|october|november|december)\s+\d{1,2}$",
                pl,
                re.IGNORECASE,
            ):
                continue
            cleaned.append(p)
        if not cleaned:
            return None
        return " ".join(cleaned)

    @staticmethod
    def _clean_feed_caption_tail(raw: str) -> Optional[str]:
        caption = (raw or "").strip().lstrip(":").strip()
        if not caption:
            return None
        caption = re.sub(
            r"(\.\.\.|…)?\s*more$", "", caption, flags=re.IGNORECASE
        ).strip()
        caption = re.sub(r"\s*\.{3}$", "", caption).strip()
        caption = caption.rstrip("…").rstrip(".").strip()
        return caption or None

    @staticmethod
    def _extract_caption_from_combined_text(
        combined: str, username: str
    ) -> Optional[str]:
        """Parse home-feed IgTextLayoutView text: 'username caption… more'."""
        text = (combined or "").strip()
        if not text:
            return None
        uname = username.lstrip("@").strip()
        if not uname:
            return None
        for pattern in (
            rf"(?i)@?{re.escape(uname)}\s*(.*)",
            rf"(?i){re.escape(uname)}\s*(.*)",
        ):
            match = re.match(pattern, text)
            if match:
                parsed = PostsViewList._clean_feed_caption_tail(match.group(1))
                if parsed:
                    return parsed
        lower_text = text.lower()
        idx = lower_text.find(uname.lower())
        if idx >= 0:
            rest = text[idx + len(uname) :].lstrip(" @:").strip()
            parsed = PostsViewList._clean_feed_caption_tail(rest)
            if parsed:
                return parsed
        return None

    @staticmethod
    def _feed_dedup_key(username: str, caption_text: str) -> str:
        """Duplicate-post key: owner + first three caption words (IG truncates captions)."""
        uname = username.lstrip("@").strip().upper()
        preview = PostsViewList._feed_caption_preview(caption_text, 3)
        if not preview:
            return uname
        return f"{uname}|{preview.upper()}"

    def _caption_from_layout_buttons(
        self, layout_view, username: str
    ) -> Optional[str]:
        """Caption is often the 2nd Button child (username btn, then caption btn, then 'more')."""
        uname = username.lower().lstrip("@")
        try:
            caption_btn = layout_view.child(className=ClassName.BUTTON, instance=1)
            if caption_btn.exists(Timeout.TINY):
                try:
                    desc = (
                        caption_btn.get_desc()
                        or caption_btn.get_text(error=False)
                        or ""
                    ).strip()
                except DeviceFacade.JsonRpcError:
                    desc = ""
                if desc and desc.lower() not in {uname, f"@{uname}", "more"}:
                    return desc
        except DeviceFacade.JsonRpcError:
            pass
        try:
            for child in layout_view:
                try:
                    info = child.ui_info()
                except DeviceFacade.JsonRpcError:
                    continue
                if info.get("className") != ClassName.BUTTON:
                    continue
                try:
                    desc = (
                        child.get_desc() or child.get_text(error=False) or ""
                    ).strip()
                except DeviceFacade.JsonRpcError:
                    continue
                if not desc:
                    continue
                dl = desc.lower()
                if dl in {uname, f"@{uname}", "more"}:
                    continue
                if dl.startswith(uname):
                    parsed = PostsViewList._extract_caption_from_combined_text(
                        desc, username
                    )
                    return parsed or desc
                return desc
        except DeviceFacade.JsonRpcError:
            pass
        return None

    def _caption_from_ig_text_layout(
        self, layout_view, username: str
    ) -> Optional[str]:
        combined = self._combined_text_from_ig_layout(layout_view)
        if combined:
            parsed = self._extract_caption_from_combined_text(combined, username)
            if parsed:
                return parsed
            uname = username.lstrip("@").strip()
            if uname.lower() in combined.lower():
                tail = re.sub(
                    rf"(?i)^@?{re.escape(uname)}\s*", "", combined
                ).strip()
                tail = PostsViewList._clean_feed_caption_tail(tail)
                if tail:
                    return tail
        caption = self._caption_from_layout_buttons(layout_view, username)
        if caption:
            return caption
        parts = self._collect_view_text_parts(layout_view)
        return self._format_reel_caption_parts(parts, username)

    def _find_feed_caption_text(self, username: str) -> Optional[str]:
        """Home feed: clips overlay caption, IgTextLayoutView, or row_feed_text."""
        if not username:
            return None
        if self._is_feed_clips_viewer():
            return self._read_clips_feed_caption()
        ui_timeout = self._feed_caption_find_timeout()
        uname = username.lstrip("@")

        reel_caption = self._find_post_caption_layout(username)
        if reel_caption.exists(Timeout.ZERO):
            return self._caption_from_ig_text_layout(reel_caption, username)

        for selector in (
            {"textStartsWith": uname},
            {"textContains": uname},
        ):
            caption = self.device.find(
                index=-1,
                resourceIdMatches=ResourceID.ROW_FEED_TEXT,
                **selector,
            )
            if caption.exists(ui_timeout):
                return caption.get_text()
        return None

    def _check_if_last_post(
        self, last_description, current_job
    ) -> Tuple[bool, str, str, bool, bool, bool]:
        """check if that post has been just interacted"""
        universal_actions = UniversalActions(self.device)
        username, is_ad, is_hashtag = self._post_owner(
            current_job, Owner.GET_NAME
        )
        if not username:
            logger.info("Can't read post owner on feed; scrolling on.")
            return False, "", "", is_ad, is_hashtag, self._has_tags()
        has_tags = self._has_tags()
        caption_scroll_attempts = 0
        max_caption_scroll_attempts = 1 if FEED_DEBUG_FAST else 4
        while True:
            caption_text = self._find_feed_caption_text(username)
            if caption_text:
                logger.debug("Description found!")
                new_description = PostsViewList._feed_dedup_key(
                    username, caption_text
                )
                if new_description != last_description:
                    return False, new_description, username, is_ad, is_hashtag, has_tags
                logger.info(
                    "This post has the same description and author as the last one."
                )
                return True, new_description, username, is_ad, is_hashtag, has_tags
            else:
                gap_view_obj = self.device.find(resourceId=ResourceID.GAP_VIEW)
                feed_composer = self.device.find(
                    resourceId=ResourceID.FEED_INLINE_COMPOSER_BUTTON_TEXTVIEW
                )
                if gap_view_obj.exists() and gap_view_obj.get_bounds()["bottom"] < (
                    self.device.get_info()["displayHeight"] / 3
                ):
                    universal_actions._swipe_points(
                        direction=Direction.DOWN, delta_y=200
                    )
                    caption_scroll_attempts += 1
                    if caption_scroll_attempts >= max_caption_scroll_attempts:
                        logger.debug(
                            "Caption still not visible after scrolling; treating as new post."
                        )
                        return False, "", username, is_ad, is_hashtag, has_tags
                    continue
                row_feed_profile_header = self.device.find(
                    resourceId=ResourceID.ROW_FEED_PROFILE_HEADER
                )
                if row_feed_profile_header.count_items() > 1:
                    logger.debug(
                        "Multiple feed headers visible; treating post as new (no caption row)."
                    )
                    return False, "", username, is_ad, is_hashtag, has_tags
                profile_header_is_above = row_feed_profile_header.is_above_this(
                    gap_view_obj if gap_view_obj.exists() else feed_composer
                )
                if profile_header_is_above is not None:
                    logger.debug(
                        "No caption row for this post (common for video/reel posts); continuing."
                    )
                    return False, "", username, is_ad, is_hashtag, has_tags

                caption_scroll_attempts += 1
                if caption_scroll_attempts >= max_caption_scroll_attempts:
                    logger.debug(
                        f"Can't find caption for @{username} after {max_caption_scroll_attempts} scrolls; continuing."
                    )
                    return False, "", username, is_ad, is_hashtag, has_tags
                logger.debug(
                    f"Can't find the description of {username}'s post, try to swipe a little bit down."
                )
                universal_actions._swipe_points(
                    direction=Direction.DOWN, delta_y=200
                )

    def _if_action_bar_is_over_obj_swipe(self, obj):
        """do a swipe of the amount of the action bar"""
        action_bar_exists, _, action_bar_bottom = PostsViewList(
            self.device
        )._get_action_bar_position()
        if action_bar_exists:
            obj_top = obj.get_bounds()["top"]
            if action_bar_bottom > obj_top:
                UniversalActions(self.device)._swipe_points(
                    direction=Direction.UP, delta_y=action_bar_bottom
                )

    def _get_action_bar_position(self) -> Tuple[bool, int, int]:
        """action bar is overlay, if you press on it, you go back to the first post
        knowing his position is important to avoid it: exists, top, bottom"""
        action_bar = self.device.find(resourceIdMatches=ResourceID.ACTION_BAR_CONTAINER)
        if action_bar.exists():
            return (
                True,
                action_bar.get_bounds()["top"],
                action_bar.get_bounds()["bottom"],
            )
        else:
            return False, 0, 0

    def _refresh_feed(self):
        logger.info("Refresh feed..")
        for attempt in range(2):
            try:
                if not self.device.is_alive():
                    self.device.reconnect()
                refresh_pill = self.device.find(resourceId=ResourceID.NEW_FEED_PILL)
                if refresh_pill.exists(Timeout.SHORT):
                    refresh_pill.click()
                    random_sleep(inf=5, sup=8, modulable=False)
                else:
                    UniversalActions(self.device)._reload_page()
                return
            except DeviceFacade.JsonRpcError:
                if attempt == 0:
                    logger.warning(
                        "Feed refresh failed — reconnecting and retrying once."
                    )
                    self.device.reconnect()
                    continue
                logger.warning("Feed refresh skipped after reconnect failure.")
                return
            except Exception as e:
                if attempt == 0:
                    logger.warning(
                        "Feed refresh connection issue (%s) — reconnecting.",
                        e,
                    )
                    self.device.reconnect()
                    continue
                logger.warning("Feed refresh skipped: %s", e)
                return

    def _read_owner_from_open_post_caption(self) -> Optional[str]:
        """Profile/grid opened post — owner is the first username button in the caption row."""
        row = self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_VIEW_GROUP_BUTTONS
        )
        if not row.exists(Timeout.ZERO):
            return None
        caption_block = row.sibling(className=ClassName.VIEW_GROUP)
        if not caption_block.exists(Timeout.ZERO):
            return None
        caption = caption_block.child(className=ClassName.IG_TEXT_LAYOUT)
        if not caption.exists(Timeout.ZERO):
            return None
        try:
            owner_btn = caption.child(className=ClassName.BUTTON, index=0)
            if owner_btn.exists(Timeout.ZERO):
                label = (
                    owner_btn.get_desc() or owner_btn.get_text(error=False) or ""
                ).strip().lstrip("@")
                if label and label.lower() not in {"more", "like", "comment"}:
                    return label
        except DeviceFacade.JsonRpcError:
            pass
        combined = self._combined_text_from_ig_layout(caption)
        if combined:
            return combined.split()[0].lstrip("@").strip() or None
        return None

    def _post_owner(self, current_job, mode: Owner, username=None):
        """returns a tuple[var, bool, bool]"""
        is_ad = False
        is_hashtag = False
        if current_job == "feed":
            if not self.prepare_home_feed_post():
                logger.info("Like button not on screen — skip feed post reads.")
                return False, is_ad, is_hashtag
            if mode == Owner.GET_NAME and self._is_feed_clips_viewer():
                clips_owner = self._read_clips_feed_owner()
                if clips_owner:
                    logger.debug("Owner from clips overlay: @%s", clips_owner)
                    return clips_owner, is_ad, is_hashtag
                logger.info("Clips overlay open but owner not found.")
                return False, is_ad, is_hashtag
        if current_job != "feed" and mode == Owner.GET_NAME:
            caption_owner = self._read_owner_from_open_post_caption()
            if caption_owner:
                logger.debug("Owner from open-post caption: @%s", caption_owner)
                return caption_owner, is_ad, is_hashtag
        if username is None:
            if current_job == "feed":
                post_owner_obj = self._find_feed_post_owner_view()
            else:
                post_owner_obj = self.device.find(
                    resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME
                )
        else:
            for _ in range(2):
                post_owner_obj = self.device.find(
                    resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME,
                    textStartsWith=username,
                )
                notification = self.device.find(
                    resourceIdMatches=ResourceID.NOTIFICATION_MESSAGE
                )
                if not post_owner_obj.exists and notification.exists():
                    logger.warning(
                        "There is a notification there! Please disable them in settings.. We will wait 10 seconds before continue.."
                    )
                    sleep(10)
        post_owner_clickable = False
        feed_read = current_job == "feed" and mode == Owner.GET_NAME

        for _ in range(3):
            if not post_owner_obj.exists(
                Timeout.ZERO if feed_read else Timeout.TINY
            ):
                if feed_read:
                    post_owner_obj = self._find_feed_post_owner_view(username)
                    if post_owner_obj.exists(Timeout.ZERO):
                        post_owner_clickable = True
                    break
                if mode == Owner.OPEN:
                    comment_description = self.device.find(
                        resourceIdMatches=ResourceID.ROW_FEED_COMMENT_TEXTVIEW_LAYOUT,
                        textStartsWith=username,
                    )
                    if (
                        not comment_description.exists()
                        and comment_description.count_items() >= 1
                    ):
                        comment_description = self.device.find(
                            resourceIdMatches=ResourceID.ROW_FEED_COMMENT_TEXTVIEW_LAYOUT,
                            text=comment_description.get_text(),
                        )

                    if comment_description.exists():
                        logger.info("Open post owner from description.")
                        comment_description.child().click()
                        return True, is_ad, is_hashtag
                UniversalActions(self.device)._swipe_points(direction=Direction.UP)
                post_owner_obj = self.device.find(
                    resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME,
                )
            else:
                post_owner_clickable = True
                break

        if not post_owner_clickable:
            logger.info("Can't find the owner name, skip.")
            return False, is_ad, is_hashtag
        if mode == Owner.OPEN:
            logger.info("Open post owner.")
            PostsViewList(self.device)._if_action_bar_is_over_obj_swipe(post_owner_obj)
            post_owner_obj.click()
            return True, is_ad, is_hashtag
        elif mode == Owner.GET_NAME:
            if current_job == "feed":
                is_ad, is_hashtag, username = PostsViewList(
                    self.device
                )._check_if_ad_or_hashtag(post_owner_obj)
            if username is None:
                username = (
                    post_owner_obj.get_text().replace("•", "").strip().split(" ", 1)[0]
                )
            return username, is_ad, is_hashtag

        elif mode == Owner.GET_POSITION:
            return post_owner_obj.get_bounds(), is_ad
        else:
            return None, is_ad, is_hashtag

    def _get_post_owner_name(self):
        return self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_PHOTO_PROFILE_NAME
        ).get_text()

    def _get_media_container(self):
        media = self.device.find(resourceIdMatches=ResourceID.CAROUSEL_AND_MEDIA_GROUP)
        content_desc = media.get_desc() if media.exists() else None
        return media, content_desc

    @staticmethod
    def detect_media_type(content_desc) -> Tuple[Optional[MediaType], Optional[int]]:
        """
        Detect the nature and amount of a media
        :return: MediaType and count
        :rtype: MediaType, int
        """
        obj_count = 1
        if content_desc is None:
            return None, None
        if re.match(r"^,|^\s*$", content_desc, re.IGNORECASE):
            logger.info(
                "That media is missing content description, so I don't know which kind of video it is."
            )
            media_type = MediaType.UNKNOWN
        elif re.match(r"^Photo|^Hidden Photo", content_desc, re.IGNORECASE):
            logger.info("It's a photo.")
            media_type = MediaType.PHOTO
        elif re.match(r"^Video|^Hidden Video", content_desc, re.IGNORECASE):
            logger.info("It's a video.")
            media_type = MediaType.VIDEO
        elif re.match(r"^IGTV", content_desc, re.IGNORECASE):
            logger.info("It's a IGTV.")
            media_type = MediaType.IGTV
        elif re.match(r"^Reel", content_desc, re.IGNORECASE):
            logger.info("It's a Reel.")
            media_type = MediaType.REEL
        else:
            carousel_obj = re.finditer(
                r"((?P<photo>\d+) photo)|((?P<video>\d+) video)",
                content_desc,
                re.IGNORECASE,
            )
            n_photos = 0
            n_videos = 0
            for match in carousel_obj:
                if match.group("photo"):
                    n_photos = int(match.group("photo"))
                if match.group("video"):
                    n_videos = int(match.group("video"))
            logger.info(
                f"It's a carousel with {n_photos} photo(s) and {n_videos} video(s)."
            )
            obj_count = n_photos + n_videos
            media_type = MediaType.CAROUSEL
        return media_type, obj_count

    def detect_media_type_by_container(self) -> Optional[MediaType]:
        """Fallback media detection using resource-ids when contentDescription is empty."""
        clips_viewer = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.CLIPS_VIEWER_CONTAINER)
        )
        if clips_viewer.exists():
            logger.debug("Clips viewer present — it's a reel.")
            return MediaType.REEL
        video_container = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.VIDEO_CONTAINER_AND_CLIPS_VIDEO_CONTAINER
            )
        )
        if video_container.exists():
            logger.debug("Video container present — it's a video.")
            return MediaType.VIDEO
        carousel = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.CAROUSEL_MEDIA_GROUP)
        )
        if carousel.exists():
            logger.debug("Carousel media group present — it's a carousel.")
            return MediaType.CAROUSEL
        media = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.MEDIA_CONTAINER)
        )
        if media.exists():
            logger.debug("Image media container present — it's a photo.")
            return MediaType.PHOTO
        return None

    def get_home_feed_media_type(self) -> Optional[MediaType]:
        """Home feed after tap: clips/reel overlay → REEL, otherwise PHOTO."""
        if self._is_feed_clips_viewer():
            return MediaType.REEL
        return MediaType.PHOTO

    def get_media_type_and_count(self) -> Tuple[Optional[MediaType], int]:
        """Best-effort media type and item count for the open post."""
        _, content_desc = self._get_media_container()
        if content_desc:
            media_type, obj_count = self.detect_media_type(content_desc)
            if media_type not in (None, MediaType.UNKNOWN):
                return media_type, obj_count or 1
        container_type = self.detect_media_type_by_container()
        return container_type, 1

    def get_media_type(self) -> Optional[MediaType]:
        """Best-effort media type: contentDescription first, then container fallback."""
        if self._get_feed_align_key() is not None:
            return self.get_home_feed_media_type()
        media_type, _ = self.get_media_type_and_count()
        return media_type

    def _open_post_feed_like_button(self, *, fast: bool = False):
        """Heart on the bottom action row (profile post or classic feed)."""
        like_btn = self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
        )
        timeouts = (Timeout.ZERO,) if fast else (Timeout.ZERO, self._feed_find_timeout())
        for ui_timeout in timeouts:
            if not like_btn.exists(ui_timeout):
                continue
            try:
                bounds = like_btn.get_bounds()
                center_x = (bounds["left"] + bounds["right"]) / 2
                if center_x < self.device.get_info()["displayWidth"] / 2:
                    return like_btn
            except DeviceFacade.JsonRpcError:
                return like_btn
        return like_btn

    def _likers_open_status_open_post(
        self, started: float, tap_offset_x: int = DEFAULT_LIKERS_TAP_OFFSET_X
    ) -> Tuple[bool, int]:
        """Open-post likers probe: tap beside heart, wait for 'Liked by'."""
        opened_post_view = OpenedPostView(self.device)
        like_btn = self._open_post_feed_like_button(fast=True)
        if like_btn.exists(Timeout.ZERO):
            logger.info(
                "likers_open_status: heart found (%.1fs)", time.monotonic() - started
            )
            count = self._read_like_count_from_like_row(like_btn)
            if count == 1:
                logger.info(
                    "likers_open_status: only 1 like (%.1fs)",
                    time.monotonic() - started,
                )
                return False, 1
            opened, _ = self._open_likers_by_tap(tap_offset_x, like_btn=like_btn)
            if not opened:
                logger.info(
                    "likers_open_status: hidden likes (%.1fs)",
                    time.monotonic() - started,
                )
                return False, LIKES_COUNT_HIDDEN
            if count is not None and count > 1:
                return True, count
            return True, -1
        if opened_post_view.likers_sheet_visible():
            logger.info(
                "likers_open_status: likers sheet already open (%.1fs)",
                time.monotonic() - started,
            )
            return True, -1
        logger.info(
            "likers_open_status: like button missing (%.1fs)",
            time.monotonic() - started,
        )
        return False, 0

    def likers_open_status(
        self,
        owner: Optional[str] = None,
        *,
        current_job: str = "feed",
        tap_offset_x: int = DEFAULT_LIKERS_TAP_OFFSET_X,
    ) -> Tuple[bool, int]:
        """Whether the likers sheet can be opened, and the parsed like count if known."""
        started = time.monotonic()
        logger.info("likers_open_status: start (owner=%s, job=%s)", owner, current_job)
        if owner is None:
            username, _, _ = self._post_owner(current_job, Owner.GET_NAME)
            owner = username if isinstance(username, str) else None
        elif current_job == "feed" and not self.prepare_home_feed_post(owner):
            return False, 0
        if owner is None:
            logger.info("likers_open_status: no owner (%.1fs)", time.monotonic() - started)
            return False, 0
        if current_job == "blogger-post-likers":
            return self._likers_open_status_open_post(started, tap_offset_x)
        # On a single open profile/grid post, scope by owner triggers a slow
        # caption-tree scan — the visible like row is enough.
        ui_owner = None if current_job == "blogger-post-likers" else owner
        ui_timeout = self._feed_find_timeout()
        like_btn = self._find_post_like_button_scoped(ui_owner)
        if not like_btn.exists(ui_timeout):
            logger.info(
                "likers_open_status: like button missing (%.1fs)",
                time.monotonic() - started,
            )
            return False, 0
        count = self._read_like_count(ui_owner, like_btn=like_btn)
        logger.info(
            "likers_open_status: count=%s can_open=%s (%.1fs)",
            count,
            count is not None and count not in (0, 1, LIKES_COUNT_HIDDEN)
            and (count > 1 or count == -1),
            time.monotonic() - started,
        )
        if count is None:
            return False, LIKES_COUNT_HIDDEN
        if count == LIKES_COUNT_HIDDEN:
            return False, LIKES_COUNT_HIDDEN
        if count == 1:
            return False, 1
        if count > 1:
            return True, count
        if count == -1:
            return True, -1
        return False, 0

    def _like_in_post_view(
        self,
        mode: LikeMode,
        skip_media_check: bool = False,
        already_watched: bool = False,
    ):
        post_view_list = PostsViewList(self.device)
        opened_post_view = OpenedPostView(self.device)
        media_type = None
        if not skip_media_check:
            media_type = post_view_list.get_media_type()
            if media_type is None:
                return
            if not already_watched:
                opened_post_view.watch_media(media_type)
        if mode == LikeMode.DOUBLE_CLICK:
            if media_type in (MediaType.CAROUSEL, MediaType.PHOTO):
                logger.info("Double tap near screen center to like.")
                self.device.double_tap_screen_center()
            else:
                self._like_in_post_view(
                    mode=LikeMode.SINGLE_CLICK, skip_media_check=True
                )
        elif mode == LikeMode.SINGLE_CLICK:
            like_btn = post_view_list._find_post_like_button()
            if like_btn.exists():
                logger.info("Clicking on the little heart ❤️.")
                like_btn.click()

    def _follow_in_post_view(self):
        logger.info("Follow blogger in place.")
        self.device.find(resourceIdMatches=ResourceID.BUTTON).click()

    def _comment_in_post_view(self):
        logger.info("Open comments of post.")
        self.device.find(resourceIdMatches=ResourceID.ROW_FEED_BUTTON_COMMENT).click()

    def find_feed_like_button(self, max_scroll_attempts: int = 3):
        """Find home-feed like button (classic row or clips overlay after tap)."""
        if self._is_feed_clips_viewer():
            return self._clips_feed_like_button()
        for attempt in range(max_scroll_attempts + 1):
            like = self.device.find(
                resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE,
            )
            if like.exists(Timeout.SHORT):
                return like
            if attempt < max_scroll_attempts:
                UniversalActions(self.device).scroll_to_reveal_feed_actions()
                random_sleep(0.3, 0.6, modulable=False, log=False)
        return self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE,
        )

    def _check_if_liked(self, scroll_attempts: int = 0, max_scroll_attempts: int = 3):
        logger.debug("Check if like succeeded in post view.")
        if self._is_feed_clips_viewer():
            like_btn = self._clips_feed_like_button()
            if like_btn.exists(Timeout.ZERO):
                if like_btn.get_selected():
                    logger.debug("Clips overlay post is liked (selected).")
                    return True
                try:
                    desc = (like_btn.get_desc() or "").lower()
                except DeviceFacade.JsonRpcError:
                    desc = ""
                if "unlike" in desc:
                    logger.debug("Clips overlay post is liked (Unlike desc).")
                    return True
                logger.debug("Clips overlay post is not liked.")
                return False
            logger.debug("Clips like button not found.")
            return False
        bnt_like_obj = self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
        )
        if bnt_like_obj.exists():
            STR = "Liked"
            if self.device.find(descriptionMatches=case_insensitive_re(STR)).exists():
                logger.debug("Like is present.")
                return True
            else:
                logger.debug("Like is not present.")
                return False
        if scroll_attempts < max_scroll_attempts:
            UniversalActions(self.device).scroll_to_reveal_feed_actions()
            random_sleep(0.3, 0.6, modulable=False, log=False)
            return PostsViewList(self.device)._check_if_liked(
                scroll_attempts=scroll_attempts + 1,
                max_scroll_attempts=max_scroll_attempts,
            )
        logger.debug("Like button not found after scrolling.")
        return False

    def _check_if_ad_or_hashtag(
        self, post_owner_obj
    ) -> Tuple[bool, bool, Optional[str]]:
        is_hashtag = False
        is_ad = False
        logger.debug("Checking if it's an AD or an hashtag..")
        ad_like_obj = post_owner_obj.sibling(
            resourceId=ResourceID.SECONDARY_LABEL,
        )

        owner_name = post_owner_obj.get_text() or post_owner_obj.get_desc() or ""
        if not owner_name:
            logger.info("Can't find the owner name, need to use OCR.")
            try:
                import pytesseract as pt

                owner_name = self.get_text_from_screen(pt, post_owner_obj)
            except ImportError:
                logger.error(
                    "You need to install pytesseract (the wrapper: pip install pytesseract) in order to use OCR feature."
                )
            except pt.TesseractNotFoundError:
                logger.error(
                    "You need to install Tesseract (the engine: it depends on your system) in order to use OCR feature."
                )
        if owner_name.startswith("#"):
            is_hashtag = True
            logger.debug("Looks like an hashtag, skip.")
        if ad_like_obj.exists():
            sponsored_txt = "Sponsored"
            ad_like_txt = ad_like_obj.get_text() or ad_like_obj.get_desc()
            if ad_like_txt.casefold() == sponsored_txt.casefold():
                logger.debug("Looks like an AD, skip.")
                is_ad = True
            elif is_hashtag:
                owner_name = owner_name.split("•")[0].strip()

        return is_ad, is_hashtag, owner_name

    def get_text_from_screen(self, pt, obj) -> Optional[str]:

        if platform.system() == "Windows":
            pt.pytesseract.tesseract_cmd = (
                r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            )

        screenshot = self.device.screenshot()
        bounds = obj.ui_info().get("visibleBounds", None)
        if bounds is None:
            logger.info("Can't find the bounds of the object.")
            return None
        screenshot_cropped = screenshot.crop(
            [
                bounds.get("left"),
                bounds.get("top"),
                bounds.get("right"),
                bounds.get("bottom"),
            ]
        )
        return pt.image_to_string(screenshot_cropped).split(" ")[0].rstrip()


class LanguageView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def setLanguage(self, language: str):
        logger.debug(f"Set language to {language}.")
        search_edit_text = self.device.find(
            resourceId=ResourceID.SEARCH,
            className=ClassName.EDIT_TEXT,
        )
        search_edit_text.set_text(language, Mode.PASTE if args.dont_type else Mode.TYPE)

        list_view = self.device.find(
            resourceId=ResourceID.LANGUAGE_LIST_LOCALE,
            className=ClassName.LIST_VIEW,
        )
        first_item = list_view.child(index=0)
        first_item.click()
        random_sleep()


class AccountView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def navigateToLanguage(self):
        logger.debug("Navigate to Language")
        button = self.device.find(
            className=ClassName.BUTTON,
            index=6,
        )
        if button.exists():
            button.click()
            return LanguageView(self.device)
        else:
            logger.error("Not able to set your app in English! Do it by yourself!")
            exit(0)

    def navigate_to_main_account(self):
        logger.debug("Navigating to main account...")
        profile_view = ProfileView(self.device)
        profile_view.click_on_avatar()
        if profile_view.getFollowingCount() is None:
            profile_view.click_on_avatar()

    def _normalize_username(self, value: Optional[str]) -> str:
        return (value or "").strip().lstrip("@")

    def _username_text_pattern(self, username: str) -> str:
        safe = re.escape(self._normalize_username(username))
        return f"(?i)^@?{safe}$"

    def _account_switcher_open(self) -> bool:
        return self.device.find(resourceId=ResourceID.LIST).exists(Timeout.SHORT)

    def changeToUsername(self, username: str):
        username = self._normalize_username(username)
        if not username:
            return False
        action_bar = ProfileView._getActionBarTitleBtn(self)
        if action_bar is not None:
            current_profile_name = self._normalize_username(action_bar.get_text())
            # in private accounts there is little lock which is codec as two spaces (should be \u1F512)
            if current_profile_name.upper() == username.upper():
                logger.info(
                    f"You are already logged as {username}!",
                    extra={"color": f"{Style.BRIGHT}{Fore.BLUE}"},
                )
                return True
            logger.debug(f"You're logged as {current_profile_name}")
        if not self._account_switcher_open():
            selector = self.device.find(resourceId=ResourceID.ACTION_BAR_TITLE_CHEVRON)
            if not selector.exists(Timeout.MEDIUM):
                return False
            selector.click()
        if self._find_username(username):
            action_bar = ProfileView._getActionBarTitleBtn(self)
            if action_bar is not None:
                current_profile_name = self._normalize_username(action_bar.get_text())
                if current_profile_name.upper() == username.upper():
                    return True
            else:
                logger.error(
                    "Cannot find action bar (where you select your account)!"
                )
            return True
        return False

    def _find_username(self, username, has_scrolled=False):
        username = self._normalize_username(username)
        list_view = self.device.find(resourceId=ResourceID.LIST)
        username_obj = self.device.find(
            resourceIdMatches=f"{ResourceID.ROW_USER_TEXTVIEW}|{ResourceID.USERNAME_TEXTVIEW}",
            textMatches=self._username_text_pattern(username),
        )
        if username_obj.exists(Timeout.SHORT):
            logger.info(
                f"Switching to {username}...",
                extra={"color": f"{Style.BRIGHT}{Fore.BLUE}"},
            )
            username_obj.click()
            return True
        elif list_view.is_scrollable() and not has_scrolled:
            logger.debug("User list is scrollable.")
            list_view.scroll(Direction.DOWN)
            return self._find_username(username, has_scrolled=True)
        return False

    def refresh_account(self):
        textview = self.device.find(
            resourceIdMatches=ResourceID.ROW_PROFILE_HEADER_TEXTVIEW_POST_CONTAINER
        )
        universal_actions = UniversalActions(self.device)
        if textview.exists(Timeout.SHORT):
            logger.info("Refresh account...")
            universal_actions._swipe_points(
                direction=Direction.UP,
                start_point_y=textview.get_bounds()["bottom"],
                delta_y=280,
            )
            random_sleep(modulable=False)
        obj = self.device.find(
            resourceIdMatches=ResourceID.ROW_PROFILE_HEADER_TEXTVIEW_POST_CONTAINER
        )
        if not obj.exists(Timeout.MEDIUM):
            logger.debug(
                "Can't see Posts, Followers and Following after the refresh, maybe we moved a little bit bottom.. Swipe down."
            )
            universal_actions._swipe_points(Direction.UP)


class SettingsView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def navigateToAccount(self):
        logger.debug("Navigate to Account")
        button = self.device.find(
            className=ClassName.BUTTON,
            index=5,
        )
        if button.exists():
            button.click()
            return AccountView(self.device)
        else:
            logger.error("Not able to set your app in English! Do it by yourself!")
            exit(2)


class OptionsView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def navigateToSettings(self):
        logger.debug("Navigate to Settings")
        button = self.device.find(
            resourceId=ResourceID.MENU_OPTION_TEXT,
            className=ClassName.TEXT_VIEW,
        )
        if button.exists():
            button.click()
            return SettingsView(self.device)
        else:
            logger.error("Not able to set your app in English! Do it by yourself!")
            exit(0)


class OpenedPostView:
    def __init__(self, device: DeviceFacade):
        self.device = device
        self.has_tags = False

    def _get_focused_post_media(self) -> Optional[DeviceFacade.View]:
        """Largest visible media when browsing profile Posts (RecyclerView feed)."""
        best_height = 0
        best_view = None
        for rid in (
            ResourceID.CAROUSEL_VIDEO_MEDIA_GROUP,
            ResourceID.VIDEO_CONTAINER,
            ResourceID.CAROUSEL_MEDIA_GROUP,
            ResourceID.MEDIA_CONTAINER,
        ):
            views = self.device.find(
                resourceIdMatches=case_insensitive_re(rid),
            )
            if not views.exists(Timeout.ZERO):
                continue
            try:
                count = views.count_items()
            except Exception:
                count = 1
            for index in range(max(count, 1)):
                candidate = (
                    views
                    if count <= 1
                    else self.device.find(
                        resourceIdMatches=case_insensitive_re(rid), index=index
                    )
                )
                if not candidate.exists(Timeout.ZERO):
                    continue
                try:
                    bounds = candidate.get_bounds()
                except DeviceFacade.JsonRpcError:
                    continue
                height = bounds["bottom"] - bounds["top"]
                if height > best_height:
                    best_height = height
                    best_view = candidate
        return best_view

    def _get_post_like_button(self) -> Optional[DeviceFacade.View]:
        post_view_list = PostsViewList(self.device)
        if post_view_list._is_feed_clips_viewer():
            like_button = post_view_list._clips_feed_like_button()
            if like_button.exists(Timeout.ZERO):
                return like_button
            return None

        media = self._get_focused_post_media()
        if media is not None and media.exists(Timeout.ZERO):
            try:
                media_bottom = media.get_bounds()["bottom"]
            except DeviceFacade.JsonRpcError:
                media_bottom = None
            if media_bottom is not None:
                rows = self.device.find(
                    resourceIdMatches=ResourceID.ROW_FEED_VIEW_GROUP_BUTTONS
                )
                if rows.exists(Timeout.SHORT):
                    try:
                        row_count = rows.count_items()
                    except Exception:
                        row_count = 1
                    best_like = None
                    best_gap = None
                    for index in range(max(row_count, 1)):
                        row = (
                            rows
                            if row_count <= 1
                            else self.device.find(
                                resourceIdMatches=ResourceID.ROW_FEED_VIEW_GROUP_BUTTONS,
                                index=index,
                            )
                        )
                        if not row.exists(Timeout.ZERO):
                            continue
                        try:
                            row_top = row.get_bounds()["top"]
                        except DeviceFacade.JsonRpcError:
                            continue
                        gap = row_top - media_bottom
                        if gap < -30 or gap > 250:
                            continue
                        like_button = row.child(
                            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
                        )
                        if like_button.exists(Timeout.ZERO):
                            if best_gap is None or abs(gap) < abs(best_gap):
                                best_gap = gap
                                best_like = like_button
                    if best_like is not None:
                        return best_like

        like_btn = post_view_list._open_post_feed_like_button()
        if like_btn.exists(Timeout.SHORT):
            return like_btn
        return post_view_list._find_post_like_button(max_scroll_attempts=1)

    def _is_post_liked(self) -> Tuple[Optional[bool], Optional[DeviceFacade.View]]:
        """
        Check if post is liked
        :return: post is liked or not
        :rtype: bool
        """
        like_btn_view = self._get_post_like_button()
        if not like_btn_view or not like_btn_view.exists(Timeout.ZERO):
            return None, None

        try:
            if like_btn_view.get_selected():
                return True, like_btn_view
        except DeviceFacade.JsonRpcError:
            pass
        try:
            desc = (like_btn_view.get_desc() or "").lower()
            if "unlike" in desc or desc == "liked":
                return True, like_btn_view
        except DeviceFacade.JsonRpcError:
            pass
        if self.device.find(
            descriptionMatches=case_insensitive_re("^Liked$")
        ).exists(Timeout.ZERO):
            return True, like_btn_view
        return False, like_btn_view

    def like_post(self) -> bool:
        """
        Like the post with a heart tap, falling back to double-tap on the media.
        :return: post has been liked
        :rtype: bool
        """
        like_btn = self._get_post_like_button()
        if like_btn and like_btn.exists(Timeout.SHORT):
            logger.info("Clicking on the little heart ❤️.")
            like_btn.click()
            UniversalActions.detect_block(self.device)
            liked, _ = self._is_post_liked()
            if liked:
                return True

        media = self._get_focused_post_media()
        if media is not None and media.exists(Timeout.ZERO):
            logger.info("Liking video with double-tap on media.")
            media.double_click()
        else:
            logger.info("Liking post with double-tap near screen center.")
            self.device.double_tap_screen_center()
        UniversalActions.detect_block(self.device)
        liked, _ = self._is_post_liked()
        return bool(liked)

    def start_video(self) -> bool:
        """
        Press on play button if present
        :return: has play button been pressed
        :rtype: bool
        """
        play_button = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.VIEW_PLAY_BUTTON)
        )
        if play_button.exists(Timeout.TINY):
            logger.debug("Pressing on play button.")
            play_button.click()
            return True
        return False

    def open_video(self) -> bool:
        """
        Open / play the video. Modern Instagram autoplays feed videos inline
        (no separate fullscreen), so a present video container counts as open.
        :return: video is open / playing
        :rtype: bool
        """
        already_open, _ = self._is_video_in_fullscreen()
        if already_open:
            logger.info("Video already playing.")
            return True
        post_media_view = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.MEDIA_CONTAINER)
        )
        if not post_media_view.exists():
            post_media_view = self.device.find(
                resourceIdMatches=case_insensitive_re(
                    ResourceID.VIDEO_CONTAINER_AND_CLIPS_VIDEO_CONTAINER
                )
            )
        if post_media_view.exists():
            logger.info("Tapping video to play / open.")
            post_media_view.click()
            in_fullscreen, _ = self._is_video_in_fullscreen()
            return in_fullscreen
        return False

    def watch_media(self, media_type: MediaType) -> None:
        """
        Watch media for the amount of time specified in config
        :return: None
        :rtype: None
        """
        if (
            media_type
            in (MediaType.IGTV, MediaType.REEL, MediaType.VIDEO, MediaType.UNKNOWN)
            and args.watch_video_time != "0"
        ):
            in_fullscreen, _ = self._is_video_in_fullscreen()
            time_left = self._get_video_time_left()
            watching_time = get_value(
                args.watch_video_time, name=None, default=0, its_time=True
            )
            if time_left > 0 and media_type != MediaType.REEL and in_fullscreen:
                logger.info(f"This video is about {time_left}s long.")
                # hardcoded 5 seconds, so we have the time to doing everything without going to the next video, hopefully
                watching_time = min(
                    watching_time,
                    time_left - 5,
                )
            logger.info(
                f"Watching video for {watching_time if watching_time > 0 else 'few '}s."
            )

        elif (
            media_type in (MediaType.CAROUSEL, MediaType.PHOTO)
            and args.watch_photo_time != "0"
        ):
            self._has_tags()
            watching_time = get_value(
                args.watch_photo_time, "Watching photo for {}s.", 0, its_time=True
            )
        else:
            return None
        if watching_time > 0:
            sleep(watching_time)

    def _get_video_time_left(self) -> int:
        timer = self.device.find(resourceId=ResourceID.TIMER)
        if timer.exists():
            raw_time = timer.get_text().split(":")
            try:
                return int(raw_time[0]) * 60 + int(raw_time[1])
            except (IndexError, ValueError):
                return 0
        return 0

    def _has_bottom_feed_like_row(self) -> bool:
        """True when the classic bottom-left heart row is visible (feed / profile post)."""
        like_button = self.device.find(
            resourceIdMatches=ResourceID.ROW_FEED_BUTTON_LIKE
        )
        if not like_button.exists(Timeout.ZERO) and not like_button.exists(
            Timeout.TINY
        ):
            return False
        try:
            bounds = like_button.get_bounds()
        except DeviceFacade.JsonRpcError:
            return True
        center_x = (bounds["left"] + bounds["right"]) / 2
        return center_x < self.device.get_info()["displayWidth"] / 2

    def _is_video_in_fullscreen(self) -> Tuple[bool, DeviceFacade.View]:
        """
        Check if video is in full-screen mode.

        Two signals mark full-screen playback:
          1. a ``video_container`` / ``clips_video_container`` is present, or
          2. the like button sits on the right side of the screen (the vertical
             action rail used by reels / full-screen video), whereas in the feed
             it lives at the bottom-left under the media.
        """
        if self._like_button_on_right_side():
            like_button = self.device.find(
                resourceIdMatches=case_insensitive_re(ResourceID.LIKE_BUTTON)
            )
            return True, like_button
        video_container = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.VIDEO_CONTAINER_AND_CLIPS_VIDEO_CONTAINER
            )
        )
        if video_container.exists():
            if self._has_bottom_feed_like_row():
                return False, video_container
            return True, video_container
        return False, video_container

    def _like_button_on_right_side(self) -> bool:
        """True when the like button is on the right half of the screen.

        A right-side like button is the reliable, version-agnostic marker of a
        full-screen video (reels / clips vertical action rail). In the normal feed
        the like button is on the left, so this stays False there.
        """
        like_button = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.LIKE_BUTTON)
        )
        if not like_button.exists(Timeout.ZERO):
            return False
        try:
            bounds = like_button.get_bounds()
        except Exception:
            return False
        display_width = self.device.get_info()["displayWidth"]
        center_x = (bounds["left"] + bounds["right"]) / 2
        return center_x > display_width / 2

    def _is_video_liked(self) -> Tuple[Optional[bool], Optional[DeviceFacade.View]]:
        """
        Check if video has been liked
        """
        like_button = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.LIKE_BUTTON)
        )
        if like_button.exists():
            return like_button.get_selected(), like_button
        return False, None

    def _find_fullscreen_like_button(self):
        """Find the heart/like button in the full-screen video (clips/reels) viewer."""
        return self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.LIKE_BUTTON)
        )

    def _find_fullscreen_comment_button(self):
        """Find the comment button in the full-screen video (clips/reels) viewer."""
        btn = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.COMMENT_BUTTON)
        )
        if btn.exists(Timeout.SHORT):
            return btn
        btn = self.device.find(
            descriptionMatches=case_insensitive_re(r"^Comment(\s|$| number)"),
        )
        if btn.exists(Timeout.SHORT):
            return btn
        stack = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.UFI_STACK)
        )
        if stack.exists(Timeout.ZERO):
            child = stack.child(
                resourceIdMatches=case_insensitive_re(ResourceID.COMMENT_BUTTON)
            )
            if child.exists(Timeout.SHORT):
                return child
        return self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.COMMENT_BUTTON)
        )

    def find_comment_button(self):
        """Feed post comment row or fullscreen reels UFI comment button."""
        feed_btn = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.ROW_FEED_BUTTON_COMMENT),
        )
        if feed_btn.exists(Timeout.ZERO):
            return feed_btn
        in_fullscreen, _ = self._is_video_in_fullscreen()
        if in_fullscreen:
            return self._find_fullscreen_comment_button()
        return feed_btn

    def _find_fullscreen_likes_label(self):
        """Find the word 'likes' shown under the heart in a full-screen video.

        When a full-screen video shows the *word* 'likes' (e.g. "Likes", any case)
        rather than a number beneath the like button, the owner has hidden their like
        count. Tapping that word reveals the count. The label may expose the word via
        its ``text`` or its ``contentDescription``, so we check both and return the
        first match that sits below the heart (or ``None``).
        """
        like_button = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.LIKE_BUTTON)
        )
        if not like_button.exists(Timeout.ZERO):
            return None
        try:
            like_bounds = like_button.get_bounds()
        except Exception:
            return None
        # Match the word "likes" anywhere in the label (case-insensitive).
        pattern = case_insensitive_re(r".*\blikes\b.*")
        for selector in ("textMatches", "descriptionMatches"):
            label = self.device.find(**{selector: pattern})
            if not label.exists():
                continue
            try:
                count = label.count_items()
            except Exception:
                count = 1
            for index in range(max(count, 1)):
                candidate = (
                    label
                    if count <= 1
                    else self.device.find(**{selector: pattern}, index=index)
                )
                try:
                    label_bounds = candidate.get_bounds()
                except Exception:
                    continue
                # The label must sit below the heart (the hidden-likes affordance).
                if label_bounds["top"] < like_bounds["top"]:
                    continue
                return candidate
        return None

    def fullscreen_likes_hidden(self) -> bool:
        """True when a full-screen video shows the word 'likes' (owner hid the count)."""
        in_fullscreen, _ = self._is_video_in_fullscreen()
        if not in_fullscreen:
            return False
        return self._find_fullscreen_likes_label() is not None

    def reveal_fullscreen_hidden_likes(self) -> bool:
        """Tap the word 'likes' under a full-screen video's heart to reveal the count.

        Only works in full-screen playback. Returns True when the 'likes' label was
        found and tapped.
        """
        in_fullscreen, _ = self._is_video_in_fullscreen()
        if not in_fullscreen:
            logger.debug("Not full-screen; cannot reveal hidden likes here.")
            return False
        label = self._find_fullscreen_likes_label()
        if label is None:
            return False
        logger.info("Full-screen video hides likes — tapping 'likes' to reveal count.")
        label.click()
        return True

    def open_likers_fullscreen(
        self, tap_offset_y: int = DEFAULT_LIKERS_TAP_OFFSET_Y
    ) -> Optional[Tuple[int, int]]:
        """Open the likers list for a full-screen video.

        In full-screen playback the like count sits *below* the heart (not beside it
        like feed posts), so we tap ``tap_offset_y`` pixels under the like button.
        If the owner hid the count, the word 'likes' appears there instead — tapping
        it reveals the count first, then we open the likers list.
        Returns the tapped (x, y) on success, or ``None`` when the video isn't in
        full-screen or the like button can't be found.
        """
        in_fullscreen, _ = self._is_video_in_fullscreen()
        if not in_fullscreen:
            logger.debug(
                "Video is not full-screen; feed likers flow should be used instead."
            )
            return None
        if self._find_fullscreen_likes_label() is not None:
            self.reveal_fullscreen_hidden_likes()
            random_sleep(0.5, 1, modulable=False, log=False)
        like_btn = self._find_fullscreen_like_button()
        if not like_btn.exists(Timeout.MEDIUM):
            logger.debug("Full-screen like button not found for likers tap.")
            return None
        bounds = like_btn.get_bounds()
        display = self.device.get_info()
        x = int((bounds["left"] + bounds["right"]) / 2)
        y = int(bounds["bottom"] + tap_offset_y)
        x = min(max(x, 0), display["displayWidth"] - 1)
        y = min(max(y, 0), display["displayHeight"] - 1)
        logger.info(
            "Tapping full-screen likers at (%s, %s) — %spx below %s",
            x,
            y,
            tap_offset_y,
            ResourceID.LIKE_BUTTON,
        )
        self.device.deviceV2.click(x, y)
        return x, y

    def _has_tags(self) -> bool:
        tags_icon = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.INDICATOR_ICON_VIEW)
        )
        self.has_tags = tags_icon.exists()
        return self.has_tags

    def like_video(self) -> bool:
        """
        Like the video with a double click and check if it's liked
        :return: video has been liked
        :rtype: bool
        """
        sidebar = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.UFI_STACK)
        )
        liked = False
        full_screen, obj = self._is_video_in_fullscreen()
        if full_screen:
            logger.info("Liking video.")
            obj.double_click()
            UniversalActions.detect_block(self.device)
            if not sidebar.exists():
                logger.debug("Showing sidebar...")
                obj.click()
            liked, like_button = self._is_video_liked()
            if not liked:
                logger.info("Double click failed, clicking on the little heart ❤️.")
                if like_button is not None:
                    like_button.click()
                    UniversalActions.detect_block(self.device)
                else:
                    logger.error("We are seeing another video.")
                liked, _ = self._is_video_liked()
        return liked

    def likers_sheet_visible(self) -> bool:
        """True when the likers bottom sheet is open.

        Instagram's likers header varies by app version and post type:
        "Liked by …" (facepile row), "Likes", or — when a post also has
        emoji reactions — "Likes and reactions". Match any of them so we
        don't wrongly conclude the owner hid likes.
        """
        header = case_insensitive_re(r"(liked by.*|likes and reactions|likes)")
        if self.device.find(textMatches=header).exists(Timeout.ZERO):
            return True
        for needle in ("Liked by", "Likes and reactions"):
            if self.device.find(textContains=needle).exists(Timeout.ZERO):
                return True
        return False

    def wait_for_likers_sheet(self, ui_timeout: Timeout = Timeout.MEDIUM) -> bool:
        """Wait up to 5s for the likers header after tapping the like count."""
        deadline = time.monotonic() + DeviceFacade.View.get_ui_timeout(ui_timeout)
        while time.monotonic() < deadline:
            if self.likers_sheet_visible():
                logger.info("Likers sheet open — header visible.")
                return True
            sleep(0.25)
        logger.info("Likers header did not appear — likes are hidden.")
        return False

    def _getListViewLikers(self):
        for _ in range(2):
            obj = self.device.find(resourceId=ResourceID.LIST)
            if obj.exists(Timeout.LONG):
                return obj
            logger.debug("Can't find likers list, try again..")
        logger.error("Can't load likers list..")
        return None

    def _getUserContainer(self):
        obj = self.device.find(
            resourceIdMatches=ResourceID.USER_LIST_CONTAINER,
        )
        return obj if obj.exists(Timeout.LONG) else None

    def _getUserName(self, container):
        return container.child(
            resourceId=ResourceID.ROW_USER_PRIMARY_NAME,
        )

    def _isFollowing(self, container):
        text = container.child(
            resourceId=ResourceID.BUTTON,
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
        )
        if not isinstance(text, str):
            text = text.get_text() if text.exists() else ""
        return text in ["Following", "Requested"]


class PostsGridView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def scrollDown(self):
        coordinator_layout = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.COORDINATOR_ROOT_LAYOUT)
        )
        if coordinator_layout.exists():
            coordinator_layout.scroll(Direction.DOWN)
            return True

        return False

    def _get_post_view(self):
        return self.device.find(resourceIdMatches=case_insensitive_re(ResourceID.LIST))

    def _grid_cell(self, row: int, col: int):
        post_list_view = self._get_post_view()
        if not post_list_view.exists(Timeout.ZERO):
            return self.device.find(resourceId="__gramaddict_missing__")
        offset = 1  # row with post thumbnails starts at index 1
        row_view = post_list_view.child(index=row + offset)
        if not row_view.exists(Timeout.ZERO):
            return self.device.find(resourceId="__gramaddict_missing__")
        return row_view.child(index=col)

    def is_post_tappable(self, row: int = 0, col: int = 0) -> bool:
        """True when the profile grid cell at (row, col) is on screen and clickable."""
        return self._grid_cell(row, col).exists(Timeout.ZERO)

    def is_cell_pinned(self, row: int, col: int) -> bool:
        """Best-effort pin badge detection (Instagram often omits it from the a11y tree)."""
        cell = self._grid_cell(row, col)
        if not cell.exists(Timeout.ZERO):
            return False
        for pattern in (r".*pin.*", r"^pinned$"):
            pat = case_insensitive_re(pattern)
            if cell.child(descriptionMatches=pat).exists(Timeout.ZERO):
                return True
            if cell.child(textMatches=pat).exists(Timeout.ZERO):
                return True
        pin_overlay = cell.child(
            resourceIdMatches=case_insensitive_re(r".*(?:pin|pinned).*"),
        )
        if pin_overlay.exists(Timeout.ZERO):
            rid = str((pin_overlay.ui_info() or {}).get("resourceId") or "").lower()
            if "image_button" not in rid:
                return True
        return False

    def should_skip_cell(self, row: int, col: int, skip_top: int) -> bool:
        flat = row * 3 + col
        if skip_top > 0 and flat < skip_top:
            return True
        return self.is_cell_pinned(row, col)

    def open_first_post_after_skip(self, skip_top: int = 3):
        if not self.is_post_tappable(0, 0) and not self.is_post_tappable(1, 0):
            ProfileView(self.device).swipe_to_fit_posts()
        for row in range(6):
            for col in range(3):
                if not self.is_post_tappable(row, col):
                    continue
                if self.should_skip_cell(row, col, skip_top):
                    reason = (
                        "pin badge in a11y tree"
                        if self.is_cell_pinned(row, col)
                        else f"top {skip_top} grid slot(s)"
                    )
                    logger.info(
                        f"Skip profile grid post at row {row + 1} col {col + 1} ({reason}).",
                        extra={"color": f"{Fore.CYAN}"},
                    )
                    continue
                logger.info(
                    f"Opening first post after skip at row {row + 1} col {col + 1}."
                )
                return self.navigateToPost(row, col)
        logger.warning("No post found on profile grid after skipping top slots.")
        return None, None, None

    def find_post_by_grid_position(self, row: int, col: int):
        """Find a grid thumbnail by Instagram's a11y content-desc.

        Each thumbnail's content-desc encodes its *visual* position, e.g.
        ``"Reel by <name> at row 2, column 1"`` (casing of "row"/"column"
        varies by build). Matching on this is far more reliable than
        ``child(index=...)`` — uiautomator2 does not expose grid cells in
        visual order, so index-based lookups land on the wrong cell.

        ``row``/``col`` are 1-based to match Instagram's own numbering.
        """
        pattern = case_insensitive_re(rf".*\bat row {row}, column {col}\b.*")
        return self.device.find(
            resourceIdMatches=ResourceID.IMAGE_BUTTON,
            descriptionMatches=pattern,
        )

    def _ensure_grid_cell_tappable(self, post_view) -> bool:
        """Scroll the profile grid until ``post_view`` has a tappable height.

        When row 2 peeks just above the tab bar, its bounds can be a few dozen
        pixels tall — clicking that sliver often misses or hits the wrong cell.
        """
        try:
            bounds = post_view.get_bounds()
        except Exception:
            return True
        height = int(bounds.get("bottom", 0) - bounds.get("top", 0))
        if height >= 120:
            return True
        logger.debug(
            f"Grid cell only {height}px tall — scrolling to bring it fully on screen."
        )
        for _ in range(3):
            UniversalActions(self.device)._swipe_points(
                direction=Direction.DOWN, delta_y=randint(280, 380)
            )
            random_sleep(0.4, 0.8, modulable=False)
            if not post_view.exists(Timeout.SHORT):
                return False
            try:
                bounds = post_view.get_bounds()
                height = int(bounds.get("bottom", 0) - bounds.get("top", 0))
            except Exception:
                return True
            if height >= 120:
                return True
        return height >= 60

    def _post_view_opened(self) -> bool:
        """True when a post/reel media view is on screen (grid tap succeeded)."""
        try:
            return OpenedPostView(self.device)._get_focused_post_media() is not None
        except Exception:
            return False

    def open_post_by_grid_position(self, row: int, col: int):
        """Open the grid post at the given 1-based visual ``row``/``col``.

        Uses Instagram's a11y content-desc (``at row N, column M``). Returns
        ``(OpenedPostView, media_type, obj_count)`` on success, or
        ``(None, None, None)`` when the thumbnail can't be located / didn't open.
        """
        post_view = self.find_post_by_grid_position(row, col)
        if not post_view.exists(Timeout.MEDIUM):
            return None, None, None
        if not self._ensure_grid_cell_tappable(post_view):
            post_view = self.find_post_by_grid_position(row, col)
            if not post_view.exists(Timeout.SHORT):
                return None, None, None
        post_view.click()
        random_sleep(0.4, 0.8, modulable=False)
        if not self._post_view_opened():
            logger.debug(
                f"Content-desc tap for row {row} col {col} did not open a post."
            )
            return None, None, None
        media_type, obj_count = PostsViewList(self.device).get_media_type_and_count()
        return OpenedPostView(self.device), media_type, obj_count

    def open_post_by_hierarchy_index(self, row: int, col: int):
        """Open the post via hierarchy child indexes (0-based row/col).

        Works on older/different profile layouts where content-desc doesn't
        include ``at row N, column M``. Can be wrong when the a11y tree order
        doesn't match the visual grid — see ``navigateToPost``.
        """
        post_list_view = self._get_post_view()
        post_list_view.wait(Timeout.MEDIUM)
        OFFSET = 1  # row with post starts from index 1
        row_view = post_list_view.child(index=row + OFFSET)
        if not row_view.exists():
            return None, None, None
        post_view = row_view.child(index=col)
        if not post_view.exists():
            return None, None, None
        post_view.click()
        random_sleep(0.4, 0.8, modulable=False)
        if not self._post_view_opened():
            logger.debug(
                f"Hierarchy-index tap for row {row + 1} col {col + 1} did not open a post."
            )
            return None, None, None
        media_type, obj_count = PostsViewList(self.device).get_media_type_and_count()
        return OpenedPostView(self.device), media_type, obj_count

    def navigateToPost(self, row, col):
        """Open the post at 0-based ``row``/``col`` using both lookup methods.

        Profiles differ across Instagram builds:
          1) content-desc (``at row N, column M``) — best when present
          2) hierarchy child(index=…) — still needed when desc is missing

        Try content-desc first; if that cell isn't found or the tap didn't open
        a post, fall back to the hierarchy-index method.
        """
        has_desc = self.find_post_by_grid_position(row + 1, col + 1).exists(
            Timeout.SHORT
        )
        if has_desc:
            opened, media_type, obj_count = self.open_post_by_grid_position(
                row + 1, col + 1
            )
            if opened is not None:
                logger.debug(
                    f"Opened grid post via content-desc (row {row + 1}, col {col + 1})."
                )
                return opened, media_type, obj_count
            # Content-desc existed but tap failed — back to the grid if needed,
            # then try hierarchy index.
            if not self._get_post_view().exists(Timeout.ZERO):
                self.device.back()
                random_sleep(0.4, 0.8, modulable=False)

        opened, media_type, obj_count = self.open_post_by_hierarchy_index(row, col)
        if opened is not None:
            logger.debug(
                f"Opened grid post via hierarchy index (row {row + 1}, col {col + 1})."
            )
            return opened, media_type, obj_count

        # Last try: content-desc if we skipped it (wasn't visible at first).
        if not has_desc:
            opened, media_type, obj_count = self.open_post_by_grid_position(
                row + 1, col + 1
            )
            if opened is not None:
                logger.debug(
                    f"Opened grid post via content-desc (retry) "
                    f"(row {row + 1}, col {col + 1})."
                )
                return opened, media_type, obj_count

        return None, None, None


class ProfileView(ActionBarView):
    def __init__(self, device: DeviceFacade, is_own_profile=False):
        super().__init__(device)
        self.device = device
        self.is_own_profile = is_own_profile

    def navigateToOptions(self):
        logger.debug("Navigate to Options")
        button = self.action_bar.child(index=2)
        button.click()

        return OptionsView(self.device)

    def _getActionBarTitleBtn(self, watching_stories=False, ui_timeout=None):
        bar = case_insensitive_re(
            [
                ResourceID.TITLE_VIEW,
                ResourceID.ACTION_BAR_TITLE,
                ResourceID.ACTION_BAR_LARGE_TITLE,
                ResourceID.ACTION_BAR_TEXTVIEW_TITLE,
                ResourceID.ACTION_BAR_TITLE_AUTO_SIZE,
                ResourceID.ACTION_BAR_LARGE_TITLE_AUTO_SIZE,
            ]
        )
        action_bar = self.device.find(
            resourceIdMatches=bar,
        )
        timeout = ui_timeout
        if timeout is None:
            timeout = Timeout.ZERO if watching_stories else Timeout.LONG
        if not watching_stories and action_bar.exists(timeout) or watching_stories:
            return action_bar
        logger.error(
            "Unable to find action bar! (The element with the username at top)"
        )
        return None

    def _is_count_text(self, text: str) -> bool:
        t = text.strip().casefold()
        if not t:
            return True
        return bool(re.match(r"^[\d,.\s]+[kmb]?$", t))

    def _label_from_content_desc(self, desc: Optional[str]) -> Optional[str]:
        if not desc:
            return None
        cleaned = desc.strip()
        parts = cleaned.split()
        if len(parts) >= 2 and self._is_count_text(parts[0]):
            return " ".join(parts[1:]).casefold()
        # The familiar header glues the count to the label with no space, e.g.
        # "2posts", "23following", "1.2Kfollowers". Strip a leading count token so
        # the label ("posts"/"followers"/"following") can still be recovered.
        glued = re.match(r"^\s*[\d.,]+[kmb]?\s*([a-z].*)$", cleaned, re.IGNORECASE)
        if glued and glued.group(1):
            return glued.group(1).strip().casefold()
        return cleaned.casefold()

    def _get_profile_stat_label(
        self,
        value_resource_id: str,
        label_resource_id: str,
        legacy_container_id: str,
        container_resource_id: Optional[str] = None,
    ) -> Optional[str]:
        label_view = self.device.find(
            resourceIdMatches=case_insensitive_re(label_resource_id)
        )
        if label_view.exists(Timeout.SHORT):
            text = label_view.get_text()
            if text:
                return text.casefold()

        value_view = self.device.find(
            resourceIdMatches=case_insensitive_re(value_resource_id)
        )
        if value_view.exists(Timeout.SHORT):
            parent = value_view.parent()
            if parent.exists(Timeout.SHORT):
                for idx in range(6):
                    child = parent.child(index=idx)
                    if not child.exists():
                        break
                    text = child.get_text()
                    if text and not self._is_count_text(text):
                        return text.casefold()
                label = self._label_from_content_desc(parent.get_desc())
                if label and not self._is_count_text(label):
                    return label
            label = self._label_from_content_desc(value_view.get_desc())
            if label and not self._is_count_text(label):
                return label

        # Sturdiest fallback: the familiar stat container exposes count+label in
        # its content-desc ("2posts", "23following"), which survives even when the
        # inner value/label text views haven't rendered yet.
        if container_resource_id:
            container = self.device.find(
                resourceIdMatches=case_insensitive_re(container_resource_id)
            )
            if container.exists(Timeout.SHORT):
                label = self._label_from_content_desc(container.get_desc())
                if label and not self._is_count_text(label):
                    return label

        legacy = self.device.find(resourceIdMatches=legacy_container_id)
        if legacy.exists(Timeout.SHORT):
            return legacy.child(index=1).get_text().casefold()
        return None

    def _getSomeText(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Get some text from the profile to check the language"""
        has_header = self.device.find(
            resourceIdMatches=ResourceID.PROFILE_HEADER_FAMILIAR_POST_COUNT_VALUE
        ).exists(Timeout.SHORT) or self.device.find(
            resourceIdMatches=ResourceID.ROW_PROFILE_HEADER_POST_COUNT_CONTAINER
        ).exists(Timeout.SHORT)
        if not has_header:
            UniversalActions(self.device)._swipe_points(Direction.UP)
        try:
            post = self._get_profile_stat_label(
                ResourceID.PROFILE_HEADER_FAMILIAR_POST_COUNT_VALUE,
                ResourceID.PROFILE_HEADER_FAMILIAR_POST_COUNT_LABEL,
                ResourceID.ROW_PROFILE_HEADER_POST_COUNT_CONTAINER,
                ResourceID.PROFILE_HEADER_POST_COUNT_FRONT_FAMILIAR,
            )
            followers = self._get_profile_stat_label(
                ResourceID.PROFILE_HEADER_FAMILIAR_FOLLOWERS_VALUE,
                ResourceID.PROFILE_HEADER_FAMILIAR_FOLLOWERS_LABEL,
                ResourceID.ROW_PROFILE_HEADER_FOLLOWERS_CONTAINER_LEGACY,
                ResourceID.PROFILE_HEADER_FOLLOWERS_STACKED_FAMILIAR,
            )
            following = self._get_profile_stat_label(
                ResourceID.PROFILE_HEADER_FAMILIAR_FOLLOWING_VALUE,
                ResourceID.PROFILE_HEADER_FAMILIAR_FOLLOWING_LABEL,
                ResourceID.ROW_PROFILE_HEADER_FOLLOWING_CONTAINER_LEGACY,
                ResourceID.PROFILE_HEADER_FOLLOWING_STACKED_FAMILIAR,
            )
            if None in {post, followers, following}:
                raise ValueError("profile header labels not found")
            return post, followers, following
        except Exception as e:
            logger.debug(f"Exception: {e}")
            logger.warning(
                "Can't get post/followers/following text for check the language! Save a crash to understand the reason."
            )
            save_crash(self.device)
            return None, None, None

    def _new_ui_profile_button(self) -> bool:
        found = False
        buttons = self.device.find(className=ResourceID.BUTTON)
        for button in buttons:
            if button.get_desc() == "Profile":
                button.click()
                found = True
        return found

    def _old_ui_profile_button(self) -> bool:
        found = False
        obj = self.device.find(resourceIdMatches=ResourceID.TAB_AVATAR)
        if obj.exists(Timeout.MEDIUM):
            obj.click()
            found = True
        return found

    def click_on_avatar(self):
        while True:
            if self._new_ui_profile_button():
                break
            if self._old_ui_profile_button():
                break
            self.device.back()

    def getFollowButton(self):
        button_regex = f"{ClassName.BUTTON}|{ClassName.TEXT_VIEW}"
        following_regex_all = "^following|^requested|^follow back|^follow"
        following_or_follow_back_button = self.device.find(
            classNameMatches=button_regex,
            clickable=True,
            textMatches=case_insensitive_re(following_regex_all),
        )
        if following_or_follow_back_button.exists(Timeout.MEDIUM):
            button_text = following_or_follow_back_button.get_text().casefold()
            if button_text in ["following", "requested"]:
                button_status = FollowStatus.FOLLOWING
            elif button_text == "follow back":
                button_status = FollowStatus.FOLLOW_BACK
            else:
                button_status = FollowStatus.FOLLOW
            return following_or_follow_back_button, button_status
        else:
            logger.warning(
                "The follow button doesn't exist! Maybe the profile is not loaded!"
            )
            return None, FollowStatus.NONE

    def getUsername(self, watching_stories=False, ui_timeout=None):
        action_bar = self._getActionBarTitleBtn(
            watching_stories, ui_timeout=ui_timeout
        )
        if action_bar is not None:
            return action_bar.get_text(error=not watching_stories).strip()
        if not watching_stories:
            logger.error("Cannot get username.")
        return None

    def getLinkInBio(self):
        obj = self.device.find(resourceIdMatches=ResourceID.PROFILE_HEADER_WEBSITE)
        if obj.exists():
            website = obj.get_text()
            return website if website != "" else None
        return None

    def getMutualFriends(self) -> int:
        logger.debug("Looking for mutual friends tab.")
        follow_context = self.device.find(
            resourceIdMatches=ResourceID.PROFILE_HEADER_FOLLOW_CONTEXT_TEXT
        )
        if follow_context.exists():
            text = follow_context.get_text()
            mutual_friends = re.finditer(
                r"((?P<others>\s\d+\s)|(?P<extra>,))",
                text,
                re.IGNORECASE,
            )
            n_others = 0
            n_extra = 0
            for match in mutual_friends:
                if match.group("others"):
                    n_others = int(match.group("others"))
                if match.group("extra"):
                    n_extra = 2
            if n_others != 0:
                mutual_friends = n_others + n_extra if n_extra != 0 else n_others + 1
            else:
                mutual_friends = n_extra if n_extra != 0 else 1
        else:
            mutual_friends = 0
        return mutual_friends

    def _parseCounter(self, raw_text: str) -> Optional[int]:
        multiplier = 1
        regex = r"(?!(K|M|\.))\D+"
        subst = "."
        text = re.sub(regex, subst, raw_text)
        if "K" in text:
            value = float(text.replace("K", ""))
            multiplier = 1_000
        elif "M" in text:
            value = float(text.replace("M", ""))
            multiplier = 1_000_000
        else:
            try:
                value = int(text.replace(".", ""))
            except ValueError:
                logger.error(f"Cannot parse {repr(raw_text)}.")
                return None
        return int(value * multiplier)

    def _getFollowersTextView(self):
        followers_text_view = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.ROW_PROFILE_HEADER_TEXTVIEW_FOLLOWERS_COUNT
            ),
            className=ClassName.TEXT_VIEW,
        )
        followers_text_view.wait(Timeout.MEDIUM)
        return followers_text_view

    def getFollowersCount(self) -> Optional[int]:
        followers = None
        followers_text_view = self._getFollowersTextView()
        if followers_text_view.exists():
            followers_text = followers_text_view.get_text()
            if followers_text:
                followers = self._parseCounter(followers_text)
            else:
                logger.error("Cannot get followers count text.")
        else:
            logger.error("Cannot find followers count view.")

        return followers

    def _getFollowingTextView(self):
        following_text_view = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.ROW_PROFILE_HEADER_TEXTVIEW_FOLLOWING_COUNT
            ),
            className=ClassName.TEXT_VIEW,
        )
        following_text_view.wait(Timeout.MEDIUM)
        return following_text_view

    def getFollowingCount(self) -> Optional[int]:
        following = None
        following_text_view = self._getFollowingTextView()
        if following_text_view.exists(Timeout.MEDIUM):
            following_text = following_text_view.get_text()
            if following_text:
                following = self._parseCounter(following_text)
            else:
                logger.error("Cannot get following count text.")
        else:
            logger.error("Cannot find following count view.")

        return following

    def getPostsCount(self) -> int:
        post_count_view = self.device.find(
            resourceIdMatches=case_insensitive_re(
                ResourceID.ROW_PROFILE_HEADER_TEXTVIEW_POST_COUNT
            )
        )
        if post_count_view.exists(Timeout.MEDIUM):
            count = post_count_view.get_text()
            if count is not None:
                return self._parseCounter(count)
        logger.error("Cannot get posts count text.")
        return 0

    def count_photo_in_view(self) -> Tuple[int, int]:
        """return rows filled and the number of post in the last row"""
        views = f"({ClassName.RECYCLER_VIEW}|{ClassName.VIEW})"
        grid_post = self.device.find(
            classNameMatches=views, resourceIdMatches=ResourceID.LIST
        )
        if not grid_post.exists(Timeout.MEDIUM):
            return 0, 0
        for i in range(2, 6):
            lin_layout = grid_post.child(index=i, className=ClassName.LINEAR_LAYOUT)
            if i == 5 or not lin_layout.exists():
                last_index = i - 1
                last_lin_layout = grid_post.child(index=last_index)
                for n in range(1, 4):
                    if n == 3 or not last_lin_layout.child(index=n).exists():
                        if n == 3:
                            return last_index, 0
                        else:
                            return last_index - 1, n

    def getProfileInfo(self):
        username = self.getUsername()
        posts = self.getPostsCount()
        followers = self.getFollowersCount()
        following = self.getFollowingCount()

        # The profile header is often still rendering at session start (and right
        # after switching accounts), so the count views read as missing:
        # posts=0 and followers/following=None. Give it a moment and read once
        # more before accepting the miss — this prevents the false
        # "Cannot get posts count text." and the downstream soft-ban false alarm.
        if username is None or followers is None or following is None:
            logger.debug(
                "Profile header looks unloaded (username=%s, followers=%s, "
                "following=%s); retrying profile read once.",
                username,
                followers,
                following,
            )
            random_sleep(2.0, 3.0, modulable=False)
            if username is None:
                username = self.getUsername()
            posts = self.getPostsCount()
            retry_followers = self.getFollowersCount()
            retry_following = self.getFollowingCount()
            if retry_followers is not None:
                followers = retry_followers
            if retry_following is not None:
                following = retry_following

        return username, posts, followers, following

    def getProfileBiography(self) -> str:
        biography = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.PROFILE_HEADER_BIO_TEXT),
            className=ClassName.TEXT_VIEW,
        )
        if biography.exists():
            biography_text = biography.get_text()
            # If the biography is very long, blabla text and end with "...more" click the bottom of the text and get the new text
            is_long_bio = re.compile(
                r"{0}$".format("… more"), flags=re.IGNORECASE
            ).search(biography_text)
            if is_long_bio is not None:
                logger.debug('Found "… more" in bio - trying to expand')
                username = self.getUsername()
                biography.click(Location.BOTTOMRIGHT)
                if username != self.getUsername():
                    logger.debug(
                        "We're not in the same page - did we click a hashtag or a tag? Go back."
                    )
                    self.device.back()
                    logger.info("Failed to expand biography - checking short view.")
                return biography.get_text()
            return biography_text
        return ""

    def getFullName(self):
        full_name_view = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.PROFILE_HEADER_FULL_NAME),
            className=ClassName.TEXT_VIEW,
        )
        if full_name_view.exists(Timeout.SHORT):
            fullname_text = full_name_view.get_text()
            if fullname_text is not None:
                return fullname_text
        return ""

    def isPrivateAccount(self):
        private_profile_view = self.device.find(
            resourceIdMatches=case_insensitive_re(
                [
                    ResourceID.PRIVATE_PROFILE_EMPTY_STATE,
                    ResourceID.ROW_PROFILE_HEADER_EMPTY_PROFILE_NOTICE_TITLE,
                    ResourceID.ROW_PROFILE_HEADER_EMPTY_PROFILE_NOTICE_CONTAINER,
                ]
            )
        )
        return private_profile_view.exists()

    def StoryRing(self) -> DeviceFacade.View:
        return self.device.find(
            resourceId=ResourceID.REEL_RING,
        )

    def has_story_to_like(self, ui_timeout=Timeout.SHORT) -> bool:
        live_timeout = Timeout.ZERO if ui_timeout == Timeout.ZERO else Timeout.TINY
        if self.live_marker().exists(live_timeout):
            return False
        return self.StoryRing().exists(ui_timeout)

    def has_unviewed_story(self) -> bool:
        if not self.has_story_to_like():
            return False
        ring = self.StoryRing()
        try:
            desc = (ring.ui_info().get("contentDescription") or "").lower()
            if "seen" in desc:
                return False
        except Exception:
            pass
        return True

    def live_marker(self) -> DeviceFacade.View:
        return self.device.find(resourceId=ResourceID.LIVE_BADGE_VIEW)

    def profileImage(self):
        return self.device.find(
            resourceId=ResourceID.ROW_PROFILE_HEADER_IMAGEVIEW,
        )

    def navigateToFollowers(self):
        logger.info("Navigate to followers.")
        followers_button = self.device.find(
            resourceIdMatches=ResourceID.ROW_PROFILE_HEADER_FOLLOWERS_CONTAINER
        )
        if followers_button.exists(Timeout.LONG):
            followers_button.click()
            followers_tab = self.device.find(
                resourceIdMatches=ResourceID.UNIFIED_FOLLOW_LIST_TAB_LAYOUT
            ).child(textContains="Followers")
            if followers_tab.exists(Timeout.LONG):
                if not followers_tab.get_property("selected"):
                    followers_tab.click()
                return True
        else:
            # The button is often just hidden behind a popup/permission sheet.
            # Ask the vision model to dismiss it, then retry once.
            if _recover_from_popup(self.device, "navigateToFollowers"):
                if followers_button.exists(Timeout.LONG):
                    followers_button.click()
                    followers_tab = self.device.find(
                        resourceIdMatches=ResourceID.UNIFIED_FOLLOW_LIST_TAB_LAYOUT
                    ).child(textContains="Followers")
                    if followers_tab.exists(Timeout.LONG):
                        if not followers_tab.get_property("selected"):
                            followers_tab.click()
                        return True
            logger.error("Can't find followers tab!")
            return False

    def navigateToFollowing(self):
        logger.info("Navigate to following.")
        following_button = self.device.find(
            resourceIdMatches=ResourceID.ROW_PROFILE_HEADER_FOLLOWING_CONTAINER
        )
        if following_button.exists(Timeout.LONG):
            following_button.click_retry()
            following_tab = self.device.find(
                resourceIdMatches=ResourceID.UNIFIED_FOLLOW_LIST_TAB_LAYOUT
            ).child(textContains="Following")
            if following_tab.exists(Timeout.LONG):
                if not following_tab.get_property("selected"):
                    following_tab.click()
                return True
        else:
            # The button is often just hidden behind a popup/permission sheet.
            # Ask the vision model to dismiss it, then retry once.
            if _recover_from_popup(self.device, "navigateToFollowing"):
                if following_button.exists(Timeout.LONG):
                    following_button.click_retry()
                    following_tab = self.device.find(
                        resourceIdMatches=ResourceID.UNIFIED_FOLLOW_LIST_TAB_LAYOUT
                    ).child(textContains="Following")
                    if following_tab.exists(Timeout.LONG):
                        if not following_tab.get_property("selected"):
                            following_tab.click()
                        return True
            logger.error("Can't find following tab!")
            return False

    def navigateToMutual(self):
        logger.info("Navigate to mutual friends.")
        has_mutual = False
        follow_context = self.device.find(
            resourceIdMatches=ResourceID.PROFILE_HEADER_FOLLOW_CONTEXT_TEXT
        )
        if follow_context.exists():
            follow_context.click()
            has_mutual = True
        return has_mutual

    def swipe_to_fit_posts(self):
        """Scroll profile down until the post grid is tappable (legacy GramAddict helper)."""
        if PostsGridView(self.device).is_post_tappable(0, 0):
            logger.debug("Post grid already on screen — skip profile scroll.")
            return 0

        displayWidth = self.device.get_info()["displayWidth"]
        element_to_swipe_over_obj = self.device.find(
            resourceIdMatches=ResourceID.PROFILE_TABS_CONTAINER
        )
        for _ in range(2):
            if PostsGridView(self.device).is_post_tappable(0, 0):
                logger.debug("Post grid visible — skip profile scroll.")
                return 0
            if not element_to_swipe_over_obj.exists():
                UniversalActions(self.device)._swipe_points(
                    direction=Direction.DOWN, delta_y=randint(300, 350)
                )
                element_to_swipe_over_obj = self.device.find(
                    resourceIdMatches=ResourceID.PROFILE_TABS_CONTAINER
                )
                continue

            try:
                element_to_swipe_over = element_to_swipe_over_obj.get_bounds()["top"]
                bar_container = self.device.find(
                    resourceIdMatches=ResourceID.ACTION_BAR_CONTAINER
                ).get_bounds()["bottom"]
                if element_to_swipe_over <= bar_container + 8:
                    logger.debug(
                        "Profile tabs already aligned with action bar — skip scroll."
                    )
                    return 0
                logger.info("Scrolled down to see more posts.")
                self.device.swipe_points(
                    displayWidth / 2,
                    element_to_swipe_over,
                    displayWidth / 2,
                    bar_container,
                )
                return element_to_swipe_over - bar_container
            except (DeviceFacade.JsonRpcError, Exception) as e:
                logger.debug("Profile precision scroll failed: %s", e)
                UniversalActions(self.device)._swipe_points(
                    direction=Direction.DOWN, delta_y=randint(300, 350)
                )
                if PostsGridView(self.device).is_post_tappable(0, 0):
                    logger.debug("Post grid visible after fallback profile scroll.")
                    return 1
                return 0
        logger.warning(
            "Maybe a private/empty profile in which check failed or after whatching stories the view moves down :S.. Skip"
        )
        return -1

    def navigateToPostsTab(self):
        self._navigateToTab(TabBarText.POSTS_CONTENT_DESC)
        return PostsGridView(self.device)

    def navigateToIgtvTab(self):
        self._navigateToTab(TabBarText.IGTV_CONTENT_DESC)
        raise Exception("Not implemented")

    def navigateToReelsTab(self):
        self._navigateToTab(TabBarText.REELS_CONTENT_DESC)
        raise Exception("Not implemented")

    def navigateToEffectsTab(self):
        self._navigateToTab(TabBarText.EFFECTS_CONTENT_DESC)
        raise Exception("Not implemented")

    def navigateToPhotosOfYouTab(self):
        self._navigateToTab(TabBarText.PHOTOS_OF_YOU_CONTENT_DESC)
        raise Exception("Not implemented")

    def _navigateToTab(self, tab: TabBarText):
        tabs_view = self.device.find(
            resourceIdMatches=case_insensitive_re(ResourceID.PROFILE_TAB_LAYOUT),
            className=ClassName.HORIZONTAL_SCROLL_VIEW,
        )
        button = tabs_view.child(
            descriptionMatches=case_insensitive_re(tab),
            resourceIdMatches=case_insensitive_re(ResourceID.PROFILE_TAB_ICON_VIEW),
            className=ClassName.IMAGE_VIEW,
        )

        attempts = 0
        while not button.exists():
            attempts += 1
            self.device.swipe(Direction.UP, scale=0.1)
            if attempts > 2:
                logger.error(f"Cannot navigate to tab '{tab}'")
                save_crash(self.device)
                return

        button.click()

    def _getRecyclerView(self):
        views = f"({ClassName.RECYCLER_VIEW}|{ClassName.VIEW})"

        return self.device.find(classNameMatches=views)


class FollowingView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def _find_row_username_view(self, user_row):
        """Locate the username text view inside a following-list row."""
        name_view = self.device.find(resourceId=ResourceID.FOLLOW_LIST_USERNAME)
        if name_view.exists(Timeout.SHORT):
            return name_view
        try:
            candidate = user_row.child(resourceId=ResourceID.FOLLOW_LIST_USERNAME)
            if candidate.exists(Timeout.SHORT):
                return candidate
        except Exception:
            pass
        try:
            candidate = user_row.child(index=1).child().child()
            if candidate.exists(Timeout.SHORT):
                return candidate
        except Exception:
            pass
        return None

    def _open_profile_via_username_text(self, user_row) -> bool:
        """Open a user's profile by tapping the first characters of their name.

        Tapping the avatar/row opens the story ring instead of the profile, so we
        tap the left edge of the username text (its first couple of characters).
        """
        name_view = self._find_row_username_view(user_row)
        if name_view is None:
            logger.error("Cannot find username text to open profile.")
            return False
        try:
            bounds = name_view.get_bounds()
        except Exception:
            logger.error("Cannot read username bounds to open profile.")
            return False
        # Tap the first few characters (left edge of the name, not the avatar).
        x = int(bounds["left"] + max(8, (bounds["right"] - bounds["left"]) * 0.15))
        y = int((bounds["top"] + bounds["bottom"]) / 2)
        display = self.device.get_info()
        x = min(max(x, 0), display["displayWidth"] - 1)
        y = min(max(y, 0), display["displayHeight"] - 1)
        logger.debug("Tapping username text at (%s, %s) to open profile.", x, y)
        self.device.deviceV2.click(x, y)
        random_sleep(1, 2, modulable=False)
        return True

    def _unfollow_from_open_profile(self, username) -> bool:
        """Unfollow the currently-open profile via its Following/Requested button."""
        following_button = self.device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            clickable=True,
            textMatches=case_insensitive_re("^Following|^Requested"),
        )
        if not following_button.exists(Timeout.MEDIUM):
            logger.error(f"No Following button on {username}'s profile.")
            return False
        following_button.click()
        random_sleep(1, 2, modulable=False)
        confirm_unfollow_button = self.device.find(
            resourceIdMatches=case_insensitive_re(
                f"{ResourceID.PRIMARY_BUTTON}|{ResourceID.FOLLOW_SHEET_UNFOLLOW_ROW}"
            ),
            textMatches=case_insensitive_re("^Unfollow$"),
        )
        if confirm_unfollow_button.exists(Timeout.MEDIUM):
            random_sleep(1, 2)
            confirm_unfollow_button.click()
        UniversalActions.detect_block(self.device)
        # After unfollowing, the button becomes "Follow" (they don't follow us) or
        # "Follow Back" (they follow us). Match both, but NOT "Following".
        follow_button = self.device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            clickable=True,
            textMatches=case_insensitive_re(r"^Follow(\sBack)?$"),
        )
        if follow_button.exists(Timeout.MEDIUM):
            logger.info(
                f"{username} unfollowed.",
                extra={"color": f"{Style.BRIGHT}{Fore.GREEN}"},
            )
            return True
        # Fallback: unfollow is confirmed if the "Following/Requested" button is gone.
        still_following = self.device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            clickable=True,
            textMatches=case_insensitive_re("^Following|^Requested"),
        )
        if not still_following.exists(Timeout.SHORT):
            logger.info(
                f"{username} unfollowed.",
                extra={"color": f"{Style.BRIGHT}{Fore.GREEN}"},
            )
            return True
        logger.error(f"Cannot confirm unfollow for {username}.")
        save_crash(self.device)
        return False

    def do_unfollow_from_list(self, username, user_row=None) -> bool:
        if user_row is None:
            user_row = self.device.find(
                resourceId=ResourceID.FOLLOW_LIST_CONTAINER,
                className=ClassName.LINEAR_LAYOUT,
            )
        if not user_row.exists(Timeout.MEDIUM):
            logger.error(f"Cannot find {username} in following list.")
            return False
        # Open the profile by tapping the username text (first characters) rather
        # than the avatar, which would open the user's story instead.
        if not self._open_profile_via_username_text(user_row):
            return False
        unfollowed = self._unfollow_from_open_profile(username)
        # Return to the following list so callers can search for the next user.
        self.device.back()
        random_sleep(1, 2, modulable=False)
        return unfollowed


class FollowersView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def _find_user_to_remove(self, username):
        """Return the follow-list row whose username matches exactly (case-insensitive).

        Instagram shows multiple rows; matching the exact username avoids acting on
        the wrong (usually top) account.
        """
        target = (username or "").strip().lstrip("@").lower()
        rows = self.device.find(resourceId=ResourceID.FOLLOW_LIST_CONTAINER)
        if not rows.exists(Timeout.MEDIUM):
            return None
        try:
            count = rows.count_items()
        except Exception:
            count = 1
        for index in range(max(count, 1)):
            row = (
                rows
                if count <= 1
                else self.device.find(
                    resourceId=ResourceID.FOLLOW_LIST_CONTAINER, index=index
                )
            )
            name_view = row.child(resourceId=ResourceID.FOLLOW_LIST_USERNAME)
            if not name_view.exists():
                continue
            row_username = (name_view.get_text() or "").strip().lstrip("@").lower()
            if row_username == target:
                return row
        return None

    def _get_remove_button(self, row_obj):
        REMOVE_TEXT = "^Remove$"
        # Remove is scoped to the specific row (Button or TextView). No screen-wide
        # fallback — that could hit the wrong account's button.
        remove = row_obj.child(
            resourceId=ResourceID.BUTTON, textMatches=case_insensitive_re(REMOVE_TEXT)
        )
        if remove.exists(Timeout.SHORT):
            return remove
        return row_obj.child(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            textMatches=case_insensitive_re(REMOVE_TEXT),
        )

    def _get_dismiss_button(self, row_obj):
        """The X icon (contentDescription 'Dismiss') on the given row.

        It's an ImageView with no resource-id, so we match it by content-description.
        Scoped to the row only — a screen-wide match would tap the top account's X.
        """
        DISMISS_DESC = "^Dismiss$"
        return row_obj.child(descriptionMatches=case_insensitive_re(DISMISS_DESC))

    def _get_action_sheet_remove(self):
        """The 'Remove' option inside the bottom-sheet action menu (after tapping X)."""
        obj = self.device.find(
            resourceId=ResourceID.ACTION_SHEET_ROW_TEXT_VIEW,
            textMatches=case_insensitive_re("^Remove$"),
        )
        if obj.exists(Timeout.SHORT):
            return obj
        return self.device.find(resourceId=ResourceID.ACTION_SHEET_ROW_TEXT_VIEW)

    def _click_button(self, obj, obj_name):
        if obj is not None and obj.exists(Timeout.SHORT):
            logger.info(f"Pressing on {obj_name} button.")
            obj.click()
            return True
        logger.info(f"Object {obj_name} doesn't exists. Can't press on it!")
        return False

    def _confirm_remove_follower(self) -> bool:
        """Tap the confirm 'Remove' in the dialog some versions show after the sheet."""
        confirm = self.device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            textMatches=case_insensitive_re("^Remove$"),
        )
        if confirm.exists(Timeout.SHORT):
            return self._click_button(confirm, "remove confirmation")
        return False

    def remove_follower(self, username):
        user_row = self._find_user_to_remove(username)
        if user_row is None or not user_row.exists():
            logger.error(f"Cannot find {username} in followers list.")
            return False

        # Case 1: a Remove button is shown directly on the row.
        direct_remove = self._get_remove_button(user_row)
        if direct_remove.exists(Timeout.SHORT):
            self._click_button(direct_remove, "remove")
            random_sleep(1, 2, modulable=False)
            self._confirm_remove_follower()
            return True

        # Case 2 (modern): tap the X (Dismiss) to open the action sheet, then tap
        # its 'Remove' row — that tap performs the removal.
        dismiss = self._get_dismiss_button(user_row)
        if not self._click_button(dismiss, "dismiss (X)"):
            logger.error(f"No Remove button or X (Dismiss) for {username}.")
            return False
        random_sleep(1, 2, modulable=False)
        action_remove = self._get_action_sheet_remove()
        if not self._click_button(action_remove, "action sheet remove"):
            logger.error(f"Action sheet Remove not found for {username}.")
            return False
        random_sleep(1, 2, modulable=False)
        # Some versions then show a confirm dialog; tap it if present (optional).
        self._confirm_remove_follower()
        return True


class CurrentStoryView:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def getStoryFrame(self) -> DeviceFacade.View:
        return self.device.find(
            resourceId=ResourceID.REEL_VIEWER_MEDIA_CONTAINER,
        )

    def getUsername(self) -> str:
        reel_viewer_title = self.device.find(
            resourceId=ResourceID.REEL_VIEWER_TITLE,
        )
        reel_exists = reel_viewer_title.exists(ignore_bug=True)
        if reel_exists == "BUG!":
            return reel_exists
        return (
            ""
            if not reel_exists
            else reel_viewer_title.get_text(error=False).replace(" ", "")
        )

    def getTimestamp(self) -> Optional[datetime.datetime]:
        reel_viewer_timestamp = self.device.find(
            resourceId=ResourceID.REEL_VIEWER_TIMESTAMP,
        )
        if reel_viewer_timestamp.exists():
            timestamp = reel_viewer_timestamp.get_text().strip()
            value = int(re.sub("[^0-9]", "", timestamp))
            if timestamp[-1] == "s":
                return datetime.timestamp(
                    datetime.datetime.now() - datetime.timedelta(seconds=value)
                )
            elif timestamp[-1] == "m":
                return datetime.timestamp(
                    datetime.datetime.now() - datetime.timedelta(minutes=value)
                )
            elif timestamp[-1] == "h":
                return datetime.timestamp(
                    datetime.datetime.now() - datetime.timedelta(hours=value)
                )
            else:
                return datetime.timestamp(
                    datetime.datetime.now() - datetime.timedelta(days=value)
                )
        return None


class UniversalActions:
    def __init__(self, device: DeviceFacade):
        self.device = device

    def scroll_to_reveal_feed_actions(self, fraction: float = 0.2, log: bool = True) -> int:
        """Scroll feed down ~fraction of screen height so the like row below the image is visible."""
        display_height = self.device.get_info()["displayHeight"]
        delta_y = max(int(display_height * fraction), 120)
        if log:
            logger.info(
                "Scrolling feed down ~%.0f%% to reveal like button.", fraction * 100
            )
        else:
            logger.debug(
                "Scrolling feed down ~%.0f%% to reveal like button.", fraction * 100
            )
        self._swipe_points(
            direction=Direction.DOWN,
            delta_y=delta_y,
        )
        return delta_y

    def _swipe_points(
        self,
        direction: Direction,
        start_point_x=-1,
        start_point_y=-1,
        delta_x=-1,
        delta_y=450,
    ) -> None:
        displayWidth = self.device.get_info()["displayWidth"]
        displayHeight = self.device.get_info()["displayHeight"]
        middle_point_x = displayWidth / 2
        if start_point_y == -1:
            start_point_y = displayHeight / 2
        if direction == Direction.UP:
            if start_point_y + delta_y > displayHeight:
                delta = start_point_y + delta_y - displayHeight
                start_point_y = start_point_y - delta
            self.device.swipe_points(
                middle_point_x,
                start_point_y,
                middle_point_x,
                start_point_y + delta_y,
            )
        elif direction == Direction.DOWN:
            if start_point_y - delta_y < 0:
                delta = abs(start_point_y - delta_y)
                start_point_y = start_point_y + delta
            self.device.swipe_points(
                middle_point_x,
                start_point_y,
                middle_point_x,
                start_point_y - delta_y,
            )
        elif direction == Direction.LEFT:
            if start_point_x == -1:
                start_point_x = displayWidth * 2 / 3
            if delta_x == -1:
                delta_x = uniform(0.95, 1.25) * (displayWidth / 2)
            self.device.swipe_points(
                start_point_x,
                start_point_y,
                start_point_x - delta_x,
                start_point_y,
            )

    def press_button_back(self) -> None:
        back_button = self.device.find(
            resourceIdMatches=ResourceID.ACTION_BAR_BUTTON_BACK
        )
        if back_button.exists():
            logger.info("Pressing on back button.")
            back_button.click()

    def _reload_page(self) -> None:
        logger.debug("Reload page.")
        self._swipe_points(direction=Direction.UP)
        random_sleep(inf=5, sup=8, modulable=False)

    @staticmethod
    def detect_block(device) -> bool:
        if not args.disable_block_detection:
            return False
        logger.debug("Checking for block...")
        if "blocked" in device.deviceV2.toast.get_message(1.0, 2.0, default=""):
            logger.warning("Toast detected!")
        serius_block = device.find(
            className=ClassName.IMAGE,
            textMatches=case_insensitive_re("Force reset password icon"),
        )
        if serius_block.exists():
            raise ActionBlockedError("Serius block detected :(")
        block_dialog = device.find(
            resourceIdMatches=ResourceID.BLOCK_POPUP,
        )
        popup_body = device.find(
            resourceIdMatches=ResourceID.IGDS_HEADLINE_BODY,
        )
        popup_appears = block_dialog.exists()
        if popup_appears:
            if popup_body.exists():
                regex = r".+deleted"
                is_post_deleted = re.match(regex, popup_body.get_text(), re.IGNORECASE)
                if is_post_deleted:
                    logger.info(f"{is_post_deleted.group()}")
                    logger.debug("Click on OK button.")
                    device.find(
                        resourceIdMatches=ResourceID.NEGATIVE_BUTTON,
                    ).click()
                    is_blocked = False
                else:
                    is_blocked = True
            else:
                is_blocked = True
        else:
            is_blocked = False

        if is_blocked:
            logger.error("Probably block dialog is shown.")
            raise ActionBlockedError(
                "Seems that action is blocked. Consider reinstalling Instagram app and be more careful with limits!"
            )

    def _check_if_no_posts(self) -> bool:
        obj = self.device.find(resourceId=ResourceID.IGDS_HEADLINE_EMPHASIZED_HEADLINE)
        return obj.exists(Timeout.MEDIUM)

    @staticmethod
    def _partial_search_query(username: str) -> str:
        """Return ~50% of the username to type into search.

        Long usernames typed in full sometimes fail to surface the matching result
        in the dropdown, so we only enter a prefix (about half the characters). A
        short minimum keeps the query specific enough to find the right person.
        """
        text = (username or "").strip()
        length = len(text)
        if length <= 4:
            return text
        typed_len = max(4, (length + 1) // 2)
        return text[:typed_len]

    def search_text(self, username):
        search_row = self.device.find(resourceId=ResourceID.ROW_SEARCH_EDIT_TEXT)
        if search_row.exists(Timeout.MEDIUM):
            query = self._partial_search_query(username)
            if query != username:
                logger.debug(
                    "Typing partial search query '%s' for '%s' (long usernames may "
                    "not surface when typed in full).",
                    query,
                    username,
                )
            search_row.set_text(query, Mode.PASTE if args.dont_type else Mode.TYPE)
            return True
        else:
            return False

    @staticmethod
    def close_keyboard(device):
        flag = DeviceFacade(device.device_id, device.app_id)._is_keyboard_show()
        if flag:
            logger.debug("The keyboard is currently open. Press back to close.")
            device.back()
        elif flag is None:
            tabbar_container = device.find(
                resourceId=ResourceID.FIXED_TABBAR_TABS_CONTAINER
            )
            if tabbar_container.exists():
                delta = tabbar_container.get_bounds()["bottom"]
            else:
                delta = 375
            logger.debug(
                "Failed to check if keyboard is open! Will do a little swipe up to prevent errors."
            )
            UniversalActions(device)._swipe_points(
                direction=Direction.UP,
                start_point_y=randint(delta + 10, delta + 150),
                delta_y=randint(50, 100),
            )
