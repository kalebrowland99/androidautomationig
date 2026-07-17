import logging
import sys

from colorama import Fore

from GramAddict.core.device_facade import Timeout
from GramAddict.core.utils import random_sleep
from GramAddict.core.views import (
    HashTagView,
    OpenedPostView,
    PlacesView,
    PostsGridView,
    ProfileView,
    TabBarView,
    UniversalActions,
)

logger = logging.getLogger(__name__)

# Fallback coordinates for the first post of the SECOND profile-grid row, as
# screen-proportional coordinates (measured on a 1080x2094 device with the
# profile scrolled to the top). Only used when Instagram doesn't expose the
# thumbnail's position in the a11y tree — see `open_second_row_first_post`.
SECOND_ROW_FIRST_POST_FRACTION = (181 / 1080, 1862 / 2094)


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

    Profiles differ across Instagram builds, so try both reliable openers:
      1) a11y content-desc (``at row 2, column 1``)
      2) hierarchy child indexes (row=1, col=0)
    Coordinate tap is only the last resort.
    """
    grid_view = PostsGridView(device)
    # Make sure the grid is loaded and the second row is on screen.
    if not grid_view.is_post_tappable(0, 0) and not grid_view.is_post_tappable(1, 0):
        ProfileView(device).swipe_to_fit_posts()

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
        # Skip the top row (pinned posts) and open the first post of the
        # second row, located via the thumbnail's a11y content-desc.
        logger.info(
            f"Opening first post of the second row (skip first row) for {username}."
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
