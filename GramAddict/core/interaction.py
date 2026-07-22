import logging
import os
import re
from argparse import Namespace
from datetime import datetime
from os import path
from random import choice, randint, shuffle, uniform
from time import sleep, time
from typing import Optional, Tuple, List

import emoji
import spintax
from colorama import Fore, Style

from GramAddict.core import storage
from GramAddict.core.device_facade import (
    DeviceFacade,
    Location,
    Mode,
    SleepTime,
    Timeout,
)
from GramAddict.core.report import print_scrape_report, print_short_report
from GramAddict.core.resources import ClassName
from GramAddict.core.resources import ResourceID as resources
from GramAddict.core.session_state import SessionState
from GramAddict.core.utils import (
    append_to_file,
    check_instagram_rate_limit,
    get_value,
    random_choice,
    random_sleep,
    save_crash,
    skip_first_row_enabled,
)
from GramAddict.core.views import (
    CurrentStoryView,
    Direction,
    MediaType,
    OpenedPostView,
    PostsGridView,
    ProfileView,
    UniversalActions,
    case_insensitive_re,
)

logger = logging.getLogger(__name__)

ResourceID = None


def load_config(config):
    global args
    global configs
    global ResourceID
    args = config.args
    configs = config
    ResourceID = resources(config.args.app_id)


def comments_are_limited(device: DeviceFacade) -> bool:
    """True when Instagram shows that comments on this post are limited.

    Exact wording varies slightly ("have been" / "is"); match the durable
    "comments on this post" + "limited" phrase so we skip instead of typing.
    """
    needles = (
        "Comments on this post have been limited",
        "Comments on this post is limited",
        "Comments on this post are limited",
        "comments on this post have been limited",
        "comments on this post is limited",
    )
    for text in needles:
        if device.find(textContains=text).exists(Timeout.ZERO):
            return True
    # Broader fallback for wording/ellipsis differences across IG builds.
    limited = device.find(textContains="comments on this post")
    if limited.exists(Timeout.ZERO):
        try:
            shown = (limited.get_text() or "").lower()
        except Exception:
            shown = ""
        if "limited" in shown:
            return True
    return False


def user_comment_allowed(storage, username: str, args) -> bool:
    """False when this user was commented within comment-cooldown-days."""
    if storage is None:
        return True
    cooldown_days = get_value(
        getattr(args, "comment_cooldown_days", None), None, 7
    )
    if storage.can_comment_user(username, cooldown_days):
        return True
    days_label = int(cooldown_days) if cooldown_days else 7
    logger.info(
        f"@{username}: commented within the last {days_label} day(s) — skip comment (cooldown).",
        extra={"color": f"{Fore.CYAN}"},
    )
    return False


def find_comment_thread_edittext(device: DeviceFacade):
    """Comment compose — layout_comment_thread_edittext_multiline inside edittext_container."""
    global ResourceID
    if ResourceID is None:
        from GramAddict.core import views as ga_views

        ResourceID = getattr(ga_views, "ResourceID", None) or resources(
            getattr(getattr(ga_views, "args", None), "app_id", "com.instagram.android")
        )
    rid = ResourceID

    for enabled in (None, "false", "true"):
        opts: dict = {"resourceIdMatches": rid.LAYOUT_COMMENT_THREAD_EDITTEXT}
        if enabled is not None:
            opts["enabled"] = enabled
        box = device.find(**opts)
        if box.exists(Timeout.SHORT):
            return box

    container = device.find(resourceId=rid.EDITTEXT_CONTAINER)
    if not container.exists(Timeout.SHORT):
        container = device.find(resourceIdMatches=rid.LAYOUT_COMMENT_THREAD_EDITTEXT)
    if container.exists(Timeout.SHORT):
        for child_rid in (
            rid.LAYOUT_COMMENT_THREAD_EDITTEXT_MULTILINE,
            rid.LAYOUT_COMMENT_THREAD_EDITTEXT,
        ):
            edit = container.child(resourceId=child_rid)
            if edit.exists(Timeout.SHORT):
                return edit
        edit = container.child(className=ClassName.EDIT_TEXT)
        if edit.exists(Timeout.SHORT):
            return edit

    for enabled in (None, "false", "true"):
        opts = {
            "resourceIdMatches": rid.LAYOUT_COMMENT_THREAD_EDITTEXT,
            "className": ClassName.EDIT_TEXT,
        }
        if enabled is not None:
            opts["enabled"] = enabled
        box = device.find(**opts)
        if box.exists(Timeout.SHORT):
            return box

    return container


def find_post_comment_button(device: DeviceFacade):
    """Feed post comment row or fullscreen reels UFI comment button."""
    return OpenedPostView(device).find_comment_button()


def _confirm_comment_posted(
    device: DeviceFacade, my_username: str, comment: str
) -> bool:
    """Confirm a comment was posted (comment thread or inline on profile Posts view)."""
    username = my_username.lstrip("@")
    needle = comment.strip()
    snippet = needle[: min(len(needle), 40)]

    posted_text = device.find(text=f"{username} {needle}")
    if posted_text.exists(Timeout.SHORT):
        when_posted = posted_text.sibling(
            resourceId=ResourceID.ROW_COMMENT_SUB_ITEMS_BAR
        ).child(resourceId=ResourceID.ROW_COMMENT_TEXTVIEW_TIME_AGO)
        if when_posted.exists(Timeout.SHORT):
            return True

    for selector in (
        {"text": f"{username} {needle}"},
        {"textContains": f"{username} {snippet}"},
        {"textContains": snippet},
    ):
        if device.find(**selector).exists(Timeout.SHORT):
            return True

    posted = device.find(
        resourceId=ResourceID.ROW_COMMENT_TEXTVIEW_COMMENT,
        textContains=snippet,
    )
    if posted.exists(Timeout.SHORT):
        return True

    return device.find(
        resourceIdMatches=ResourceID.ROW_FEED_COMMENT_TEXTVIEW_LAYOUT,
        textContains=snippet,
    ).exists(Timeout.SHORT)


