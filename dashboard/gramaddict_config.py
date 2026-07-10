"""GramAddict account and config management for the dashboard."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = PROJECT_ROOT / "accounts"
CONFIG_TEMPLATE = PROJECT_ROOT / "config-examples" / "config.yml"

_bot_processes: dict[str, subprocess.Popen[str]] = {}
_bot_log_buffers: dict[str, list[str]] = {}

from dashboard.gramaddict_fields import (
    ACCOUNT_CONFIG_TABS,
    COLLAPSED_SECTIONS,
    EDITABLE_FIELDS,
    SECTION_LABELS,
)
from dashboard.gramaddict_field_help import (
    ACCOUNT_TAB_HELP,
    CONFIG_HELP,
    FILE_HELP,
    FILTER_HELP,
    GRAMADDICT_TERMINOLOGY,
    SECTION_HELP,
    TELEGRAM_HELP,
    VPN_APP_HELP,
    enrich_fields,
)
from dashboard.gramaddict_filters_fields import (
    ACCOUNT_BUNDLE,
    ACCOUNT_LIST_FILES,
    ACCOUNT_TEXT_FILES,
    FILTER_FIELDS,
    FILTER_SECTION_LABELS,
    TELEGRAM_FIELDS,
    TEMPLATE_ACCOUNT_FILES,
)

TARGETS_LIST_FILE = "targets.txt"
POST_URLS_FILE = "post_urls.txt"
LIKE_URLS_FILE = "like_urls.txt"
UNFOLLOW_LIST_FILENAME = "unfollow_list.txt"
REMOVE_LIST_FILENAME = "remove_list.txt"
WHITELIST_FILENAME = "whitelist.txt"
INTERACT_JOB_KEY = "interact-from-file"
POSTS_JOB_KEY = "posts-from-file"
INTERACT_LIST_KEY = "interact-from-file-list"
INTERACT_LIMIT_KEY = "interact-from-file-limit"
POSTS_LIST_KEY = "posts-from-file-list"
LIKE_LIST_KEY = "like-from-urls-list"
UNFOLLOW_LIMIT_KEY = "unfollow-from-list"
REMOVE_LIMIT_KEY = "remove-followers-from-list"
# Dashboard-only keys — never written to config.yml (run.py does not accept them).
DASHBOARD_ONLY_CONFIG_KEYS = frozenset({"brand-pool"})


def _safe_account_id(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]", "_", name.strip())
    if not cleaned:
        raise ValueError("Account name is required")
    return cleaned


def _config_path(account_id: str) -> Path:
    return ACCOUNTS_DIR / account_id / "config.yml"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _save_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        # default_flow_style=None renders scalar-only lists inline ([a, b]) which is
        # the format configargparse (run.py --config) requires. Block-style lists
        # ("- item" without indentation) break its config parser.
        # width=inf keeps long lists on a single line — configargparse reads the
        # config line-by-line, so a wrapped list would break parsing.
        yaml.dump(
            data,
            handle,
            default_flow_style=None,
            sort_keys=False,
            allow_unicode=True,
            width=float("inf"),
        )


def _save_account_config_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write accounts/*/config.yml — strips dashboard-only keys run.py cannot parse."""
    _save_yaml(path, _strip_dashboard_only_keys(dict(data)))


def _lines_to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return [str(value)]


def _list_to_lines(value: Any) -> str:
    return "\n".join(_lines_to_list(value))


def _file_job_entries(entries: Any) -> list[str]:
    if not entries:
        return []
    if isinstance(entries, list):
        return [str(entry).strip() for entry in entries if str(entry).strip()]
    text = str(entries).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _parse_file_job_entry(entry: str) -> tuple[str, str]:
    parts = str(entry).strip().split(None, 1)
    filename = parts[0] if parts else ""
    limit = parts[1] if len(parts) > 1 else "10"
    return filename, limit


def _first_file_job_limit(entries: Any, expected_file: str | None = None) -> str:
    for entry in _file_job_entries(entries):
        filename, limit = _parse_file_job_entry(entry)
        if expected_file is None or filename == expected_file:
            return limit
    return ""


def _write_line_list_file(path: Path, lines: list[str]) -> None:
    cleaned = [line.strip() for line in lines if str(line).strip()]
    if cleaned:
        path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    elif path.is_file():
        path.write_text("", encoding="utf-8")


