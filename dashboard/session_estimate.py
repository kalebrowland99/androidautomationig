"""Heuristic session-duration estimates for GramAddict dashboard configs.

GramAddict terminology (see docs.gramaddict.org):
  - **Session**: one START → jobs → FINISH cycle (stored in sessions.json).
  - **Repeat** + **total-sessions**: schedule further sessions after a break.
  - Session length is capped by total-*-limit fields and per-source interactions-count.
"""

from __future__ import annotations

import re
from typing import Any

from dashboard.gramaddict_field_help import GRAMADDICT_TERMINOLOGY

# Fixed overhead per session (seconds)
STARTUP_SECONDS = 90
TEARDOWN_SECONDS = 25
COUNTDOWN_SECONDS = 10

# Rough per-action times (seconds)
SECONDS_PER_FEED_LIKE = 35
SECONDS_PER_UNFOLLOW = 50
SECONDS_PER_REMOVE = 55
SECONDS_PER_POST_URL = 40
SECONDS_PER_REEL_POST = 240

# AI follow-vision overhead PER profile visited (2 screenshots + 4 profile
# swipes + one OpenAI Vision request). The high end assumes a slow API round-trip.
VISION_SECONDS_LO = 9.0
VISION_SECONDS_HI = 20.0
# Share of visited profiles that pass the vision filter (the rest are skipped, so
# more profiles must be visited to reach the successful-interactions cap).
VISION_PASS_RATIO = 0.4
# Posting one comment (open composer, type, submit).
SECONDS_PER_COMMENT = 14.0
# Extra time to generate one comment via OpenAI (only when ai-comment-enabled).
SECONDS_PER_AI_COMMENT_GEN = 5.0

HASHTAG_JOBS = {
    "hashtag-likers-top",
    "hashtag-likers-recent",
    "hashtag-posts-top",
    "hashtag-posts-recent",
}

BLOGGER_JOBS = {
    "blogger",
    "blogger-followers",
    "blogger-following",
    "blogger-post-likers",
}


def _lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def parse_range(value: Any, default: float = 0.0) -> tuple[float, float, float]:
    """Return (min, max, midpoint) for '10', '10-15', or numeric values."""
    if value is None or value == "":
        return default, default, default
    if isinstance(value, (int, float)):
        v = float(value)
        return v, v, v
    text = str(value).strip()
    if not text:
        return default, default, default
    if "-" in text and not text.startswith("-"):
        parts = text.split("-", 1)
        try:
            lo = float(parts[0])
            hi = float(parts[1])
            return lo, hi, (lo + hi) / 2
        except ValueError:
            pass
    try:
        v = float(text)
        return v, v, v
    except ValueError:
        return default, default, default