def interact_with_user(
    device,
    username,
    my_username,
    likes_count,
    likes_percentage,
    stories_percentage,
    can_follow,
    follow_percentage,
    comment_percentage,
    pm_percentage,
    profile_filter,
    args,
    session_state,
    scraping_file,
    current_mode,
    storage=None,
) -> Tuple[bool, bool, bool, bool, bool, int, int, int]:
    """
    :return: (whether interaction succeed, whether @username was followed during the interaction, if you scraped that account, if you sent a PM, number of liked, number of watched, number of commented)
    """
    number_of_liked = 0
    number_of_watched = 0
    number_of_commented = 0
    comment_done = interacted = followed = scraped = sent_pm = False
    logger.debug("Checking profile..")
    start_time = time()
    profile_data, skipped = profile_filter.check_profile(device, username)
    if username == my_username:
        logger.info("It's you, skip.")
        return (
            interacted,
            followed,
            profile_data.is_private,
            scraped,
            sent_pm,
            number_of_liked,
            number_of_watched,
            number_of_commented,
        )

    if skipped:
        delta = format(time() - start_time, ".2f")
        logger.debug(f"Profile checked in {delta}s")
        return (
            interacted,
            followed,
            profile_data.is_private,
            scraped,
            sent_pm,
            number_of_liked,
            number_of_watched,
            number_of_commented,
        )

    profile_view = ProfileView(device)
    delta = format(time() - start_time, ".2f")
    logger.debug(f"Profile checked in {delta}s")

    if scraping_file is None:
        from GramAddict.core.follow_vision_account import profile_passes_follow_vision

        if not profile_passes_follow_vision(device, username, my_username):
            return (
                interacted,
                followed,
                profile_data.is_private,
                scraped,
                sent_pm,
                number_of_liked,
                number_of_watched,
                number_of_commented,
            )

    if profile_data.is_private or (profile_data.posts_count == 0):
        private_empty = "Private" if profile_data.is_private else "Empty"
        logger.info(f"{private_empty} account.")
        if (
            pm_percentage != 0
            and can_send_PM(session_state, pm_percentage)
            and profile_filter.can_pm_to_private_or_empty
        ):
            sent_pm = _send_PM(
                device, session_state, my_username, 0, profile_data.is_private
            )
            if sent_pm:
                interacted = True
        can_follow_private_or_empty = profile_filter.can_follow_private_or_empty()
        if can_follow and can_follow_private_or_empty:
            if scraping_file is None:
                followed = _follow(
                    device, username, follow_percentage, args, session_state, 0
                )
                if followed:
                    interacted = True
                return (
                    interacted,
                    followed,
                    profile_data.is_private,
                    scraped,
                    sent_pm,
                    number_of_liked,
                    number_of_watched,
                    number_of_commented,
                )
        else:
            if not can_follow_private_or_empty:
                logger.info(
                    "follow_private_or_empty is disabled in filters. Skip.",
                    extra={"color": f"{Fore.GREEN}"},
                )
            else:
                logger.info(
                    "Your follow-percentage is not 100%, not following this time. Skip.",
                    extra={"color": f"{Fore.GREEN}"},
                )
            return (
                interacted,
                followed,
                profile_data.is_private,
                scraped,
                sent_pm,
                number_of_liked,
                number_of_watched,
                number_of_commented,
            )

    # handle the scraping mode
    if scraping_file is not None:
        append_to_file(scraping_file, username)
        logger.info(
            f"Added @{username} at {scraping_file}",
            extra={"color": f"{Style.BRIGHT}{Fore.GREEN}"},
        )
        scraped = True
        return (
            interacted,
            followed,
            profile_data.is_private,
            scraped,
            sent_pm,
            number_of_liked,
            number_of_watched,
            number_of_commented,
        )

    # if not in scarping mode, we will interact
    number_of_watched = _watch_stories(
        device,
        profile_view,
        username,
        stories_percentage,
        args,
        session_state,
    )
    swipe_amount = 0

    if number_of_watched >= 1:
        interacted = True
    if can_like(session_state, likes_percentage):
        if profile_data.posts_count > 3:
            swipe_amount = ProfileView(device).swipe_to_fit_posts()
        else:
            logger.debug(
                f"We don't need to scroll, there is/are only {profile_data.posts_count} post(s)."
            )
        if swipe_amount == -1:
            return (
                interacted,
                followed,
                profile_data.is_private,
                scraped,
                sent_pm,
                number_of_liked,
                number_of_watched,
                number_of_commented,
            )

        likes_value = get_value(likes_count, "Likes count: {}", 2)
        (
            _,
            _,
            _,
            can_comment_job,
        ) = profile_filter.can_comment(current_mode)
        if can_comment_job and comment_percentage != 0:
            max_comments_pro_user = get_value(
                args.max_comments_pro_user, "Max comment count: {}", 1
            )
        if likes_value > 12:
            logger.error("Max number of likes per user is 12.")
            likes_value = 12

        start_time = time()
        full_rows, columns_last_row = profile_view.count_photo_in_view()
        end_time = format(time() - start_time, ".2f")
        photos_indices = list(range(full_rows * 3 + columns_last_row))

        if len(photos_indices) == profile_data.posts_count and len(photos_indices) > 1:
            del photos_indices[-1]
            logger.debug(
                "This is a temporary fix, for avoid bot to crash we have removed the last picture form the list."
            )

        logger.info(
            f"There {f'is {len(photos_indices)} post' if len(photos_indices)<=1 else f'are {len(photos_indices)} posts'} fully visible. Calculated in {end_time}s"
        )
        # "Skip first row" removes the first grid row (top 3 slots).
        skip_top = 3 if skip_first_row_enabled(args) else 0
        post_grid_view = PostsGridView(device)
        if skip_top:
            before = len(photos_indices)
            photos_indices = [
                idx
                for idx in photos_indices
                if not post_grid_view.should_skip_cell(idx // 3, idx % 3, skip_top)
            ]
            skipped = before - len(photos_indices)
            if skipped:
                logger.info(
                    f"Skipping first row: {skipped} profile grid post(s).",
                    extra={"color": f"{Fore.CYAN}"},
                )
        if current_mode in [
            "hashtag-posts-recent",
            "hashtag-posts-top",
            "place-posts-recent",
            "place-posts-top",
            "feed",
        ]:
            # in these jobs we did a like already at the post
            photos_indices = photos_indices[1:]
            # sometimes we liked not the last picture, have to introduce the already liked thing..

        if likes_value > len(photos_indices):
            logger.info(
                f"Only {len(photos_indices)} {'photo' if len(photos_indices)<=1 else 'photos'} available."
            )
        else:
            shuffle(photos_indices)
            photos_indices = photos_indices[:likes_value]
            photos_indices = sorted(photos_indices)
        for i in range(len(photos_indices)):
            photo_index = photos_indices[i]
            row = photo_index // 3
            column = photo_index - row * 3
            logger.info(f"Open post #{i + 1} ({row + 1} row, {column + 1} column).")
            opened_post_view, media_type, obj_count = post_grid_view.navigateToPost(
                row, column
            )

            like_succeed = False
            if opened_post_view is None:
                save_crash(device)
                continue
            already_liked, _ = opened_post_view._is_post_liked()
            if already_liked:
                logger.info("Post already liked!")
            elif opened_post_view and already_liked is not None:
                if media_type in (MediaType.REEL, MediaType.IGTV, MediaType.VIDEO):
                    opened_post_view.start_video()
                    opened_post_view.open_video()
                    in_fullscreen, _ = opened_post_view._is_video_in_fullscreen()
                    opened_post_view.watch_media(media_type)
                    if in_fullscreen:
                        like_succeed = opened_post_view.like_video()
                        stay_for_comment = (
                            comment_percentage != 0
                            and can_comment_job
                            and number_of_commented
                            < max_comments_pro_user
                            and user_comment_allowed(storage, username, args)
                            and can_comment(
                                media_type, profile_filter, current_mode
                            )
                        )
                        if not stay_for_comment:
                            logger.debug("Closing video...")
                            device.back()
                        else:
                            logger.debug(
                                "Staying in reel view to comment after like."
                            )
                    else:
                        logger.debug(
                            "Inline profile/feed video — liking on post view."
                        )
                        like_succeed = opened_post_view.like_post()
                elif media_type in (MediaType.CAROUSEL, MediaType.PHOTO):
                    if media_type == MediaType.CAROUSEL:
                        _browse_carousel(device, obj_count)
                    opened_post_view.watch_media(media_type)
                    like_succeed = opened_post_view.like_post()
                if like_succeed:
                    register_like(device, session_state)
                    number_of_liked += 1
                else:
                    logger.warning("Fail to like post. Let's continue...")
                if comment_percentage != 0 and can_comment(
                    media_type, profile_filter, current_mode
                ):
                    if (
                        number_of_commented < max_comments_pro_user
                        and user_comment_allowed(storage, username, args)
                    ):
                        comment_done = _comment(
                            device,
                            my_username,
                            comment_percentage,
                            args,
                            session_state,
                            media_type,
                        )
                        if comment_done:
                            number_of_commented += 1
                    else:
                        logger.info(
                            f"You've already did {max_comments_pro_user} {'comment' if max_comments_pro_user<=1 else 'comments'} for this user!"
                        )
            else:
                logger.warning("Can't find the post element!")
                save_crash(device)
            if like_succeed or comment_done:
                interacted = True

            if not opened_post_view or (not like_succeed and not already_liked):
                reason = "open" if not opened_post_view else "like"
                logger.info(
                    f"Could not {reason} media. Posts count: {profile_data.posts_count}."
                )
            logger.info("Back to profile.")
            while not post_grid_view._get_post_view().exists():
                logger.debug("We are in the wrong place...")
                device.back()
            device.back()

    if pm_percentage != 0 and can_send_PM(session_state, pm_percentage):
        sent_pm = _send_PM(device, session_state, my_username, swipe_amount)
        swipe_amount = 0
        if sent_pm:
            interacted = True
    if can_follow:
        followed = _follow(
            device,
            username,
            follow_percentage,
            args,
            session_state,
            swipe_amount,
        )
        if followed:
            interacted = True

    return (
        interacted,
        followed,
        profile_data.is_private,
        scraped,
        sent_pm,
        number_of_liked,
        number_of_watched,
        number_of_commented,
    )


def can_send_PM(session_state: SessionState, pm_percentage: int) -> bool:
    pm_chance = randint(1, 100)
    return not session_state.check_limit(
        limit_type=session_state.Limit.PM, output=True
    ) and (pm_chance <= pm_percentage)


def can_like(session_state: SessionState, likes_percentage: int) -> bool:
    likes_chance = randint(1, 100)
    return not session_state.check_limit(
        limit_type=session_state.Limit.LIKES, output=True
    ) and (likes_chance <= likes_percentage)


def can_comment(media_type: MediaType, profile_filter, current_mode) -> bool:
    (
        can_comment_photos,
        can_comment_videos,
        can_comment_carousels,
        can_comment_job,
    ) = profile_filter.can_comment(current_mode)
    if can_comment_job:
        if media_type == MediaType.PHOTO and can_comment_photos:
            return True
        elif (
            media_type in (MediaType.VIDEO, MediaType.IGTV, MediaType.REEL)
            and can_comment_videos
        ):
            return True
        elif media_type == MediaType.CAROUSEL and can_comment_carousels:
            return True
    logger.warning(
        f"Can't comment this {media_type} because filters are: can_comment_photos = {can_comment_photos}, can_comment_videos = {can_comment_videos}, can_comment_carousels = {can_comment_carousels}, can_comment_{current_mode} = {can_comment_job}. Check your filters.yml."
    )
    return False


def register_like(device, session_state):
    UniversalActions.detect_block(device)
    logger.debug("Like succeed.")
    session_state.totalLikes += 1
    session_state._publish_live_progress()


def register_story_like(session_state):
    session_state.register_story_like()


def register_story_account_liked(session_state):
    session_state.register_story_account_liked()


def register_daily_story_account(session_state):
    session_state.register_daily_story_account()


def is_follow_limit_reached_for_source(session_state, follow_limit, source):
    if follow_limit is None:
        return False

    followed_count = session_state.totalFollowed.get(source)
    return followed_count is not None and followed_count >= follow_limit


def _on_interaction(
    source,
    succeed,
    followed,
    scraped,
    interactions_limit,
    likes_limit,
    sessions,
    session_state,
    args,
):
    session_state = sessions[-1]
    session_state.add_interaction(source, succeed, followed, scraped)

    can_continue = True

    inside_working_hours, _ = SessionState.inside_working_hours(
        args.working_hours, args.time_delta_session
    )
    if not inside_working_hours:
        can_continue = False
    else:
        successful_interactions_count = session_state.successfulInteractions.get(source)
        if (
            successful_interactions_count
            and successful_interactions_count >= interactions_limit
        ):
            logger.info(
                "Reached interaction limit for that source, going to the next one..",
                extra={"color": f"{Fore.CYAN}"},
            )
            can_continue = False

        if args.scrape_to_file is not None:
            if session_state.check_limit(
                limit_type=session_state.Limit.SCRAPED, output=True
            ):
                logger.info(
                    "Reached scraped limit, finish.", extra={"color": f"{Fore.CYAN}"}
                )
                can_continue = False
        else:
            if (
                session_state.check_limit(
                    limit_type=session_state.Limit.LIKES, output=False
                )
                and args.end_if_likes_limit_reached
            ):
                logger.info(
                    "Reached liked limit, finish.", extra={"color": f"{Fore.CYAN}"}
                )
                can_continue = False

            if (
                session_state.check_limit(
                    limit_type=session_state.Limit.FOLLOWS, output=False
                )
                and args.end_if_follows_limit_reached
            ):
                logger.info(
                    "Reached followed limit, finish.", extra={"color": f"{Fore.CYAN}"}
                )
                can_continue = False

            if (
                session_state.check_limit(
                    limit_type=session_state.Limit.WATCHES, output=False
                )
                and args.end_if_watches_limit_reached
            ):
                logger.info(
                    "Reached watched limit, finish.", extra={"color": f"{Fore.CYAN}"}
                )
                can_continue = False

            if (
                session_state.check_limit(
                    limit_type=session_state.Limit.PM, output=False
                )
                and args.end_if_pm_limit_reached
            ):
                logger.info(
                    "Reached pm limit, finish.", extra={"color": f"{Fore.CYAN}"}
                )
                can_continue = False

            if (
                session_state.check_limit(
                    limit_type=session_state.Limit.COMMENTS, output=False
                )
                and args.end_if_comments_limit_reached
            ):
                logger.info(
                    "Reached comments limit, finish.", extra={"color": f"{Fore.CYAN}"}
                )
                can_continue = False

            if session_state.check_limit(
                limit_type=session_state.Limit.TOTAL, output=False
            ):
                logger.info(
                    "Reached total interaction limit, finish.",
                    extra={"color": f"{Fore.CYAN}"},
                )
                can_continue = False
            if session_state.check_limit(
                limit_type=session_state.Limit.SUCCESS, output=False
            ):
                logger.info(
                    "Reached total successfully interaction limit, finish.",
                    extra={"color": f"{Fore.CYAN}"},
                )
                can_continue = False

    if (can_continue and succeed) or scraped:
        if scraped:
            print_scrape_report(source, session_state)
        else:
            print_short_report(source, session_state)

    return can_continue


def _browse_carousel(device: DeviceFacade, obj_count: int) -> None:
    carousel_percentage = get_value(configs.args.carousel_percentage, None, 0)
    carousel_count = get_value(configs.args.carousel_count, None, 1)
    if carousel_percentage > randint(0, 100) and carousel_count > 1:
        media_obj = device.find(resourceIdMatches=ResourceID.CAROUSEL_MEDIA_GROUP)
        logger.info("Watching photos/videos in carousel.")
        if obj_count < carousel_count:
            logger.info(f"There are only {obj_count} media(s) in this carousel!")
            carousel_count = obj_count
        if media_obj.exists():
            media_obj_bounds = media_obj.get_bounds()
            n = 1
            while n < carousel_count:
                if media_obj.child(
                    resourceIdMatches=ResourceID.CAROUSEL_IMAGE_MEDIA_GROUP
                ).exists():
                    watch_photo_time = get_value(
                        configs.args.watch_photo_time,
                        "Watching photo for {}s.",
                        0,
                        its_time=True,
                    )
                    sleep(watch_photo_time)
                elif media_obj.child(
                    resourceIdMatches=ResourceID.CAROUSEL_VIDEO_MEDIA_GROUP
                ).exists():
                    watch_video_time = get_value(
                        configs.args.watch_video_time,
                        "Watching video for {}s.",
                        0,
                        its_time=True,
                    )
                    sleep(watch_video_time)
                start_point_y = (
                    (media_obj_bounds["bottom"] + media_obj_bounds["top"])
                    / 2
                    * uniform(0.85, 1.15)
                )
                start_point_x = uniform(0.85, 1.10) * (
                    media_obj_bounds["right"] * 5 / 6
                )
                delta_x = media_obj_bounds["right"] * uniform(0.5, 0.7)
                UniversalActions(device)._swipe_points(
                    start_point_y=start_point_y,
                    start_point_x=start_point_x,
                    delta_x=delta_x,
                    direction=Direction.LEFT,
                )
                n += 1


def _comment(
    device: DeviceFacade,
    my_username: str,
    comment_percentage: int,
    args,
    session_state: SessionState,
    media_type: MediaType,
) -> bool:
    if not session_state.check_limit(
        limit_type=session_state.Limit.COMMENTS, output=False
    ):
        if not random_choice(comment_percentage):
            return False
        universal_actions = UniversalActions(device)
        opened_post_view = OpenedPostView(device)
        in_fullscreen, fullscreen_media = opened_post_view._is_video_in_fullscreen()
        if not in_fullscreen:
            # we have to do a little swipe for preventing get the previous post comments button (which is covered by top bar, but present in hierarchy!!)
            universal_actions._swipe_points(
                direction=Direction.DOWN, delta_y=randint(150, 250)
            )
            tab_bar = device.find(
                resourceId=ResourceID.TAB_BAR,
            )
            media = device.find(
                resourceIdMatches=ResourceID.MEDIA_CONTAINER,
            )
            if (
                tab_bar.exists()
                and media.exists()
                and int(tab_bar.get_bounds()["top"]) - int(media.get_bounds()["bottom"])
                < 150
            ):
                universal_actions._swipe_points(
                    direction=Direction.DOWN, delta_y=randint(150, 250)
                )
        # look at hashtag of comment
        for _ in range(2):
            comment_button = opened_post_view.find_comment_button()
            if comment_button.exists():
                logger.info("Open comments of post.")
                comment_button.click()
                random_sleep(0.6, 1.2, modulable=False)
                # Limited-comments sheet opens, but typing is blocked — skip the post.
                if comments_are_limited(device):
                    logger.info(
                        "Comments on this post have been limited — skipping.",
                        extra={"color": f"{Fore.CYAN}"},
                    )
                    universal_actions.close_keyboard(device)
                    device.back()
                    return False
                comment_box = find_comment_thread_edittext(device)
                if comment_box.exists():
                    comment = load_random_comment(my_username, media_type)
                    if comment is None:
                        UniversalActions.close_keyboard(device)
                        device.back()
                        return False
                    # Re-check right before typing — the limited banner sometimes
                    # appears a beat after the compose field is visible.
                    if comments_are_limited(device):
                        logger.info(
                            "Comments on this post have been limited — skipping.",
                            extra={"color": f"{Fore.CYAN}"},
                        )
                        universal_actions.close_keyboard(device)
                        device.back()
                        return False
                    logger.info(
                        f"Write comment: {comment}", extra={"color": f"{Fore.CYAN}"}
                    )
                    try:
                        comment_box.set_text(
                            comment, Mode.PASTE if args.dont_type else Mode.TYPE
                        )
                    except Exception as exc:
                        # Typing can fail when comments are limited or the field
                        # lost focus — treat as skip rather than crashing the bot.
                        if comments_are_limited(device):
                            logger.info(
                                "Comments on this post have been limited — skipping.",
                                extra={"color": f"{Fore.CYAN}"},
                            )
                        else:
                            logger.warning(f"Could not type comment: {exc}")
                        universal_actions.close_keyboard(device)
                        device.back()
                        return False

                    post_button = device.find(
                        resourceId=ResourceID.LAYOUT_COMMENT_THREAD_POST_BUTTON_ICON
                    )
                    post_button.click()
                else:
                    logger.info(
                        "Comments on this post have been limited — skipping.",
                        extra={"color": f"{Fore.CYAN}"},
                    )
                    universal_actions.close_keyboard(device)
                    device.back()
                    return False

                universal_actions.detect_block(device)
                universal_actions.close_keyboard(device)
                comment_confirmed = _confirm_comment_posted(
                    device, my_username, comment
                )
                if comment_confirmed:
                    logger.info("Comment succeed.", extra={"color": f"{Fore.GREEN}"})
                    session_state.totalComments += 1
                else:
                    logger.warning("Failed to check if comment succeed.")

                logger.info("Go back to post view.")
                device.back()
                return comment_confirmed
            if in_fullscreen:
                if fullscreen_media.exists():
                    logger.debug("Revealing reel sidebar to find comment button...")
                    fullscreen_media.click()
                    random_sleep(0.3, 0.6, modulable=False, log=False)
                continue
            like_button = device.find(
                resourceId=ResourceID.ROW_FEED_BUTTON_LIKE,
            )
            if like_button.exists():
                logger.info("This post has comments disabled.")
                return False
            universal_actions._swipe_points(
                direction=Direction.DOWN, delta_y=randint(150, 250)
            )
    return False


def _send_PM(
    device,
    session_state: SessionState,
    my_username: str,
    swipe_amount: int,
    private: bool = False,
) -> bool:
    universal_actions = UniversalActions(device)
    if private:
        options = device.find(
            classNameMatches=ClassName.FRAME_LAYOUT,
            descriptionMatches=case_insensitive_re("^Options$"),
        )
        if options.exists(Timeout.SHORT):
            options.click()
        else:
            return False
        send_pm = device.find(
            classNameMatches=ClassName.BUTTON,
            textMatches=case_insensitive_re("^Send Message$"),
        )
        if send_pm.exists(Timeout.SHORT):
            send_pm.click()
        else:
            return False
    else:
        coordinator_layout = device.find(resourceId=ResourceID.COORDINATOR_ROOT_LAYOUT)
        if coordinator_layout.exists() and swipe_amount != 0:
            universal_actions._swipe_points(
                direction=Direction.UP, delta_y=swipe_amount
            )
        message_button = device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            enabled=True,
            textMatches="Message",
        )
        if message_button.exists(Timeout.SHORT):
            message_button.click()
        else:
            logger.warning("Cannot find the button for sending PMs!")
            return False
    message_box = device.find(
        resourceId=ResourceID.ROW_THREAD_COMPOSER_EDITTEXT,
        className=ClassName.EDIT_TEXT,
        enabled="true",
    )

    if message_box.exists():
        message = load_random_message(my_username)
        if message is None:
            logger.warning(
                "If you don't want to comment set 'pm-percentage: 0' in your config.yml."
            )
            device.back()
            return False
        nl = "\n"
        nlv = "\\n"
        logger.info(
            f"Write private message: {message.replace(nl, nlv)}",
            extra={"color": f"{Fore.CYAN}"},
        )
        message_box.set_text(message, Mode.PASTE if args.dont_type else Mode.TYPE)
        send_button = device.find(
            resourceIdMatches=ResourceID.ROW_THREAD_COMPOSER_BUTTON_SEND,
        )
        if send_button.exists():
            send_button.click()
            universal_actions.detect_block(device)
            universal_actions.close_keyboard(device)
            posted_text = device.find(text=f"{message}")
            message_sending_icon = device.find(
                resourceId=ResourceID.ACTION_ICON, className=ClassName.IMAGE_VIEW
            )
            if message_sending_icon.exists():
                random_sleep()
            if posted_text.exists(Timeout.MEDIUM) and not message_sending_icon.exists():
                logger.info("PM send succeed.", extra={"color": f"{Fore.GREEN}"})
                session_state.totalPm += 1
                pm_confirmed = True
            else:
                logger.warning("Failed to check if PM send succeed.")
                pm_confirmed = False
            logger.info("Go back to profile view.")
            device.back()
            return pm_confirmed
        else:
            logger.warning("Can't find SEND button!")
            universal_actions.close_keyboard(device)
            device.back()
            return False
    else:
        logger.info("PM to this user have been limited.")
        universal_actions.close_keyboard(device)
        device.back()
        return False


