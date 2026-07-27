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
EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)

# Fixed ending for every auto-generated reel caption.
REQUIRED_CAPTION_HASHTAGS = "#nashvillewedding #nashvilleweddingvideographer"

DEFAULT_PROMPT_615 = (
    "Write one Instagram Reel caption sentence for 615FILMS (Nashville wedding video). "
    "Sound like a real person — casual, specific, not polished or AI. "
    "Exactly ONE sentence. No emojis. No quotation marks. No hashtags. "
    "Return only that sentence."
)
DEFAULT_PROMPT_YLF = (
    "Write one Instagram Reel caption sentence for YourLoveFilms (couples / love stories). "
    "Sound like a real person — casual, specific, not polished or AI. "
    "Exactly ONE sentence. No emojis. No quotation marks. No hashtags. "
    "Return only that sentence."
)


def limit_hashtags(text: str, max_count: int = MAX_HASHTAGS) -> str:
    """Trim extra hashtags from the end of a caption (Instagram allows 5 max)."""
    matches = list(HASHTAG_RE.finditer(text))
    if len(matches) <= max_count:
        return text.strip()
    for match in reversed(matches[max_count:]):
        text = text[: match.start()] + text[match.end() :]
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _strip_emojis(text: str) -> str:
    return EMOJI_RE.sub("", text)


def normalize_reel_caption(text: str) -> str:
    """One human sentence + fixed Nashville hashtags; no emojis."""
    cleaned = _strip_emojis(text or "")
    cleaned = cleaned.replace('"', "").replace("“", "").replace("”", "")
    # Drop any model-generated hashtags; we append the required ones.
    cleaned = HASHTAG_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    # Keep only the first sentence.
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    sentence = (parts[0] if parts else cleaned).strip()
    if sentence and sentence[-1] not in ".!?":
        sentence = sentence.rstrip(" .,;:") + "."
    sentence = sentence.strip()
    if not sentence:
        sentence = "A little piece of their day."
    return f"{sentence} {REQUIRED_CAPTION_HASHTAGS}".strip()


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


def _media_queue_fingerprint(files: list[Path]) -> str:
    return "|".join(p.name for p in files)


def sync_media_queue_state(account_id: str, files: list[Path]) -> int:
    """Reset the rotation counter when the on-disk video queue changes.

  When a fresh batch of pool videos is uploaded the filenames change, so we
  start again at the first file instead of resuming an old counter from a
  previous single-reel run.
    """
    state = load_post_reel_state(account_id)
    fingerprint = _media_queue_fingerprint(files)
    if state.get("media_fingerprint") != fingerprint:
        state["media_selection_counter"] = 1
        state["media_fingerprint"] = fingerprint
        save_post_reel_state(account_id, state)
        logger.info(
            "post_media queue changed for %s — starting from the first video.",
            account_id,
        )
    return int(state.get("media_selection_counter") or 1)


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
                    "You write short Instagram Reel captions that sound human, "
                    "never AI. Exactly one sentence. No emojis. No hashtags. "
                    "No quotation marks. Return only the sentence."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=120,
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("OpenAI returned empty caption")
    return normalize_reel_caption(text)


DEFAULT_POST_RETRY_ATTEMPTS = 3  # total tries per post number (1 initial + retries)


def _restart_instagram_for_post_retry(device) -> bool:
    """Force-close Instagram and reopen so a failed reel can start clean."""
    from GramAddict.core.utils import close_instagram, open_instagram, random_sleep

    try:
        close_instagram(device)
    except Exception as exc:
        logger.warning("Could not close Instagram before reel retry: %s", exc)
    random_sleep(2, 4, modulable=False)
    try:
        ok = open_instagram(device)
    except Exception as exc:
        logger.warning("Could not reopen Instagram before reel retry: %s", exc)
        return False
    return bool(ok)


