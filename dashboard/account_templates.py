"""Save and apply named setting templates; copy settings between accounts.

A template captures EVERYTHING that lives under an account's tab: main config,
filters, posting/vision settings and prompts, telegram, comment/PM templates,
and username lists. Applying a template does a full replace on the target so it
matches the source exactly — the only things kept are the target account's own
``username`` and ``device`` (phone link) so it stays wired to the right phone.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dashboard.gramaddict_config import (
    PROJECT_ROOT,
    _account_dir,
    _load_yaml,
    _safe_account_id,
    _save_account_config_yaml,
    _save_yaml,
    sync_config_for_bot,
)

TEMPLATES_DIR = PROJECT_ROOT / "account-templates"

# Every settings file that belongs to an account tab (config handled separately).
SETTINGS_FILES = (
    "filters.yml",
    "telegram.yml",
    "post_reel.yml",
    "post_reel_prompts.yml",
    "follow_vision.yml",
    "follow_vision_prompts.yml",
    "comments_list.txt",
    "pm_list.txt",
    "whitelist.txt",
    "blacklist.txt",
    "story_likes.txt",
    "unfollow_list.txt",
    "remove_list.txt",
)

# Kept on the target account so it stays linked to its own phone/handle.
PRESERVE_CONFIG_KEYS = ("username", "device")


def _template_dir(template_id: str) -> Path:
    folder = TEMPLATES_DIR / template_id
    if not folder.is_dir():
        raise FileNotFoundError(f"Template not found: {template_id}")
    return folder


def _resolve_source(source_type: str, source_id: str) -> tuple[Path, str]:
    kind = (source_type or "").strip().lower()
    sid = (source_id or "").strip()
    if not sid:
        raise ValueError("Source id is required")
    if kind == "account":
        return _account_dir(sid), sid
    if kind == "template":
        return _template_dir(sid), sid
    raise ValueError("source_type must be 'account' or 'template'")


def _copy_file(src_dir: Path, dest_dir: Path, filename: str) -> bool:
    src = src_dir / filename
    if not src.is_file():
        return False
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / filename)
    return True


def _replace_config_from_source(target_id: str, source_dir: Path) -> bool:
    """Overwrite target config.yml with the source's, keeping username/device."""
    source_path = source_dir / "config.yml"
    if not source_path.is_file():
        return False
    target_path = _account_dir(target_id) / "config.yml"
    target_data = _load_yaml(target_path) if target_path.is_file() else {}
    preserved = {
        key: target_data[key] for key in PRESERVE_CONFIG_KEYS if key in target_data
    }
    new_data = _load_yaml(source_path)
    for key in PRESERVE_CONFIG_KEYS:
        new_data.pop(key, None)
    new_data.update(preserved)
    new_data = sync_config_for_bot(target_id, new_data)
    _save_account_config_yaml(target_path, new_data)
    return True


def list_setting_templates() -> list[dict[str, Any]]:
    if not TEMPLATES_DIR.is_dir():
        return []
    templates: list[dict[str, Any]] = []
    for folder in sorted(TEMPLATES_DIR.iterdir()):
        if not folder.is_dir():
            continue
        meta = _load_yaml(folder / "meta.yml")
        templates.append(
            {
                "id": folder.name,
                "name": str(meta.get("name") or folder.name),
                "source_account": str(meta.get("source_account") or ""),
                "created_at": str(meta.get("created_at") or ""),
            }
        )
    return templates


def save_setting_template(account_id: str, name: str) -> dict[str, Any]:
    label = (name or "").strip()
    if not label:
        raise ValueError("Template name is required")
    template_id = _safe_account_id(label)
    src_dir = _account_dir(account_id)
    dest_dir = TEMPLATES_DIR / template_id
    if dest_dir.exists():
        raise FileExistsError(f"Template already exists: {template_id}")
    dest_dir.mkdir(parents=True)
    copied: list[str] = []
    for filename in ("config.yml", *SETTINGS_FILES):
        if _copy_file(src_dir, dest_dir, filename):
            copied.append(filename)
    meta = {
        "name": label,
        "source_account": account_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": copied,
    }
    _save_yaml(dest_dir / "meta.yml", meta)
    return {"id": template_id, **meta}


def delete_setting_template(template_id: str) -> dict[str, Any]:
    folder = _template_dir(template_id)
    shutil.rmtree(folder)
    return {"deleted": True, "id": template_id}


def apply_settings_to_account(
    target_id: str,
    *,
    source_type: str,
    source_id: str,
    include_lists: bool = True,
) -> dict[str, Any]:
    if source_type == "account" and target_id == source_id:
        raise ValueError("Cannot copy settings from an account into itself")
    src_dir, resolved_id = _resolve_source(source_type, source_id)
    dest_dir = _account_dir(target_id)
    copied: list[str] = []

    list_files = {"whitelist.txt", "blacklist.txt", "story_likes.txt", "unfollow_list.txt", "remove_list.txt"}
    for filename in SETTINGS_FILES:
        if not include_lists and filename in list_files:
            continue
        if _copy_file(src_dir, dest_dir, filename):
            copied.append(filename)

    if _replace_config_from_source(target_id, src_dir):
        copied.append("config.yml")

    return {
        "target_id": target_id,
        "source_type": source_type,
        "source_id": resolved_id,
        "include_lists": include_lists,
        "copied": copied,
    }