def _load_and_clean_txt_file(
    my_username: str, txt_filename: str
) -> Optional[List[str]]:
    def nonblank_lines(f):
        for ln in f:
            line = ln.rstrip()
            if line:
                yield line

    lines = []
    file_name = os.path.join(storage.ACCOUNTS, my_username, txt_filename)
    if path.isfile(file_name):
        try:
            with open(file_name, "r", encoding="utf-8") as f:
                for line in nonblank_lines(f):
                    lines.append(line)
                if lines:
                    return lines
                logger.warning(f"{file_name} is empty! Check your account folder.")
                return None
        except Exception as e:
            logger.error(f"Error: {e}.")
            return None
    logger.warning(f"{file_name} not found! Check your account folder.")
    return None


def load_random_message(my_username: str) -> Optional[str]:
    lines = _load_and_clean_txt_file(my_username, storage.FILENAME_MESSAGES)
    if lines is not None:
        random_message = choice(lines)
        return emoji.emojize(
            spintax.spin(random_message.replace("\\n", "\n")),
            use_aliases=True,
        )
    return None


def load_random_comment(my_username: str, media_type: MediaType) -> Optional[str]:
    try:
        from GramAddict.core.follow_vision_account import (
            ai_comments_enabled,
            generate_ai_comment,
        )

        if ai_comments_enabled(my_username):
            comment = generate_ai_comment(my_username)
            if comment:
                logger.debug("Using AI-generated comment (ai-comment-enabled in follow_vision.yml).")
                return emoji.emojize(comment, use_aliases=True)
    except Exception as exc:
        logger.warning(
            f"AI comment generation failed ({exc}); falling back to comments file."
        )
    lines = _load_and_clean_txt_file(my_username, storage.FILENAME_COMMENTS)
    if lines is None:
        return None
    try:
        photo_header = lines.index("%PHOTO")
        video_header = lines.index("%VIDEO")
        carousel_header = lines.index("%CAROUSEL")
    except ValueError:
        logger.warning(
            f"You didn't follow the rules for sections in your {storage.FILENAME_COMMENTS} txt file! Look at config example."
        )
        return None
    photo_comments = lines[photo_header + 1 : video_header]
    video_comments = lines[video_header + 1 : carousel_header]
    carousel_comments = lines[carousel_header + 1 :]
    random_comment = ""
    if media_type == MediaType.PHOTO:
        random_comment = choice(photo_comments) if len(photo_comments) > 0 else ""
    elif media_type in (MediaType.VIDEO, MediaType.IGTV, MediaType.REEL):
        random_comment = choice(video_comments) if len(video_comments) > 0 else ""
    elif media_type == MediaType.CAROUSEL:
        random_comment = choice(carousel_comments) if len(carousel_comments) > 0 else ""
    if random_comment != "":
        return emoji.emojize(spintax.spin(random_comment), use_aliases=True)
    else:
        return None


