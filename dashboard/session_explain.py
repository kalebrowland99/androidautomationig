"""AI-generated plain-language explanation of what a session's settings will do.

Feeds the deterministic session estimate plus the account's key settings into
OpenAI and returns a short, human-readable summary of what the bot will actually
do when it runs. Falls back to a rule-based summary when no OpenAI key is set so
the panel is never empty.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dashboard.session_estimate import estimate_session

logger = logging.getLogger(__name__)

# Session-cap / safety settings worth surfacing to the model even when they are
# not tied to a specific job in the estimate breakdown.
_LIMIT_KEYS = (
    "total-likes-limit",
    "total-follows-limit",
    "total-interactions-limit",
    "total-successful-interactions-limit",
    "total-watches-limit",
    "total-comments-limit",
    "total-scraped-limit",
    "likes-count",
    "likes-percentage",
    "follow-percentage",
    "comment-percentage",
    "interact-percentage",
    "interactions-count",
    "watch-video-time",
    "working-hours",
    "speed-multiplier",
)


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _build_summary(config: dict[str, Any], estimate: dict[str, Any]) -> dict[str, Any]:
    """Compact, model-friendly view of what is enabled and the timing estimate."""
    jobs = [
        {"job": j.get("job"), "label": j.get("label"), "time": j.get("minutes")}
        for j in estimate.get("job_breakdown", [])
    ]

    limits: dict[str, str] = {}
    for key in _LIMIT_KEYS:
        val = _clean(config.get(key))
        if val:
            limits[key] = val

    schedule = estimate.get("schedule", {}) or {}
    warnings = [
        w.get("message")
        for w in estimate.get("warnings", [])
        if w.get("level") in ("warn", "info") and w.get("message")
    ]

    actions = {
        a.get("action"): {"label": a.get("label"), "low": a.get("low"), "high": a.get("high")}
        for a in estimate.get("action_estimates", [])
        if a.get("action")
    }

    total_actions = estimate.get("total_actions") or {}
    # Rough daily projection when the account repeats sessions.
    total_sessions = schedule.get("total_sessions")
    daily_actions = None
    if isinstance(total_sessions, int) and total_sessions > 1 and total_actions.get("high"):
        daily_actions = {
            "low": total_actions.get("low", 0) * total_sessions,
            "high": total_actions.get("high", 0) * total_sessions,
            "sessions": total_sessions,
        }

    return {
        "enabled_jobs": jobs,
        "session_length": (estimate.get("session_minutes") or {}).get("label"),
        "expected_profiles_visited": estimate.get("expected_profiles"),
        "successful_interactions": estimate.get("successful_interactions"),
        "expected_actions_per_session": actions,
        "total_actions_per_session": total_actions,
        "projected_actions_per_day": daily_actions,
        "ai_vision": estimate.get("ai_vision"),
        "stops_at": estimate.get("binding_limit"),
        "schedule": {
            "summary": schedule.get("label"),
            "total_sessions": schedule.get("total_sessions"),
            "repeat_minutes": schedule.get("repeat_minutes"),
            "working_hours_per_day": schedule.get("working_hours_per_day"),
        },
        "limits": limits,
        "warnings": warnings,
    }


def _fallback_explanation(summary: dict[str, Any]) -> str:
    jobs = summary.get("enabled_jobs") or []
    if not jobs:
        return (
            "No jobs are enabled, so the bot will start a session but do almost "
            "nothing. Enable a job (story likes, targets, hashtags, etc.) to give "
            "it work to do."
        )
    lines = ["Each session your bot will:"]
    for j in jobs:
        label = j.get("label") or j.get("job")
        if label:
            lines.append(f"- {label}")
    vision = summary.get("ai_vision") or {}
    if vision.get("screens_profiles"):
        pct = round(float(vision.get("pass_ratio") or 0) * 100)
        note = f"\nAI vision is screening every profile (~{pct}% pass"
        if vision.get("ai_comments"):
            note += ", AI writes the comments"
        note += "), which adds time per profile and extra OpenAI calls."
        lines.append(note)
    success = summary.get("successful_interactions") or {}
    if success.get("high"):
        lines.append(
            f"Expect ~{success.get('low')}–{success.get('high')} successful interactions."
        )
    actions = summary.get("expected_actions_per_session") or {}
    if actions:
        outcome = ", ".join(
            f"{v.get('low')}–{v.get('high')} {(v.get('label') or k).lower()}"
            for k, v in actions.items()
        )
        lines.append(f"Roughly per session: {outcome}.")
    total = summary.get("total_actions_per_session") or {}
    if total.get("high"):
        lines.append(
            f"Total actions per session: ~{total.get('low')}–{total.get('high')}."
        )
    daily = summary.get("projected_actions_per_day")
    if daily:
        lines.append(
            f"Across {daily['sessions']} sessions/day that's ~{daily['low']}–{daily['high']} actions/day."
        )
    if summary.get("session_length"):
        lines.append(f"Estimated length: {summary['session_length']}.")
    if summary.get("stops_at"):
        lines.append(f"It will stop when it hits: {summary['stops_at']}.")
    sched = (summary.get("schedule") or {}).get("summary")
    if sched:
        lines.append(f"Schedule: {sched}.")
    return "\n".join(lines)


def _openai_credentials(account_id: str) -> tuple[str, str]:
    try:
        from GramAddict.core.post_reel_account import get_account_post_reel

        settings = get_account_post_reel(account_id)
    except Exception:  # noqa: BLE001 - missing settings is non-fatal
        return "", "gpt-4o"
    api_key = _clean(settings.get("openai-api-key"))
    model = _clean(settings.get("openai-model")) or "gpt-4o"
    return api_key, model


_SYSTEM_PROMPT = (
    "You explain an Instagram automation bot's session settings to its owner in "
    "plain English. You are given a JSON summary of the jobs that are enabled, the "
    "per-session limits, the estimated session length, and the repeat schedule. "
    "Write a short, confident explanation of exactly what the bot will do when it "
    "runs, so the user doesn't have to interpret raw numbers.\n\n"
    "Rules:\n"
    "- Address the user as 'you'/'your bot'.\n"
    "- Start with one sentence summarizing the run at a high level.\n"
    "- Then a short bulleted list of what happens each session, in the order the "
    "jobs run, with the real numbers.\n"
    "- Always state the expected per-session outcomes from expected_actions_per_session "
    "that apply (e.g. likes, accounts followed, story likes, comments, DMs) with their "
    "number ranges. Only mention the ones present in that object.\n"
    "- Always give the TOTAL actions per session from total_actions_per_session as an "
    "explicit line (e.g. 'Total: ~X–Y actions per session'). If "
    "projected_actions_per_day is present, also state the projected actions per day.\n"
    "- If ai_vision.screens_profiles is true, explain that every profile is "
    "screenshotted and checked by OpenAI Vision before interacting, that only "
    "~pass_ratio of profiles pass (so it visits more profiles and makes more API "
    "calls), and mention AI-written comments if ai_comments is true. Distinguish "
    "'profiles visited' from 'successful interactions'.\n"
    "- End with one line about when it stops and how often it repeats.\n"
    "- If any warnings are present, add a brief 'Heads up:' note.\n"
    "- Be concrete and specific. No fluff, no disclaimers, no markdown headings.\n"
    "- Keep it under ~120 words."
)


def explain_session(
    config: dict[str, Any],
    *,
    post_reel: dict[str, Any] | None = None,
    follow_vision: dict[str, Any] | None = None,
    account_id: str = "",
) -> dict[str, Any]:
    """Return an AI (or fallback) explanation of what the session settings do."""
    post_reel = post_reel or {}
    follow_vision = follow_vision or {}
    estimate = estimate_session(
        config, post_reel=post_reel, follow_vision=follow_vision
    )
    summary = _build_summary(config, estimate)

    api_key, model = _openai_credentials(account_id)
    if not api_key:
        return {
            "explanation": _fallback_explanation(summary),
            "source": "fallback",
            "model": None,
            "message": "Add an OpenAI API key in Posting settings for an AI explanation.",
        }

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Explain what my bot will do with these settings:\n"
                        + json.dumps(summary, indent=2)
                    ),
                },
            ],
            max_tokens=350,
            temperature=0.4,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("OpenAI returned an empty explanation")
        return {"explanation": text, "source": "ai", "model": model}
    except Exception as exc:  # noqa: BLE001 - surface a usable fallback
        logger.warning("Session AI explanation failed: %s", exc)
        return {
            "explanation": _fallback_explanation(summary),
            "source": "fallback",
            "model": None,
            "message": f"AI explanation unavailable ({exc}).",
        }
