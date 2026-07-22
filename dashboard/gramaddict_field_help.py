"""Plain-language help for dashboard settings (based on GramAddict docs)."""

from __future__ import annotations

CONFIG_HELP: dict[str, str] = {
    "username": "The Instagram account logged in on the phone. Same as your @ handle, without the @.",
    "brand-pool": "Share follow/like/comment history with other accounts in this brand. Same-brand accounts won't interact with the same people twice. Target bloggers (post likers, followers, etc.) can still overlap.",
    "device": "Only needed with multiple phones plugged in. Leave blank to use the phone you picked on Farm.",
    "app-id": "Leave the default unless you use a special copy of Instagram. Advanced users only.",
    "use-cloned-app": "Turn on if Instagram is a duplicate app, not the normal one from the app store.",
    "allow-untested-ig-version": "Allow running when your Instagram app is newer than what GramAddict tested. May be less stable.",
    "ensure-vpn": "Open your VPN app and connect before Instagram launches.",
    "screen-sleep": "Turn the screen off during waits to save battery.",
    "screen-record": "Save a screen recording while the bot runs. Good for troubleshooting; uses storage.",
    "speed-multiplier": "1 is normal. Lower = slower and safer. Higher = faster.",
    "debug": "Show more step-by-step detail in the activity log.",
    "close-apps": "Close other apps before starting so nothing interrupts the session.",
    "kill-atx-agent": "Shut down the phone automation service when finished.",
    "restart-atx-agent": "Restart the phone automation service before each run (can fix connection glitches).",
    "disable-block-detection": "Don’t check whether Instagram blocked or limited the account.",
    "disable-filters": "Ignore filters.yml — interact with everyone, not just profiles that pass your rules.",
    "dont-type": "Paste text instead of typing character by character (helps on some phones).",
    "shuffle-jobs": "Mix up the order of jobs each session instead of always doing the same order.",
    "truncate-sources": "When you listed several hashtags or accounts under Jobs, only use some of them each session so activity varies.",
    "total-crashes-limit": "End the session after this many errors or crashes.",
    "count-app-crashes": "Treat Instagram closing unexpectedly as a crash toward the limit.",
    "feed": "How many posts to like while scrolling your home feed.",
    "daily-story-likes": "Visit every account in story_likes.txt each run (ignores interaction history). Always likes every story segment (seen or new), then moves on. Artists who pass follow vision are added to this list automatically.",
    "daily-story-likes-enabled": "Enable daily story likes. When off, usernames are kept in story_likes.txt but the bot skips this job.",
    "post-reels": "How many reel videos are in your queue (post_media/). The bot posts up to posts-per-session from post_reel.yml each run, back-to-back, before other jobs. Locked to 0 for @615films and @yourlovefilms.",
    "blogger": "Open these profiles and interact (like, follow, etc.) based on your limits.",
    "blogger-followers": "Open someone’s follower list and interact with those people — good for growing by following a competitor’s audience.",
    "blogger-following": "Open who someone follows and interact with those accounts.",
    "blogger-post-likers": "Find people who liked that account’s recent posts and interact with them.",
    "hashtag-likers-top": "Find people who liked top posts for a hashtag.",
    "hashtag-likers-recent": "Find people who liked recent posts for a hashtag.",
    "hashtag-posts-top": "Browse top posts on a hashtag and interact from there.",
    "hashtag-posts-recent": "Browse recent posts on a hashtag and interact from there.",
    "interact-from-file": "Usernames to visit directly — one per line. Saved to targets.txt automatically.",
    "interact-from-file-limit": "How many usernames from your list to process each session. Example: 10-15.",
    "posts-from-file": "Instagram post links to like — one URL per line. Saved to post_urls.txt automatically.",
    "like-from-urls": "A second list of Instagram post links to like — one URL per line. Saved to like_urls.txt automatically.",
    "watch-video-time": "How long to watch a video before liking or commenting. Use 0 to skip waiting.",
    "watch-photo-time": "How long to look at a photo before interacting.",
    "can-reinteract-after": "Don’t visit the same person again until this many hours have passed.",
    "comment-cooldown-days": "Don't leave another comment (including AI comments) on the same person until this many days have passed. Likes, follows, and story likes are not affected.",
    "delete-interacted-users": "After visiting someone from a list file, remove their name from that file.",
    "unfollow": "Unfollow up to this many people you currently follow.",
    "unfollow-any": "Unfollow up to this many accounts, regardless of whether they follow you back.",
    "unfollow-non-followers": "Unfollow people who don’t follow you back.",
    "unfollow-any-non-followers": "Similar to unfollow non-followers, with a broader selection.",
    "unfollow-any-followers": "Unfollow even people who still follow you — use carefully.",
    "unfollow-from-list": "Unfollow this many people from your unfollow list (Lists tab). Example: 1-2.",
    "sort-followers-newest-to-oldest": "Start unfollowing your most recent follows first.",
    "unfollow-delay": "Minimum days since the bot followed someone before it will unfollow them (not seconds).",
    "remove-followers-from-list": "Remove this many followers from your remove list (Lists tab). Example: 1-2.",
    "delete-removed-followers": "Remove each name from the file after they’ve been removed.",
    "interactions-count": "How many people to visit for each hashtag, account, or other target.",
    "likes-count": "How many posts to like on each person’s profile.",
    "likes-percentage": "How often to like when visiting someone (100 = always try).",
    "stories-count": "How many stories to watch per person. Use 0 to skip stories.",
    "stories-percentage": "How often to watch stories when visiting someone.",
    "carousel-count": "How many slides to swipe on multi-photo posts.",
    "carousel-percentage": "How often to swipe through carousel posts.",
    "max-comments-pro-user": "Cap comments on a single person’s posts per session.",
    "comment-percentage": "How often to leave a comment (needs templates under Comments & PM).",
    "pm-percentage": "How often to send a DM (needs templates under Comments & PM).",
    "interact-percentage": "When browsing hashtag feeds, how often to tap into a post’s author.",
    "follow-percentage": "How often to follow someone after visiting their profile.",
    "follow-limit": "Maximum follows allowed per target (per hashtag, account, etc.) in one session.",
    "skipped-list-limit": "How long to keep scrolling past people you already visited before moving on.",
    "skipped-posts-limit": "How many already-liked posts to skip in a row before moving on.",
    "skip-first-row": "Skip the entire first row of a profile grid (the usual spot for pinned posts) and start from the first post of the second row.",
    "fling-when-skipped": "Advanced scrolling behavior. Usually leave at 0.",
    "min-following": "Only visit profiles that follow at least this many accounts.",
    "total-likes-limit": "Stop liking after this many likes in one session.",
    "total-follows-limit": "Stop following after this many follows in one session.",
    "total-follows-limit-daily": "Hard cap on follows per day across all sessions. 0 turns it off. When set, each session only follows up to the day's remaining allowance, and once the daily total is hit no more sessions run until tomorrow.",
    "total-unfollows-limit": "Stop unfollowing after this many in one session.",
    "total-watches-limit": "Stop watching stories after this many in one session.",
    "total-interactions-limit": "Cap all actions combined in one session.",
    "total-successful-interactions-limit": "Cap actions that actually succeeded.",
    "total-comments-limit": "Stop commenting after this many in one session.",
    "total-pm-limit": "Stop sending DMs after this many in one session.",
    "total-scraped-limit": "When saving usernames to a file, stop after collecting this many.",
    "end-if-likes-limit-reached": "End the whole session as soon as the like cap is reached.",
    "end-if-follows-limit-reached": "End the session when the follow cap is reached.",
    "end-if-watches-limit-reached": "End the session when the story cap is reached.",
    "end-if-comments-limit-reached": "End the session when the comment cap is reached.",
    "end-if-pm-limit-reached": "End the session when the DM cap is reached.",
    "working-hours": "The bot only runs during these windows each day. Outside them it waits.",
    "time-delta": "Shift start times by a few random minutes so you don’t begin at exactly the same time every day.",
    "repeat": "Minutes to wait after a session ends before starting the next one.",
    "rate-limit-break": (
        "Legacy field (ignored). Action-limit pauses escalate automatically in a row: "
        "1–1.5h → 3h → 8h → 12h → 24h. Streak resets after a stretch of successful work."
    ),
    "total-sessions": "1 runs once. -1 keeps scheduling sessions until you press Stop.",
    "pre-script": "Optional phone script to run before the bot (advanced).",
    "post-script": "Optional phone script to run after the bot (advanced).",
    "scrape-to-file": "Save usernames the bot discovers into a file for later lists.",
    "telegram-reports": "Message you on Telegram when a session finishes. Also fill in credentials under Reports.",
}

