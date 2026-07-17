"""Save and apply named setting templates; copy settings between accounts.

A template captures EVERYTHING that lives under an account's tab: main config,
filters, posting/vision settings and prompts, telegram, comment/PM templates,
and username lists. Applying a template does a full replace on the target so it
matches the source exactly — the only things kept are the target account's own
``username`` and ``device`` (phone link) so it stays wired to the right phone.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atomicwrites import atomic_write

from dashboard.gramaddict_config import (
    PROJECT_ROOT,
    _account_dir,
    _load_yaml,
    _prefer_story_likes_job_order,
    _safe_account_id,
    _save_account_config_yaml,
    _save_yaml,
    _strip_dashboard_only_keys,
    list_accounts,
)

TEMPLATES_DIR = PROJECT_ROOT / "account-templates"

# Tracks which template each account currently has applied.
APPLIED_FILE = TEMPLATES_DIR / ".applied.json"

# Username / URL lists that only copy when the user opts in ("Include lists").
# When opted out, the target keeps its own lists while still adopting the
# template's jobs and limits (the job entries just point at the target's files).
LIST_FILES = (
    "whitelist.txt",
    "blacklist.txt",
    "story_likes.txt",
    "unfollow_list.txt",
    "remove_list.txt",
    "targets.txt",
    "post_urls.txt",
    "like_urls.txt",
)

# Settings state that must always travel with the config so nothing is skipped
# (e.g. whether daily story likes is enabled and its limit).
STATE_SETTINGS_FILES = ("story_likes.meta.yml",)

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
    *STATE_SETTINGS_FILES,
    *LIST_FILES,
)

# Files compared to decide whether an account still matches its template. Lists
# are excluded so opting out of list-copy doesn't read as "drift"; state files
# are excluded because they're re-serialized from the config on apply.
CORE_SETTINGS_FILES = tuple(
    f for f in SETTINGS_FILES if f not in LIST_FILES and f not in STATE_SETTINGS_FILES
)

# Kept on the target account so it stays linked to its own phone/handle.
PRESERVE_CONFIG_KEYS = ("username", "device")


def _template_dir(template_id: str) -> Path:
    folder = TEMPLATES_DIR / template_id
    if not folder.is_dir():
        raise FileNotFoundError(f"Template not found: {template_id}")
    return folder


# ── Applied-template tracking ─────────────────────────────────────────────

def _load_applied() -> dict[str, dict[str, Any]]:
    if not APPLIED_FILE.is_file():
        return {}
    try:
        data = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_applied(data: dict[str, dict[str, Any]]) -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    with atomic_write(APPLIED_FILE, overwrite=True) as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def _set_applied(account_id: str, template_id: str) -> None:
    data = _load_applied()
    data[account_id] = {
        "template_id": template_id,
        "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_applied(data)


def _clear_applied_account(account_id: str) -> None:
    data = _load_applied()
    if account_id in data:
        data.pop(account_id, None)
        _save_applied(data)


def _clear_applied_for_template(template_id: str) -> None:
    data = _load_applied()
    changed = False
    for acct_id in list(data.keys()):
        if data[acct_id].get("template_id") == template_id:
            data.pop(acct_id, None)
            changed = True
    if changed:
        _save_applied(data)


# ── Drift detection ───────────────────────────────────────────────────────

def _normalized_config(directory: Path) -> str:
    """Config text with per-account keys stripped so templates compare equal."""
    path = directory / "config.yml"
    if not path.is_file():
        return ""
    data = _load_yaml(path)
    for key in PRESERVE_CONFIG_KEYS:
        data.pop(key, None)
    return json.dumps(data, sort_keys=True, default=str)


def _settings_signature(directory: Path) -> str:
    """Stable hash of an account/template's core settings (lists excluded)."""
    hasher = hashlib.sha256()
    hasher.update(b"config:")
    hasher.update(_normalized_config(directory).encode("utf-8"))
    for filename in CORE_SETTINGS_FILES:
        hasher.update(f"\n{filename}:".encode("utf-8"))
        path = directory / filename
        if path.is_file():
            try:
                hasher.update(path.read_bytes())
            except OSError:
                pass
    return hasher.hexdigest()


