import logging
import os
from functools import partial
from os import path
from typing import Optional

from atomicwrites import atomic_write
from colorama import Fore

from GramAddict.core.device_facade import Direction, Timeout
from GramAddict.core.interaction import like_all_profile_stories, register_daily_story_account
from GramAddict.core.story_likes_log import append_story_likes_log, backfill_story_checks_from_todays_log
from GramAddict.core.navigation import (
    nav_to_blogger,
    nav_to_feed,
    nav_to_hashtag_or_place,
    nav_to_post_likers,
)
from GramAddict.core.resources import ClassName
from GramAddict.core.storage import FollowingStatus
from GramAddict.core.utils import (
    EmptyList,
    check_instagram_rate_limit,
    get_value,
    inspect_current_view,
    navigate_to_profile_via_url,
    random_choice,
    random_sleep,
    remove_usernames_from_list_file,
    skip_first_row_enabled,
)
from GramAddict.core.views import (
    FollowingView,
    LIKES_COUNT_HIDDEN,
    LikeMode,
    OpenedPostView,
    Owner,
    PostsViewList,
    ProfileView,
    SwipeTo,
    TabBarView,
    UniversalActions,
    case_insensitive_re,
)

logger = logging.getLogger(__name__)


def suggested_for_you_top(device) -> Optional[int]:
    header = device.find(textMatches=case_insensitive_re("Suggested for you"))
    if header.exists(Timeout.SHORT):
        bounds = header.get_bounds()
        if bounds:
            return bounds.get("top")
    return None


def item_at_or_below_y(item, y_top: Optional[int]) -> bool:
    if y_top is None:
        return False
    bounds = item.get_bounds()
    if not bounds:
        return False
    return bounds.get("top", 0) >= y_top


def find_see_more_button(device, resource_id):
    see_more = device.find(resourceId=resource_id.SEE_MORE_BUTTON)
    if see_more.exists(Timeout.ZERO):
        return see_more
    return device.find(textMatches=case_insensitive_re("^See more$"))


def _followers_list_view(device, resource_id):
    return device.find(
        resourceId=resource_id.LIST, className=ClassName.LIST_VIEW
    )


def see_more_visible(device, resource_id) -> bool:
    return find_see_more_button(device, resource_id).exists(Timeout.SHORT)


def try_tap_see_more(device, resource_id) -> bool:
    see_more = find_see_more_button(device, resource_id)
    if see_more.exists(Timeout.SHORT):
        return _tap_see_more(see_more)
    return False


def reveal_see_more_above_suggestions(
    device, resource_id, *, attempts: int = 4
) -> bool:
    """
    See more and Suggested-for-you share one ListView. Scrolling down can move
    See more off the top while suggestions stay visible — scroll up to reveal it.
    """
    if see_more_visible(device, resource_id):
        return True
    list_view = _followers_list_view(device, resource_id)
    if not list_view.exists(Timeout.SHORT):
        return False
    for _ in range(attempts):
        list_view.scroll(Direction.UP)
        random_sleep(0.25, 0.5, modulable=False)
        if see_more_visible(device, resource_id):
            return True
    return False


def _tap_see_more(see_more) -> bool:
    logger.info(
        'Press "See more" to load additional followers.',
        extra={"color": f"{Fore.GREEN}"},
    )
    see_more.click_retry()
    random_sleep(2, 4, modulable=False)
    return True


def seek_and_tap_see_more(
    device, resource_id, *, max_scroll_attempts: int = 15
) -> bool:
    """Find See more in the followers ListView (scroll gently if needed) and tap it."""
    if try_tap_see_more(device, resource_id):
        return True

    list_view = _followers_list_view(device, resource_id)

    if max_scroll_attempts <= 0:
        if reveal_see_more_above_suggestions(device, resource_id):
            return try_tap_see_more(device, resource_id)
        return False

    if not list_view.exists(Timeout.SHORT):
        return False

    for _ in range(max_scroll_attempts):
        list_view.scroll(Direction.DOWN)
        random_sleep(0.35, 0.7, modulable=False)
        if try_tap_see_more(device, resource_id):
            return True

    if reveal_see_more_above_suggestions(device, resource_id):
        return try_tap_see_more(device, resource_id)
    return False


def expand_truncated_followers_list(device, resource_id, *, max_scroll_attempts: int = 15) -> bool:
    return seek_and_tap_see_more(
        device, resource_id, max_scroll_attempts=max_scroll_attempts
    )


def truncated_followers_fully_shown(device, resource_id) -> bool:
    """
    True when every real follower batch is loaded: Suggested-for-you is showing
    and See more is gone. Both live in the same ListView, so scroll up first —
    See more may sit just above suggestions but off-screen.
    """
    if not suggested_for_you_top(device):
        return False
    if see_more_visible(device, resource_id):
        return False
    if reveal_see_more_above_suggestions(device, resource_id):
        return False
    return True


