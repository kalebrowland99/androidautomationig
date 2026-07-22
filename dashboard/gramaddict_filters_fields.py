"""filters.yml field definitions for the dashboard UI."""

from __future__ import annotations

from typing import Any

FILTER_FIELDS: dict[str, list[dict[str, Any]]] = {
    "profile_type": [
        {"key": "skip_if_private", "label": "Skip private accounts", "type": "bool"},
        {"key": "skip_if_public", "label": "Skip public accounts", "type": "bool"},
        {"key": "skip_business", "label": "Skip business accounts", "type": "bool"},
        {"key": "skip_non_business", "label": "Skip personal accounts", "type": "bool"},
        {"key": "skip_following", "label": "Skip people you already follow", "type": "bool"},
        {"key": "skip_follower", "label": "Skip people who follow you", "type": "bool"},
        {"key": "skip_if_link_in_bio", "label": "Skip if bio has a link", "type": "bool"},
        {"key": "follow_private_or_empty", "label": "Only follow private or empty-bio accounts", "type": "bool"},
    ],
    "profile_stats_options": [
        {"key": "ignore_following_count", "label": "Ignore following count", "type": "bool"},
        {"key": "ignore_potency", "label": "Ignore follower ratio (potency)", "type": "bool"},
    ],
    "profile_stats": [
        {"key": "min_followers", "label": "Minimum followers", "type": "text", "default": "50"},
        {"key": "max_followers", "label": "Maximum followers", "type": "text", "default": "2500"},
        {"key": "min_followings", "label": "Minimum following count", "type": "text", "default": "50"},
        {"key": "max_followings", "label": "Maximum following count", "type": "text", "default": "2500"},
        {"key": "min_potency_ratio", "label": "Minimum follower ratio", "type": "text", "default": "0.5"},
        {"key": "max_potency_ratio", "label": "Maximum follower ratio", "type": "text", "default": "5"},
        {"key": "min_posts", "label": "Minimum posts on profile", "type": "text", "default": "3"},
        {"key": "mutual_friends", "label": "Minimum mutual followers", "type": "text", "default": "-1"},
    ],
    "biography": [
        {"key": "blacklist_words", "label": "Skip if bio, name, or @handle has these words", "type": "lines"},
        {"key": "blacklist_words_bio", "label": "Skip if bio has these words", "type": "lines"},
        {"key": "blacklist_words_name", "label": "Skip if display name has these words", "type": "lines"},
        {"key": "blacklist_words_handle", "label": "Skip if @handle has these words", "type": "lines"},
        {"key": "skip_story_like", "label": "Skip story likes if @handle or name contains", "type": "lines"},
        {"key": "mandatory_words", "label": "Only if bio contains these words", "type": "lines"},
        {"key": "specific_alphabet", "label": "Allowed alphabets", "type": "lines"},
        {"key": "biography_language", "label": "Allowed bio languages", "type": "lines"},
        {"key": "biography_banned_language", "label": "Blocked bio languages", "type": "lines"},
    ],
    "comment_filters": [
        {"key": "comment_hashtag_likers_top", "label": "Allow comments: top hashtag likers", "type": "bool"},
        {"key": "comment_hashtag_likers_recent", "label": "Allow comments: recent hashtag likers", "type": "bool"},
        {"key": "comment_hashtag_posts_top", "label": "Allow comments: top hashtag posts", "type": "bool"},
        {"key": "comment_hashtag_posts_recent", "label": "Allow comments: recent hashtag posts", "type": "bool"},
        {"key": "comment_blogger_followers", "label": "Allow comments: someone’s followers", "type": "bool"},
        {"key": "comment_blogger_following", "label": "Allow comments: who someone follows", "type": "bool"},
        {"key": "comment_blogger_post_likers", "label": "Allow comments: post likers", "type": "bool"},
        {"key": "comment_blogger", "label": "Allow comments: direct profile visits", "type": "bool"},
        {"key": "comment_interact_usernames", "label": "Allow comments: username lists", "type": "bool"},
        {"key": "comment_interact_from_file", "label": "Allow comments: list files", "type": "bool"},
        {"key": "comment_feed", "label": "Allow comments: home feed", "type": "bool"},
        {"key": "comment_photos", "label": "Allow comments on photos", "type": "bool"},
        {"key": "comment_videos", "label": "Allow comments on videos", "type": "bool"},
        {"key": "comment_carousels", "label": "Allow comments on carousels", "type": "bool"},
    ],
    "pm_filters": [
        {"key": "pm_to_private_or_empty", "label": "Only DM private or empty-bio accounts", "type": "bool"},
    ],
    "post_likers": [
        {"key": "min_likers", "label": "Minimum likes on the post", "type": "text", "default": "1"},
        {"key": "max_likers", "label": "Maximum likes on the post", "type": "text", "default": "1000"},
    ],
}