FILTER_HELP: dict[str, str] = {
    "skip_if_private": "Skip profiles that are private.",
    "skip_if_public": "Skip profiles that are public.",
    "skip_business": "Skip business accounts.",
    "skip_non_business": "Skip personal (non-business) accounts.",
    "skip_following": "Skip people you already follow.",
    "skip_follower": "Skip people who already follow you.",
    "skip_if_link_in_bio": "Skip profiles that have a link in their bio.",
    "follow_private_or_empty": "Only follow private accounts or accounts with an empty bio.",
    "ignore_following_count": "Do not skip profiles based on how many accounts they follow.",
    "ignore_potency": "Do not skip profiles based on follower ratio (followers ÷ following).",
    "min_followers": "Skip accounts with fewer than this many followers.",
    "max_followers": "Skip accounts with more than this many followers.",
    "min_followings": "Skip accounts that follow fewer than this many people.",
    "max_followings": "Skip accounts that follow more than this many people.",
    "min_potency_ratio": "Followers divided by following must be at least this (higher = more followers vs who they follow).",
    "max_potency_ratio": "Followers divided by following must be at most this.",
    "min_posts": "Skip accounts with fewer than this many posts on their grid.",
    "mutual_friends": "Require at least this many mutual followers. Use -1 to ignore this filter.",
    "blacklist_words": "Skip the account if any of these words appears in their bio, display name, or @handle (one per line). Bio and name match whole words; @handle matches anywhere.",
    "blacklist_words_bio": "Skip only if the bio contains any of these words (whole-word match). One per line.",
    "blacklist_words_name": "Skip only if the display name contains any of these words (whole-word match). One per line.",
    "blacklist_words_handle": "Skip only if the @handle contains any of these words (matches anywhere, since handles have no spaces). One per line.",
    "skip_story_like": "Skip liking stories if the @handle or display name contains any of these words (matches inside compound usernames too, e.g. texaphotossleep / lrow.photography). One per line.",
    "mandatory_words": "Only interact if their bio or name contains at least one of these words.",
    "specific_alphabet": "Only interact if their text uses these alphabets (e.g. LATIN).",
    "biography_language": "Only interact if their bio appears to be in these languages.",
    "biography_banned_language": "Skip if their bio appears to be in these languages.",
    "comment_hashtag_likers_top": "Allow comments when the job is hashtag likers (top posts).",
    "comment_hashtag_likers_recent": "Allow comments when the job is hashtag likers (recent posts).",
    "comment_hashtag_posts_top": "Allow comments when browsing top hashtag posts.",
    "comment_hashtag_posts_recent": "Allow comments when browsing recent hashtag posts.",
    "comment_blogger_followers": "Allow comments when interacting with a blogger's followers.",
    "comment_blogger_following": "Allow comments when interacting with who a blogger follows.",
    "comment_blogger_post_likers": "Allow comments when interacting with post likers.",
    "comment_blogger": "Allow comments on direct blogger interactions.",
    "comment_interact_usernames": "Allow comments when interacting from username lists.",
    "comment_interact_from_file": "Allow comments when interacting from a file.",
    "comment_feed": "Allow comments while liking posts in the home feed.",
    "comment_photos": "Allow comments on photo posts.",
    "comment_videos": "Allow comments on video posts.",
    "comment_carousels": "Allow comments on carousel (multi-photo) posts.",
    "pm_to_private_or_empty": "Only send DMs to private accounts or accounts with an empty bio.",
    "min_likers": "When using post-likers jobs, skip posts with fewer than this many likes.",
    "max_likers": "When using post-likers jobs, skip posts with more than this many likes.",
}