def advance_foreign_followers_list(device, resource_id, list_view) -> str:
    """
    Paginate someone else's followers list. See more and Suggested-for-you share
    one ListView — never fling; scroll one step then seek See more above/below.
    """
    if seek_and_tap_see_more(device, resource_id, max_scroll_attempts=0):
        return "expanded"
    if truncated_followers_fully_shown(device, resource_id):
        return "done"
    if list_view.exists(Timeout.SHORT):
        list_view.scroll(Direction.DOWN)
        random_sleep(0.35, 0.7, modulable=False)
    if seek_and_tap_see_more(device, resource_id, max_scroll_attempts=8):
        return "expanded"
    if truncated_followers_fully_shown(device, resource_id):
        return "done"
    return "scrolled" if list_view.exists(Timeout.ZERO) else "none"


def interact(
    storage,
    is_follow_limit_reached,
    username,
    interaction,
    device,
    session_state,
    current_job,
    target,
    on_interaction,
):
    can_follow = False
    if is_follow_limit_reached is not None:
        can_follow = not is_follow_limit_reached() and storage.get_following_status(
            username
        ) in [FollowingStatus.NONE, FollowingStatus.NOT_IN_LIST]

    (
        interaction_succeed,
        followed,
        requested,
        scraped,
        pm_sent,
        number_of_liked,
        number_of_watched,
        number_of_comments,
    ) = interaction(
        device, username=username, can_follow=can_follow, storage=storage
    )

    add_interacted_user = partial(
        storage.add_interacted_user,
        session_id=session_state.id,
        job_name=current_job,
        target=target,
    )

    add_interacted_user(
        username,
        followed=followed,
        is_requested=requested,
        scraped=scraped,
        liked=number_of_liked,
        watched=number_of_watched,
        commented=number_of_comments,
        pm_sent=pm_sent,
    )
    return on_interaction(
        succeed=interaction_succeed,
        followed=followed,
        scraped=scraped,
    )


def handle_blogger(
    self,
    device,
    session_state,
    blogger,
    current_job,
    storage,
    profile_filter,
    on_interaction,
    interaction,
    is_follow_limit_reached,
):
    if not nav_to_blogger(device, blogger, session_state.my_username):
        return
    can_interact = False
    if storage.is_user_in_blacklist(blogger):
        logger.info(f"@{blogger} is in blacklist. Skip.")
    else:
        interacted, interacted_when = storage.check_user_was_interacted(blogger)
        if interacted:
            can_reinteract = storage.can_be_reinteract(
                interacted_when, get_value(self.args.can_reinteract_after, None, 0)
            )
            logger.info(
                f"@{blogger}: already interacted on {interacted_when:%Y/%m/%d %I:%M:%S %p}. {'Interacting again now' if can_reinteract else 'Skip'}."
            )
            if can_reinteract:
                can_interact = True
        else:
            can_interact = True

    if can_interact:
        logger.info(
            f"@{blogger}: interact",
            extra={"color": f"{Fore.YELLOW}"},
        )
        if not interact(
            storage=storage,
            is_follow_limit_reached=is_follow_limit_reached,
            username=blogger,
            interaction=interaction,
            device=device,
            session_state=session_state,
            current_job=current_job,
            target=blogger,
            on_interaction=on_interaction,
        ):
            return