FILTER_SECTION_LABELS = {
    "profile_type": "What kind of account",
    "profile_stats_options": "Quick skips",
    "profile_stats": "Follower & post counts",
    "biography": "Bio & name rules",
    "comment_filters": "Where comments are allowed",
    "pm_filters": "DM rules",
    "post_likers": "Post popularity",
}

TELEGRAM_FIELDS: list[dict[str, Any]] = [
    {"key": "telegram-api-token", "label": "Bot token", "type": "text", "placeholder": "From @BotFather on Telegram"},
    {"key": "telegram-chat-id", "label": "Your chat ID", "type": "text", "placeholder": "From @myidbot on Telegram"},
    {
        "key": "telegram-status-commands",
        "label": "Allow Telegram status commands",
        "type": "bool",
    },
    {
        "key": "telegram-ai-assistant",
        "label": "AI assistant — answer free-form questions in Telegram",
        "type": "bool",
    },
    {
        "key": "telegram-alerts",
        "label": "Telegram alerts on blocks and errors",
        "type": "bool",
    },
]

ACCOUNT_LIST_FILES = {
    "whitelist.txt": "Always allow these usernames",
    "blacklist.txt": "Never interact with these usernames",
    "story_likes.txt": "Daily story likes — accounts to check for new stories",
    "unfollow_list.txt": "Usernames to unfollow",
    "remove_list.txt": "Followers to remove",
}

ACCOUNT_TEXT_FILES = {
    "comments_list.txt": "Comment templates",
    "pm_list.txt": "Private message templates",
}

# Every file bundled with a new account and where it is edited in the dashboard.
ACCOUNT_BUNDLE: list[dict[str, str]] = [
    {
        "name": "config.yml",
        "label": "Main settings",
        "tab": "basics",
        "description": "Jobs, limits, schedule, and behavior for this Instagram account.",
    },
    {
        "name": "filters.yml",
        "label": "Profile filters",
        "tab": "filters",
        "description": "Who to skip based on followers, bio, business account, and more.",
    },
    {
        "name": "story_likes.txt",
        "label": "Daily story likes list",
        "tab": "lists",
        "description": "Accounts to visit daily — like all segments in any new story.",
    },
    {
        "name": "whitelist.txt",
        "label": "Whitelist",
        "tab": "lists",
        "description": "Usernames to always allow.",
    },
    {
        "name": "blacklist.txt",
        "label": "Blacklist",
        "tab": "lists",
        "description": "Usernames to always skip.",
    },
    {
        "name": "unfollow_list.txt",
        "label": "Unfollow list",
        "tab": "lists",
        "description": "Usernames to unfollow in debug and with unfollow-from-file.",
    },
    {
        "name": "remove_list.txt",
        "label": "Remove list",
        "tab": "lists",
        "description": "Followers to remove in debug and with remove-followers-from-file.",
    },
    {
        "name": "comments_list.txt",
        "label": "Comment templates",
        "tab": "comments",
        "description": "Random comments by post type (photo, video, carousel).",
    },
    {
        "name": "pm_list.txt",
        "label": "DM templates",
        "tab": "comments",
        "description": "Random direct messages the bot can send.",
    },
    {
        "name": "telegram.yml",
        "label": "Telegram login",
        "tab": "reports",
        "description": "Bot token and chat ID for session summaries.",
    },
    {
        "name": "post_reel.yml",
        "label": "Reel posting settings",
        "tab": "posting",
        "description": "Posts per session, OpenAI key, prompt batch.",
    },
    {
        "name": "post_reel_prompts.yml",
        "label": "Reel caption prompts",
        "tab": "posting",
        "description": "615FILMS and YourLoveFilms OpenAI prompt templates.",
    },
    {
        "name": "follow_vision.yml",
        "label": "Follow vision settings",
        "tab": "posting",
        "description": "Enable OpenAI vision profile filter before interacting.",
    },
    {
        "name": "follow_vision_prompts.yml",
        "label": "Follow vision prompts",
        "tab": "posting",
        "description": "615FILMS and YourLoveFilms vision prompt templates.",
    },
]

TEMPLATE_ACCOUNT_FILES = [item["name"] for item in ACCOUNT_BUNDLE]