def _read_line_list_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _schema_field_entries() -> list[dict[str, Any]]:
    """Expand compound inline file fields into form keys for load/save."""
    entries: list[dict[str, Any]] = []
    for section in EDITABLE_FIELDS.values():
        for field in section:
            ftype = field.get("type")
            if ftype == "inline-file-job":
                entries.append({**field, "key": f"{field['key']}-list", "type": "lines"})
                entries.append(
                    {
                        **field,
                        "key": f"{field['key']}-limit",
                        "type": "text",
                        "placeholder": field.get("limit_placeholder", "10-15"),
                    }
                )
            elif ftype == "inline-lines-file":
                entries.append({**field, "key": f"{field['key']}-list", "type": "lines"})
            else:
                entries.append(field)
    return entries


def _strip_dashboard_only_keys(data: dict[str, Any]) -> dict[str, Any]:
    for key in DASHBOARD_ONLY_CONFIG_KEYS:
        data.pop(key, None)
    return data


def hydrate_config_for_ui(data: dict[str, Any], account_id: str) -> dict[str, Any]:
    """Fill dashboard list fields from saved config or legacy file-job keys."""
    account_dir = _account_dir(account_id)
    out = dict(data)

    usernames = out.get(INTERACT_LIST_KEY)
    if not isinstance(usernames, list) or not usernames:
        usernames = _read_line_list_file(account_dir / TARGETS_LIST_FILE)
        if not usernames:
            merged: list[str] = []
            seen: set[str] = set()
            for entry in _file_job_entries(out.get(INTERACT_JOB_KEY)):
                filename, _ = _parse_file_job_entry(entry)
                if filename:
                    for name in _read_line_list_file(account_dir / filename):
                        if name not in seen:
                            seen.add(name)
                            merged.append(name)
            usernames = merged
        if not usernames:
            usernames = _lines_to_list(out.pop("interact-usernames", None))
        if usernames:
            out[INTERACT_LIST_KEY] = usernames

    if not str(out.get(INTERACT_LIMIT_KEY) or "").strip():
        limit = _first_file_job_limit(out.get(INTERACT_JOB_KEY), TARGETS_LIST_FILE)
        if not limit:
            limit = _first_file_job_limit(out.get(INTERACT_JOB_KEY))
        if not limit:
            limit = str(out.pop("interact-usernames-limit", "") or "").strip()
        if limit:
            out[INTERACT_LIMIT_KEY] = limit

    if not str(out.get(UNFOLLOW_LIMIT_KEY) or "").strip():
        limit = _first_file_job_limit(out.get("unfollow-from-file"), UNFOLLOW_LIST_FILENAME)
        if limit:
            out[UNFOLLOW_LIMIT_KEY] = limit

    if not str(out.get(REMOVE_LIMIT_KEY) or "").strip():
        limit = _first_file_job_limit(
            out.get("remove-followers-from-file"), REMOVE_LIST_FILENAME
        )
        if limit:
            out[REMOVE_LIMIT_KEY] = limit

    job_files = [
        _parse_file_job_entry(entry)[0]
        for entry in _file_job_entries(out.get(POSTS_JOB_KEY))
        if _parse_file_job_entry(entry)[0]
    ]

    urls = out.get(POSTS_LIST_KEY)
    if not isinstance(urls, list) or not urls:
        urls = _read_line_list_file(account_dir / POST_URLS_FILE)
        if not urls:
            for filename in job_files:
                if filename == LIKE_URLS_FILE:
                    continue
                urls = _read_line_list_file(account_dir / filename)
                if urls:
                    break
        if not urls:
            urls = _lines_to_list(out.pop("post-urls", None))
        if urls:
            out[POSTS_LIST_KEY] = urls

    like_urls = out.get(LIKE_LIST_KEY)
    if not isinstance(like_urls, list) or not like_urls:
        like_urls = _read_line_list_file(account_dir / LIKE_URLS_FILE)
        if not like_urls and LIKE_URLS_FILE in job_files:
            like_urls = _read_line_list_file(account_dir / LIKE_URLS_FILE)
        if not like_urls:
            legacy = out.get("like-from-urls")
            if legacy:
                filename = str(legacy).strip().split()[0]
                if filename:
                    like_urls = _read_line_list_file(account_dir / filename)
        if like_urls:
            out[LIKE_LIST_KEY] = like_urls

    from GramAddict.core.brand_pool import pool_for_account_id

    out["brand-pool"] = pool_for_account_id(account_id) or ""

    return out


