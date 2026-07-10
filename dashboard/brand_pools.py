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
            }
        )
    return pools


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
