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
            "Configure OpenAI captions in post_reel.yml. "
            "posts-per-session in post_reel.yml controls how many reels to publish per run."
        )
        self.arguments = [
            {
                "arg": "--post-reels",
                "nargs": None,
                "help": "reels queue size in post_media/ (must be >= 1 to enable; "
                "posts-per-session in post_reel.yml sets how many to post per run)",
                "metavar": "6",
                "default": None,
                "operation": True,
            },
        ]

    def run(self, device, configs, storage, sessions, profile_filter, plugin):
        from GramAddict.core.post_reel import list_local_media
        from GramAddict.core.post_reel_account import (
            get_account_post_reel,
            media_dir_for_account,
            run_post_reel_session,
        )

        username = configs.args.username
        if not username:
            logger.error("username is required for post-reels")
            return

        from GramAddict.core.account_safety import is_autopost_locked

        if is_autopost_locked(username):
            logger.warning(
                "post-reels is disabled for @%s (autopost safety lock).",
                username.lstrip("@"),
            )
            return

        from GramAddict.core.brand_pool import (
            pool_posting_enabled,
            resolve_brand_pool,
        )

        pool_id = resolve_brand_pool(
            config=getattr(configs, "config", None),
            ig_username=username,
        )
        if pool_id and not pool_posting_enabled(pool_id):
            logger.info(
                "Reel posting is disabled for brand pool '%s' — skipping post-reels for @%s.",
                pool_id,
                username.lstrip("@"),
            )
            return

        queue_size = configs.args.post_reels
        try:
            queue_size = int(queue_size)
        except (TypeError, ValueError):
            logger.error("post-reels must be a number in config.yml")
            return

        if queue_size < 1:
            return

        settings = get_account_post_reel(username)
        per_session = max(1, int(settings.get("posts-per-session") or 1))
        media_dir = media_dir_for_account(username)
        files = list_local_media(media_dir)
        if not files:
            logger.error("No videos in %s/ — upload reels before running.", media_dir.name)
            return

        # post-reels in config.yml = queue size; posts-per-session in post_reel.yml
        # = how many to publish back-to-back this run (capped by what's on disk).
        posts_count = min(per_session, len(files), queue_size)
        if posts_count < per_session:
            logger.info(
                "Posting %s reel(s) this session (%s configured in post_reel.yml, "
                "%s file(s) in post_media/, queue %s in config.yml).",
                posts_count,
                per_session,
                len(files),
                queue_size,
            )

        serial = configs.device_id or device.device_id
        logger.info(
            f"Posting {posts_count} reel(s) for @{username}",
            extra={"color": f"{Style.BRIGHT}"},
        )
        result = run_post_reel_session(device, serial, username, posts_count=posts_count)
        if result.get("skipped"):
            logger.info(result.get("message", "Reel queue already posted"))
        elif result.get("success"):
            logger.info(
                result.get("message", "Reels posted"),
                extra={"color": f"{Style.BRIGHT}{Style.RESET_ALL}"},
            )
        else:
            logger.error(result.get("message", "Reel posting failed"))
        random_sleep(2, 4, modulable=False)
