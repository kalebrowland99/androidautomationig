"""Dashboard API for brand-level interaction pools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from GramAddict.core.brand_pool import (
    KNOWN_POOLS,
    ensure_pool,
    load_interacted_users,
    load_pool_meta,
    migrate_account_interactions,
    normalize_pool_id,
    pool_for_account_id,
    save_pool_accounts,
    set_pool_posting_enabled,
)
from dashboard.gramaddict_config import ACCOUNTS_DIR, _load_yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _account_username(account_id: str) -> str:
    cfg = ACCOUNTS_DIR / account_id / "config.yml"
    if cfg.is_file():
        data = _load_yaml(cfg)
        return str(data.get("username") or account_id).strip()
    return account_id


def list_brand_pools() -> list[dict[str, Any]]:
    pools: list[dict[str, Any]] = []
    for pool_id, display_name in KNOWN_POOLS.items():
        ensure_pool(pool_id)
        meta = load_pool_meta(pool_id)
        interacted = load_interacted_users(pool_id)
        members = []
        for account_id in meta["accounts"]:
            members.append(
                {
                    "account_id": account_id,
                    "username": _account_username(account_id),
                }
            )
        pools.append(
            {
                "id": pool_id,
                "name": meta.get("name") or display_name,
                "accounts": members,
                "interacted_count": len(interacted),
                "posting_enabled": bool(meta.get("posting_enabled", True)),
            }
        )
    return pools


def set_pool_posting(pool_id: str, enabled: bool) -> dict[str, Any]:
    """Enable/disable the post-reels job for every account in a pool."""
    meta = set_pool_posting_enabled(pool_id, enabled)
    return {
        "id": meta["id"],
        "name": meta["name"],
        "posting_enabled": bool(meta["posting_enabled"]),
        "accounts": [
            {"account_id": account_id, "username": _account_username(account_id)}
            for account_id in meta["accounts"]
        ],
        "interacted_count": len(load_interacted_users(meta["id"])),
    }


def list_unassigned_accounts() -> list[dict[str, str]]:
    assigned: set[str] = set()
    for pool_id in KNOWN_POOLS:
        meta = load_pool_meta(pool_id)
        assigned.update(meta["accounts"])
    unassigned: list[dict[str, str]] = []
    if not ACCOUNTS_DIR.is_dir():
        return unassigned
    for folder in sorted(ACCOUNTS_DIR.iterdir()):
        if not folder.is_dir() or not (folder / "config.yml").is_file():
            continue
        account_id = folder.name
        if account_id in assigned:
            continue
        unassigned.append({"account_id": account_id, "username": _account_username(account_id)})
    return unassigned


def set_pool_accounts(pool_id: str, account_ids: list[str]) -> dict[str, Any]:
    pool_id = normalize_pool_id(pool_id)
    if not pool_id:
        raise ValueError(f"Unknown brand pool: {pool_id}")

    # Remove these accounts from other pools first.
    for other_id in KNOWN_POOLS:
        if other_id == pool_id:
            continue
        meta = load_pool_meta(other_id)
        remaining = [a for a in meta["accounts"] if a not in account_ids]
        if len(remaining) != len(meta["accounts"]):
            save_pool_accounts(other_id, remaining)

    meta = save_pool_accounts(pool_id, account_ids)

    for account_id in account_ids:
        ig_username = _account_username(account_id)
        migrate_account_interactions(ig_username, pool_id)
        migrate_account_interactions(account_id, pool_id)

    return {
        "id": pool_id,
        "name": meta["name"],
        "accounts": [
            {"account_id": account_id, "username": _account_username(account_id)}
            for account_id in meta["accounts"]
        ],
        "interacted_count": len(load_interacted_users(pool_id)),
    }


def _pool_member_ids(pool_id: str) -> list[str]:
    pool_id = normalize_pool_id(pool_id)
    if not pool_id:
        raise ValueError("Unknown brand pool")
    return list(load_pool_meta(pool_id)["accounts"])


def list_pool_media(pool_id: str) -> dict[str, Any]:
    """Aggregate the post-reel videos present across a pool's member accounts.

    Each video is keyed by filename; ``member_count`` says how many of the
    pool's accounts currently have it, so the UI can show whether an upload
    reached everyone.
    """
    from dashboard.post_reel_config import list_post_media_files

    member_ids = _pool_member_ids(pool_id)
    agg: dict[str, dict[str, Any]] = {}
    for account_id in member_ids:
        try:
            files = list_post_media_files(account_id)
        except Exception:
            continue
        for f in files:
            entry = agg.get(f["name"])
            if entry is None:
                entry = {
                    "name": f["name"],
                    "size": f["size"],
                    "size_label": f["size_label"],
                    "member_count": 0,
                }
                agg[f["name"]] = entry
            entry["member_count"] += 1
    files_sorted = sorted(agg.values(), key=lambda x: x["name"].lower())
    return {"files": files_sorted, "member_total": len(member_ids)}


def upload_media_to_pool(pool_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """Save one video into every member account's post-reel media folder.

    Autopost-locked accounts are skipped, and members that already have an
    identical file (same name + size) are left untouched. This is the pool-wide
    version of the per-account "distribute" action so a video only has to be
    uploaded once for the whole brand.
    """
    from GramAddict.core.account_safety import is_autopost_locked
    from GramAddict.core.post_reel_account import media_dir_for_account
    from dashboard.post_reel_config import _safe_media_filename, _unique_media_path

    member_ids = _pool_member_ids(pool_id)
    safe = _safe_media_filename(filename)
    size = len(data)
    copied: list[str] = []
    skipped_locked: list[str] = []
    skipped_existing: list[str] = []
    errors: list[dict[str, str]] = []
    for account_id in member_ids:
        username = _account_username(account_id)
        try:
            if is_autopost_locked(account_id, username):
                skipped_locked.append(account_id)
                continue
            dest_dir = media_dir_for_account(account_id)
            existing = dest_dir / safe
            if existing.is_file() and existing.stat().st_size == size:
                skipped_existing.append(account_id)
                continue
            _unique_media_path(dest_dir, safe).write_bytes(data)
            copied.append(account_id)
        except Exception as exc:  # noqa: BLE001 - report per-account failures
            errors.append({"account_id": account_id, "error": str(exc)})
    return {
        "pool_id": normalize_pool_id(pool_id),
        "filename": safe,
        "member_total": len(member_ids),
        "copied": copied,
        "skipped_locked": skipped_locked,
        "skipped_existing": skipped_existing,
        "errors": errors,
    }


def delete_media_from_pool(pool_id: str, filename: str) -> dict[str, Any]:
    """Remove a video from every member account's post-reel media folder."""
    from GramAddict.core.post_reel_account import media_dir_for_account
    from dashboard.post_reel_config import _safe_media_filename

    member_ids = _pool_member_ids(pool_id)
    safe = _safe_media_filename(filename)
    deleted: list[str] = []
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    for account_id in member_ids:
        try:
            media_dir = media_dir_for_account(account_id).resolve()
            target = (media_dir / safe).resolve()
            if not str(target).startswith(str(media_dir)):
                raise ValueError("Invalid filename")
            if not target.is_file():
                missing.append(account_id)
                continue
            target.unlink()
            deleted.append(account_id)
        except Exception as exc:  # noqa: BLE001 - report per-account failures
            errors.append({"account_id": account_id, "error": str(exc)})
    return {
        "pool_id": normalize_pool_id(pool_id),
        "filename": safe,
        "member_total": len(member_ids),
        "deleted": deleted,
        "missing": missing,
        "errors": errors,
    }


def sync_account_brand_pool(account_id: str, brand_pool: str | None) -> None:
    """Keep master pool membership in sync when an account's brand-pool field changes."""
    pool_id = normalize_pool_id(brand_pool)
    current = pool_for_account_id(account_id)

    if pool_id == current:
        if pool_id:
            ig_username = _account_username(account_id)
            migrate_account_interactions(ig_username, pool_id)
            migrate_account_interactions(account_id, pool_id)
        return

    if current:
        meta = load_pool_meta(current)
        save_pool_accounts(current, [a for a in meta["accounts"] if a != account_id])

    if pool_id:
        meta = load_pool_meta(pool_id)
        accounts = list(meta["accounts"])
        if account_id not in accounts:
            accounts.append(account_id)
            save_pool_accounts(pool_id, accounts)
        ig_username = _account_username(account_id)
        migrate_account_interactions(ig_username, pool_id)
        migrate_account_interactions(account_id, pool_id)
