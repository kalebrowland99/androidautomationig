import logging

from colorama import Style

from GramAddict.core.plugin_loader import Plugin
from GramAddict.core.utils import random_sleep

logger = logging.getLogger(__name__)


class PostReels(Plugin):
    """Post Reels from post_media/ via ADB before other session jobs."""

    def __init__(self):
        super().__init__()
        self.description = (
            "Post Instagram Reels from the account post_media/ folder. "
            "Configure OpenAI captions in post_reel.yml. Posts 1 reel per session."
        )
        self.arguments = [
            {
                "arg": "--post-reels",
                "nargs": None,
                "help": "reels queue size — posts 1 reel per session from post_media/ (uses post_reel.yml)",
                "metavar": "3",
                "default": None,
                "operation": True,
            },
        ]

    def run(self, device, configs, storage, sessions, profile_filter, plugin):
        from GramAddict.core.post_reel_account import run_post_reel_session

        username = configs.args.username
        if not username:
            logger.error("username is required for post-reels")
            return

        count = configs.args.post_reels
        try:
            count = int(count)
        except (TypeError, ValueError):
            logger.error("post-reels must be a number in config.yml")
            return

        if count < 1:
            return

        serial = configs.device_id or device.device_id
        logger.info(
            f"Posting 1 reel for @{username} (queue size {count}; 1 per session)",
            extra={"color": f"{Style.BRIGHT}"},
        )
        result = run_post_reel_session(device, serial, username, posts_count=1)
        if result.get("success"):
            logger.info(
                result.get("message", "Reels posted"),
                extra={"color": f"{Style.BRIGHT}{Style.RESET_ALL}"},
            )
        else:
            logger.error(result.get("message", "Reel posting failed"))
        random_sleep(2, 4, modulable=False)
