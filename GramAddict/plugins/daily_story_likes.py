import logging

from colorama import Style

from GramAddict.core.decorators import run_safely
from GramAddict.core.handle_sources import handle_daily_story_likes_from_file
from GramAddict.core.plugin_loader import Plugin
from GramAddict.core.utils import sample_sources

logger = logging.getLogger(__name__)


class DailyStoryLikes(Plugin):
    """Like new stories for a fixed list of accounts (daily story likes)."""

    def __init__(self):
        super().__init__()
        self.description = (
            "Visit accounts from a list, like all segments in any new story, "
            "then move to the next account. Use can-reinteract-after: 24 for once per day."
        )
        self.arguments = [
            {
                "arg": "--daily-story-likes",
                "nargs": "+",
                "help": "filenames of daily story-like username lists [*.txt]",
                "metavar": ("story_likes.txt",),
                "default": None,
                "operation": True,
            },
        ]

    def run(self, device, configs, storage, sessions, profile_filter, plugin):
        self.device_id = configs.args.device
        self.sessions = sessions
        self.session_state = sessions[-1]
        self.args = configs.args

        sources = [f for f in self.args.daily_story_likes if f.strip()]
        for source in sample_sources(sources, self.args.truncate_sources):
            active_limits_reached, _, actions_limit_reached = self.session_state.check_limit(
                limit_type=self.session_state.Limit.ALL
            )
            if active_limits_reached or actions_limit_reached:
                logger.info("Session limits reached before daily story likes.")
                return

            logger.info(
                f"Daily story likes: {source}",
                extra={"color": f"{Style.BRIGHT}"},
            )

            @run_safely(
                device=device,
                device_id=self.device_id,
                sessions=self.sessions,
                session_state=self.session_state,
                screen_record=self.args.screen_record,
                configs=configs,
            )
            def job():
                handle_daily_story_likes_from_file(self, device, source, storage)

            job()