def handle_blogger_from_file(
    self,
    device,
    parameter_passed,
    current_job,
    storage,
    on_interaction,
    interaction,
    is_follow_limit_reached,
):
    need_to_refresh = True
    on_following_list = False
    limit_reached = False

    filename: str = os.path.join(storage.account_path, parameter_passed.split(" ")[0])
    try:
        amount_of_users = get_value(parameter_passed.split(" ")[1], None, 10)
    except IndexError:
        amount_of_users = 10
        logger.warning(
            f"You didn't passed how many users should be processed from the list! Default is {amount_of_users} users."
        )
    if path.isfile(filename):
        with open(filename, "r", encoding="utf-8") as f:
            usernames = [line.replace(" ", "") for line in f if line != "\n"]
        len_usernames = len(usernames)
        if len_usernames < amount_of_users:
            amount_of_users = len_usernames
        logger.info(
            f"In {filename} there are {len_usernames} entries, {amount_of_users} users will be processed."
        )
        not_found = []
        processed_users = 0
        try:
            for line, username_raw in enumerate(usernames, start=1):
                username = username_raw.strip()
                can_interact = False
                if current_job == "unfollow-from-file":
                    unfollowed = do_unfollow_from_list(
                        device, username, on_following_list
                    )
                    on_following_list = True
                    if unfollowed:
                        storage.add_interacted_user(
                            username, self.session_state.id, unfollowed=True
                        )
                        self.session_state.totalUnfollowed += 1
                        limit_reached = self.session_state.check_limit(
                            limit_type=self.session_state.Limit.UNFOLLOWS
                        )
                        processed_users += 1
                    else:
                        not_found.append(username_raw)
                    if limit_reached:
                        logger.info("Unfollows limit reached.")
                        break
                    if processed_users == amount_of_users:
                        logger.info(
                            f"{processed_users} users have been unfollowed, going to the next job."
                        )
                        break
                else:
                    if storage.is_user_in_blacklist(username):
                        logger.info(f"@{username} is in blacklist. Skip.")
                    else:
                        (
                            interacted,
                            interacted_when,
                        ) = storage.check_user_was_interacted(username)
                        if interacted:
                            can_reinteract = storage.can_be_reinteract(
                                interacted_when,
                                get_value(self.args.can_reinteract_after, None, 0),
                            )
                            logger.info(
                                f"@{username}: already interacted on {interacted_when:%Y/%m/%d %I:%M:%S %p}. {'Interacting again now' if can_reinteract else 'Skip'}."
                            )
                            if can_reinteract:
                                can_interact = True
                        else:
                            can_interact = True

                    if not can_interact:
                        continue
                    if need_to_refresh:
                        search_view = TabBarView(device).navigateToSearch()
                    profile_view = search_view.navigate_to_target(username, current_job)
                    need_to_refresh = False
                    if not profile_view:
                        not_found.append(username_raw)
                        continue

                    if not interact(
                        storage=storage,
                        is_follow_limit_reached=is_follow_limit_reached,
                        username=username,
                        interaction=interaction,
                        device=device,
                        session_state=self.session_state,
                        current_job=current_job,
                        target=username,
                        on_interaction=on_interaction,
                    ):
                        return
                    device.back()
                    processed_users += 1
                    if processed_users == amount_of_users:
                        logger.info(
                            f"{processed_users} users have been interracted, going to the next job."
                        )
                        return
        finally:
            if not_found:
                with open(
                    f"{os.path.splitext(filename)[0]}_not_found.txt",
                    mode="a+",
                    encoding="utf-8",
                ) as f:
                    f.writelines(not_found)
            if self.args.delete_interacted_users and len_usernames != 0:
                with atomic_write(filename, overwrite=True, encoding="utf-8") as f:
                    f.writelines(usernames[line:])
    else:
        logger.warning(
            f"File {filename} not found. You have to specify the right relative path from this point: {os.getcwd()}"
        )
        return

    logger.info(f"Interact with users in {filename} completed.")
    device.back()