def run_post_reel_session(
    device,
    serial: str,
    account_id: str,
    *,
    posts_count: Optional[int] = None,
) -> dict[str, Any]:
    """Post N reels; increment counter only after each confirmed success.

    On failure for a given post number, close/reopen Instagram and retry that
    same queue slot from the beginning (never advances the counter until the
    post is confirmed). If Share already went through, detect the pending-upload
    banner and count it as success so the same reel is not double-posted.
    """
    from GramAddict.core.post_reel import (
        list_local_media,
        looks_like_reel_uploaded,
        run_single_reel_post,
        wait_for_uploads_to_finish,
    )
    from GramAddict.core.utils import random_sleep

    settings = get_account_post_reel(account_id)
    count = posts_count if posts_count is not None else int(settings.get("posts-per-session") or 1)
    count = max(1, count)
    try:
        max_attempts = int(settings.get("retry-attempts") or DEFAULT_POST_RETRY_ATTEMPTS)
    except (TypeError, ValueError):
        max_attempts = DEFAULT_POST_RETRY_ATTEMPTS
    max_attempts = max(1, max_attempts)

    media_dir = media_dir_for_account(account_id)
    files = list_local_media(media_dir)
    if not files:
        return {"success": False, "message": f"No videos in {POST_MEDIA_DIRNAME}/", "posted": 0}

    count = min(count, len(files))
    sync_media_queue_state(account_id, files)
    counter = get_media_selection_number(account_id)
    if counter > len(files):
        return {
            "success": True,
            "message": f"All {len(files)} reel(s) in queue already posted",
            "posted": 0,
            "skipped": True,
        }

    remaining = len(files) - (counter - 1)
    count = min(count, remaining)
    if count <= 0:
        return {
            "success": True,
            "message": "No reels left in queue",
            "posted": 0,
            "skipped": True,
        }

    clear_each = bool(settings.get("clear-gallery-before-each", True))
    # Multi-reel runs must clear the device gallery before each push — otherwise
    # gallery_select numbering drifts and the wrong file gets posted from post 2+.
    if count > 1:
        clear_each = True

    posted = 0
    results: list[dict[str, Any]] = []

    for _ in range(count):
        sync_media_queue_state(account_id, files)
        counter = get_media_selection_number(account_id)
        media_index = (counter - 1) % len(files)
        gallery_select = 1 if clear_each else counter
        media_name = files[media_index].name

        try:
            caption = generate_caption(account_id)
        except Exception as exc:
            return {
                "success": False,
                "message": f"Caption generation failed: {exc}",
                "posted": posted,
                "results": results,
            }

        last_result: dict[str, Any] = {
            "success": False,
            "message": "Reel posting did not run",
            "steps": [],
        }
        succeeded = False

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Posting reel #%s (%s) for %s — attempt %s/%s",
                counter,
                media_name,
                account_id,
                attempt,
                max_attempts,
            )
            try:
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
            except Exception as exc:
                # Never let a reel-posting error (e.g. an adb timeout while clearing
                # the gallery) crash the whole bot — fail this attempt, then retry
                # the same post number after a clean app restart.
                logger.error(
                    "Reel post #%s attempt %s/%s error for %s: %s",
                    counter,
                    attempt,
                    max_attempts,
                    account_id,
                    exc,
                )
                result = {
                    "success": False,
                    "message": f"Reel posting error: {exc}",
                    "steps": [],
                    "media_file": media_name,
                }

            last_result = dict(result)
            last_result["attempt"] = attempt
            last_result["post_number"] = counter
            results.append(last_result)

            if result.get("success"):
                succeeded = True
                break

            steps = list(result.get("steps") or [])
            share_tapped = "share" in steps
            # Share may have worked even if composer confirmation timed out —
            # check for the pending-upload banner before retrying the same file.
            if share_tapped:
                try:
                    if looks_like_reel_uploaded(device):
                        logger.info(
                            "Post #%s (%s) looks already uploading after Share — "
                            "counting as success (skip retry to avoid double-post).",
                            counter,
                            media_name,
                        )
                        last_result["success"] = True
                        last_result["message"] = (
                            f"Reel posted ({media_name}) — confirmed via upload banner"
                        )
                        last_result["confirmed_via"] = "upload_pending"
                        succeeded = True
                        break
                except Exception as exc:
                    logger.debug("Upload-banner check failed: %s", exc)

            if attempt >= max_attempts:
                break

            logger.warning(
                "Post #%s (%s) failed (%s) — closing Instagram and retrying "
                "the same post number from the start (%s/%s).",
                counter,
                media_name,
                result.get("message", "unknown"),
                attempt + 1,
                max_attempts,
            )
            if not _restart_instagram_for_post_retry(device):
                logger.error(
                    "Could not restart Instagram for post #%s retry — aborting this reel.",
                    counter,
                )
                break
            # After a Share-tapped ambiguity, check again on a fresh Home.
            if share_tapped:
                try:
                    if looks_like_reel_uploaded(device):
                        logger.info(
                            "Post #%s already uploading after app restart — "
                            "counting as success (no double-post).",
                            counter,
                        )
                        last_result["success"] = True
                        last_result["message"] = (
                            f"Reel posted ({media_name}) — confirmed via upload banner after restart"
                        )
                        last_result["confirmed_via"] = "upload_pending_after_restart"
                        succeeded = True
                        break
                except Exception:
                    pass
            random_sleep(2, 4, modulable=False)

        if not succeeded:
            return {
                "success": False,
                "message": last_result.get("message", f"Post #{counter} failed"),
                "posted": posted,
                "results": results,
                "failed_post_number": counter,
            }

        # Only advance the queue after this specific post number succeeded.
        increment_media_counter(account_id)
        posted += 1
        if posted < count:
            # Land back on Home and let the previous upload settle before the
            # next create tap — otherwise IG often stays on the composer/upload
            # sheet and gallery thumbnails never appear.
            try:
                from GramAddict.core.post_reel import tap_home_tab

                tap_home_tab(device)
            except Exception:
                pass
            random_sleep(4, 7, modulable=False)
            try:
                device.deviceV2.press("back")
                random_sleep(0.8, 1.4, modulable=False)
                tap_home_tab(device)
            except Exception:
                pass
            random_sleep(2, 4, modulable=False)

    # All reels submitted — land on Home and wait for Instagram to finish the
    # background uploads before the session moves on (closing too early can drop
    # a post). Non-fatal: a timeout here still counts the reels as posted.
    if posted > 0:
        try:
            wait_for_uploads_to_finish(device)
        except Exception as exc:
            logger.warning("Upload-completion wait skipped for %s: %s", account_id, exc)

    return {
        "success": True,
        "message": f"Posted {posted} reel(s)",
        "posted": posted,
        "results": results,
    }