def _enabled_job_count(value: Any) -> float:
    """Non-zero job config → enabled; return midpoint of range or line count."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, list):
        return float(len(value)) if value else 0.0
    lines = _lines(value)
    if lines:
        return float(len(lines))
    lo, hi, mid = parse_range(value)
    return mid if mid > 0 else 0.0


def _avg_random_sleep(speed: float) -> float:
    return max(0.3, 1.75 / max(speed, 0.1))


def _profile_visit_seconds(config: dict[str, Any]) -> tuple[float, float]:
    """Estimate seconds for one profile visit (min, max)."""
    lo_speed = parse_range(config.get("speed-multiplier"), 1.0)[0]
    hi_speed = parse_range(config.get("speed-multiplier"), 1.0)[1]
    speed_lo = max(hi_speed, 0.1)
    speed_hi = max(lo_speed, 0.1)

    def nav(speed: float) -> float:
        return 12 * _avg_random_sleep(speed)

    sp_lo, sp_hi, sp_mid = parse_range(config.get("stories-percentage"), 0)
    sc_lo, sc_hi, _ = parse_range(config.get("stories-count"), 0)
    stories_lo = (sp_lo / 100) * sc_lo * 5.25
    stories_hi = (sp_hi / 100) * sc_hi * 5.25

    lk_lo, lk_hi, _ = parse_range(config.get("likes-count"), 1)
    wp_lo, wp_hi, _ = parse_range(config.get("watch-photo-time"), 3.5)
    wv_lo, wv_hi, _ = parse_range(config.get("watch-video-time"), 20.0)

    post_lo = lk_lo * (0.65 * wp_lo + 0.35 * wv_lo)
    post_hi = lk_hi * (0.65 * wp_hi + 0.35 * wv_hi)
    post_nav_lo = lk_lo * _avg_random_sleep(speed_lo)
    post_nav_hi = lk_hi * _avg_random_sleep(speed_hi)

    lo = nav(speed_lo) + stories_lo + post_lo + post_nav_lo + 8
    hi = nav(speed_hi) + stories_hi + post_hi + post_nav_hi + 8
    return lo, hi


def _working_hours_minutes(value: Any) -> float:
    """Total allowed minutes per day from working-hours list."""
    if not value:
        return 24 * 60
    items = value if isinstance(value, list) else str(value).split(",")
    total = 0.0
    for item in items:
        text = str(item).strip()
        if not text or "-" not in text:
            continue
        start, end = text.split("-", 1)
        try:
            sh, sm = _parse_clock(start.strip())
            eh, em = _parse_clock(end.strip())
            total += max(0, (eh * 60 + em) - (sh * 60 + sm))
        except ValueError:
            continue
    return total if total > 0 else 24 * 60


def _parse_clock(text: str) -> tuple[int, int]:
    text = text.replace(".", ":")
    if ":" in text:
        h, m = text.split(":", 1)
        return int(h), int(m)
    if len(text) == 4 and text.isdigit():
        return int(text[:2]), int(text[2:])
    raise ValueError(text)


def _format_minutes(minutes: float) -> str:
    if minutes < 1:
        return "<1 min"
    if minutes < 60:
        return f"{round(minutes)} min"
    hours = int(minutes // 60)
    mins = round(minutes % 60)
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def _format_range(lo_min: float, hi_min: float) -> str:
    if abs(lo_min - hi_min) < 2:
        return _format_minutes((lo_min + hi_min) / 2)
    return f"{_format_minutes(lo_min)} – {_format_minutes(hi_min)}"


def estimate_session(
    config: dict[str, Any],
    *,
    post_reel: dict[str, Any] | None = None,
    follow_vision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return session timing estimate and config warnings for dashboard display."""
    post_reel = post_reel or {}
    follow_vision = follow_vision or {}
    warnings: list[dict[str, str]] = []
    job_breakdown: list[dict[str, Any]] = []

    speed_mid = parse_range(config.get("speed-multiplier"), 1.0)[2]
    prof_lo, prof_hi = _profile_visit_seconds(config)

    # AI vision runs on every profile the interaction jobs visit: it adds capture +
    # OpenAI-request time per profile and skips a share of them, so the account has
    # to visit more profiles to reach the successful-interactions cap.
    vision_enabled = bool(follow_vision.get("enabled"))
    # AI comments are independent of vision screening: GramAddict uses an AI-generated
    # comment whenever `ai-comment-enabled` is set, even if the vision filter is off.
    ai_comment_enabled = bool(follow_vision.get("ai-comment-enabled"))
    vision_pass_ratio = VISION_PASS_RATIO if vision_enabled else 1.0
    if vision_enabled:
        prof_lo += VISION_SECONDS_LO
        prof_hi += VISION_SECONDS_HI
    # NOTE: comment time (incl. AI generation) is added per-comment below, capped by
    # total-comments-limit — not spread across every profile.
    ic_lo, ic_hi, ic_mid = parse_range(config.get("interactions-count"), 35)
    trunc_lo, trunc_hi, trunc_mid = parse_range(config.get("truncate-sources"), 3)
    interact_pct_lo, interact_pct_hi, interact_pct_mid = parse_range(
        config.get("interact-percentage"), 35
    )

    expected_profiles_lo = 0.0
    expected_profiles_hi = 0.0
    # Profile-visit jobs that AI vision actually screens (targets/bloggers/hashtags),
    # excluding daily story likes which don't run the vision filter.
    vision_profiles_hi = 0.0
    # Story likes are counted separately (they don't go through the profile flow).
    story_likes_lo = 0.0
    story_likes_hi = 0.0

    # Reels: when post-reels >= 1 in config.yml, publish up to posts-per-session
    # from post_reel.yml back-to-back at session start (capped by queue size).
    reel_lo, reel_hi, reel_mid = parse_range(config.get("post-reels"), 0)
    per_session = parse_range((post_reel or {}).get("posts-per-session"), 1)[2]
    reels_this_session = 0
    if reel_mid >= 1:
        reels_this_session = int(min(reel_hi, per_session, reel_mid))
        reel_lo = reel_hi = float(reels_this_session)
    if reels_this_session:
        reel_sec_lo = reel_sec_hi = SECONDS_PER_REEL_POST
        job_breakdown.append(
            {
                "job": "post-reels",
                "label": f"Post {reels_this_session} reel(s)",
                "minutes": _format_range(reel_sec_lo / 60, reel_sec_hi / 60),
            }
        )
    else:
        reel_lo = reel_hi = 0.0

    # Feed likes
    feed_lo, feed_hi, feed_mid = parse_range(config.get("feed"), 0)
    if feed_mid > 0:
        job_breakdown.append(
            {
                "job": "feed",
                "label": f"Feed likes ({int(feed_lo) if feed_lo == feed_hi else f'{int(feed_lo)}–{int(feed_hi)}'})",
                "minutes": _format_range(feed_lo * SECONDS_PER_FEED_LIKE / 60, feed_hi * SECONDS_PER_FEED_LIKE / 60),
            }
        )

    # Post URLs
    post_urls = _lines(config.get("posts-from-file-list"))
    if not post_urls:
        post_urls = _lines(config.get("post-urls"))
    if post_urls:
        sec = len(post_urls) * SECONDS_PER_POST_URL
        job_breakdown.append(
            {
                "job": "posts-from-file",
                "label": f"Like {len(post_urls)} post link(s)",
                "minutes": _format_minutes(sec / 60),
            }
        )

    # Target username list
    iul_lo, iul_hi, iul_mid = parse_range(
        config.get("interact-from-file-limit") or config.get("interact-usernames-limit"), 0
    )
    if iul_mid > 0:
        expected_profiles_lo += iul_lo
        expected_profiles_hi += iul_hi
        vision_profiles_hi += iul_hi
        job_breakdown.append(
            {
                "job": "interact-from-file",
                "label": f"Visit {int(iul_lo) if iul_lo == iul_hi else f'{int(iul_lo)}–{int(iul_hi)}'} target username(s)",
                "minutes": _format_range(iul_lo * prof_lo / 60, iul_hi * prof_hi / 60),
            }
        )

    # Daily story likes
    story_enabled = bool(config.get("daily-story-likes-enabled"))
    sl_lo, sl_hi, sl_mid = parse_range(config.get("daily-story-likes-limit"), 0)
    if not sl_mid and story_enabled:
        story_list = _lines(config.get("daily-story-likes-list"))
        if story_list:
            sl_lo = sl_hi = sl_mid = float(len(story_list))
    if story_enabled and sl_mid > 0:
        story_sec_lo, story_sec_hi = 6, 12
        job_breakdown.append(
            {
                "job": "daily-story-likes",
                "label": f"Daily story likes ({int(sl_lo) if sl_lo == sl_hi else f'{int(sl_lo)}–{int(sl_hi)}'} account(s))",
                "minutes": _format_range(sl_lo * story_sec_lo / 60, sl_hi * story_sec_hi / 60),
            }
        )
        expected_profiles_lo += sl_lo * 0.5
        expected_profiles_hi += sl_hi
        story_likes_lo = sl_lo
        story_likes_hi = sl_hi

    # Blogger / hashtag jobs
    for key in sorted(BLOGGER_JOBS | HASHTAG_JOBS):
        sources = _lines(config.get(key))
        if not sources:
            continue
        src_lo = min(len(sources), trunc_lo)
        src_hi = min(len(sources), trunc_hi)
        pct_lo = interact_pct_lo / 100 if key in HASHTAG_JOBS else 1.0
        pct_hi = interact_pct_hi / 100 if key in HASHTAG_JOBS else 1.0
        p_lo = src_lo * ic_lo * pct_lo
        p_hi = src_hi * ic_hi * pct_hi
        expected_profiles_lo += p_lo
        expected_profiles_hi += p_hi
        vision_profiles_hi += p_hi
        job_breakdown.append(
            {
                "job": key,
                "label": f"{key.replace('-', ' ')} ({len(sources)} source(s))",
                "minutes": _format_range(p_lo * prof_lo / 60, p_hi * prof_hi / 60),
            }
        )

    # Unfollow / remove
    unfollow_sec_lo = unfollow_sec_hi = 0.0
    unfollow_count_lo = unfollow_count_hi = 0.0
    for key, label, per_sec in (
        ("unfollow-from-list", "Unfollow from list", SECONDS_PER_UNFOLLOW),
        ("unfollow", "Unfollow", SECONDS_PER_UNFOLLOW),
        ("unfollow-any", "Unfollow any", SECONDS_PER_UNFOLLOW),
        ("unfollow-non-followers", "Unfollow non-followers", SECONDS_PER_UNFOLLOW),
        ("unfollow-any-non-followers", "Unfollow any non-followers", SECONDS_PER_UNFOLLOW),
        ("unfollow-any-followers", "Unfollow any followers", SECONDS_PER_UNFOLLOW),
        ("remove-followers-from-list", "Remove followers from list", SECONDS_PER_REMOVE),
    ):
        lo, hi, mid = parse_range(config.get(key), 0)
        if mid <= 0:
            continue
        unfollow_sec_lo += lo * per_sec
        unfollow_sec_hi += hi * per_sec
        unfollow_count_lo += lo
        unfollow_count_hi += hi
        job_breakdown.append(
            {
                "job": key,
                "label": label,
                "minutes": _format_range(lo * per_sec / 60, hi * per_sec / 60),
            }
        )
    # Unfollows are capped per session by total-unfollows-limit.
    if unfollow_count_hi > 0:
        uc_lo, uc_hi, _ = parse_range(config.get("total-unfollows-limit"), 0)
        if uc_lo > 0:
            unfollow_count_lo = min(unfollow_count_lo, uc_lo)
        if uc_hi > 0:
            unfollow_count_hi = min(unfollow_count_hi, uc_hi)

    # AI vision applies to profile-visit jobs (targets / bloggers / hashtags).
    has_profile_jobs = vision_profiles_hi > 0
    if vision_enabled and has_profile_jobs:
        pass_pct = round(vision_pass_ratio * 100)
        vision_note = f"AI vision on: adds ~{int(VISION_SECONDS_LO)}–{int(VISION_SECONDS_HI)}s per profile"
        if ai_comment_enabled:
            vision_note += " + AI comments"
        job_breakdown.append(
            {
                "job": "follow-vision",
                "label": f"AI vision screening (~{pass_pct}% pass)",
                "minutes": vision_note,
            }
        )
        warnings.append(
            {
                "level": "info",
                "message": (
                    f"AI vision is enabled — each profile is screenshotted and sent to "
                    f"OpenAI before interacting (~{int(VISION_SECONDS_LO)}–{int(VISION_SECONDS_HI)}s each). "
                    f"Roughly {pass_pct}% pass, so the bot visits more profiles (and makes "
                    f"more API calls) to hit your successful-interactions limit."
                ),
            }
        )
    elif vision_enabled and not has_profile_jobs:
        warnings.append(
            {
                "level": "info",
                "message": (
                    "AI vision is enabled but no profile-visit jobs (targets, bloggers, "
                    "hashtags) are configured, so it won't screen anyone this session."
                ),
            }
        )

    if not job_breakdown:
        warnings.append(
            {
                "level": "info",
                "message": "No jobs enabled — the bot will start a session but do little work.",
            }
        )

    # Session caps (GramAddict picks one random value from each range at session start)
    cap_success_lo, cap_success_hi, cap_success_mid = parse_range(
        config.get("total-successful-interactions-limit"), 120
    )
    cap_total_lo, cap_total_hi, cap_total_mid = parse_range(
        config.get("total-interactions-limit"), 280
    )
    cap_likes_lo, cap_likes_hi, cap_likes_mid = parse_range(config.get("total-likes-limit"), 135)
    cap_follows_lo, cap_follows_hi, _ = parse_range(config.get("total-follows-limit"), 0)
    cap_comments_lo, cap_comments_hi, _ = parse_range(config.get("total-comments-limit"), 0)
    cap_pm_lo, cap_pm_hi, _ = parse_range(config.get("total-pm-limit"), 0)

    likes_pct = parse_range(config.get("likes-percentage"), 100)[2] / 100
    likes_per_prof = parse_range(config.get("likes-count"), 1)[2]
    follow_pct = parse_range(config.get("follow-percentage"), 0)[2] / 100
    comment_pct = parse_range(config.get("comment-percentage"), 0)[2] / 100
    pm_pct = parse_range(config.get("private-messages-percentage") or config.get("pm-percentage"), 0)[2] / 100
    stories_pct = parse_range(config.get("stories-percentage"), 0)[2] / 100
    max_comments_per_user = parse_range(config.get("max-comments-pro-user"), 1)[2] or 1

    # How many of each action happen per successful (vision-passing) profile.
    likes_rate = likes_per_prof * likes_pct
    comments_rate = min(comment_pct, max_comments_per_user) if comment_pct > 0 else 0.0

    def _flag(key: str, default: bool) -> bool:
        val = config.get(key, default)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    # Which per-action limits actually END the session (vs. just capping the count).
    end_if_likes = _flag("end-if-likes-limit-reached", True)
    end_if_follows = _flag("end-if-follows-limit-reached", False)
    end_if_comments = _flag("end-if-comments-limit-reached", False)
    end_if_pm = _flag("end-if-pm-limit-reached", False)

    # Job volume expressed as successful (vision-passing) interactions.
    job_success_lo = expected_profiles_lo * vision_pass_ratio
    job_success_hi = expected_profiles_hi * vision_pass_ratio

    # Everything that can END the session, expressed as a ceiling on successful
    # interactions. total-successful and total-interactions always end it; the
    # per-action limits only end it when their end-if-*-limit-reached flag is on.
    # e.g. with follow-percentage 100 + end-if-follows-limit-reached true, the
    # session stops after `total-follows-limit` follows — which also bounds how
    # many likes/comments can happen, since they run on the same profiles.
    success_caps: list[tuple[str, float, float]] = [
        ("total-successful-interactions-limit", cap_success_lo, cap_success_hi),
        (
            "total-interactions-limit",
            cap_total_lo * vision_pass_ratio,
            cap_total_hi * vision_pass_ratio,
        ),
    ]
    if end_if_likes and likes_rate > 0 and cap_likes_hi > 0:
        success_caps.append(
            ("total-likes-limit", cap_likes_lo / likes_rate, cap_likes_hi / likes_rate)
        )
    if end_if_follows and follow_pct > 0 and cap_follows_hi > 0:
        success_caps.append(
            ("total-follows-limit", cap_follows_lo / follow_pct, cap_follows_hi / follow_pct)
        )
    if end_if_comments and comments_rate > 0 and cap_comments_hi > 0:
        success_caps.append(
            (
                "total-comments-limit",
                cap_comments_lo / comments_rate,
                cap_comments_hi / comments_rate,
            )
        )
    if end_if_pm and pm_pct > 0 and cap_pm_hi > 0:
        success_caps.append(("total-pm-limit", cap_pm_lo / pm_pct, cap_pm_hi / pm_pct))

    # Successful interactions = the smallest ceiling (or job volume, if under caps).
    candidates = success_caps + [
        ("job volume (under session caps)", job_success_lo, job_success_hi)
    ]
    successful_lo = min(c[1] for c in candidates)
    successful_hi = min(c[2] for c in candidates)
    binder = min(candidates, key=lambda c: c[1])
    binding_limits = [
        c[0] for c in success_caps if c[1] <= successful_lo + 1e-9
    ]

    if binder[0] == "job volume (under session caps)":
        binding_label = (
            "job volume (under session caps)" if job_success_hi > 0 else "startup / idle"
        )
    else:
        binding_label = binder[0].replace("total-", "").replace("-limit", "")

    # Profiles VISITED = successful / pass-ratio (vision skips the rest).
    profiles_lo = successful_lo / vision_pass_ratio if vision_pass_ratio else successful_lo
    profiles_hi = successful_hi / vision_pass_ratio if vision_pass_ratio else successful_hi

    likes_lo = successful_lo * likes_rate
    likes_hi = successful_hi * likes_rate

    # Per-action outcome estimates (what actually happens), each honoring its
    # percentage setting and its own session cap. Only surfaced when applicable.
    def _capped(lo: float, hi: float, cap_key: str) -> tuple[float, float]:
        c_lo, c_hi, _ = parse_range(config.get(cap_key), 0)
        if c_lo > 0:
            lo = min(lo, c_lo)
        if c_hi > 0:
            hi = min(hi, c_hi)
        return lo, hi

    action_estimates: list[dict[str, Any]] = []

    likes_capped_lo, likes_capped_hi = _capped(likes_lo, likes_hi, "total-likes-limit")
    if likes_capped_hi >= 1:
        action_estimates.append(
            {"action": "likes", "label": "Likes", "low": round(likes_capped_lo), "high": round(likes_capped_hi)}
        )

    if follow_pct > 0:
        fol_lo, fol_hi = successful_lo * follow_pct, successful_hi * follow_pct
        fol_lo, fol_hi = _capped(fol_lo, fol_hi, "total-follows-limit")
        if fol_hi >= 1:
            action_estimates.append(
                {"action": "follows", "label": "Accounts followed", "low": round(fol_lo), "high": round(fol_hi)}
            )

    if story_likes_hi >= 1:
        action_estimates.append(
            {
                "action": "story_likes",
                "label": "Story likes",
                "low": round(story_likes_lo),
                "high": round(story_likes_hi),
            }
        )
    elif stories_pct > 0:
        # Stories watched during profile visits (not the daily-story-likes job).
        st_lo, st_hi = successful_lo * stories_pct, successful_hi * stories_pct
        if st_hi >= 1:
            action_estimates.append(
                {"action": "stories", "label": "Stories watched", "low": round(st_lo), "high": round(st_hi)}
            )

    comment_sec_lo = comment_sec_hi = 0.0
    if comment_pct > 0:
        com_lo, com_hi = successful_lo * comments_rate, successful_hi * comments_rate
        com_lo, com_hi = _capped(com_lo, com_hi, "total-comments-limit")
        if com_hi >= 1:
            per_comment = SECONDS_PER_COMMENT + (
                SECONDS_PER_AI_COMMENT_GEN if ai_comment_enabled else 0.0
            )
            comment_sec_lo = com_lo * per_comment
            comment_sec_hi = com_hi * per_comment
            action_estimates.append(
                {
                    "action": "comments",
                    "label": "AI comments" if ai_comment_enabled else "Comments",
                    "low": round(com_lo),
                    "high": round(com_hi),
                    "ai_generated": ai_comment_enabled,
                }
            )

    if pm_pct > 0:
        pm_lo, pm_hi = successful_lo * pm_pct, successful_hi * pm_pct
        pm_lo, pm_hi = _capped(pm_lo, pm_hi, "total-pm-limit")
        if pm_hi >= 1:
            action_estimates.append(
                {"action": "pms", "label": "DMs sent", "low": round(pm_lo), "high": round(pm_hi)}
            )

    if unfollow_count_hi >= 1:
        action_estimates.append(
            {
                "action": "unfollows",
                "label": "Unfollows",
                "low": round(unfollow_count_lo),
                "high": round(unfollow_count_hi),
            }
        )

    # Total actions per session = sum of every real action (likes, follows,
    # unfollows, comments, DMs, story likes). Stories *watched* are passive and
    # excluded. Instagram-safety folklore counts follows+unfollows+likes toward a
    # combined daily cap, so this total helps gauge how sessions/day stack up.
    total_actions_lo = 0.0
    total_actions_hi = 0.0
    for a in action_estimates:
        if a["action"] == "stories":
            continue
        total_actions_lo += a["low"]
        total_actions_hi += a["high"]

    interact_sec_lo = profiles_lo * prof_lo
    interact_sec_hi = profiles_hi * prof_hi

    feed_sec_lo = feed_lo * SECONDS_PER_FEED_LIKE
    feed_sec_hi = feed_hi * SECONDS_PER_FEED_LIKE
    post_url_sec = len(post_urls) * SECONDS_PER_POST_URL
    reel_sec_lo = reel_lo * SECONDS_PER_REEL_POST
    reel_sec_hi = reel_hi * SECONDS_PER_REEL_POST

    session_lo = (
        STARTUP_SECONDS
        + COUNTDOWN_SECONDS
        + reel_sec_lo
        + feed_sec_lo
        + post_url_sec
        + interact_sec_lo
        + comment_sec_lo
        + unfollow_sec_lo
        + TEARDOWN_SECONDS
    )
    session_hi = (
        STARTUP_SECONDS
        + COUNTDOWN_SECONDS
        + reel_sec_hi
        + feed_sec_hi
        + post_url_sec
        + interact_sec_hi
        + comment_sec_hi
        + unfollow_sec_hi
        + TEARDOWN_SECONDS
    )

    session_mid_min = (session_lo + session_hi) / 2 / 60

    # Scheduling (repeat + total-sessions)
    repeat_lo, repeat_hi, repeat_mid = parse_range(config.get("repeat"), 0)
    ts_raw = config.get("total-sessions")
    try:
        total_sessions = int(str(ts_raw).strip()) if ts_raw not in (None, "") else 1
    except ValueError:
        total_sessions = 1
    infinite_sessions = total_sessions == -1
    has_repeat = repeat_mid > 0

    if has_repeat and infinite_sessions:
        schedule_label = "Repeats forever (until you press Stop)"
        warnings.append(
            {
                "level": "warn",
                "message": "repeat is set and total-sessions is -1 — the bot keeps scheduling sessions until you stop it.",
            }
        )
    elif has_repeat and total_sessions > 1:
        gap = _format_range(repeat_lo, repeat_hi)
        sessions_label = f"{total_sessions} sessions"
        bot_lo = session_lo / 60 * total_sessions + repeat_lo * (total_sessions - 1)
        bot_hi = session_hi / 60 * total_sessions + repeat_hi * (total_sessions - 1)
        schedule_label = f"{sessions_label}, {gap} apart → {_format_range(bot_lo, bot_hi)} total"
    elif has_repeat:
        schedule_label = f"1 session, then waits {_format_range(repeat_lo, repeat_hi)} (set total-sessions > 1 to repeat)"
    else:
        schedule_label = "1 session then stops (set repeat to schedule more)"

    if not has_repeat and total_sessions not in (1, -1, None):
        warnings.append(
            {
                "level": "info",
                "message": "total-sessions only applies when repeat is set — otherwise the bot runs once.",
            }
        )

    wh_minutes = _working_hours_minutes(config.get("working-hours"))
    if wh_minutes < 24 * 60:
        wh_hours = round(wh_minutes / 60, 1)
        if session_mid_min > wh_minutes:
            warnings.append(
                {
                    "level": "warn",
                    "message": f"Estimated session (~{_format_minutes(session_mid_min)}) may exceed today's working-hours window ({wh_hours}h allowed).",
                }
            )

    if session_mid_min > 180:
        warnings.append(
            {
                "level": "warn",
                "message": f"Long session estimate (~{_format_minutes(session_mid_min)}) — consider lowering interactions-count or session limits.",
            }
        )

    if speed_mid > 2:
        warnings.append(
            {
                "level": "info",
                "message": "speed-multiplier above 2 — faster taps can increase detection risk and UI misses.",
            }
        )

    crashes_mid = parse_range(config.get("total-crashes-limit"), 5)[2]
    source_count = sum(len(_lines(config.get(k))) for k in BLOGGER_JOBS | HASHTAG_JOBS)
    source_count += len(_lines(config.get("interact-from-file-list")))
    source_count += len(_lines(config.get("daily-story-likes-list"))) if config.get("daily-story-likes-enabled") else 0
    source_count += len(_lines(config.get("interact-usernames")))
    if crashes_mid >= 3 and source_count > 5:
        warnings.append(
            {
                "level": "info",
                "message": "Many sources with crash recovery enabled — a bad run may retry and extend session time.",
            }
        )

    if expected_profiles_hi > 80:
        warnings.append(
            {
                "level": "info",
                "message": "High profile volume configured — session caps will likely cut the run short (good safety).",
            }
        )

    return {
        "terminology": GRAMADDICT_TERMINOLOGY,
        "session_minutes": {
            "low": round(session_lo / 60, 1),
            "high": round(session_hi / 60, 1),
            "label": _format_range(session_lo / 60, session_hi / 60),
        },
        "binding_limit": binding_label,
        "binding_limits": binding_limits,
        "expected_profiles": {
            "low": round(profiles_lo),
            "high": round(profiles_hi),
        },
        "successful_interactions": {
            "low": round(successful_lo),
            "high": round(successful_hi),
        },
        "action_estimates": action_estimates,
        "total_actions": {
            "low": round(total_actions_lo),
            "high": round(total_actions_hi),
        },
        "ai_vision": {
            "enabled": vision_enabled,
            "screens_profiles": vision_enabled and has_profile_jobs,
            "pass_ratio": round(vision_pass_ratio, 2),
            "ai_comments": ai_comment_enabled,
        },
        "job_breakdown": job_breakdown,
        "schedule": {
            "label": schedule_label,
            "repeat_minutes": repeat_mid if has_repeat else None,
            "total_sessions": total_sessions,
            "working_hours_per_day": round(wh_minutes / 60, 1),
        },
        "warnings": warnings,
    }
