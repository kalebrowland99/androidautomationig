"""Account-level Reel posting settings, state, and OpenAI captions."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

ACCOUNTS = Path("accounts")
POST_REEL_FILENAME = "post_reel.yml"
POST_REEL_PROMPTS_FILENAME = "post_reel_prompts.yml"
POST_REEL_STATE_FILENAME = "post_reel_state.json"
POST_MEDIA_DIRNAME = "post_media"
MAX_HASHTAGS = 5
HASHTAG_RE = re.compile(r"#\w+")

DEFAULT_PROMPT_615 = (
    "Write an Instagram Reel caption for 615FILMS. "
    "Tone: cinematic, professional, wedding/film production brand. "
    "Include exactly 5 relevant hashtags at the end (Instagram max is 5). "
    "Keep under 2200 characters. Return only the caption text."
)
DEFAULT_PROMPT_YLF = (
    "Write an Instagram Reel caption for YourLoveFilms. "
    "Tone: romantic, warm, couples and love stories. "
    "Include exactly 5 relevant hashtags at the end (Instagram max is 5). "
    "Keep under 2200 characters. Return only the caption text."
)


def limit_hashtags(text: str, max_count: int = MAX_HASHTAGS) -> str:
    """Trim extra hashtags from the end of a caption (Instagram allows 5 max)."""
    matches = list(HASHTAG_RE.finditer(text))
    if len(matches) <= max_count:
        return text.strip()
    for match in reversed(matches[max_count:]):
        text = text[: match.start()] + text[match.end() :]
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _account_dir(account_id: str) -> Path:
    folder = ACCOUNTS / account_id
    if not folder.is_dir():
        raise FileNotFoundError(f"Account not found: {account_id}")
    return folder


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _save_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle, default_flow_style=False, sort_keys=False, allow_unicode=True)


def media_dir_for_account(account_id: str) -> Path:
    path = _account_dir(account_id) / POST_MEDIA_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_post_reel_yml() -> dict[str, Any]:
    return {
        "posts-per-session": 1,
        "prompt-batch": "615FILMS",
        "openai-api-key": "",
        "openai-model": "gpt-4o",
        "clear-gallery-before-each": True,
    }


def default_post_reel_prompts_yml() -> dict[str, str]:
    return {
        "615FILMS": DEFAULT_PROMPT_615,
        "YourLoveFilms": DEFAULT_PROMPT_YLF,
    }


def get_account_post_reel(account_id: str) -> dict[str, Any]:
    path = _account_dir(account_id) / POST_REEL_FILENAME
    data = _load_yaml(path) if path.is_file() else default_post_reel_yml()
    return data if isinstance(data, dict) else default_post_reel_yml()


def get_account_post_reel_prompts(account_id: str) -> dict[str, str]:
    path = _account_dir(account_id) / POST_REEL_PROMPTS_FILENAME
    if not path.is_file():
        return default_post_reel_prompts_yml()
    data = _load_yaml(path)
    if not isinstance(data, dict):
        return default_post_reel_prompts_yml()
    defaults = default_post_reel_prompts_yml()
    for key in defaults:
        data.setdefault(key, defaults[key])
    return {str(k): str(v) for k, v in data.items()}


def load_post_reel_state(account_id: str) -> dict[str, Any]:
    path = _account_dir(account_id) / POST_REEL_STATE_FILENAME
    if not path.is_file():
        return {"media_selection_counter": 1}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("media_selection_counter", 1)
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
    return {"media_selection_counter": 1}


def save_post_reel_state(account_id: str, state: dict[str, Any]) -> dict[str, Any]:
    path = _account_dir(account_id) / POST_REEL_STATE_FILENAME
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def increment_media_counter(account_id: str) -> int:
    state = load_post_reel_state(account_id)
    counter = int(state.get("media_selection_counter") or 1)
    state["media_selection_counter"] = counter + 1
    save_post_reel_state(account_id, state)
    return counter + 1


def get_media_selection_number(account_id: str) -> int:
    return int(load_post_reel_state(account_id).get("media_selection_counter") or 1)


def generate_caption(account_id: str, *, batch: Optional[str] = None) -> str:
    settings = get_account_post_reel(account_id)
    prompts = get_account_post_reel_prompts(account_id)
    batch_name = batch or str(settings.get("prompt-batch") or "615FILMS")
    prompt = prompts.get(batch_name) or prompts.get("615FILMS") or DEFAULT_PROMPT_615
    api_key = str(settings.get("openai-api-key") or "").strip()
    model = str(settings.get("openai-model") or "gpt-4o").strip()
    if not api_key:
        raise ValueError("openai-api-key not set in post_reel.yml")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install openai: pip install openai") from exc

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You write Instagram Reel captions with hashtags. "
                    "Instagram allows a maximum of 5 hashtags per post."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=800,
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("OpenAI returned empty caption")
    trimmed = limit_hashtags(text)
    if trimmed != text:
        logger.info("Trimmed caption hashtags to Instagram max of %s", MAX_HASHTAGS)
    return trimmed


def run_post_reel_session(
    device,
    serial: str,
    account_id: str,
    *,
    posts_count: Optional[int] = None,
) -> dict[str, Any]:
    """Post N reels; increment counter only after each confirmed success."""
    from GramAddict.core.post_reel import list_local_media, run_single_reel_post
    from GramAddict.core.utils import random_sleep

    settings = get_account_post_reel(account_id)
    count = posts_count if posts_count is not None else int(settings.get("posts-per-session") or 1)
    count = max(1, count)
    media_dir = media_dir_for_account(account_id)
    files = list_local_media(media_dir)
    if not files:
        return {"success": False, "message": f"No videos in {POST_MEDIA_DIRNAME}/", "posted": 0}

    clear_each = bool(settings.get("clear-gallery-before-each", True))
    posted = 0
    results: list[dict[str, Any]] = []

    for _ in range(count):
        counter = get_media_selection_number(account_id)
        media_index = (counter - 1) % len(files)
        gallery_select = 1 if clear_each else counter

        try:
            caption = generate_caption(account_id)
        except Exception as exc:
            return {
                "success": False,
                "message": f"Caption generation failed: {exc}",
                "posted": posted,
                "results": results,
            }

        result = run_single_reel_post(
            device,
            serial,
            media_dir=media_dir,
            media_index=media_index,
            gallery_select_number=gallery_select,
            caption=caption,
            clear_gallery=clear_each,
            paste_caption=True,
        )
        results.append(result)
        if not result.get("success"):
            return {
                "success": False,
                "message": result.get("message", "Post failed"),
                "posted": posted,
                "results": results,
            }
        increment_media_counter(account_id)
        posted += 1
        if posted < count:
            random_sleep(3, 6, modulable=False)

    return {
        "success": True,
        "message": f"Posted {posted} reel(s)",
        "posted": posted,
        "results": results,
    }