def _follow(device, username, follow_percentage, args, session_state, swipe_amount):
    if not session_state.check_limit(
        limit_type=session_state.Limit.FOLLOWS, output=False
    ):
        follow_chance = randint(1, 100)
        if follow_chance > follow_percentage:
            return False
        universal_actions = UniversalActions(device)
        coordinator_layout = device.find(resourceId=ResourceID.COORDINATOR_ROOT_LAYOUT)
        if coordinator_layout.exists(Timeout.MEDIUM) and swipe_amount != 0:
            universal_actions._swipe_points(
                direction=Direction.UP, delta_y=swipe_amount
            )

        FOLLOW_REGEX = "^Follow$"
        follow_button = device.find(
            clickable=True,
            textMatches=case_insensitive_re(FOLLOW_REGEX),
        )
        UNFOLLOW_REGEX = "^Following|^Requested"
        unfollow_button = device.find(
            clickable=True,
            textMatches=case_insensitive_re(UNFOLLOW_REGEX),
        )
        FOLLOWBACK_REGEX = "^Follow Back$"
        followback_button = device.find(
            clickable=True,
            textMatches=case_insensitive_re(FOLLOWBACK_REGEX),
        )

        if followback_button.exists():
            logger.info(
                f"@{username} already follows you.",
                extra={"color": f"{Fore.GREEN}"},
            )
            return False
        elif unfollow_button.exists():
            logger.info(
                f"You already follow @{username}.", extra={"color": f"{Fore.GREEN}"}
            )
            return False
        elif follow_button.exists():
            max_tries = 3
            for n in range(max_tries):
                follow_button.click()
                if device.find(
                    textMatches=UNFOLLOW_REGEX,
                    clickable=True,
                ).exists(Timeout.SHORT):
                    logger.info(f"Followed @{username}", extra={"color": Fore.GREEN})
                    universal_actions.detect_block(device)
                    return True
                else:
                    if n < max_tries - 1:
                        logger.debug(
                            "Looks like the click on the button didn't work, try again."
                        )
            logger.warning(
                f"Looks like I was not able to follow @{username}, maybe you got soft-banned for this action!",
                extra={"color": Fore.RED},
            )
            universal_actions.detect_block(device)
        else:
            logger.error(
                "Cannot find neither Follow button, Follow Back button, nor Unfollow button."
            )
            save_crash(device)

    else:
        logger.info("Reached total follows limit, not following.")
    return False