def handle_daily_story_likes_from_file(self, device, parameter_passed, storage):
    """Visit each username in the list and like all new stories."""

    filename: str = os.path.join(storage.account_path, parameter_passed.split(" ")[0])
    try:
        amount_of_users = get_value(parameter_passed.split(" ")[1], None, 10)
    except IndexError:
        amount_of_users = 10
        logger.warning(
            f"You didn't pass how many users should be processed from the list! Default is {amount_of_users} users."
        )
    if not path.isfile(filename):
        logger.warning(
            f"File {filename} not found. You have to specify the right relative path from this point: {os.getcwd()}"
        )
        return

    with open(filename, "r", encoding="utf-8") as f:
        usernames = [line.replace(" ", "") for line in f if line.strip() and not line.strip().startswith("#")]
    len_usernames = len(usernames)
    if len_usernames < amount_of_users:
        amount_of_users = len_usernames
    list_set = {u.strip().lstrip("@") for u in usernames if u.strip()}
    backfilled = backfill_story_checks_from_todays_log(
        storage,
        storage.my_username,
        self.session_state.id,
        list_usernames=list_set,
    )
    if backfilled:
        logger.info(
            f"Daily story likes: restored {backfilled} account(s) checked earlier today from log."
        )
    already_today = storage.count_story_checks_today_in_list(usernames)
    remaining = max(0, amount_of_users - already_today)
    logger.info(
        f"Daily story likes: {len_usernames} entries in {filename}, "
        f"{already_today} already checked today, up to {remaining} remaining "
        f"(daily cap {amount_of_users})."
    )
    append_story_likes_log(
        storage.my_username,
        f"Session start — {already_today} done today, {remaining} remaining (cap {amount_of_users}).",
    )
    if amount_of_users <= 0:
        logger.info("Daily story likes limit is 0 — skipping.")
        append_story_likes_log(storage.my_username, "Skipped — limit is 0.")
        return
    if remaining <= 0:
        logger.info("Daily story likes quota already met for today — skipping.")
        append_story_likes_log(
            storage.my_username,
            f"Already completed today's batch ({already_today}/{amount_of_users}).",
        )
        return

    removed_usernames = set()
    processed_users = 0
    self.session_state.daily_story_likes_limit = amount_of_users
    self.session_state.totalDailyStoryAccounts = already_today
    self.session_state._publish_live_progress()

    try:
        for line, username_raw in enumerate(usernames, start=1):
            username = username_raw.strip().lstrip("@")
            if not username:
                continue

            check_instagram_rate_limit(device)

            if storage.story_checked_today(username):
                logger.debug(f"@{username}: already checked today — skip.")
                continue

            if storage.is_user_in_blacklist(username):
                logger.info(f"@{username} is in blacklist. Skip.")
                append_story_likes_log(storage.my_username, f"@{username}: blacklist — skip.")
                continue

            opened, account_missing = navigate_to_profile_via_url(
                device, username, fast=True
            )
            if account_missing:
                removed_usernames.add(username)
                append_story_likes_log(
                    storage.my_username,
                    f"@{username}: account not found — removed from list.",
                )
                continue
            if not opened:
                append_story_likes_log(
                    storage.my_username,
                    f"@{username}: could not open profile — skip (will retry later).",
                )
                continue

            profile_view = ProfileView(device, is_own_profile=False)
            liked = like_all_profile_stories(
                device,
                profile_view,
                username,
                self.args,
                self.session_state,
                always_like_stories=True,
                daily_story_likes=True,
            )
            if liked:
                logger.info(
                    f"@{username}: liked {liked} story segment(s).",
                    extra={"color": f"{Fore.GREEN}"},
                )
                append_story_likes_log(
                    storage.my_username,
                    f"@{username}: liked {liked} story segment(s).",
                )
            else:
                append_story_likes_log(
                    storage.my_username,
                    f"@{username}: checked — no story or like failed.",
                )
            storage.record_story_check(username, self.session_state.id)
            # Next account opens via deep link — skip back() to avoid an extra
            # blank transition between profiles.
            processed_users += 1
            register_daily_story_account(self.session_state)

            if processed_users >= remaining:
                logger.info(
                    f"{processed_users} new account(s) checked for stories this run "
                    f"({already_today + processed_users}/{amount_of_users} today) — next job."
                )
                append_story_likes_log(
                    storage.my_username,
                    f"Finished batch — {already_today + processed_users}/{amount_of_users} checked today.",
                )
                break
    finally:
        if removed_usernames:
            removed_count = remove_usernames_from_list_file(filename, removed_usernames)
            logger.info(
                f"Removed {removed_count} missing account(s) from {filename}."
            )
            append_story_likes_log(
                storage.my_username,
                f"Removed {removed_count} missing account(s) from story list.",
            )
        if self.args.delete_interacted_users and len_usernames != 0:
            with atomic_write(filename, overwrite=True, encoding="utf-8") as f:
                f.writelines(usernames[line:])

    logger.info(f"Daily story likes from {filename} completed.")
    append_story_likes_log(
        storage.my_username,
        f"Job complete — {already_today + processed_users}/{amount_of_users} checked today "
        f"({processed_users} this run).",
    )
    self.session_state.daily_story_likes_limit = None
    self.session_state._publish_live_progress()


def do_unfollow_from_list(device, username, on_following_list):
    if not on_following_list:
        ProfileView(device).click_on_avatar()
        if ProfileView(device).navigateToFollowing() and UniversalActions(
            device
        ).search_text(username):
            return FollowingView(device).do_unfollow_from_list(username)
    else:
        if username is not None:
            UniversalActions(device).search_text(username)
        return FollowingView(device).do_unfollow_from_list(username)