TELEGRAM_HELP: dict[str, str] = {
    "telegram-api-token": "Token from @BotFather on Telegram. Creates the bot that sends you reports.",
    "telegram-chat-id": "Your chat ID from @myidbot — where the bot sends session summaries.",
    "telegram-status-commands": "Reply when you text status or update in Telegram. Requires the dashboard to be running.",
    "telegram-ai-assistant": "Ask the bot anything in Telegram (e.g. 'how's progress?', 'why did it stop?') and an AI answers using the live status and logs. Reuses the account's OpenAI key (from follow_vision.yml or post_reel.yml). Requires the dashboard to be running.",
    "telegram-alerts": "Message you immediately when Instagram blocks an action or the bot hits a fatal error.",
}

FILE_HELP: dict[str, str] = {
    "whitelist.txt": "Usernames that always pass filters — the bot will interact with them even if they would normally be skipped.",
    "blacklist.txt": "Usernames to never interact with (one per line).",
    "story_likes.txt": "Accounts to check daily for new stories — one @username per line.",
    "unfollow_list.txt": "Usernames to unfollow — used by the unfollow job and debug steps (one per line).",
    "remove_list.txt": "Followers to remove — used by the remove job and debug steps (one per line).",
    "comments_list.txt": "Comment templates grouped by post type. The bot picks one at random when it leaves a comment.",
    "pm_list.txt": "DM templates — one per line. The bot picks one at random when it sends a message.",
    "filters.yml": "Rules for which profiles to skip or allow (followers, bio words, business accounts, etc.).",
    "telegram.yml": "Telegram bot token and chat ID for session report messages.",
    "config.yml": "Main bot settings: jobs, limits, schedule, and behavior for this Instagram account.",
}

