"""Account-level follow vision settings, prompts, and OpenAI profile checks."""

from __future__ import annotations

import base64
import io
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

ACCOUNTS = Path("accounts")
FOLLOW_VISION_FILENAME = "follow_vision.yml"
FOLLOW_VISION_PROMPTS_FILENAME = "follow_vision_prompts.yml"
POST_REEL_FILENAME = "post_reel.yml"
FOUND_VIDEOGRAPHERS_FILENAME = "found_videographers_tn.txt"
VIDEOGRAPHER_PHRASE = "music videographer"

DEFAULT_PROMPT_615 = (
    "You will receive two Instagram profile screenshots (top of profile, then scrolled down) "
    "and the profile biography text below. "
    "Respond with exactly ONE of these phrases:\n"
    "- potential musician — musician, artist, or rapper (not a videographer)\n"
    "- music videographer — filmmaker/videographer who shoots music videos or works with musicians "
    "(read bio/posts for videographer, music video, MV director, DP, cinematographer, filmmaker)\n"
    "- no — everyone else"
)
DEFAULT_PROMPT_YLF = (
    "You will receive two Instagram profile screenshots: the top of the profile (first image) "
    "and the same profile after scrolling partway down (second image). "
    "Is this a person who is likely in a relationship or getting married soon? "
    "Only respond with exactly one of these two phrases: potential couple or no."
)
DEFAULT_COMMENT_PROMPT = (
    "Write one very short, casual Instagram comment aimed at a musician or artist, "
    "hinting that you want to collaborate or work together soon. "
    "Keep it under 8 words, sound like a real person, lowercase is fine, "
    "and sometimes end with a fire emoji. "
    "Examples: \"let's work soon 🔥\", \"we need to link up soon\", "
    "\"need to shoot something soon 🔥\", \"lets create together soon\". "
    "Return only the comment text — no quotes, no hashtags, no extra words."
)

# Number of near-full-screen down-swipes before the second screenshot.
PROFILE_SCROLL_SWIPES = 2

PASS_PHRASES: dict[str, str] = {
    "615FILMS": "potential musician",
    "YourLoveFilms": "potential couple",
}

VISION_MODEL = "gpt-4.1-nano"


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


def resolve_account_dir(account_key: str) -> Path:
    """Resolve the account folder from username or dashboard account id."""
    key = str(account_key or "").strip()
    if not key:
        raise FileNotFoundError("Account key is empty")
    direct = ACCOUNTS / key
    if direct.is_dir():
        return direct
    lowered = key.lower()
    for folder in ACCOUNTS.iterdir():
        if not folder.is_dir():
            continue
        if folder.name.lower() == lowered:
            return folder
        config_path = folder / "config.yml"
        if not config_path.is_file():
            continue
        data = _load_yaml(config_path)
        username = str(data.get("username") or "").strip()
        if username.lower() == lowered:
            return folder
    raise FileNotFoundError(f"Account not found: {account_key}")


def default_follow_vision_yml() -> dict[str, Any]:
    return {
        "enabled": False,
        "prompt-batch": "615FILMS",
        "log-videographers": True,
        "ai-comment-enabled": False,
        "ai-comment-prompt": DEFAULT_COMMENT_PROMPT,
    }


def default_follow_vision_prompts_yml() -> dict[str, str]:
    return {
        "615FILMS": DEFAULT_PROMPT_615,
        "YourLoveFilms": DEFAULT_PROMPT_YLF,
    }


def get_account_follow_vision(account_key: str) -> dict[str, Any]:
    folder = resolve_account_dir(account_key)
    path = folder / FOLLOW_VISION_FILENAME
    data = _load_yaml(path) if path.is_file() else default_follow_vision_yml()
    defaults = default_follow_vision_yml()
    for key, value in defaults.items():
        data.setdefault(key, value)
    if "log-videographers" not in data and "log-tn-videographers" in data:
        data["log-videographers"] = bool(data["log-tn-videographers"])
    data.pop("openai-model", None)
    return data