def handle_likers(
    self,
    device,
    session_state,
    target,
    current_job,
    storage,
    profile_filter,
    posts_end_detector,
    on_interaction,
    interaction,
    is_follow_limit_reached,
):
    skip_first_row = skip_first_row_enabled(self.args)
    if (
        current_job == "blogger-post-likers"
        and not nav_to_post_likers(
            device, target, session_state.my_username, skip_first_row=skip_first_row
        )
        or current_job != "blogger-post-likers"
        and not nav_to_hashtag_or_place(device, target, current_job)
    ):
        return False
    post_description = ""
    nr_same_post = 0
    nr_same_posts_max = 3
    while True:
        flag, post_description, _, _, _, _ = PostsViewList(device)._check_if_last_post(
            post_description, current_job
        )
        if flag:
            nr_same_post += 1
            logger.info(f"Warning: {nr_same_post}/{nr_same_posts_max} repeated posts.")
            if nr_same_post == nr_same_posts_max:
                logger.info(
                    f"Scrolled through {nr_same_posts_max} posts with same description and author. Finish.",
                    extra={"color": f"{Fore.CYAN}"},
                )
                break
        else:
            nr_same_post = 0

        post_view = PostsViewList(device)
        likers_owner = target if current_job == "blogger-post-likers" else None
        can_open_likers, number_of_likers = post_view.likers_open_status(
            likers_owner, current_job=current_job
        )
        if number_of_likers == LIKES_COUNT_HIDDEN:
            logger.info(
                "Skipping post — owner hid likes.",
                extra={"color": f"{Fore.CYAN}"},
            )
            post_view.swipe_to_fit_posts(SwipeTo.NEXT_POST)
            continue
        if (
            can_open_likers
            and profile_filter.is_num_likers_in_range(number_of_likers)
        ):
            if post_view.open_likers_container() is None:
                if not OpenedPostView(device).likers_sheet_visible():
                    logger.info(
                        "Skipping post — likers sheet did not open (hidden likes).",
                        extra={"color": f"{Fore.CYAN}"},
                    )
                    post_view.swipe_to_fit_posts(SwipeTo.NEXT_POST)
                    continue
        else:
            PostsViewList(device).swipe_to_fit_posts(SwipeTo.NEXT_POST)
            continue

        posts_end_detector.notify_new_page()

        likes_list_view = OpenedPostView(device)._getListViewLikers()
        if likes_list_view is None:
            return
        prev_screen_iterated_likers = []

        while True:
            logger.info("Iterate over visible likers.")
            screen_iterated_likers = []
            opened = False
            user_container = OpenedPostView(device)._getUserContainer()
            if user_container is None:
                logger.warning("Likers list didn't load :(")
                return
            try:
                row_height, n_users = inspect_current_view(user_container)
            except EmptyList:
                logger.info(
                    "No likers visible on screen — nothing to iterate.",
                    extra={"color": f"{Fore.GREEN}"},
                )
                return
            try:
                for item in user_container:
                    cur_row_height = item.get_height()
                    if cur_row_height < row_height:
                        continue
                    element_opened = False
                    username_view = OpenedPostView(device)._getUserName(item)
                    if not username_view.exists(Timeout.MEDIUM):
                        logger.info(
                            "Next item not found: probably reached end of the screen.",
                            extra={"color": f"{Fore.GREEN}"},
                        )
                        break

                    username = username_view.get_text()
                    screen_iterated_likers.append(username)
                    posts_end_detector.notify_username_iterated(username)
                    can_interact = False
                    if storage.is_user_in_blacklist(username):
                        logger.info(f"@{username} is in blacklist. Skip.")
                    else:
                        (
                            interacted,
                            interacted_when,
                        ) = storage.check_user_was_interacted(username)
                        if interacted:
                            can_reinteract = storage.can_be_reinteract(
                                interacted_when,
                                get_value(self.args.can_reinteract_after, None, 0),
                            )
                            logger.info(
                                f"@{username}: already interacted on {interacted_when:%Y/%m/%d %I:%M:%S %p}. {'Interacting again now' if can_reinteract else 'Skip'}."
                            )
                            if can_reinteract:
                                can_interact = True
                        else:
                            can_interact = True

                    if can_interact:
                        logger.info(
                            f"@{username}: interact",
                            extra={"color": f"{Fore.YELLOW}"},
                        )
                        element_opened = username_view.click_retry()

                        if element_opened and not interact(
                            storage=storage,
                            is_follow_limit_reached=is_follow_limit_reached,
                            username=username,
                            interaction=interaction,
                            device=device,
                            session_state=session_state,
                            current_job=current_job,
                            target=target,
                            on_interaction=on_interaction,
                        ):
                            return
                    if element_opened:
                        opened = True
                        logger.info("Back to likers list.")
                        device.back()

            except IndexError:
                logger.info(
                    "Cannot get next item: probably reached end of the screen.",
                    extra={"color": f"{Fore.GREEN}"},
                )
                break
            go_back = False
            if screen_iterated_likers == prev_screen_iterated_likers:
                logger.info(
                    "Iterated exactly the same likers twice.",
                    extra={"color": f"{Fore.GREEN}"},
                )
                go_back = True
            if go_back:
                prev_screen_iterated_likers.clear()
                prev_screen_iterated_likers += screen_iterated_likers
                logger.info(
                    f"Back to {target}'s posts list.",
                    extra={"color": f"{Fore.GREEN}"},
                )
                device.back()
                logger.info("Going to the next post.")
                PostsViewList(device).swipe_to_fit_posts(SwipeTo.NEXT_POST)
                break
            if posts_end_detector.is_fling_limit_reached():
                logger.info(
                    "Reached fling limit. Fling to see other likers.",
                    extra={"color": f"{Fore.GREEN}"},
                )
                likes_list_view.fling(Direction.DOWN)
            else:
                logger.info(
                    "Scroll to see other likers.",
                    extra={"color": f"{Fore.GREEN}"},
                )
                likes_list_view.scroll(Direction.DOWN)

            prev_screen_iterated_likers.clear()
            prev_screen_iterated_likers += screen_iterated_likers
            if posts_end_detector.is_the_end():
                device.back()
                PostsViewList(device).swipe_to_fit_posts(SwipeTo.NEXT_POST)
                break
            if not opened:
                logger.info(
                    "All likers skipped.",
                    extra={"color": f"{Fore.GREEN}"},
                )
                posts_end_detector.notify_skipped_all()
                if posts_end_detector.is_skipped_limit_reached():
                    posts_end_detector.reset_skipped_all()
                    return