def sync_config_for_bot(account_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Write list files and GramAddict file-job keys from dashboard fields."""
    account_dir = _account_dir(account_id)
    out = dict(data)

    usernames = out.pop(INTERACT_LIST_KEY, out.pop("interact-usernames", None))
    limit = str(out.pop(INTERACT_LIMIT_KEY, out.pop("interact-usernames-limit", None)) or "10").strip()
    if isinstance(usernames, list) and usernames:
        cleaned = [str(u).strip().lstrip("@") for u in usernames if str(u).strip()]
        _write_line_list_file(account_dir / TARGETS_LIST_FILE, cleaned)
        out[INTERACT_JOB_KEY] = [f"{TARGETS_LIST_FILE} {limit}"]
    else:
        out.pop(INTERACT_JOB_KEY, None)

    unfollow_limit = str(out.get(UNFOLLOW_LIMIT_KEY) or "").strip()
    if unfollow_limit:
        out["unfollow-from-file"] = [f"{UNFOLLOW_LIST_FILENAME} {unfollow_limit}"]
    else:
        out.pop("unfollow-from-file", None)

    remove_limit = str(out.get(REMOVE_LIMIT_KEY) or "").strip()
    if remove_limit:
        out["remove-followers-from-file"] = [f"{REMOVE_LIST_FILENAME} {remove_limit}"]
    else:
        out.pop("remove-followers-from-file", None)

    job_files: list[str] = []

    urls = out.pop(POSTS_LIST_KEY, out.pop("post-urls", None))
    post_cleaned = (
        [str(u).strip() for u in urls if str(u).strip()]
        if isinstance(urls, list)
        else []
    )
    _write_line_list_file(account_dir / POST_URLS_FILE, post_cleaned)
    if post_cleaned:
        job_files.append(POST_URLS_FILE)

    like_urls = out.pop(LIKE_LIST_KEY, None)
    like_cleaned = (
        [str(u).strip() for u in like_urls if str(u).strip()]
        if isinstance(like_urls, list)
        else []
    )
    _write_line_list_file(account_dir / LIKE_URLS_FILE, like_cleaned)
    if like_cleaned:
        job_files.append(LIKE_URLS_FILE)

    if job_files:
        out[POSTS_JOB_KEY] = job_files
    else:
        out.pop(POSTS_JOB_KEY, None)

    out.pop("like-from-urls", None)
    return _strip_dashboard_only_keys(out)


def _value_for_ui(key: str, field: dict[str, Any], value: Any) -> Any:
    ftype = field["type"]
    if ftype == "lines":
        return _list_to_lines(value)
    if ftype == "bool":
        return bool(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if value is None:
        return field.get("default", "")
    return value


def config_for_ui(data: dict[str, Any], account_id: str | None = None) -> dict[str, Any]:
    if account_id:
        data = hydrate_config_for_ui(data, account_id)
    ui: dict[str, Any] = {}
    for field in _schema_field_entries():
        key = field["key"]
        ui[key] = _value_for_ui(key, field, data.get(key))
    return ui


def _value_from_ui(key: str, field: dict[str, Any], value: Any) -> Any:
    ftype = field["type"]
    if ftype == "bool":
        return bool(value)
    if ftype == "lines":
        lines = _lines_to_list(value)
        return lines if lines else None
    if ftype == "device":
        text = str(value).strip() if value is not None else ""
        return text or None
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if key == "working-hours":
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def config_from_ui(updates: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    field_by_key = {field["key"]: field for field in _schema_field_entries()}
    field_types = {key: field["type"] for key, field in field_by_key.items()}
    for key, value in updates.items():
        if key not in field_types:
            continue
        field = field_by_key[key]
        parsed = _value_from_ui(key, field, value)
        out[key] = parsed
    return out


def get_field_schema() -> dict[str, Any]:
    sections = {
        section_id: enrich_fields(fields, CONFIG_HELP)
        for section_id, fields in EDITABLE_FIELDS.items()
    }
    return {
        "sections": sections,
        "labels": SECTION_LABELS,
        "tabs": ACCOUNT_CONFIG_TABS,
        "section_help": SECTION_HELP,
        "tab_help": ACCOUNT_TAB_HELP,
        "terminology": GRAMADDICT_TERMINOLOGY,
        "collapsed": COLLAPSED_SECTIONS,
    }


def get_filters_schema() -> dict[str, Any]:
    sections = {
        section_id: enrich_fields(fields, FILTER_HELP)
        for section_id, fields in FILTER_FIELDS.items()
    }
    return {
        "sections": sections,
        "labels": FILTER_SECTION_LABELS,
        "section_help": SECTION_HELP,
    }


def get_telegram_schema() -> dict[str, Any]:
    return {"fields": enrich_fields(TELEGRAM_FIELDS, TELEGRAM_HELP)}


def get_vpn_help() -> str:
    return VPN_APP_HELP


def _account_dir(account_id: str) -> Path:
    folder = ACCOUNTS_DIR / account_id
    if not folder.is_dir():
        raise FileNotFoundError(f"Account not found: {account_id}")
    return folder


def _filter_field_by_key() -> dict[str, dict[str, Any]]:
    return {
        field["key"]: field
        for section in FILTER_FIELDS.values()
        for field in section
    }


def filters_for_ui(data: dict[str, Any]) -> dict[str, Any]:
    field_by_key = _filter_field_by_key()
    ui: dict[str, Any] = {}
    for key, field in field_by_key.items():
        value = data.get(key)
        ftype = field["type"]
        if ftype == "lines":
            ui[key] = _list_to_lines(value)
        elif ftype == "bool":
            ui[key] = bool(value)
        elif value is None:
            ui[key] = field.get("default", "")
        else:
            ui[key] = value
    return ui


def filters_from_ui(updates: dict[str, Any]) -> dict[str, Any]:
    field_by_key = _filter_field_by_key()
    out: dict[str, Any] = {}
    for key, value in updates.items():
        field = field_by_key.get(key)
        if not field:
            continue
        ftype = field["type"]
        if ftype == "bool":
            out[key] = bool(value)
        elif ftype == "lines":
            lines = _lines_to_list(value)
            out[key] = lines if lines else None
        else:
            text = str(value).strip() if value is not None else ""
            if not text:
                out[key] = None
            elif key in ("min_followers", "max_followers", "min_followings", "max_followings", "min_posts", "mutual_friends", "min_likers", "max_likers"):
                try:
                    out[key] = int(text)
                except ValueError:
                    out[key] = text
            elif key in ("min_potency_ratio", "max_potency_ratio"):
                try:
                    out[key] = float(text)
                except ValueError:
                    out[key] = text
            else:
                out[key] = text
    return out


def get_account_filters(account_id: str) -> dict[str, Any]:
    path = _account_dir(account_id) / "filters.yml"
    data = _load_yaml(path) if path.is_file() else {}
    raw_yaml = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"form": filters_for_ui(data), "raw_yaml": raw_yaml}


def save_account_filters(account_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    path = _account_dir(account_id) / "filters.yml"
    data = _load_yaml(path) if path.is_file() else {}
    merged = filters_from_ui(updates)
    for key, value in merged.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    _save_yaml(path, data)
    return get_account_filters(account_id)


def get_account_telegram(account_id: str) -> dict[str, Any]:
    path = _account_dir(account_id) / "telegram.yml"
    data = _load_yaml(path) if path.is_file() else {}
    form = {field["key"]: data.get(field["key"], "") for field in TELEGRAM_FIELDS}
    raw_yaml = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"form": form, "raw_yaml": raw_yaml}


def save_account_telegram(account_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    path = _account_dir(account_id) / "telegram.yml"
    data = _load_yaml(path) if path.is_file() else {}
    for field in TELEGRAM_FIELDS:
        key = field["key"]
        if key in updates:
            text = str(updates[key]).strip()
            if text:
                data[key] = text
            else:
                data.pop(key, None)
    _save_yaml(path, data)
    return get_account_telegram(account_id)


def list_account_files(account_id: str) -> list[dict[str, str]]:
    folder = _account_dir(account_id)
    files: list[dict[str, str]] = []
    for path in sorted(folder.iterdir()):
        if path.is_file():
            files.append({"name": path.name, "size": str(path.stat().st_size)})
    return files


def get_account_file(account_id: str, filename: str) -> dict[str, str]:
    folder = _account_dir(account_id)
    path = (folder / filename).resolve()
    if not str(path).startswith(str(folder.resolve())):
        raise ValueError("Invalid path")
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {filename}")
    return {"name": filename, "content": path.read_text(encoding="utf-8")}


def save_account_file(account_id: str, filename: str, content: str) -> dict[str, str]:
    folder = _account_dir(account_id)
    path = (folder / filename).resolve()
    if not str(path).startswith(str(folder.resolve())):
        raise ValueError("Invalid path")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"name": filename, "content": content}


def get_list_files_meta() -> dict[str, Any]:
    return {
        "lists": ACCOUNT_LIST_FILES,
        "text": ACCOUNT_TEXT_FILES,
        "bundle": ACCOUNT_BUNDLE,
        "template_files": TEMPLATE_ACCOUNT_FILES,
        "file_help": FILE_HELP,
        "tab_help": ACCOUNT_TAB_HELP,
    }


def get_account_bundle_status(account_id: str) -> dict[str, Any]:
    folder = _account_dir(account_id)
    present = {p.name for p in folder.iterdir() if p.is_file()}
    files = []
    for item in ACCOUNT_BUNDLE:
        name = item["name"]
        files.append({**item, "present": name in present})
    return {
        "account_id": account_id,
        "files": files,
        "missing": [f["name"] for f in files if not f["present"]],
    }


def ensure_account_template_files(account_id: str) -> dict[str, Any]:
    folder = _account_dir(account_id)
    examples = CONFIG_TEMPLATE.parent
    added: list[str] = []
    for filename in TEMPLATE_ACCOUNT_FILES:
        if filename == "config.yml":
            continue
        dest = folder / filename
        if dest.is_file():
            continue
        src = examples / filename
        if src.is_file():
            shutil.copy(src, dest)
            added.append(filename)
    status = get_account_bundle_status(account_id)
    status["added"] = added
    return status


def list_accounts() -> list[dict[str, Any]]:
    if not ACCOUNTS_DIR.is_dir():
        return []
    accounts: list[dict[str, Any]] = []
    for folder in sorted(ACCOUNTS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        config_path = folder / "config.yml"
        if not config_path.is_file():
            continue
        data = _load_yaml(config_path)
        accounts.append(
            {
                "id": folder.name,
                "username": data.get("username") or folder.name,
                "device": data.get("device") or "",
                "config_path": str(config_path.relative_to(PROJECT_ROOT)),
                "running": folder.name in _bot_processes
                and _bot_processes[folder.name].poll() is None,
            }
        )
    return accounts


def create_account(name: str) -> dict[str, Any]:
    account_id = _safe_account_id(name)
    folder = ACCOUNTS_DIR / account_id
    config_path = _config_path(account_id)
    if folder.exists():
        raise FileExistsError(f"Account already exists: {account_id}")

    folder.mkdir(parents=True)
    if CONFIG_TEMPLATE.is_file():
        shutil.copy(CONFIG_TEMPLATE, config_path)
        examples = CONFIG_TEMPLATE.parent
        for extra in (
            "whitelist.txt",
            "blacklist.txt",
            "comments_list.txt",
            "filters.yml",
            "telegram.yml",
            "pm_list.txt",
            "post_reel.yml",
            "post_reel_prompts.yml",
            "follow_vision.yml",
            "follow_vision_prompts.yml",
        ):
            src = examples / extra
            if src.is_file():
                shutil.copy(src, folder / extra)
    else:
        _save_account_config_yaml(config_path, {"username": account_id})

    data = _load_yaml(config_path)
    data["username"] = name.strip()
    _save_account_config_yaml(config_path, data)
    (folder / "post_media").mkdir(exist_ok=True)
    return get_account(account_id)


def username_for_device(serial: str) -> str | None:
    """Instagram @handle linked to this phone serial in account config, if any."""
    device_serial = serial.strip()
    if not device_serial or not ACCOUNTS_DIR.is_dir():
        return None
    for folder in ACCOUNTS_DIR.iterdir():
        if not folder.is_dir():
            continue
        config_path = folder / "config.yml"
        if not config_path.is_file():
            continue
        data = _load_yaml(config_path)
        if data.get("device") == device_serial:
            handle = (data.get("username") or folder.name or "").strip().lstrip("@")
            return handle or None
    return None


def account_id_for_device(serial: str) -> str | None:
    """Account folder id linked to this phone serial, if any."""
    device_serial = serial.strip()
    if not device_serial or not ACCOUNTS_DIR.is_dir():
        return None
    for folder in ACCOUNTS_DIR.iterdir():
        if not folder.is_dir():
            continue
        config_path = folder / "config.yml"
        if not config_path.is_file():
            continue
        data = _load_yaml(config_path)
        if data.get("device") == device_serial:
            return folder.name
    return None


def parse_username_list_text(content: str) -> list[str]:
    """Parse a username list file; skip blanks and # comment lines."""
    usernames: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        usernames.append(stripped.lstrip("@"))
    return usernames


def read_account_username_list(account_id: str, filename: str) -> list[str]:
    """Read usernames from an account list file; returns [] if missing."""
    path = ACCOUNTS_DIR / account_id / filename
    if not path.is_file():
        return []
    return parse_username_list_text(path.read_text(encoding="utf-8"))


def unfollow_practice_username_for_device(serial: str, index: int = 0) -> str | None:
    """First username from unfollow_list.txt for the account on this device."""
    account_id = account_id_for_device(serial)
    if not account_id:
        return None
    names = read_account_username_list(account_id, UNFOLLOW_LIST_FILENAME)
    if not names or index >= len(names):
        return None
    return names[index]


def remove_practice_username_for_device(serial: str, index: int = 0) -> str | None:
    """First username from remove_list.txt for the account on this device."""
    account_id = account_id_for_device(serial)
    if not account_id:
        return None
    names = read_account_username_list(account_id, REMOVE_LIST_FILENAME)
    if not names or index >= len(names):
        return None
    return names[index]


def get_account_telegram_for_device(serial: str) -> dict[str, Any]:
    """telegram.yml contents for the account linked to this device."""
    account_id = account_id_for_device(serial)
    if not account_id:
        return {}
    path = ACCOUNTS_DIR / account_id / "telegram.yml"
    if not path.is_file():
        return {}
    data = _load_yaml(path)
    return data if isinstance(data, dict) else {}


def is_username_whitelisted_for_device(serial: str, username: str) -> bool:
    account_id = account_id_for_device(serial)
    if not account_id:
        return False
    normalized = username.strip().lstrip("@").lower()
    whitelist = read_account_username_list(account_id, WHITELIST_FILENAME)
    return normalized in {name.lower() for name in whitelist}


def assign_account_to_device(serial: str, username: str | None) -> dict[str, Any]:
    device_serial = serial.strip()
    if not device_serial:
        raise ValueError("Device serial is required")

    handle = (username or "").strip().lstrip("@")
    if not handle:
        if not ACCOUNTS_DIR.is_dir():
            return {"serial": device_serial, "account_id": None, "username": ""}
        for folder in ACCOUNTS_DIR.iterdir():
            if not folder.is_dir():
                continue
            config_path = folder / "config.yml"
            if not config_path.is_file():
                continue
            data = _load_yaml(config_path)
            if data.get("device") == device_serial:
                data.pop("device", None)
                _save_account_config_yaml(config_path, data)
        return {"serial": device_serial, "account_id": None, "username": ""}

    account_id = _safe_account_id(handle)
    if not _config_path(account_id).is_file():
        create_account(handle)

    if ACCOUNTS_DIR.is_dir():
        for folder in ACCOUNTS_DIR.iterdir():
            if not folder.is_dir() or folder.name == account_id:
                continue
            config_path = folder / "config.yml"
            if not config_path.is_file():
                continue
            data = _load_yaml(config_path)
            if data.get("device") == device_serial:
                data.pop("device", None)
                _save_account_config_yaml(config_path, data)

    save_account_config(account_id, {"username": handle, "device": device_serial})
    return {
        "serial": device_serial,
        "account_id": account_id,
        "username": handle,
    }


def delete_account(account_id: str) -> dict[str, Any]:
    folder = _account_dir(account_id).resolve()
    accounts_root = ACCOUNTS_DIR.resolve()
    if accounts_root not in folder.parents:
        raise ValueError(f"Invalid account id: {account_id}")
    stop_bot(account_id)
    _bot_log_buffers.pop(account_id, None)
    shutil.rmtree(folder)
    return {"deleted": True, "id": account_id}


def get_account(account_id: str) -> dict[str, Any]:
    from dashboard.session_estimate import estimate_session

    config_path = _config_path(account_id)
    if not config_path.is_file():
        raise FileNotFoundError(f"Account not found: {account_id}")
    data = _load_yaml(config_path)
    raw_yaml = config_path.read_text(encoding="utf-8")
    post_reel: dict[str, Any] = {}
    post_reel_path = _account_dir(account_id) / "post_reel.yml"
    if post_reel_path.is_file():
        post_reel = _load_yaml(post_reel_path)
    hydrated = hydrate_config_for_ui(data, account_id)
    estimate_cfg = dict(data)
    for ui_key in (INTERACT_LIST_KEY, INTERACT_LIMIT_KEY, POSTS_LIST_KEY, LIKE_LIST_KEY):
        if ui_key in hydrated:
            estimate_cfg[ui_key] = hydrated[ui_key]
    return {
        "id": account_id,
        "username": data.get("username") or account_id,
        "device": data.get("device") or "",
        "config_path": str(config_path.relative_to(PROJECT_ROOT)),
        "raw": data,
        "form": config_for_ui(data, account_id),
        "raw_yaml": raw_yaml,
        "estimate": estimate_session(estimate_cfg, post_reel=post_reel),
        "running": account_id in _bot_processes and _bot_processes[account_id].poll() is None,
    }


def save_account_raw_yaml(account_id: str, raw_yaml: str) -> dict[str, Any]:
    config_path = _config_path(account_id)
    if not config_path.is_file():
        raise FileNotFoundError(f"Account not found: {account_id}")
    data = yaml.safe_load(raw_yaml) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yml must be a YAML mapping")
    _save_account_config_yaml(config_path, data)
    return get_account(account_id)


def save_account_config(account_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    config_path = _config_path(account_id)
    if not config_path.is_file():
        raise FileNotFoundError(f"Account not found: {account_id}")
    data = _load_yaml(config_path)
    merged = config_from_ui(updates)
    brand_pool = merged.pop("brand-pool", None) or None
    for key, value in merged.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    data = sync_config_for_bot(account_id, data)
    _save_account_config_yaml(config_path, data)
    from dashboard.brand_pools import sync_account_brand_pool

    sync_account_brand_pool(account_id, brand_pool)
    return get_account(account_id)


def bot_status(account_id: str) -> dict[str, Any]:
    proc = _bot_processes.get(account_id)
    running = proc is not None and proc.poll() is None
    return {
        "account_id": account_id,
        "running": running,
        "logs": _bot_log_buffers.get(account_id, [])[-200:],
    }


def stop_bot(account_id: str) -> dict[str, Any]:
    proc = _bot_processes.pop(account_id, None)
    if proc is None or proc.poll() is not None:
        return {"stopped": False, "message": "Bot is not running"}
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return {"stopped": True, "message": "Bot stopped"}


async def start_bot(
    account_id: str,
    *,
    device_serial: Optional[str] = None,
    vpn_app_name: Optional[str] = None,
    log_callback: Optional[Any] = None,
) -> dict[str, Any]:
    config_path = _config_path(account_id)
    if not config_path.is_file():
        raise FileNotFoundError(f"Account not found: {account_id}")

    existing = _bot_processes.get(account_id)
    if existing is not None and existing.poll() is None:
        raise RuntimeError("Bot is already running for this account")

    data = _load_yaml(config_path)
    data = sync_config_for_bot(account_id, data)
    _save_account_config_yaml(config_path, data)
    serial = (device_serial or data.get("device") or "").strip() or None

    # Use run.py (not `python -m GramAddict run`): the module entry point has a
    # strict argparse that only accepts --config, while run.py routes through
    # GramAddict's flexible parser that understands --device/--vpn-app-name.
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "run.py"),
        "--config",
        str(config_path),
    ]
    if serial:
        cmd.extend(["--device", serial])
    vpn_app = (vpn_app_name or "").strip() or None
    if vpn_app:
        cmd.extend(["--vpn-app-name", vpn_app])

    env = os.environ.copy()
    tools_dir = PROJECT_ROOT / "tools"
    if str(tools_dir) not in env.get("PYTHONPATH", ""):
        env["PYTHONPATH"] = f"{PROJECT_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    _bot_processes[account_id] = proc
    _bot_log_buffers[account_id] = [f"Started GramAddict for {account_id}"]

    async def _read_output() -> None:
        loop = asyncio.get_running_loop()
        assert proc.stdout is not None
        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            line = line.rstrip()
            _bot_log_buffers.setdefault(account_id, []).append(line)
            if len(_bot_log_buffers[account_id]) > 500:
                _bot_log_buffers[account_id] = _bot_log_buffers[account_id][-500:]
            if log_callback:
                await log_callback(account_id, line)

    asyncio.create_task(_read_output())
    return {
        "started": True,
        "account_id": account_id,
        "device": serial,
        "pid": proc.pid,
    }