SECTION_HELP: dict[str, str] = {
    "general": "Who you are on Instagram, which phone to use, and core on/off switches.",
    "actions": "Turn on the work you want. Leave sections blank if you don’t need them.",
    "extra": "File-based jobs: visit usernames from a list, and like specific posts by URL. Edited here and saved to list files automatically.",
    "modifiers": "How long to look at posts, when someone can be visited again, and cleaning up lists after visits.",
    "unfollow": "Unfollow or remove followers using the lists on the Lists tab.",
    "source_limits": "Fine-tune what happens on each person or each hashtag/account target.",
    "session_limits": "Safety caps for the entire run — total likes, follows, comments, and so on.",
    "ending": "End the whole session as soon as a cap is hit instead of continuing other jobs.",
    "scheduling": "What times of day the bot may run and how long to wait between sessions.",
    "scripts": "Optional extras: phone scripts or saving usernames to a file.",
    "reports": "Get a Telegram message when a session finishes.",
    "profile_type": "Skip or allow profiles based on private/public, business accounts, and who you already follow.",
    "profile_stats": "Skip profiles outside your comfort zone for followers, following, and post count.",
    "profile_stats_options": "Turn off specific profile-stat rules without deleting the numbers below.",
    "biography": "Filter by words, language, or alphabet in someone’s bio or name.",
    "comment_filters": "Which jobs are allowed to leave comments.",
    "pm_filters": "Rules for sending direct messages.",
    "post_likers": "When targeting people who liked a post, skip posts with too few or too many likes.",
}

VPN_APP_HELP = (
    "Name of the VPN app on the phone home screen (for example Shadowrocket). "
    "Used when Ensure VPN is turned on for an account."
)