def _story_like_resource_ids():
    """Return the initialized ResourceID instance (not the class)."""
    global ResourceID
    if ResourceID is None:
        from GramAddict.core import views as ga_views
        import GramAddict.core.utils as ga_utils

        app_id = (
            getattr(getattr(ga_utils, "args", None), "app_id", None)
            or "com.instagram.android"
        )
        ResourceID = getattr(ga_views, "ResourceID", None) or resources(app_id)
    return ResourceID


def _find_story_like_button(device: DeviceFacade, *, fast: bool = False):
    """Locate the story viewer heart / like control."""
    ui_timeout = Timeout.TINY if fast else Timeout.SHORT
    rid = _story_like_resource_ids()
    btn = device.find(
        resourceIdMatches=case_insensitive_re(rid.TOOLBAR_LIKE_BUTTON)
    )
    if btn.exists(ui_timeout):
        return btn
    for desc in ("Like", "like"):
        btn = device.find(descriptionMatches=case_insensitive_re(desc))
        if btn.exists(ui_timeout):
            return btn
    return None


def list_story_likes_only(args: Namespace) -> bool:
    """True when list jobs should only like stories (no follows / post likes).

    Enabled by setting follow-percentage and likes-percentage to 0 while
    stories-percentage is > 0. Those jobs then tap the story ring in the
    followers/likers list instead of opening the profile.
    """
    follow_pct = get_value(getattr(args, "follow_percentage", None), None, 40)
    likes_pct = get_value(getattr(args, "likes_percentage", None), None, 100)
    stories_pct = get_value(getattr(args, "stories_percentage", None), None, 0)
    return follow_pct == 0 and likes_pct == 0 and stories_pct > 0


