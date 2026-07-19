import logging
from functools import partial
from random import seed

from colorama import Style

from GramAddict.core.decorators import run_safely
from GramAddict.core.handle_sources import handle_followers
from GramAddict.core.interaction import (
    interact_with_user,
    is_follow_limit_reached_for_source,
)
from GramAddict.core.plugin_loader import Plugin
from GramAddict.core.resources import ResourceID as resources
from GramAddict.core.scroll_end_detector import ScrollEndDetector
from GramAddict.core.utils import get_value, init_on_things, sample_sources

logger = logging.getLogger(__name__)


# Script Initialization
seed()


class InteractBloggerFollowers_Following(Plugin):
    """Handles the functionality of interacting with a bloggers followers/following"""

    def __init__(self):
        super().__init__()
        self.description = (
            "Handles the functionality of interacting with a bloggers followers"
        )
        self.arguments = [
            {
                "arg": "--blogger-followers",
                "nargs": "+",
                "help": "list of usernames with whose followers you want to interact",
                "metavar": ("username1", "username2"),
                "default": None,
                "operation": True,
            },
            {
                "arg": "--blogger-following",
                "nargs": "+",
                "help": "list of usernames with whose following you want to interact",
                "metavar": ("username1", "username2"),
                "default": None,
                "operation": True,
            },
        ]

    def run(self, device, configs, storage, sessions, profile_filter, plugin):
        class State:
            def __init__(self):
                pass

            is_job_completed = False

        self.device_id = configs.args.device
        self.state = None
        self.sessions = sessions
        self.session_state = sessions[-1]
        self.args = configs.args
        self.ResourceID = resources(self.args.app_id)
        self.current_mode = plugin

        # IMPORTANT: in each job we assume being on the top of the Profile tab already
        if self.args.blogger_followers is not None:
            sources = [s for s in self.args.blogger_followers if s.strip()]
            is_followers_job = True
        else:
            sources = [s for s in self.args.blogger_following if s.strip()]
            is_followers_job = False

        account_key = (
            getattr(self.session_state, "my_username", None)
            or getattr(self.args, "username", None)
            or ""
        )

        # Sample an initial set, then keep draining sources discovered live by
        # follow vision (music videographers / wedding photo-video vendors).
        pending = list(sample_sources(sources, self.args.truncate_sources))
        seen = {s.lstrip("@").strip().casefold() for s in pending if s.strip()}

        while pending:
            source = pending.pop(0)
            (
                active_limits_reached,
                _,
                actions_limit_reached,
            ) = self.session_state.check_limit(limit_type=self.session_state.Limit.ALL)
            limit_reached = active_limits_reached or actions_limit_reached

            self.state = State()
            is_myself = source.lstrip("@") == self.session_state.my_username
            its_you = is_myself and " (it's you)" or ""
            logger.info(
                f"Handle {source} {its_you}", extra={"color": f"{Style.BRIGHT}"}
            )

            # Init common things
            (
                on_interaction,
                stories_percentage,
                likes_percentage,
                follow_percentage,
                comment_percentage,
                pm_percentage,
                interact_percentage,
            ) = init_on_things(source, self.args, self.sessions, self.session_state)

            @run_safely(
                device=device,
                device_id=self.device_id,
                sessions=self.sessions,
                session_state=self.session_state,
                screen_record=self.args.screen_record,
                configs=configs,
            )
            def job():
                self.handle_blogger(
                    device,
                    source,
                    plugin,
                    storage,
                    profile_filter,
                    on_interaction,
                    stories_percentage,
                    likes_percentage,
                    follow_percentage,
                    comment_percentage,
                    pm_percentage,
                    interact_percentage,
                )
                self.state.is_job_completed = True

            while not self.state.is_job_completed and not limit_reached:
                job()

            if limit_reached:
                logger.info("Ending session.")
                self.session_state.check_limit(
                    limit_type=self.session_state.Limit.ALL, output=True
                )
                break

            if is_followers_job and account_key:
                try:
                    from GramAddict.core.follow_vision_account import (
                        drain_discovered_bloggers,
                    )

                    for discovered in drain_discovered_bloggers(account_key):
                        key = discovered.lstrip("@").strip().casefold()
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        pending.append(discovered)
                        logger.info(
                            f"Queued newly found blogger-followers source @{discovered} (live).",
                            extra={"color": f"{Style.BRIGHT}"},
                        )
                except Exception as exc:
                    logger.debug("Could not drain discovered bloggers: %s", exc)

    def handle_blogger(
        self,
        device,
        username,
        current_job,
        storage,
        profile_filter,
        on_interaction,
        stories_percentage,
        likes_percentage,
        follow_percentage,
        comment_percentage,
        pm_percentage,
        interact_percentage,
    ):
        interaction = partial(
            interact_with_user,
            my_username=self.session_state.my_username,
            likes_count=self.args.likes_count,
            likes_percentage=likes_percentage,
            stories_percentage=stories_percentage,
            follow_percentage=follow_percentage,
            comment_percentage=comment_percentage,
            pm_percentage=pm_percentage,
            profile_filter=profile_filter,
            args=self.args,
            session_state=self.session_state,
            scraping_file=self.args.scrape_to_file,
            current_mode=self.current_mode,
        )
        source_follow_limit = (
            get_value(self.args.follow_limit, None, 15)
            if self.args.follow_limit is not None
            else None
        )
        is_follow_limit_reached = partial(
            is_follow_limit_reached_for_source,
            session_state=self.session_state,
            follow_limit=source_follow_limit,
            source=username,
        )

        skipped_list_limit = get_value(self.args.skipped_list_limit, None, 15)
        skipped_fling_limit = get_value(self.args.fling_when_skipped, None, 0)

        posts_end_detector = ScrollEndDetector(
            repeats_to_end=2,
            skipped_list_limit=skipped_list_limit,
            skipped_fling_limit=skipped_fling_limit,
        )
        handle_followers(
            self,
            device,
            self.session_state,
            username,
            current_job,
            storage,
            on_interaction,
            interaction,
            is_follow_limit_reached,
            posts_end_detector,
        )