def get_account_follow_vision_prompts(account_key: str) -> dict[str, str]:
    folder = resolve_account_dir(account_key)
    path = folder / FOLLOW_VISION_PROMPTS_FILENAME
    if not path.is_file():
        return default_follow_vision_prompts_yml()
    data = _load_yaml(path)
    if not isinstance(data, dict):
        return default_follow_vision_prompts_yml()
    defaults = default_follow_vision_prompts_yml()
    for key in defaults:
        data.setdefault(key, defaults[key])
    return {str(k): str(v) for k, v in data.items()}


def _openai_api_key(account_key: str) -> str:
    folder = resolve_account_dir(account_key)
    vision = get_account_follow_vision(account_key)
    key = str(vision.get("openai-api-key") or "").strip()
    if key:
        return key
    post_reel = _load_yaml(folder / POST_REEL_FILENAME)
    return str(post_reel.get("openai-api-key") or "").strip()


def _openai_model(_account_key: str) -> str:
    return VISION_MODEL


def _normalize_response(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def response_passes(text: str, batch_name: str) -> bool:
    normalized = _normalize_response(text)
    phrase = PASS_PHRASES.get(batch_name, "potential musician")
    if phrase in normalized:
        return True
    if normalized == "no" or normalized.endswith(" no"):
        return False
    # Strict: anything other than the pass phrase is treated as no.
    return False


def response_is_music_videographer(text: str) -> bool:
    normalized = _normalize_response(text)
    if "potential musician" in normalized:
        return False
    return normalized == VIDEOGRAPHER_PHRASE or normalized.startswith(
        f"{VIDEOGRAPHER_PHRASE} "
    )


def response_is_tn_music_videographer(text: str) -> bool:
    """Backward-compatible alias."""
    return response_is_music_videographer(text)


def _videographer_log_enabled(settings: dict[str, Any]) -> bool:
    if "log-videographers" in settings:
        return bool(settings.get("log-videographers"))
    return bool(settings.get("log-tn-videographers", True))


def _videographer_log_path(account_key: str) -> Path:
    return resolve_account_dir(account_key) / FOUND_VIDEOGRAPHERS_FILENAME


def log_found_videographer(
    account_key: str,
    username: str,
    bio: str,
    raw_response: str,
) -> None:
    """Append a music videographer lead (deduped by username)."""
    path = _videographer_log_path(account_key)
    uname = username.lstrip("@").strip()
    if not uname:
        return
    if path.is_file():
        existing = path.read_text(encoding="utf-8").lower()
        if f"@{uname.lower()}\t" in existing or f"\t@{uname.lower()}\t" in existing:
            logger.debug("Videographer @%s already in %s", uname, path.name)
            return
    bio_one_line = re.sub(r"\s+", " ", (bio or "").strip())[:300]
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}\t@{uname}\t{raw_response.strip()}\t{bio_one_line}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    logger.info(
        "Logged music videographer @%s → %s",
        uname,
        path,
    )


def log_found_tn_videographer(
    account_key: str,
    username: str,
    bio: str,
    raw_response: str,
    city: str = "",
) -> None:
    """Backward-compatible alias."""
    log_found_videographer(account_key, username, bio, raw_response)


def analyze_profile_images(
    account_key: str,
    image_bytes_list: list[bytes],
    bio_text: str = "",
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """Return (passed, raw_response). Raises on API/config errors."""
    settings = get_account_follow_vision(account_key)
    if not force and not settings.get("enabled"):
        return True, "disabled"

    if not image_bytes_list:
        raise ValueError("No profile screenshots to analyze")

    prompts = get_account_follow_vision_prompts(account_key)
    batch_name = str(settings.get("prompt-batch") or "615FILMS")
    prompt = prompts.get(batch_name) or prompts.get("615FILMS") or DEFAULT_PROMPT_615
    bio_clean = re.sub(r"\s+", " ", (bio_text or "").strip())
    if bio_clean and batch_name == "615FILMS":
        prompt = f"{prompt}\n\nProfile biography:\n{bio_clean}"
    api_key = _openai_api_key(account_key)
    if not api_key:
        raise ValueError("openai-api-key not set in follow_vision.yml or post_reel.yml")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install openai: pip install openai") from exc

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_bytes in image_bytes_list:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=_openai_model(account_key),
        messages=[{"role": "user", "content": content}],
        max_tokens=40,
    )
    raw = (response.choices[0].message.content or "").strip()
    if batch_name == "615FILMS" and response_is_music_videographer(raw):
        return False, raw
    passed = response_passes(raw, batch_name)
    return passed, raw