def _bounds_overlap_y(a: dict, b: dict) -> bool:
    """True when two UiAutomator bounds dicts overlap vertically."""
    return not (a["bottom"] <= b["top"] or a["top"] >= b["bottom"])


def find_list_row_story_ring(row, username: str):
    """Return the clickable story-ring control in a followers/likers row, or None.

    Likers sheet: ``row_user_imageview`` Button with desc ``View {user}'s story``.
    Followers list: ``follow_list_user_imageview`` FrameLayout that contains a
    ring stub View (content-desc like ``@2131975699``).

    Important: UiAutomator ``.child()`` is *not* reliably scoped to the row —
    ring stubs from other visible rows can match. Always verify Y-bounds overlap
    with the row (and prefer device-wide stub scan + bounds match for followers).
    """
    want = (username or "").strip().lstrip("@")
    if not want:
        return None

    try:
        row_bounds = row.get_bounds()
    except DeviceFacade.JsonRpcError:
        return None

    # Likers / user rows — explicit "View …'s story" a11y label.
    for desc_re in (
        rf"View\s+{re.escape(want)}'s\s+story",
        r"View\s+.+'s\s+story",
    ):
        story_btn = row.child(
            resourceIdMatches=case_insensitive_re(ResourceID.LIST_ROW_USER_AVATAR),
            descriptionMatches=case_insensitive_re(desc_re),
        )
        if not story_btn.exists(Timeout.ZERO):
            continue
        try:
            if _bounds_overlap_y(row_bounds, story_btn.get_bounds()):
                return story_btn
        except DeviceFacade.JsonRpcError:
            continue

    # Followers list — ring is android.view.View with unresolved @digits desc.
    # Match stubs device-wide, then keep only those that overlap this row.
    try:
        stubs = row.deviceV2(
            className=ClassName.VIEW,
            resourceId=ResourceID.FOLLOW_LIST_USER_IMAGEVIEW,
            descriptionMatches=r"@\d+",
        )
        stub_count = stubs.count
    except Exception:
        stub_count = 0
        stubs = None

    for i in range(stub_count):
        stub = stubs[i]
        try:
            stub_bounds = stub.info["bounds"]
        except Exception:
            continue
        if not _bounds_overlap_y(row_bounds, stub_bounds):
            continue
        # Click the FrameLayout avatar that owns this ring (stub itself is not clickable).
        try:
            avatars = row.deviceV2(
                resourceId=ResourceID.FOLLOW_LIST_USER_IMAGEVIEW,
                className=ClassName.FRAME_LAYOUT,
                clickable=True,
            )
            for j in range(avatars.count):
                avatar = avatars[j]
                try:
                    avatar_bounds = avatar.info["bounds"]
                except Exception:
                    continue
                if _bounds_overlap_y(stub_bounds, avatar_bounds) and _bounds_overlap_y(
                    row_bounds, avatar_bounds
                ):
                    return DeviceFacade.View(view=avatar, device=row.deviceV2)
        except Exception:
            pass
        return DeviceFacade.View(view=stub, device=row.deviceV2)

    return None