def _account_matches_template(account_id: str, template_id: str) -> bool:
    try:
        acct_sig = _settings_signature(_account_dir(account_id))
        tmpl_sig = _settings_signature(_template_dir(template_id))
    except (FileNotFoundError, OSError):
        return False
    return acct_sig == tmpl_sig


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


def _copy_config_to_template(src_dir: Path, dest_dir: Path) -> bool:
    """Copy an account's config.yml into a template WITHOUT its identity keys.

    Templates must never carry an account's ``username``/``device`` — otherwise
    applying the template to a fresh account (one that has no identity yet) would
    silently graft the source account's handle and phone onto it, so two folders
    end up pointing at the same account. We strip those keys so a template only
    ever holds shared settings.
    """
    src = src_dir / "config.yml"
    if not src.is_file():
        return False
    data = _load_yaml(src)
    for key in PRESERVE_CONFIG_KEYS:
        data.pop(key, None)
    dest_dir.mkdir(parents=True, exist_ok=True)
    _save_yaml(dest_dir / "config.yml", data)
    return True


def _replace_config_from_source(target_id: str, source_dir: Path) -> bool:
    """Overwrite target config.yml with the source's, keeping username/device.

    A template/account config is already bot-ready (its file-job entries and the
    list files they point at are copied alongside it), so we copy it verbatim
    rather than running the UI-oriented ``sync_config_for_bot`` — that helper
    expects the dashboard's expanded list keys and would otherwise DROP
    file-based jobs (interact/targets, post/like URLs) and rebuild story-like
    limits from stale state. We only strip dashboard-only keys, keep the job
    order, and re-apply the posting safety lock.
    """
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

    from GramAddict.core.account_safety import apply_autopost_lock

    new_data = apply_autopost_lock(
        target_id, _strip_dashboard_only_keys(_prefer_story_likes_job_order(new_data))
    )
    _save_account_config_yaml(target_path, new_data)
    return True


def list_setting_templates() -> list[dict[str, Any]]:
    if not TEMPLATES_DIR.is_dir():
        return []
    applied = _load_applied()
    accounts = {a["id"]: a for a in list_accounts()}
    # Group applied accounts by template id.
    applied_by_template: dict[str, list[dict[str, Any]]] = {}
    for acct_id, info in applied.items():
        tmpl_id = info.get("template_id")
        if not tmpl_id or acct_id not in accounts:
            continue
        applied_by_template.setdefault(tmpl_id, []).append(
            {
                "account_id": acct_id,
                "username": accounts[acct_id].get("username") or acct_id,
                "applied_at": info.get("applied_at", ""),
                "modified": not _account_matches_template(acct_id, tmpl_id),
            }
        )
    templates: list[dict[str, Any]] = []
    for folder in sorted(TEMPLATES_DIR.iterdir()):
        if not folder.is_dir():
            continue
        meta = _load_yaml(folder / "meta.yml")
        members = sorted(
            applied_by_template.get(folder.name, []),
            key=lambda m: m["username"].lower(),
        )
        templates.append(
            {
                "id": folder.name,
                "name": str(meta.get("name") or folder.name),
                "source_account": str(meta.get("source_account") or ""),
                "created_at": str(meta.get("created_at") or ""),
                "file_count": len(meta.get("files") or []),
                "applied_to": members,
                "applied_count": len(members),
            }
        )
    return templates


def get_account_template_status(account_id: str) -> dict[str, Any]:
    """What template (if any) is currently applied to a single account."""
    info = _load_applied().get(account_id)
    if not info:
        return {"account_id": account_id, "template_id": "", "name": "", "modified": False}
    tmpl_id = info.get("template_id", "")
    if not tmpl_id or not (TEMPLATES_DIR / tmpl_id).is_dir():
        _clear_applied_account(account_id)
        return {"account_id": account_id, "template_id": "", "name": "", "modified": False}
    meta = _load_yaml(_template_dir(tmpl_id) / "meta.yml")
    return {
        "account_id": account_id,
        "template_id": tmpl_id,
        "name": str(meta.get("name") or tmpl_id),
        "applied_at": info.get("applied_at", ""),
        "modified": not _account_matches_template(account_id, tmpl_id),
    }


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
    if _copy_config_to_template(src_dir, dest_dir):
        copied.append("config.yml")
    for filename in SETTINGS_FILES:
        if _copy_file(src_dir, dest_dir, filename):
            copied.append(filename)
    meta = {
        "name": label,
        "source_account": account_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": copied,
    }
    _save_yaml(dest_dir / "meta.yml", meta)
    # The source account now matches this template exactly.
    _set_applied(account_id, template_id)
    return {"id": template_id, **meta}