def handle_posts(
    self,
    device,
    session_state,
    target,
    current_job,
    storage,
    profile_filter,
    on_interaction,
    interaction,
    is_follow_limit_reached,
    interact_percentage,
    scraping_file,
):
    skipped_posts_limit = get_value(
        self.args.skipped_posts_limit,
        "Skipped post limit: {}",
        5,
    )
    if current_job == "feed":
        if scraping_file:
            logger.warning(
                "Scraping and interacting with own feed doesn't make any sense. Skip."
            )
            return
        nav_to_feed(device)
        count_feed_limit = get_value(
            self.args.feed,
            "Feed interact count: {}",
            10,
        )
        count = 0
        PostsViewList(device)._refresh_feed()
    elif not nav_to_hashtag_or_place(device, target, current_job):
        return

    post_description = ""
    likes_failed = 0
    nr_same_post = 0
    nr_same_posts_max = 3
    nr_consecutive_already_interacted = 0
    already_liked_count = 0
    already_liked_count_limit = 20
    post_view_list = PostsViewList(device)
    opened_post_view = OpenedPostView(device)
    while True:
        (
            is_same_post,
            post_description,
            username,
            is_ad,
            is_hashtag,
            has_tags,
        ) = post_view_list._check_if_last_post(post_description, current_job)
        if not username:
            logger.info(
                "Could not read this feed post (tap media did not reveal like row) — scroll to next.",
                extra={"color": f"{Fore.CYAN}"},
            )
            post_view_list.swipe_to_fit_posts(SwipeTo.NEXT_POST, home_feed=True)
            continue
        can_open_likers, number_of_likers = post_view_list.likers_open_status(
            current_job=current_job
        )
        if number_of_likers == LIKES_COUNT_HIDDEN:
            logger.info(
                "Skipping post — owner hid likes.",
                extra={"color": f"{Fore.CYAN}"},
            )
            post_view_list.swipe_to_fit_posts(SwipeTo.NEXT_POST, home_feed=True)
            continue
        already_liked, _ = opened_post_view._is_post_liked()
        if not (is_ad or is_hashtag):
            if already_liked_count == already_liked_count_limit:
                logger.info(
                    f"Limit of {already_liked_count_limit} already liked posts limit reached, finish."
                )
                break
            if is_same_post:
                nr_same_post += 1
                logger.info(
                    f"Warning: {nr_same_post}/{nr_same_posts_max} repeated posts."
                )
                if nr_same_post == nr_same_posts_max:
                    logger.info(
                        f"Scrolled through {nr_same_posts_max} posts with same description and author. Finish."
                    )
                    break
            else:
                nr_same_post = 0
            if already_liked:
                logger.info(
                    "Post already liked, SKIP.", extra={"color": f"{Fore.CYAN}"}
                )
                already_liked_count += 1
            elif random_choice(interact_percentage):
                can_interact = False
                if storage.is_user_in_blacklist(username):
                    logger.info(f"@{username} is in blacklist. Skip.")
                else:
                    if current_job == "feed":
                        likes_in_range = True
                    else:
                        likes_in_range = profile_filter.is_num_likers_in_range(
                            number_of_likers
                        )
                    if current_job != "feed":
                        interacted, interacted_when = storage.check_user_was_interacted(
                            username
                        )
                        if interacted:
                            can_reinteract = storage.can_be_reinteract(
                                interacted_when,
                                get_value(self.args.can_reinteract_after, None, 0),
                            )
                            logger.info(
                                f"@{username}: already interacted on {interacted_when:%Y/%m/%d %I:%M:%S %p}. {'Interacting again now' if can_reinteract else 'Skip'}."
                            )
                            if can_reinteract:
                                can_interact = True
                                nr_consecutive_already_interacted = 0
                            else:
                                nr_consecutive_already_interacted += 1
                        else:
                            can_interact = True
                            nr_consecutive_already_interacted = 0
                    else:
                        can_interact = True

                if nr_consecutive_already_interacted == skipped_posts_limit:
                    logger.info(
                        f"Reached the limit of already interacted {skipped_posts_limit}. Going to the next source/job!"
                    )
                    break
                if can_interact and (
                    likes_in_range or not can_open_likers
                ):
                    logger.info(
                        f"@{username}: interact", extra={"color": f"{Fore.YELLOW}"}
                    )
                    if scraping_file is None:
                        opened_post_view.start_video()
                        if not session_state.check_limit(
                            limit_type=session_state.Limit.LIKES, output=True
                        ):
                            if has_tags:
                                post_view_list._like_in_post_view(LikeMode.SINGLE_CLICK)
                            else:
                                post_view_list._like_in_post_view(LikeMode.DOUBLE_CLICK)
                            UniversalActions.detect_block(device)
                            liked = post_view_list._check_if_liked()
                            if not liked:
                                post_view_list._like_in_post_view(
                                    LikeMode.SINGLE_CLICK, already_watched=True
                                )
                                UniversalActions.detect_block(device)
                                liked = post_view_list._check_if_liked()
                            if liked:
                                session_state.totalLikes += 1
                                if current_job == "feed":
                                    count += 1
                                    logger.info(
                                        f"Interacted feed bloggers: {count}/{count_feed_limit}"
                                    )
                                    likes_limit = self.session_state.check_limit(
                                        limit_type=self.session_state.Limit.LIKES
                                    )
                                    success_limit = self.session_state.check_limit(
                                        limit_type=self.session_state.Limit.SUCCESS
                                    )
                                    total_limit = self.session_state.check_limit(
                                        limit_type=self.session_state.Limit.TOTAL
                                    )
                                    if likes_limit or success_limit or total_limit:
                                        logger.info("Limit reached, finish.")
                                        break
                                    if count >= count_feed_limit:
                                        logger.info(
                                            f"Interacted {count} bloggers in feed, finish."
                                        )
                                        break
                            else:
                                likes_failed += 1
                    if current_job != "feed":
                        opened, _, _ = post_view_list._post_owner(
                            current_job, Owner.OPEN, username
                        )
                        if opened:
                            if not interact(
                                storage=storage,
                                is_follow_limit_reached=is_follow_limit_reached,
                                username=username,
                                interaction=interaction,
                                device=device,
                                session_state=session_state,
                                current_job=current_job,
                                target=target,
                                on_interaction=on_interaction,
                            ):
                                break
                            device.back()
                elif can_interact:
                    logger.info(
                        f"@{username}: skipped — like count {number_of_likers} outside filters.yml min/max likers range.",
                        extra={"color": f"{Fore.CYAN}"},
                    )
            else:
                logger.info(
                    f"Skipped because your interact % is {interact_percentage}/100 and {username}'s post was unlucky!"
                )
        if likes_failed == 10:
            logger.warning("You failed to do 10 likes! Soft-ban?!")
            if current_job == "feed":
                post_view_list.exit_home_feed_clips_overlay()
            return
        post_view_list.swipe_to_fit_posts(SwipeTo.NEXT_POST, home_feed=True)
    if current_job == "feed":
        post_view_list.exit_home_feed_clips_overlay()
    TabBarView(device).navigateToProfile()