def _followers_or_likers_list_visible(device: DeviceFacade) -> bool:
    """True when a followers list or post-likers sheet is on screen."""
    rid = _story_like_resource_ids()
    try:
        if device.find(
            resourceIdMatches=case_insensitive_re(rid.USER_LIST_CONTAINER)
        ).exists(Timeout.ZERO):
            return True
        if device.find(textMatches=case_insensitive_re(r"^Likes$")).exists(Timeout.ZERO):
            return True
    except DeviceFacade.AppHasCrashed:
        return False
    return False


def _story_viewer_visible(device: DeviceFacade) -> bool:
    rid = _story_like_resource_ids()
    try:
        return device.find(
            resourceIdMatches=case_insensitive_re(
                f"{rid.REEL_VIEWER_MEDIA_CONTAINER}|{rid.REEL_VIEWER_TITLE}"
            )
        ).exists(Timeout.ZERO)
    except DeviceFacade.AppHasCrashed:
        return False


def close_story_back_to_list(device: DeviceFacade, *, max_backs: int = 6) -> bool:
    """Leave the story viewer (or a wrongly opened profile) and return to the list."""
    for _ in range(max_backs):
        try:
            if _followers_or_likers_list_visible(device):
                return True
            # modulable=False — default back() sleep is slow and looks like a hang.
            device.back(modulable=False)
            random_sleep(0.2, 0.35, modulable=False, log=False, minimum=0.1)
        except DeviceFacade.AppHasCrashed:
            logger.warning(
                "Instagram left foreground while closing story — will restart."
            )
            raise
    return _followers_or_likers_list_visible(device)


def like_stories_from_list_row(
    device: DeviceFacade,
    row,
    username: str,
    args: Namespace,
    session_state: SessionState,
) -> int:
    """Tap the list-row story ring and like up to 8 story segments.

    Always tries to land back on the followers/likers list so the next row
    iteration is not left on a profile or inside the story viewer.
    """
    if not random_choice(
        get_value(getattr(args, "stories_percentage", None), None, 100)
    ):
        return 0
    if session_state.check_limit(
        limit_type=session_state.Limit.WATCHES, output=False
    ):
        logger.info("Reached total watch limit, not watching stories.")
        return 0

    ring = find_list_row_story_ring(row, username)
    if ring is None:
        logger.info(
            f"@{username}: no story ring — skip.",
            extra={"color": f"{Fore.GREEN}"},
        )
        return 0

    logger.info(
        f"@{username}: story ring found — opening.",
        extra={"color": f"{Fore.YELLOW}"},
    )
    try:
        ring.click(sleep=SleepTime.ZERO)
    except DeviceFacade.JsonRpcError:
        logger.info(f"@{username}: story ring vanished before tap.")
        return 0

    random_sleep(0.45, 0.8, modulable=False, log=False, minimum=0.2)
    story_view = CurrentStoryView(device)
    story_frame = story_view.getStoryFrame()
    if not story_frame.exists(Timeout.MEDIUM):
        logger.info(
            f"@{username}: story viewer didn't open — back to list.",
            extra={"color": f"{Fore.GREEN}"},
        )
        close_story_back_to_list(device)
        return 0

    # Like every segment on this profile, capped at 8 (same as profile path).
    stories_to_like = 8
    likes_counter = 0
    pause = (0.1, 0.2)
    after_like = (0.1, 0.17)

    def like_one() -> bool:
        nonlocal likes_counter
        if session_state.check_limit(
            limit_type=session_state.Limit.WATCHES, output=False
        ):
            return False
        session_state.totalWatched += 1
        for _ in range(4):
            random_sleep(*pause, modulable=False, log=False, minimum=0.05)
            obj = _find_story_like_button(device, fast=True)
            if obj is None:
                continue
            try:
                if not obj.get_selected():
                    obj.click()
                    random_sleep(*after_like, modulable=False, log=False, minimum=0.05)
                    logger.info(
                        f"@{username}: story liked ({likes_counter + 1}/{stories_to_like}).",
                        extra={"color": f"{Fore.GREEN}"},
                    )
                else:
                    logger.info(
                        f"@{username}: story already liked "
                        f"({likes_counter + 1}/{stories_to_like})."
                    )
                likes_counter += 1
                register_story_like(session_state)
                if likes_counter == 1:
                    register_story_account_liked(session_state)
                return True
            except Exception as exc:
                logger.warning("@%s: story like tap failed: %s", username, exc)
        logger.warning(f"@{username}: opened story but could not like it.")
        return False

    try:
        logger.info(
            f"@{username}: story open — liking up to {stories_to_like} "
            f"(session watches {session_state.totalWatched}).",
            extra={"color": f"{Fore.GREEN}"},
        )
        if not like_one():
            pass
        else:
            for _ in range(stories_to_like - 1):
                if session_state.check_limit(
                    limit_type=session_state.Limit.WATCHES, output=False
                ):
                    break
                # Still on this user's story?
                try:
                    current = (story_view.getUsername() or "").strip()
                    if (
                        current
                        and current != "BUG!"
                        and current.casefold() != username.casefold()
                    ):
                        break
                except Exception:
                    pass
                try:
                    logger.debug(f"@{username}: next story segment...")
                    story_frame.click(
                        mode=Location.RIGHTEDGE,
                        sleep=SleepTime.ZERO,
                        crash_report_if_fails=False,
                    )
                    random_sleep(*pause, modulable=False, log=False, minimum=0.05)
                except Exception as exc:
                    logger.debug("@%s: could not advance story: %s", username, exc)
                    break
                if not like_one():
                    break
        if likes_counter:
            logger.info(
                f"@{username}: liked {likes_counter} story segment(s).",
                extra={"color": f"{Fore.GREEN}"},
            )
    finally:
        logger.info(f"@{username}: closing story → back to list.")
        if not close_story_back_to_list(device):
            logger.warning(
                f"@{username}: could not confirm followers/likers list after story."
            )
        else:
            logger.info(f"@{username}: back on followers/likers list.")
    # Return >0 whenever the viewer opened so the list loop can scroll onward.
    return likes_counter if likes_counter > 0 else 1


