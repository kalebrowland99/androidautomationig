"""Dashboard wrappers for Reel posting account files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from dashboard.gramaddict_config import ACCOUNTS_DIR, _save_yaml
from GramAddict.core import post_reel_account as pra
from GramAddict.core.post_reel import VIDEO_EXTENSIONS

POST_REEL_FIELDS: list[dict[str, Any]] = [
    {
        "key": "posts-per-session",
        "label": "Reels to post per session",
        "type": "text",
        "default": "1",
        "placeholder": "1",
    },
    {
        "key": "prompt-batch",
        "label": "Caption prompt batch",
        "type": "select",
        "options": ["615FILMS", "YourLoveFilms"],
        "default": "615FILMS",
    },
    {
        "key": "openai-api-key",
        "label": "OpenAI API key",
        "type": "password",
        "placeholder": "sk-…",
    },
    {
        "key": "openai-model",
        "label": "OpenAI model",
        "type": "text",
        "default": "gpt-4o",
    },
    {
        "key": "clear-gallery-before-each",
        "label": "Clear phone gallery before each reel",
        "type": "bool",
        "default": True,
    },
]

POST_REEL_HELP: dict[str, str] = {
    "posts-per-session": "Debug only — production always posts 1 reel per session. Used by the Reel full post loop debug test.",
    "prompt-batch": "Which caption prompt template to use (615FILMS or YourLoveFilms).",
    "openai-api-key": "OpenAI key for generating captions and hashtags.",
    "openai-model": "Model name, e.g. gpt-4o.",
    "clear-gallery-before-each": "Delete gallery files on the phone before pushing the next video via ADB.",
}


def get_post_reel_schema() -> dict[str, Any]:
    from dashboard.gramaddict_field_help import enrich_fields

    return {"fields": enrich_fields(POST_REEL_FIELDS, POST_REEL_HELP)}


def get_account_post_reel(account_id: str) -> dict[str, Any]:
    return pra.get_account_post_reel(account_id)


def save_account_post_reel(account_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    path = ACCOUNTS_DIR / account_id / pra.POST_REEL_FILENAME
    data = get_account_post_reel(account_id)
    for key, value in updates.items():
        if key == "posts-per-session":
            try:
                data[key] = int(str(value).strip())
            except ValueError:
                data[key] = 1
        elif key == "clear-gallery-before-each":
            data[key] = bool(value)
        else:
            data[key] = value
    _save_yaml(path, data)
    return get_account_post_reel(account_id)


def get_account_post_reel_prompts(account_id: str) -> dict[str, str]:
    return pra.get_account_post_reel_prompts(account_id)


def save_account_post_reel_prompts(account_id: str, updates: dict[str, str]) -> dict[str, str]:
    path = ACCOUNTS_DIR / account_id / pra.POST_REEL_PROMPTS_FILENAME
    data = get_account_post_reel_prompts(account_id)
    data.update({k: str(v) for k, v in updates.items()})
    _save_yaml(path, data)
    return get_account_post_reel_prompts(account_id)


def get_post_reel_for_device(serial: str) -> dict[str, Any]:
    from dashboard.gramaddict_config import account_id_for_device

    account_id = account_id_for_device(serial)
    if not account_id:
        return {}
    return get_account_post_reel(account_id)


def get_media_selection_number(account_id: str) -> int:
    return pra.get_media_selection_number(account_id)


def increment_media_counter(account_id: str) -> int:
    return pra.increment_media_counter(account_id)


def media_dir_for_account(account_id: str):
    return pra.media_dir_for_account(account_id)


def generate_caption(account_id: str, *, batch: Optional[str] = None) -> str:
    return pra.generate_caption(account_id, batch=batch)


def run_post_reel_session(device, serial: str, account_id: str, *, posts_count: Optional[int] = None):
    return pra.run_post_reel_session(device, serial, account_id, posts_count=posts_count)


def _safe_media_filename(name: str) -> str:
    base = Path(name).name.strip()
    base = re.sub(r"[^\w.\-]", "_", base)
    if not base or base.startswith("."):
        base = "video.mp4"
    suffix = Path(base).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        base = f"{Path(base).stem or 'video'}.mp4"
    return base[:200]


def list_post_media_files(account_id: str) -> list[dict[str, Any]]:
    media_dir = media_dir_for_account(account_id)
    items: list[dict[str, Any]] = []
    for path in sorted(media_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        size = path.stat().st_size
        items.append(
            {
                "name": path.name,
                "size": size,
                "size_label": _format_bytes(size),
            }
        )
    return items


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _unique_media_path(media_dir: Path, filename: str) -> Path:
    dest = media_dir / filename
    if not dest.exists():
        return dest
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    for n in range(2, 1000):
        candidate = media_dir / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Too many files with the same name")


def save_post_media_upload(account_id: str, filename: str, data: bytes) -> dict[str, Any]:
    safe = _safe_media_filename(filename)
    media_dir = media_dir_for_account(account_id)
    dest = _unique_media_path(media_dir, safe)
    dest.write_bytes(data)
    size = dest.stat().st_size
    return {"name": dest.name, "size": size, "size_label": _format_bytes(size)}


def delete_post_media_file(account_id: str, filename: str) -> None:
    safe = _safe_media_filename(filename)
    media_dir = media_dir_for_account(account_id).resolve()
    target = (media_dir / safe).resolve()
    if not str(target).startswith(str(media_dir)):
        raise ValueError("Invalid filename")
    if not target.is_file():
        raise FileNotFoundError(filename)
    target.unlink()