def analyze_profile_image(account_key: str, image_bytes: bytes) -> tuple[bool, str]:
    """Backward-compatible single-image helper."""
    return analyze_profile_images(account_key, [image_bytes])


def ai_comments_enabled(account_key: str) -> bool:
    try:
        settings = get_account_follow_vision(account_key)
    except FileNotFoundError:
        return False
    return bool(settings.get("ai-comment-enabled"))


def generate_ai_comment(account_key: str) -> str:
    """Generate a short, casual collab-style Instagram comment via OpenAI."""
    settings = get_account_follow_vision(account_key)
    prompt = str(settings.get("ai-comment-prompt") or "").strip() or DEFAULT_COMMENT_PROMPT
    api_key = _openai_api_key(account_key)
    if not api_key:
        raise ValueError("openai-api-key not set in follow_vision.yml or post_reel.yml")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install openai: pip install openai") from exc

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You write short, casual, human-sounding Instagram comments. "
                    "Return only the comment text, nothing else."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=30,
        temperature=1.0,
    )
    text = (response.choices[0].message.content or "").strip().strip('"').strip()
    if not text:
        raise RuntimeError("OpenAI returned empty comment")
    return text


def screenshot_to_jpeg_bytes(device) -> bytes:
    """Capture the current device screen as JPEG bytes."""
    image = device.screenshot()
    buf = io.BytesIO()
    if hasattr(image, "save"):
        image.save(buf, format="JPEG", quality=85)
    else:
        raise RuntimeError("Device screenshot did not return an image")
    return buf.getvalue()


def capture_profile_vision_screenshots(device) -> list[bytes]:
    """Top-of-profile shot, scroll down, second shot, then scroll back up."""
    from GramAddict.core.utils import random_sleep
    from GramAddict.core.views import Direction, UniversalActions

    actions = UniversalActions(device)
    display_height = int(device.get_info()["displayHeight"])
    # A single swipe from screen center only travels ~half the screen, so use a
    # near-full-screen swipe and repeat it to scroll further down the profile.
    per_swipe_px = max(int(display_height * 0.8), 120)

    top_shot = screenshot_to_jpeg_bytes(device)
    logger.debug("Follow vision: captured top-of-profile screenshot.")

    for _ in range(PROFILE_SCROLL_SWIPES):
        actions._swipe_points(direction=Direction.DOWN, delta_y=per_swipe_px)
        random_sleep(0.3, 0.6, modulable=False)
    random_sleep(0.3, 0.6, modulable=False)
    mid_shot = screenshot_to_jpeg_bytes(device)
    logger.debug(
        "Follow vision: captured lower-profile screenshot after %d swipe(s).",
        PROFILE_SCROLL_SWIPES,
    )

    for _ in range(PROFILE_SCROLL_SWIPES):
        actions._swipe_points(direction=Direction.UP, delta_y=per_swipe_px)
        random_sleep(0.25, 0.5, modulable=False)
    logger.debug("Follow vision: scrolled back to top of profile.")

    return [top_shot, mid_shot]


def profile_passes_follow_vision(device, username: str, account_key: str) -> bool:
    """Screenshot the profile and ask OpenAI vision whether to continue."""
    settings = get_account_follow_vision(account_key)
    if not settings.get("enabled"):
        return True

    try:
        from GramAddict.core.views import ProfileView

        images = capture_profile_vision_screenshots(device)
        bio = ProfileView(device, is_own_profile=False).getProfileBiography()
        batch_name = str(settings.get("prompt-batch") or "615FILMS")

        passed, raw = analyze_profile_images(account_key, images, bio)
        if (
            batch_name == "615FILMS"
            and _videographer_log_enabled(settings)
            and response_is_music_videographer(raw)
        ):
            log_found_videographer(
                account_key,
                username,
                bio,
                raw,
            )
        if passed:
            logger.info(
                "Follow vision passed for @%s (%s)",
                username,
                raw,
            )
            return True
        logger.info(
            "Follow vision rejected @%s (%s) — skipping profile.",
            username,
            raw,
        )
        return False
    except Exception as exc:
        logger.warning(
            "Follow vision check failed for @%s (%s) — skipping profile.",
            username,
            exc,
        )
        return False
