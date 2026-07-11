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


def estimate_session(config: dict[str, Any], *, post_reel: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return session timing estimate and config warnings for dashboard display."""
    post_reel = post_reel or {}
    warnings: list[dict[str, str]] = []
    job_breakdown: list[dict[str, Any]] = []

    speed_mid = parse_range(config.get("speed-multiplier"), 1.0)[2]
    prof_lo, prof_hi = _profile_visit_seconds(config)
    ic_lo, ic_hi, ic_mid = parse_range(config.get("interactions-count"), 35)
    trunc_lo, trunc_hi, trunc_mid = parse_range(config.get("truncate-sources"), 3)
    interact_pct_lo, interact_pct_hi, interact_pct_mid = parse_range(
        config.get("interact-percentage"), 35
    )

    expected_profiles_lo = 0.0
    expected_profiles_hi = 0.0

    # Reels (runs first each session — always 1 post when queue is enabled)
    reel_lo, reel_hi, reel_mid = parse_range(config.get("post-reels"), 0)
    if reel_mid <= 0:
        reel_mid = float(post_reel.get("posts-per-session") or 0)
        reel_lo = reel_hi = reel_mid
    if reel_mid > 0:
        reel_sec_lo = reel_sec_hi = SECONDS_PER_REEL_POST
        job_breakdown.append(
            {
                "job": "post-reels",
                "label": "Post 1 reel",
                "minutes": _format_range(reel_sec_lo / 60, reel_sec_hi / 60),
            }
        )

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
        story_sec_lo, story_sec_hi = 18, 35
        job_breakdown.append(
            {
                "job": "daily-story-likes",
                "label": f"Daily story likes ({int(sl_lo) if sl_lo == sl_hi else f'{int(sl_lo)}–{int(sl_hi)}'} account(s))",
                "minutes": _format_range(sl_lo * story_sec_lo / 60, sl_hi * story_sec_hi / 60),
            }
        )
        expected_profiles_lo += sl_lo * 0.5
        expected_profiles_hi += sl_hi

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
        job_breakdown.append(
            {
                "job": key,
                "label": f"{key.replace('-', ' ')} ({len(sources)} source(s))",
                "minutes": _format_range(p_lo * prof_lo / 60, p_hi * prof_hi / 60),
            }
        )

    # Unfollow / remove
    unfollow_sec_lo = unfollow_sec_hi = 0.0
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
        job_breakdown.append(
            {
                "job": key,
                "label": label,
                "minutes": _format_range(lo * per_sec / 60, hi * per_sec / 60),
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

    likes_pct = parse_range(config.get("likes-percentage"), 100)[2] / 100
    likes_per_prof = parse_range(config.get("likes-count"), 1)[2]

    # Apply caps to profile visits
    profiles_lo = min(expected_profiles_lo, cap_success_lo, cap_total_lo)
    profiles_hi = min(expected_profiles_hi, cap_success_hi, cap_total_hi)

    likes_lo = profiles_lo * likes_per_prof * likes_pct
    likes_hi = profiles_hi * likes_per_prof * likes_pct

    binding_limits: list[str] = []
    if expected_profiles_lo > cap_success_lo:
        binding_limits.append("total-successful-interactions-limit")
    if expected_profiles_lo > cap_total_lo:
        binding_limits.append("total-interactions-limit")
    if likes_lo > cap_likes_lo and config.get("end-if-likes-limit-reached", True):
        binding_limits.append("total-likes-limit")

    if not binding_limits and expected_profiles_lo > 0:
        binding_label = "job volume (under session caps)"
    elif binding_limits:
        binding_label = binding_limits[0].replace("total-", "").replace("-limit", "")
    else:
        binding_label = "startup / idle"

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
        "job_breakdown": job_breakdown,
        "schedule": {
            "label": schedule_label,
            "repeat_minutes": repeat_mid if has_repeat else None,
            "total_sessions": total_sessions,
            "working_hours_per_day": round(wh_minutes / 60, 1),
        },
        "warnings": warnings,
    }