# GramAddict glossary shown on Account → Basics (aligned with docs.gramaddict.org)
GRAMADDICT_TERMINOLOGY: list[dict[str, str]] = [
    {
        "term": "Session",
        "definition": (
            "One full bot cycle: START → run your enabled jobs → FINISH. "
            "GramAddict logs each session in sessions.json."
        ),
    },
    {
        "term": "Job",
        "definition": (
            "One task during a session — e.g. like from a hashtag, follow from a username list, "
            "post reels, unfollow. Enable jobs on the Jobs tab."
        ),
    },
    {
        "term": "Run (total-sessions)",
        "definition": (
            "How many sessions to schedule. 1 = run once then stop. "
            "-1 = keep starting new sessions until you press Stop (requires repeat)."
        ),
    },
    {
        "term": "Repeat",
        "definition": (
            "Minutes to wait after a session ends before the next one starts. "
            "Only applies when total-sessions is greater than 1 or set to -1."
        ),
    },
    {
        "term": "Source limits",
        "definition": (
            "Caps per target (hashtag, account, place, etc.) — interactions-count, "
            "follow-limit, and related fields on the Limits tab."
        ),
    },
    {
        "term": "Session limits",
        "definition": (
            "Caps for the whole session — total likes, follows, comments, DMs, etc. "
            "When a cap is hit, GramAddict ends the session or stops that action type."
        ),
    },
    {
        "term": "Truncate sources",
        "definition": (
            "If you listed several hashtags or accounts under Jobs, only use some of them "
            "each session so targets vary (truncate-sources on Basics)."
        ),
    },
    {
        "term": "Working hours",
        "definition": (
            "Time windows when the bot may run. Outside these hours it waits until the "
            "next allowed window (Schedule tab)."
        ),
    },
    {
        "term": "Profile visit",
        "definition": (
            "Opening someone's profile to like, follow, comment, or DM. "
            "The context-bar “Profiles” estimate is how many visits your config may reach before limits stop the session."
        ),
    },
    {
        "term": "Interactions",
        "definition": (
            "Actions on profiles — likes, follows, story watches, comments, DMs. "
            "total-interactions-limit caps all of them combined per session."
        ),
    },
]

ACCOUNT_TAB_HELP: dict[str, str] = {
    "basics": "Your Instagram login, phone, VPN, and main bot switches.",
    "jobs": "What the bot should do each session. Only fill in what you need.",
    "limits": "How much the bot can do so one session doesn’t overdo it.",
    "filters": "Who to skip based on their profile (followers, bio, business account, etc.).",
    "lists": "Always-allow and never-touch username lists.",
    "comments": "Message templates for comments and DMs.",
    "schedule": "What hours the bot may run and how often it repeats.",
    "reports": "Telegram notifications when sessions complete.",
    "posting": "Reel posting: videos in post_media/, OpenAI captions, posts per session. Daily story (story_media/ + AI captions) is listed but locked until finished.",
    "files": "Every file in this account’s folder.",
    "raw": "Edit the full config file directly — for advanced users.",
}