def sync_template_from_account(account_id: str) -> dict[str, Any]:
    """Push an account's current settings back into the template it's applied to.

    Called after any account-tab save so a connected template stays in sync with
    the edits made on the account (instead of the account drifting from it).
    No-ops when the account isn't tracking a template. Note: if several accounts
    share one template, editing one account rewrites the shared template, which
    can mark the others as "modified".
    """
    info = _load_applied().get(account_id)
    template_id = (info or {}).get("template_id")
    if not template_id or not (TEMPLATES_DIR / template_id).is_dir():
        return {"synced": False, "account_id": account_id, "template_id": ""}

    src_dir = _account_dir(account_id)
    dest_dir = _template_dir(template_id)
    copied: list[str] = []
    if _copy_config_to_template(src_dir, dest_dir):
        copied.append("config.yml")
    for filename in SETTINGS_FILES:
        if _copy_file(src_dir, dest_dir, filename):
            copied.append(filename)

    meta_path = dest_dir / "meta.yml"
    meta = _load_yaml(meta_path)
    meta["files"] = copied
    meta["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_yaml(meta_path, meta)
    # Refresh the applied timestamp; the account now matches the template again.
    _set_applied(account_id, template_id)
    return {"synced": True, "account_id": account_id, "template_id": template_id, "copied": copied}


def rename_setting_template(template_id: str, name: str) -> dict[str, Any]:
    label = (name or "").strip()
    if not label:
        raise ValueError("Template name is required")
    folder = _template_dir(template_id)
    meta_path = folder / "meta.yml"
    meta = _load_yaml(meta_path)
    meta["name"] = label
    _save_yaml(meta_path, meta)
    return {"id": template_id, "name": label}


def delete_setting_template(template_id: str) -> dict[str, Any]:
    folder = _template_dir(template_id)
    shutil.rmtree(folder)
    _clear_applied_for_template(template_id)
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

    for filename in SETTINGS_FILES:
        if not include_lists and filename in LIST_FILES:
            continue
        if _copy_file(src_dir, dest_dir, filename):
            copied.append(filename)

    if _replace_config_from_source(target_id, src_dir):
        copied.append("config.yml")

    # Track applied template so the UI can show what's active. Copying from
    # another account means the target no longer tracks a named template.
    if source_type == "template":
        _set_applied(target_id, resolved_id)
    else:
        _clear_applied_account(target_id)

    return {
        "target_id": target_id,
        "source_type": source_type,
        "source_id": resolved_id,
        "include_lists": include_lists,
        "copied": copied,
    }


def _template_member_ids(template_id: str) -> list[str]:
    """Account ids currently connected (applied) to a template."""
    accounts = {a["id"] for a in list_accounts()}
    return sorted(
        acct_id
        for acct_id, meta in _load_applied().items()
        if meta.get("template_id") == template_id and acct_id in accounts
    )


def distribute_media_to_connected_accounts(
    account_id: str, filename: str
) -> dict[str, Any]:
    """Copy one ``post_media`` video from a source account to every account
    connected to the SAME template.

    The video is added alongside each target's existing videos (never replaces
    them), autopost-locked accounts are skipped, and targets that already have
    an identical file (same name + size) are left untouched.
    """
    from GramAddict.core.account_safety import is_autopost_locked
    from GramAddict.core.post_reel_account import media_dir_for_account
    from dashboard.post_reel_config import _safe_media_filename, _unique_media_path

    _account_dir(account_id)  # validate source account exists
    safe = _safe_media_filename(filename)
    src_media = media_dir_for_account(account_id).resolve()
    src_file = (src_media / safe).resolve()
    if not str(src_file).startswith(str(src_media)) or not src_file.is_file():
        raise FileNotFoundError(f"Media not found: {filename}")

    info = _load_applied().get(account_id)
    template_id = (info or {}).get("template_id")
    if not template_id or not (TEMPLATES_DIR / template_id).is_dir():
        raise ValueError(
            "This account isn't connected to a template, so there are no "
            "connected accounts to copy to."
        )

    accounts = {a["id"]: a for a in list_accounts()}
    target_ids = [t for t in _template_member_ids(template_id) if t != account_id]

    size = src_file.stat().st_size
    copied: list[str] = []
    skipped_locked: list[str] = []
    skipped_existing: list[str] = []
    errors: list[dict[str, str]] = []
    for tid in target_ids:
        username = str(accounts.get(tid, {}).get("username") or "")
        if is_autopost_locked(tid, username):
            skipped_locked.append(tid)
            continue
        try:
            dest_dir = media_dir_for_account(tid)
            existing = dest_dir / safe
            if existing.is_file() and existing.stat().st_size == size:
                skipped_existing.append(tid)
                continue
            shutil.copy2(src_file, _unique_media_path(dest_dir, safe))
            copied.append(tid)
        except Exception as exc:  # noqa: BLE001 - report per-account failures
            errors.append({"account_id": tid, "error": str(exc)})

    return {
        "source_id": account_id,
        "template_id": template_id,
        "filename": safe,
        "connected_total": len(target_ids),
        "copied": copied,
        "skipped_locked": skipped_locked,
        "skipped_existing": skipped_existing,
        "errors": errors,
    }


def delete_media_from_connected_accounts(
    account_id: str, filename: str
) -> dict[str, Any]:
    """Delete one ``post_media`` video from the source account AND every account
    connected to the SAME template.

    Autopost-locked accounts are skipped, and any target that doesn't have the
    file is simply reported as ``missing`` (not an error).
    """
    from GramAddict.core.account_safety import is_autopost_locked
    from GramAddict.core.post_reel_account import media_dir_for_account
    from dashboard.post_reel_config import _safe_media_filename

    _account_dir(account_id)  # validate source account exists
    safe = _safe_media_filename(filename)

    def _remove(target_account: str) -> str:
        """Return 'deleted' | 'missing' after trying to unlink the file."""
        media_dir = media_dir_for_account(target_account).resolve()
        target = (media_dir / safe).resolve()
        if not str(target).startswith(str(media_dir)):
            raise ValueError("Invalid filename")
        if not target.is_file():
            return "missing"
        target.unlink()
        return "deleted"

    # Source is deleted regardless of template membership.
    source_result = _remove(account_id)

    info = _load_applied().get(account_id)
    template_id = (info or {}).get("template_id")
    accounts = {a["id"]: a for a in list_accounts()}
    target_ids: list[str] = []
    if template_id and (TEMPLATES_DIR / template_id).is_dir():
        target_ids = [
            t for t in _template_member_ids(template_id) if t != account_id
        ]

    deleted: list[str] = []
    missing: list[str] = []
    skipped_locked: list[str] = []
    errors: list[dict[str, str]] = []
    for tid in target_ids:
        username = str(accounts.get(tid, {}).get("username") or "")
        if is_autopost_locked(tid, username):
            skipped_locked.append(tid)
            continue
        try:
            if _remove(tid) == "deleted":
                deleted.append(tid)
            else:
                missing.append(tid)
        except Exception as exc:  # noqa: BLE001 - report per-account failures
            errors.append({"account_id": tid, "error": str(exc)})

    return {
        "source_id": account_id,
        "source_deleted": source_result == "deleted",
        "template_id": template_id or "",
        "filename": safe,
        "connected_total": len(target_ids),
        "deleted": deleted,
        "missing": missing,
        "skipped_locked": skipped_locked,
        "errors": errors,
    }


def apply_template_to_accounts(
    template_id: str,
    account_ids: list[str],
    *,
    include_lists: bool = True,
) -> dict[str, Any]:
    """Apply one template to many accounts at once (bulk)."""
    _template_dir(template_id)  # validate existence early
    ids = [a for a in dict.fromkeys(account_ids) if a]
    if not ids:
        raise ValueError("Select at least one account")
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for account_id in ids:
        try:
            _account_dir(account_id)
            result = apply_settings_to_account(
                account_id,
                source_type="template",
                source_id=template_id,
                include_lists=include_lists,
            )
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - report per-account failures
            errors.append({"account_id": account_id, "error": str(exc)})
    return {
        "template_id": template_id,
        "applied": [r["target_id"] for r in results],
        "errors": errors,
        "include_lists": include_lists,
    }