def like_all_profile_stories(
    device: DeviceFacade,
    profile_view: ProfileView,
    username: str,
    args: Namespace,
    session_state: SessionState,
    *,
    require_unviewed: bool = False,
    always_like_stories: bool = False,
    daily_story_likes: bool = False,
    already_open: bool = False,
) -> int:
    """Open profile stories, watch each segment, and like it. Returns segments liked."""
    check_instagram_rate_limit(device)
    if session_state.check_limit(
        limit_type=session_state.Limit.WATCHES, output=True
    ):
        logger.info("Reached total watch limit, not watching stories.")
        return 0

    if not already_open:
        if always_like_stories:
            story_ui_timeout = Timeout.TINY
            if not profile_view.has_story_to_like(ui_timeout=story_ui_timeout):
                if profile_view.live_marker().exists(Timeout.ZERO):
                    logger.info(f"@{username} is making a live.")
                else:
                    logger.info(f"@{username} has no story — skip.")
                return 0
        elif require_unviewed:
            if not profile_view.has_unviewed_story():
                logger.info(f"@{username} has no new story — skip.")
                return 0
        elif not profile_view.has_story_to_like():
            if profile_view.live_marker().exists(Timeout.SHORT):
                logger.info(f"@{username} is making a live.")
            return 0

    likes_counter = 0
    fast = always_like_stories or already_open
    pause = (0.1, 0.2) if fast else (0.3, 0.6)
    after_like = (0.1, 0.17) if fast else (0.3, 0.5)
    story_open_sleep = SleepTime.ZERO if fast else SleepTime.DEFAULT
    story_wait = Timeout.TINY if fast else Timeout.MEDIUM

    def like_story() -> bool:
        nonlocal likes_counter
        for _ in range(3 if fast else 4):
            random_sleep(*pause, modulable=False, log=False, minimum=0.05)
            obj = _find_story_like_button(device, fast=fast)
            if obj is None:
                continue
            try:
                if not obj.get_selected():
                    obj.click()
                    random_sleep(*after_like, modulable=False, log=False, minimum=0.05)
                    logger.info("Story has been liked!")
                else:
                    logger.info("Story is already liked!")
                likes_counter += 1
                # Count hearts for reporting + live progress (daily + normal).
                register_story_like(session_state)
                if likes_counter == 1:
                    register_story_account_liked(session_state)
                return True
            except Exception as exc:
                logger.warning("Story like tap failed: %s", exc)
        logger.warning("Could not find story like button.")
        return False

    def watch_story() -> bool:
        if session_state.check_limit(
            limit_type=session_state.Limit.WATCHES, output=False
        ):
            return False
        logger.debug("Watching stories...")
        session_state.totalWatched += 1
        if always_like_stories or already_open:
            like_story()
            return True
        for _ in range(7):
            random_sleep(0.5, 1, modulable=False, log=False)
            if story_view.getUsername().strip().casefold() != username.casefold():
                return False
        like_story()
        return True

    stories_to_watch: int = get_value(args.stories_count, "Stories count: {}.", 1)
    if always_like_stories or already_open:
        # Daily / list story likes: like every segment on the profile, capped at 8.
        stories_to_watch = 8
    if not already_open:
        logger.debug("Open the story container.")
        profile_view.StoryRing().click(sleep=story_open_sleep)
    story_view = CurrentStoryView(device)
    story_frame = story_view.getStoryFrame()
    story_frame.wait(story_wait)
    story_username = story_view.getUsername()
    if (
        story_username == "BUG!"
        or story_username.strip().casefold() == username.casefold()
    ):
        start = datetime.now()
        try:
            if not watch_story():
                return likes_counter
        except Exception as e:
            logger.debug(f"Exception: {e}")
            logger.debug(
                "Ignore this error! Stories ended while we were interacting with it."
            )
        for _ in range(stories_to_watch - 1):
            try:
                logger.debug("Going to the next story...")
                story_frame.click(
                    mode=Location.RIGHTEDGE,
                    sleep=SleepTime.ZERO,
                    crash_report_if_fails=False,
                )
                random_sleep(*pause, modulable=False, log=False, minimum=0.05)
                if not watch_story():
                    break
            except Exception as e:
                logger.debug(f"Exception: {e}")
                logger.debug(
                    "Ignore this error! Stories ended while we were interacting with it."
                )
                break
        for _ in range(4):
            if (
                story_view.getUsername().strip().casefold()
                == username.casefold()
            ):
                device.back()
            else:
                break
        session_state.check_limit(
            limit_type=session_state.Limit.WATCHES, output=True
        )
        logger.info(
            f"Liked {likes_counter} story segment(s) in {(datetime.now()-start).total_seconds():.2f}s."
        )
        return likes_counter

    logger.warning("Failed to open the story container.")
    logger.debug(f"Story username: {story_username}")
    save_crash(device)
    if story_frame.exists():
        device.back()
    return 0


def _watch_stories(
    device: DeviceFacade,
    profile_view: ProfileView,
    username: str,
    stories_percentage: int,
    args: Namespace,
    session_state: SessionState,
) -> int:
    if not random_choice(stories_percentage):
        return 0
    return like_all_profile_stories(
        device,
        profile_view,
        username,
        args,
        session_state,
        require_unviewed=False,
    )