def handle_followers(
    self,
    device,
    session_state,
    username,
    current_job,
    storage,
    on_interaction,
    interaction,
    is_follow_limit_reached,
    scroll_end_detector,
):
    is_myself = username == session_state.my_username
    if not nav_to_blogger(device, username, current_job):
        return

    iterate_over_followers(
        self,
        device,
        interaction,
        is_follow_limit_reached,
        storage,
        on_interaction,
        is_myself,
        scroll_end_detector,
        session_state,
        current_job,
        username,
    )


def iterate_over_followers(
    self,
    device,
    interaction,
    is_follow_limit_reached,
    storage,
    on_interaction,
    is_myself,
    scroll_end_detector,
    session_state,
    current_job,
    target,
):
    device.find(
        resourceId=self.ResourceID.FOLLOW_LIST_CONTAINER,
        className=ClassName.LINEAR_LAYOUT,
    ).wait(Timeout.LONG)

    def scrolled_to_top():
        row_search = device.find(
            resourceId=self.ResourceID.ROW_SEARCH_EDIT_TEXT,
            className=ClassName.EDIT_TEXT,
        )
        return row_search.exists()

    while True:
        if not is_myself and truncated_followers_fully_shown(device, self.ResourceID):
            logger.info(
                "Reached end of visible follower batches (Suggested for you).",
                extra={"color": f"{Fore.GREEN}"},
            )
            return

        if not is_myself and seek_and_tap_see_more(
            device, self.ResourceID, max_scroll_attempts=0
        ):
            logger.info(
                'Tapped visible "See more" before iterating this screen.',
                extra={"color": f"{Fore.GREEN}"},
            )

        logger.info("Iterate over visible followers.")
        screen_iterated_followers = []
        screen_skipped_followers_count = 0
        scroll_end_detector.notify_new_page()
        user_list = device.find(
            resourceIdMatches=self.ResourceID.USER_LIST_CONTAINER,
        )
        try:
            row_height, n_users = inspect_current_view(user_list)
        except EmptyList:
            logger.info(
                "No followers visible on screen — reached the end of the list.",
                extra={"color": f"{Fore.GREEN}"},
            )
            return
        suggested_top = suggested_for_you_top(device) if not is_myself else None
        try:
            for item in user_list:
                if item_at_or_below_y(item, suggested_top):
                    logger.info(
                        "Hit 'Suggested for you' section — skip suggestion rows.",
                        extra={"color": f"{Fore.GREEN}"},
                    )
                    break
                cur_row_height = item.get_height()
                if cur_row_height < row_height:
                    continue
                user_info_view = item.child(index=1)
                user_name_view = user_info_view.child(index=0).child()
                if not user_name_view.exists():
                    logger.info(
                        "Next item not found: probably reached end of the screen.",
                        extra={"color": f"{Fore.GREEN}"},
                    )
                    break

                username = user_name_view.get_text()
                screen_iterated_followers.append(username)
                scroll_end_detector.notify_username_iterated(username)

                can_interact = False
                if storage.is_user_in_blacklist(username):
                    logger.info(f"@{username} is in blacklist. Skip.")
                else:
                    interacted, interacted_when = storage.check_user_was_interacted(
                        username
                    )
                    if interacted:
                        can_reinteract = storage.can_be_reinteract(
                            interacted_when,
                            get_value(self.args.can_reinteract_after, None, 0),
                        )
                        logger.info(
                            f"@{username}: already interacted on {interacted_when:%Y/%m/%d %I:%M:%S %p}. {'Interacting again now' if can_reinteract else 'Skip'}."
                        )
                        if can_reinteract:
                            can_interact = True
                        else:
                            screen_skipped_followers_count += 1
                    else:
                        can_interact = True

                if can_interact:
                    logger.info(
                        f"@{username}: interact", extra={"color": f"{Fore.YELLOW}"}
                    )
                    element_opened = user_name_view.click_retry()

                    if element_opened:
                        if not interact(
                            storage=storage,
                            is_follow_limit_reached=is_follow_limit_reached,
                            username=username,
                            interaction=interaction,
                            device=device,
                            session_state=session_state,
                            current_job=current_job,
                            target=target,
                            on_interaction=on_interaction,
                        ):
                            return
                    if element_opened:
                        logger.info("Back to followers list")
                        device.back()

        except IndexError:
            logger.info(
                "Cannot get next item: probably reached end of the screen.",
                extra={"color": f"{Fore.GREEN}"},
            )

        if is_myself and scrolled_to_top():
            logger.info("Scrolled to top, finish.", extra={"color": f"{Fore.GREEN}"})
            return
        elif len(screen_iterated_followers) > 0:
            load_more_button = device.find(
                resourceId=self.ResourceID.ROW_LOAD_MORE_BUTTON
            )
            load_more_button_exists = load_more_button.exists()

            if scroll_end_detector.is_the_end():
                return

            need_swipe = screen_skipped_followers_count == len(
                screen_iterated_followers
            )
            list_view = device.find(
                resourceId=self.ResourceID.LIST, className=ClassName.LIST_VIEW
            )
            if not list_view.exists():
                logger.error(
                    "Cannot find the list of followers. Trying to press back again."
                )
                device.back()
                list_view = device.find(
                    resourceId=self.ResourceID.LIST,
                    className=ClassName.LIST_VIEW,
                )

            if is_myself:
                logger.info("Need to scroll now", extra={"color": f"{Fore.GREEN}"})
                list_view.scroll(Direction.UP)
            else:
                pressed_retry = False
                if load_more_button_exists:
                    retry_button = load_more_button.child(
                        className=ClassName.IMAGE_VIEW,
                        descriptionMatches=case_insensitive_re("Retry"),
                    )
                    if retry_button.exists():
                        random_sleep()
                        """It exist but can disappear without pressing on it"""
                        if retry_button.exists():
                            logger.info('Press "Load" button and wait few seconds.')
                            retry_button.click_retry()
                            random_sleep(5, 10, modulable=False)
                            pressed_retry = True

                if not pressed_retry:
                    if need_swipe:
                        scroll_end_detector.notify_skipped_all()
                        if scroll_end_detector.is_skipped_limit_reached():
                            return

                    advance = advance_foreign_followers_list(
                        device, self.ResourceID, list_view
                    )
                    if advance == "expanded":
                        continue
                    if advance == "done":
                        logger.info(
                            "No more follower batches to load.",
                            extra={"color": f"{Fore.GREEN}"},
                        )
                        return
                    if advance == "scrolled":
                        logger.info(
                            "Scrolled followers list (seeking See more when present).",
                            extra={"color": f"{Fore.GREEN}"},
                        )
                    else:
                        logger.info(
                            "Need to scroll now", extra={"color": f"{Fore.GREEN}"}
                        )
                        if list_view.exists():
                            list_view.scroll(Direction.DOWN)
        else:
            if not is_myself:
                if seek_and_tap_see_more(device, self.ResourceID):
                    continue
                if truncated_followers_fully_shown(device, self.ResourceID):
                    return
            logger.info(
                "No followers were iterated, finish.",
                extra={"color": f"{Fore.GREEN}"},
            )
            return
