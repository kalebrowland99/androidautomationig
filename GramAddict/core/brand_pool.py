"""Brand-level shared interaction pools (615Films, YLF, …)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from atomicwrites import atomic_write

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRAND_POOLS_DIR = PROJECT_ROOT / "brand-pools"
INTERACTED_FILENAME = "interacted_users.json"

KNOWN_POOLS: dict[str, str] = {
    "615films": "615Films",
    "ylf": "YLF",
}


def normalize_pool_id(pool_id: str | None) -> str | None:
    if not pool_id:
        return None
    cleaned = str(pool_id).strip().lower()
    if cleaned in KNOWN_POOLS:
        return cleaned
    return None


def _pool_dir(pool_id: str) -> Path:
    return BRAND_POOLS_DIR / pool_id


def _pool_meta_path(pool_id: str) -> Path:
    return _pool_dir(pool_id) / "pool.yml"


def _interacted_path(pool_id: str) -> Path:
    return _pool_dir(pool_id) / INTERACTED_FILENAME


def ensure_pool(pool_id: str) -> None:
    pool_id = normalize_pool_id(pool_id)
    if not pool_id:
        raise ValueError(f"Unknown brand pool: {pool_id}")
    folder = _pool_dir(pool_id)
    folder.mkdir(parents=True, exist_ok=True)
    meta_path = _pool_meta_path(pool_id)
    if not meta_path.is_file():
        data = {"name": KNOWN_POOLS[pool_id], "accounts": []}
        with meta_path.open("w", encoding="utf-8") as handle:
            yaml.dump(data, handle, default_flow_style=None, sort_keys=False)


def load_pool_meta(pool_id: str) -> dict[str, Any]:
    ensure_pool(pool_id)
    with _pool_meta_path(pool_id).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        data = {}
    accounts = data.get("accounts") or []
    if not isinstance(accounts, list):
        accounts = []
    return {
        "id": pool_id,
        "name": str(data.get("name") or KNOWN_POOLS.get(pool_id, pool_id)),
        "accounts": [str(a).strip() for a in accounts if str(a).strip()],
    }


def save_pool_accounts(pool_id: str, account_ids: list[str]) -> dict[str, Any]:
    pool_id = normalize_pool_id(pool_id)
    if not pool_id:
        raise ValueError(f"Unknown brand pool: {pool_id}")
    ensure_pool(pool_id)
    cleaned = []
    seen: set[str] = set()
    for account_id in account_ids:
        text = str(account_id).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    meta = load_pool_meta(pool_id)
    meta["accounts"] = cleaned
    with _pool_meta_path(pool_id).open("w", encoding="utf-8") as handle:
        yaml.dump(
            {"name": meta["name"], "accounts": cleaned},
            handle,
            default_flow_style=None,
            sort_keys=False,
        )
    return meta


def pool_for_account_id(account_id: str) -> str | None:
    account_id = str(account_id).strip()
    if not account_id:
        return None
    for pool_id in KNOWN_POOLS:
        meta = load_pool_meta(pool_id)
        if account_id in meta["accounts"]:
            return pool_id
    return None


def load_interacted_users(pool_id: str) -> dict[str, Any]:
    pool_id = normalize_pool_id(pool_id)
    if not pool_id:
        return {}
    ensure_pool(pool_id)
    path = _interacted_path(pool_id)
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            logger.error("Invalid %s: %s", path, exc)
            return {}
    return data if isinstance(data, dict) else {}


def save_interacted_users(pool_id: str, data: dict[str, Any]) -> None:
    pool_id = normalize_pool_id(pool_id)
    if not pool_id:
        return
    ensure_pool(pool_id)
    path = _interacted_path(pool_id)
    with atomic_write(path, overwrite=True, encoding="utf-8") as outfile:
        json.dump(data, outfile, indent=4, sort_keys=False)


def merge_interacted_users(pool_id: str, extra: dict[str, Any]) -> dict[str, Any]:
    pool_id = normalize_pool_id(pool_id)
    if not pool_id or not extra:
        return load_interacted_users(pool_id) if pool_id else {}
    merged = load_interacted_users(pool_id)
    for username, record in extra.items():
        if username not in merged:
            merged[username] = record
            continue
        existing = merged[username]
        if not isinstance(existing, dict) or not isinstance(record, dict):
            merged[username] = record
            continue
        for key in ("liked", "watched", "commented"):
            if key in record:
                existing[key] = int(existing.get(key) or 0) + int(record.get(key) or 0)
        for key in ("followed", "unfollowed", "scraped", "pm_sent"):
            if record.get(key):
                existing[key] = record[key]
        if record.get("following_status") in ("followed", "requested"):
            existing["following_status"] = record["following_status"]
        existing_last = existing.get("last_interaction") or ""
        record_last = record.get("last_interaction") or ""
        if record_last > existing_last:
            existing["last_interaction"] = record_last
    save_interacted_users(pool_id, merged)
    return merged


def migrate_account_interactions(account_username: str, pool_id: str) -> int:
    """Merge per-account interacted_users.json into the brand pool."""
    pool_id = normalize_pool_id(pool_id)
    if not pool_id or not account_username:
        return 0
    local_path = PROJECT_ROOT / "accounts" / account_username / INTERACTED_FILENAME
    if not local_path.is_file():
        return 0
    with local_path.open(encoding="utf-8") as handle:
        try:
            local = json.load(handle)
        except json.JSONDecodeError:
            return 0
    if not isinstance(local, dict) or not local:
        return 0
    before = len(load_interacted_users(pool_id))
    merge_interacted_users(pool_id, local)
    after = len(load_interacted_users(pool_id))
    return max(0, after - before)


def resolve_brand_pool(
    *,
    config_path: str | None = None,
    config: dict[str, Any] | None = None,
    ig_username: str | None = None,
) -> str | None:
    """Return brand pool id for this session, if any."""
    if config:
        pool = normalize_pool_id(config.get("brand-pool"))
        if pool:
            return pool

    if config_path:
        account_id = Path(config_path).parent.name
        pool = pool_for_account_id(account_id)
        if pool:
            return pool

    if ig_username:
        accounts_root = PROJECT_ROOT / "accounts"
        if accounts_root.is_dir():
            for folder in accounts_root.iterdir():
                if not folder.is_dir():
                    continue
                cfg = folder / "config.yml"
                if not cfg.is_file():
                    continue
                try:
                    with cfg.open(encoding="utf-8") as handle:
                        data = yaml.safe_load(handle) or {}
                except OSError:
                    continue
                if str(data.get("username") or folder.name).lower() == ig_username.lower():
                    pool = pool_for_account_id(folder.name)
                    if pool:
                        return pool
    return None


def interacted_users_path(pool_id: str) -> str:
    pool_id = normalize_pool_id(pool_id)
    if not pool_id:
        raise ValueError("brand pool required")
    ensure_pool(pool_id)
    return str(_interacted_path(pool_id))