def _append_help(base: str, extra: str) -> str:
    base = (base or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return base
    if not base:
        return extra
    if extra in base:
        return base
    return f"{base} {extra}"


def _field_format_hint(key: str, field: dict) -> str | None:
    if key in FIELD_HINTS:
        return FIELD_HINTS[key]
    if key in RANGE_FIELDS:
        return RANGE_HINT
    if key in PERCENT_FIELDS:
        return PERCENT_HINT
    if field.get("type") == "lines":
        return LINES_HINT
    return None


def enrich_fields(fields: list[dict], help_map: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for field in fields:
        item = dict(field)
        key = item.get("key", "")
        if "help" not in item and key in help_map:
            item["help"] = help_map[key]
        ftype = item.get("type")
        if ftype == "inline-file-job":
            limit_key = f"{key}-limit"
            list_key = f"{key}-list"
            if limit_key in help_map:
                item["limit_help"] = help_map[limit_key]
            if list_key in help_map:
                item["list_help"] = help_map[list_key]
            limit_hint = _field_format_hint(limit_key, {"type": "text"})
            if limit_hint:
                item["limit_help"] = _append_help(item.get("limit_help", ""), limit_hint)
            list_hint = LINES_HINT
            item["list_help"] = _append_help(item.get("list_help", ""), list_hint)
        elif ftype == "inline-lines-file":
            list_key = f"{key}-list"
            if list_key in help_map:
                item["list_help"] = help_map[list_key]
            item["list_help"] = _append_help(item.get("list_help", ""), LINES_HINT)
        format_hint = _field_format_hint(key, item)
        if format_hint:
            item["help"] = _append_help(item.get("help", ""), format_hint)
        item.pop("hint", None)
        out.append(item)
    return out


RANGE_HINT = (
    "Enter one number, or a minimum–maximum range (example: 3-6 picks a random number in that range each time). "
    "Leave blank to use the bot’s default."
)

PERCENT_HINT = "A number from 0 to 100. How often the bot does this when it visits someone’s profile."

LINES_HINT = "One item per line. Usernames should not include @."

RANGE_FIELDS = frozenset({
    "truncate-sources",
    "feed",
    "watch-video-time",
    "watch-photo-time",
    "can-reinteract-after",
    "unfollow",
    "unfollow-any",
    "unfollow-non-followers",
    "unfollow-any-non-followers",
    "unfollow-any-followers",
    "unfollow-from-list",
    "unfollow-delay",
    "remove-followers-from-list",
    "interact-from-file-limit",
    "interactions-count",
    "likes-count",
    "stories-count",
    "carousel-count",
    "max-comments-pro-user",
    "skipped-list-limit",
    "follow-limit",
    "min-following",
    "total-likes-limit",
    "total-follows-limit",
    "total-follows-limit-daily",
    "total-unfollows-limit",
    "total-watches-limit",
    "total-interactions-limit",
    "total-successful-interactions-limit",
    "total-comments-limit",
    "total-pm-limit",
    "total-scraped-limit",
    "time-delta",
    "repeat",
    "total-crashes-limit",
})

PERCENT_FIELDS = frozenset({
    "likes-percentage",
    "stories-percentage",
    "carousel-percentage",
    "comment-percentage",
    "pm-percentage",
    "interact-percentage",
    "follow-percentage",
})

FIELD_HINTS: dict[str, str] = {
    "working-hours": "Add one or more time windows. The bot only runs during these hours each day.",
    "truncate-sources": (
        "If you added several hashtags or accounts under Jobs, how many to use each session. "
        "Example: 2-5 picks between 2 and 5 at random. Enter 0 to use your full list every time."
    ),
    "total-sessions": "1 = run once then stop. -1 = keep running sessions until you press Stop.",
    "speed-multiplier": "1 is normal speed. Use 0.5 to go slower or 2 to go faster.",
    "interact-from-file-limit": "Example: 10-15 processes between 10 and 15 usernames per session.",
    "unfollow-from-list": "Example: 1-2 unfollows 1 or 2 people from your unfollow list.",
    "remove-followers-from-list": "Example: 1-2 removes 1 or 2 followers from your remove list.",
    "pre-script": "Optional script on the phone to run before the bot starts (advanced).",
    "post-script": "Optional script on the phone to run after the bot finishes (advanced).",
    "scrape-to-file": "File name where the bot saves usernames it finds (for building lists later).",
    "fling-when-skipped": "Usually leave at 0. Advanced: fast-scroll after many skipped profiles.",
    "skipped-posts-limit": "How many already-liked posts to skip in a row before moving on.",
    "skip-first-row": "Skip the entire first row of a profile grid (the usual spot for pinned posts) and start from the first post of the second row.",
    "mutual_friends": "Minimum mutual followers required. Use -1 to turn this filter off.",
    "min_potency_ratio": "Followers ÷ following must be at least this number.",
    "max_potency_ratio": "Followers ÷ following must be at most this number.",
}
