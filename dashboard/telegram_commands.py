"""Poll Telegram for status commands and reply with bot progress."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

from GramAddict.core.live_progress import load_live_progress
from GramAddict.plugins.telegram import (
    _calculate_session_duration,
    load_sessions,
    telegram_bot_send_text,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = PROJECT_ROOT / "accounts"
LOGS_DIR = PROJECT_ROOT / "logs"
OFFSET_FILE = PROJECT_ROOT / ".telegram_update_offsets.json"

STATUS_WORDS = frozenset(
    {"status", "update", "progress", "/status", "/update", "/progress", "stats"}
)
HELP_WORDS = frozenset({"help", "/help", "/start", "commands"})

POLL_SECONDS = 4.0


@dataclass
class TelegramAccount:
    account_id: str
    username: str
    chat_id: str
    status_commands: bool = True


@dataclass
class TelegramBotListener:
    token: str
    accounts: list[TelegramAccount] = field(default_factory=list)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _load_offsets() -> dict[str, int]:
    if not OFFSET_FILE.is_file():
        return {}
    try:
        data = json.loads(OFFSET_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def _save_offsets(offsets: dict[str, int]) -> None:
    try:
        OFFSET_FILE.write_text(json.dumps(offsets, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not save Telegram offsets: %s", exc)


def _telegram_get_updates(token: str, offset: int = 0) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=10,
        )
        payload = response.json()
    except Exception as exc:
        logger.debug("Telegram getUpdates failed: %s", exc)
        return []
    if not payload.get("ok"):
        return []
    result = payload.get("result")
    return result if isinstance(result, list) else []


def _parse_command(text: str) -> tuple[str, Optional[str]]:
    cleaned = (text or "").strip()
    if not cleaned:
        return "", None
    parts = cleaned.split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1].strip().lstrip("@") if len(parts) > 1 else None
    return command, arg or None


def _format_elapsed(started_at: Optional[str]) -> str:
    if not started_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at)
        minutes = max(0, int((datetime.now() - start).total_seconds() / 60))
    except ValueError:
        return ""
    if minutes < 60:
        return f"{minutes}m"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h {rem}m"


def _tail_log_lines(username: str, *, max_lines: int = 6) -> list[str]:
    path = LOGS_DIR / f"{username}.log"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    picked: list[str] = []
    for line in reversed(lines[-80:]):
        text = line.strip()
        if not text:
            continue
        if " | " in text:
            text = text.split(" | ", 1)[-1].strip()
        text = re.sub(r"\s*\([^)]+\)\s*$", "", text)
        if len(text) < 8:
            continue
        picked.append(text)
        if len(picked) >= max_lines:
            break
    return list(reversed(picked))


def _account_running(account_id: str) -> bool:
    from dashboard.gramaddict_config import _account_bot_running

    return _account_bot_running(account_id)


def _recent_dashboard_logs(account_id: str, *, max_lines: int = 6) -> list[str]:
    from dashboard.gramaddict_config import _bot_log_buffers

    lines = _bot_log_buffers.get(account_id, [])[-max_lines:]
    return [line.strip() for line in lines if line.strip()]


def _format_account_status(account: TelegramAccount) -> str:
    username = account.username or account.account_id
    running = _account_running(account.account_id)
    live = load_live_progress(username) or load_live_progress(account.account_id)
    lines: list[str] = []

    if running or (live and live.get("running")):
        elapsed = _format_elapsed(
            (live or {}).get("session_started_at") or (live or {}).get("updated_at")
        )
        job = (live or {}).get("current_job") or "session"
        header = f"*{username}* — Running"
        if elapsed:
            header += f" ({elapsed})"
        lines.append(header)
        if live:
            likes = live.get("total_likes", 0)
            follows = live.get("total_followed", 0)
            watches = live.get("total_watched", 0)
            comments = live.get("total_comments", 0)
            limits = live.get("limits") or {}
            likes_lim = limits.get("likes")
            stats = [f"Likes {likes}" + (f"/{likes_lim}" if likes_lim else "")]
            if follows:
                stats.append(f"Follows {follows}")
            if watches:
                stats.append(f"Stories {watches}")
            if comments:
                stats.append(f"Comments {comments}")
            lines.append(f"• Job: `{job}`")
            lines.append(f"• {' · '.join(stats)}")
        else:
            lines.append("• Session active (waiting for progress snapshot)")
    else:
        lines.append(f"*{username}* — Idle")
        sessions = load_sessions(username) or load_sessions(account.account_id)
        if sessions:
            last = sessions[-1]
            duration = _calculate_session_duration(last)
            finish = last.get("finish_time") or last.get("start_time") or ""
            when = ""
            if finish:
                try:
                    parsed = datetime.strptime(finish[:26], "%Y-%m-%d %H:%M:%S.%f")
                    when = parsed.strftime("%I:%M %p")
                except ValueError:
                    try:
                        parsed = datetime.strptime(finish[:19], "%Y-%m-%d %H:%M:%S")
                        when = parsed.strftime("%I:%M %p")
                    except ValueError:
                        when = finish[:16]
            lines.append(
                f"• Last session{f' ({when})' if when else ''}: "
                f"{last.get('total_likes', 0)} likes, "
                f"{last.get('total_followed', 0)} follows, "
                f"{last.get('total_watched', 0)} stories"
                + (f", {duration}m" if duration else "")
            )
        else:
            lines.append("• No completed sessions yet")

    recent = _recent_dashboard_logs(account.account_id) or _tail_log_lines(username)
    if recent:
        lines.append("• Recent:")
        for entry in recent[-4:]:
            lines.append(f"  - {entry[:180]}")

    return "\n".join(lines)


def _build_status_reply(accounts: list[TelegramAccount], account_hint: Optional[str]) -> str:
    enabled = [acct for acct in accounts if acct.status_commands]
    if not enabled:
        return "Telegram status commands are disabled for this account."

    if account_hint:
        hint = account_hint.lower()
        matched = [
            acct
            for acct in enabled
            if acct.account_id.lower() == hint
            or acct.username.lower() == hint
        ]
        if not matched:
            names = ", ".join(f"@{acct.username or acct.account_id}" for acct in enabled)
            return f"No account matched *{account_hint}*. Try: {names}"
        targets = matched
    else:
        targets = enabled

    if len(targets) == 1:
        body = _format_account_status(targets[0])
    else:
        parts = [_format_account_status(acct) for acct in targets]
        body = "\n\n".join(parts)

    return (
        f"{body}\n\n"
        "_Reply_ `status` _or_ `update` _anytime. "
        "Add an account name like_ `status 615films` _when you have several._"
    )


def _build_help_reply() -> str:
    return (
        "*GramAddict status bot*\n\n"
        "Text any of these while the dashboard is running:\n"
        "• `status` or `update` — current progress\n"
        "• `status ACCOUNT` — one account only\n"
        "• `help` — this message\n\n"
        "Turn off in dashboard → Reports → *Allow Telegram status commands*."
    )


def discover_telegram_listeners() -> list[TelegramBotListener]:
    if not ACCOUNTS_DIR.is_dir():
        return []
    by_token: dict[str, TelegramBotListener] = {}
    for folder in sorted(ACCOUNTS_DIR.iterdir()):
        if not folder.is_dir() or not (folder / "config.yml").is_file():
            continue
        account_id = folder.name
        tg = _load_yaml(folder / "telegram.yml")
        token = str(tg.get("telegram-api-token") or "").strip()
        chat_id = str(tg.get("telegram-chat-id") or "").strip()
        if not token or not chat_id:
            continue
        config = _load_yaml(folder / "config.yml")
        username = str(config.get("username") or account_id).strip()
        status_commands = tg.get("telegram-status-commands", True) is not False
        listener = by_token.setdefault(token, TelegramBotListener(token=token))
        listener.accounts.append(
            TelegramAccount(
                account_id=account_id,
                username=username,
                chat_id=chat_id,
                status_commands=status_commands,
            )
        )
    return list(by_token.values())


def _handle_message(listener: TelegramBotListener, message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = message.get("text") or ""
    command, arg = _parse_command(text)
    if not command:
        return

    matching = [
        acct for acct in listener.accounts if acct.chat_id == chat_id
    ]
    if not matching:
        return

    if command in HELP_WORDS:
        reply = _build_help_reply()
    elif command in STATUS_WORDS:
        reply = _build_status_reply(matching, arg)
    else:
        return

    telegram_bot_send_text(listener.token, chat_id, reply)


class TelegramCommandService:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="telegram-command-poller",
            daemon=True,
        )
        self._thread.start()
        logger.info("Telegram status command listener started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        offsets = _load_offsets()
        while not self._stop.is_set():
            listeners = discover_telegram_listeners()
            if not listeners:
                self._stop.wait(POLL_SECONDS)
                continue
            for listener in listeners:
                if self._stop.is_set():
                    break
                offset = offsets.get(listener.token, 0)
                updates = _telegram_get_updates(listener.token, offset)
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    offsets[listener.token] = max(offset, update_id + 1)
                    offset = offsets[listener.token]
                    message = update.get("message") or update.get("edited_message")
                    if isinstance(message, dict):
                        try:
                            _handle_message(listener, message)
                        except Exception as exc:
                            logger.debug("Telegram command handler error: %s", exc)
                if updates:
                    _save_offsets(offsets)
            self._stop.wait(POLL_SECONDS)


telegram_command_service = TelegramCommandService()
