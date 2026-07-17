import json
import logging
import os
import sys
from datetime import datetime, timedelta
from enum import Enum, unique
from typing import Optional, Union

from atomicwrites import atomic_write

logger = logging.getLogger(__name__)

ACCOUNTS = "accounts"
REPORTS = "reports"
FILENAME_HISTORY_FILTER_USERS = "history_filters_users.json"
FILENAME_INTERACTED_USERS = "interacted_users.json"
OLD_FILTER = "filter.json"
FILTER = "filters.yml"
USER_LAST_INTERACTION = "last_interaction"
USER_LAST_COMMENTED = "last_commented"
USER_LAST_STORY_CHECK = "last_story_check"
USER_FOLLOWING_STATUS = "following_status"

FILENAME_WHITELIST = "whitelist.txt"
FILENAME_BLACKLIST = "blacklist.txt"
FILENAME_COMMENTS = "comments_list.txt"
FILENAME_MESSAGES = "pm_list.txt"


class Storage:
    def __init__(self, my_username, brand_pool: Optional[str] = None):
        if my_username is None:
            logger.error(
                "No username, thus the script won't get access to interacted users and sessions data."
            )
            return
        self.my_username = my_username
        self.brand_pool = brand_pool
        # Usernames this process has interacted with this session. Used to merge
        # only our own changes into the shared brand-pool file so that a
        # simultaneously-running account in the same pool doesn't get its entries
        # clobbered by a full-file overwrite.
        self._pool_dirty_users: set[str] = set()
        self.account_path = os.path.join(ACCOUNTS, my_username)
        if not os.path.exists(self.account_path):
            os.makedirs(self.account_path)
        self.interacted_users = {}
        self.history_filter_users = {}

        if brand_pool:
            from GramAddict.core.brand_pool import (
                interacted_users_path,
                load_interacted_users,
                migrate_account_interactions,
            )

            migrate_account_interactions(my_username, brand_pool)
            self.interacted_users_path = interacted_users_path(brand_pool)
            self.interacted_users = load_interacted_users(brand_pool)
            logger.info(
                f"Using brand pool '{brand_pool}' for interaction history "
                f"({len(self.interacted_users)} users in pool)."
            )
        else:
            self.interacted_users_path = os.path.join(
                self.account_path, FILENAME_INTERACTED_USERS
            )
            if os.path.isfile(self.interacted_users_path):
                with open(self.interacted_users_path, encoding="utf-8") as json_file:
                    try:
                        self.interacted_users = json.load(json_file)
                    except Exception as e:
                        logger.error(
                            f"Please check {json_file.name}, it contains this error: {e}"
                        )
                        sys.exit(0)
        self.history_filter_users_path = os.path.join(
            self.account_path, FILENAME_HISTORY_FILTER_USERS
        )

        if os.path.isfile(self.history_filter_users_path):
            with open(self.history_filter_users_path, encoding="utf-8") as json_file:
                try:
                    self.history_filter_users = json.load(json_file)
                except Exception as e:
                    logger.error(
                        f"Please check {json_file.name}, it contains this error: {e}"
                    )
                    sys.exit(0)
        self.filter_path = os.path.join(self.account_path, FILTER)
        if not os.path.exists(self.filter_path):
            self.filter_path = os.path.join(self.account_path, OLD_FILTER)

        whitelist_path = os.path.join(self.account_path, FILENAME_WHITELIST)
        if os.path.exists(whitelist_path):
            with open(whitelist_path, encoding="utf-8") as file:
                self.whitelist = [line.rstrip() for line in file]
        else:
            self.whitelist = []

        blacklist_path = os.path.join(self.account_path, FILENAME_BLACKLIST)
        if os.path.exists(blacklist_path):
            with open(blacklist_path, encoding="utf-8") as file:
                self.blacklist = [line.rstrip() for line in file]
        else:
            self.blacklist = []

        self.report_path = os.path.join(self.account_path, REPORTS)

    def can_be_reinteract(
        self,
        last_interaction: datetime,
        hours_that_have_to_pass: Optional[Union[int, float]],
    ) -> bool:
        if hours_that_have_to_pass is None:
            return False
        elif hours_that_have_to_pass == 0:
            return True
        return self._check_time(
            last_interaction, timedelta(hours=hours_that_have_to_pass)
        )

    def can_be_unfollowed(
        self, last_interaction: datetime, days_that_have_to_pass: Optional[int]
    ) -> bool:
        if days_that_have_to_pass is None:
            return False
        return self._check_time(
            last_interaction, timedelta(days=days_that_have_to_pass)
        )

    def _check_time(
        self, stored_time: Optional[datetime], limit_time: timedelta
    ) -> bool:
        if stored_time is None or limit_time == timedelta(hours=0):
            return True
        return datetime.now() - stored_time >= limit_time

    def check_user_was_interacted(self, username):
        """returns when a username has been interacted, False if not already interacted"""
        user = self.interacted_users.get(username)
        if user is None:
            return False, None

        last_interaction = datetime.strptime(
            user[USER_LAST_INTERACTION], "%Y-%m-%d %H:%M:%S.%f"
        )
        return True, last_interaction

    def can_comment_user(self, username, cooldown_days: Optional[Union[int, float]]) -> bool:
        """True when this user is outside the comment cooldown window."""
        if cooldown_days is None or cooldown_days <= 0:
            return True
        user = self.interacted_users.get(username)
        if not user:
            return True
        last_commented = user.get(USER_LAST_COMMENTED)
        if not last_commented:
            return True
        last_dt = datetime.strptime(last_commented, "%Y-%m-%d %H:%M:%S.%f")
        return datetime.now() - last_dt >= timedelta(days=cooldown_days)

    def check_user_story_cooldown(
        self, username, hours: Optional[Union[int, float]]
    ) -> tuple[bool, Optional[datetime]]:
        """Return (on_cooldown, last_check_time) for daily story likes."""
        if hours is None or hours <= 0:
            return False, None
        user = self.interacted_users.get(username)
        if not user:
            return False, None
        last_story_check = user.get(USER_LAST_STORY_CHECK)
        if not last_story_check:
            return False, None
        last_dt = datetime.strptime(last_story_check, "%Y-%m-%d %H:%M:%S.%f")
        if datetime.now() - last_dt < timedelta(hours=hours):
            return True, last_dt
        return False, last_dt

    def story_checked_today(self, username: str) -> bool:
        """True when this user was already visited by daily story likes today."""
        user = self.interacted_users.get(username)
        if not user:
            return False
        last_story_check = user.get(USER_LAST_STORY_CHECK)
        if not last_story_check:
            return False
        try:
            last_dt = datetime.strptime(last_story_check, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            return False
        return last_dt.date() == datetime.now().date()

    def count_story_checks_today_in_list(self, usernames: list[str]) -> int:
        count = 0
        for raw in usernames:
            username = raw.strip().lstrip("@")
            if username and self.story_checked_today(username):
                count += 1
        return count

    def record_story_check(self, username, session_id, when: Optional[datetime] = None):
        """Track a story-like visit without resetting profile interaction time."""
        stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S.%f")
        user = self.interacted_users.get(username, {})
        user[USER_LAST_STORY_CHECK] = stamp
        user["session_id"] = session_id
        self.interacted_users[username] = user
        self._pool_dirty_users.add(username)
        self._update_file()

    def get_following_status(self, username):
        user = self.interacted_users.get(username)
        if user is None:
            return FollowingStatus.NOT_IN_LIST
        else:
            return FollowingStatus[user[USER_FOLLOWING_STATUS].upper()]

    def add_filter_user(self, username, profile_data, skip_reason=None):
        user = profile_data.__dict__
        user["follow_button_text"] = (
            profile_data.follow_button_text.name
            if not profile_data.is_restricted
            else None
        )
        user["skip_reason"] = None if skip_reason is None else skip_reason.name
        self.history_filter_users[username] = user
        if self.history_filter_users_path is not None:
            with atomic_write(
                self.history_filter_users_path, overwrite=True, encoding="utf-8"
            ) as outfile:
                json.dump(self.history_filter_users, outfile, indent=4, sort_keys=False)

    def add_interacted_user(
        self,
        username,
        session_id,
        followed=False,
        is_requested=None,
        unfollowed=False,
        scraped=False,
        liked=0,
        watched=0,
        commented=0,
        pm_sent=False,
        job_name=None,
        target=None,
    ):
        user = self.interacted_users.get(username, {})
        user[USER_LAST_INTERACTION] = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

        if followed:
            if is_requested:
                user[USER_FOLLOWING_STATUS] = FollowingStatus.REQUESTED.name.casefold()
            else:
                user[USER_FOLLOWING_STATUS] = FollowingStatus.FOLLOWED.name.casefold()
        elif unfollowed:
            user[USER_FOLLOWING_STATUS] = FollowingStatus.UNFOLLOWED.name.casefold()
        elif scraped:
            user[USER_FOLLOWING_STATUS] = FollowingStatus.SCRAPED.name.casefold()
        else:
            user[USER_FOLLOWING_STATUS] = FollowingStatus.NONE.name.casefold()

        # Save only the last session_id
        user["session_id"] = session_id

        # Save only the last job_name and target
        if not user.get("job_name"):
            user["job_name"] = job_name
        if not user.get("target"):
            user["target"] = target

        # Increase the value of liked, watched or commented if we have already a value
        user["liked"] = liked if "liked" not in user else (user["liked"] + liked)
        user["watched"] = (
            watched if "watched" not in user else (user["watched"] + watched)
        )
        user["commented"] = (
            commented if "commented" not in user else (user["commented"] + commented)
        )
        if commented and commented > 0:
            user[USER_LAST_COMMENTED] = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

        # Update the followed or unfollowed boolean only if we have a real update
        user["followed"] = (
            followed
            if "followed" not in user or user["followed"] != followed
            else user["followed"]
        )
        user["unfollowed"] = (
            unfollowed
            if "unfollowed" not in user or user["unfollowed"] != unfollowed
            else user["unfollowed"]
        )
        user["scraped"] = (
            scraped
            if "scraped" not in user or user["scraped"] != scraped
            else user["scraped"]
        )
        # Save the boolean if we sent a PM
        user["pm_sent"] = (
            pm_sent
            if "pm_sent" not in user or user["pm_sent"] != pm_sent
            else user["pm_sent"]
        )
        self.interacted_users[username] = user
        self._pool_dirty_users.add(username)
        self._update_file()

    def is_user_in_whitelist(self, username):
        return username in self.whitelist

    def is_user_in_blacklist(self, username):
        return username in self.blacklist

    def _get_last_day_interactions_count(self):
        count = 0
        users_list = list(self.interacted_users.values())
        for user in users_list:
            last_interaction = datetime.strptime(
                user[USER_LAST_INTERACTION], "%Y-%m-%d %H:%M:%S.%f"
            )
            is_last_day = datetime.now() - last_interaction <= timedelta(days=1)
            if is_last_day:
                count += 1
        return count

    def _update_file(self):
        if self.interacted_users_path is None:
            return

        # Brand pool: the file is shared across accounts that may run at the same
        # time. Re-read the current file and overlay ONLY the users this process
        # touched, so we never wipe entries another account added concurrently.
        if self.brand_pool:
            merged = {}
            if os.path.isfile(self.interacted_users_path):
                try:
                    with open(self.interacted_users_path, encoding="utf-8") as handle:
                        loaded = json.load(handle)
                    if isinstance(loaded, dict):
                        merged = loaded
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning(
                        f"Could not re-read shared pool file for merge ({exc}); "
                        "writing our copy instead."
                    )
                    merged = dict(self.interacted_users)
            for username in self._pool_dirty_users:
                if username in self.interacted_users:
                    merged[username] = self.interacted_users[username]
            # Keep our in-memory view consistent with what's now on disk so later
            # reads (e.g. re-interaction checks) see other accounts' entries too.
            self.interacted_users = merged
            data_to_write = merged
        else:
            data_to_write = self.interacted_users

        with atomic_write(
            self.interacted_users_path, overwrite=True, encoding="utf-8"
        ) as outfile:
            json.dump(data_to_write, outfile, indent=4, sort_keys=False)


@unique
class FollowingStatus(Enum):
    NONE = 0
    FOLLOWED = 1
    REQUESTED = 2
    UNFOLLOWED = 3
    NOT_IN_LIST = 4
    SCRAPED = 5
