"""LLM assistant that answers free-form Telegram questions about the bot.

The Telegram poller (``dashboard/telegram_commands.py``) assembles a context
string (capabilities + live status + recent logs) and hands it to
:func:`answer_question` together with the owner's question. We reuse the same
OpenAI account key that powers the follow-vision feature, so no extra config is
required for accounts that already use AI.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Small, cheap, good enough to reason over logs. Overridable per account via
# telegram.yml -> telegram-ai-model.
DEFAULT_AI_MODEL = "gpt-4.1-mini"
MAX_REPLY_TOKENS = 450

SYSTEM_PROMPT = (
    "You are the assistant for a GramAddict Instagram-automation bot, embedded "
    "in the owner's private Telegram chat. Answer the owner's question using "
    "ONLY the CONTEXT provided (capabilities, live status, and recent logs).\n"
    "Rules:\n"
    "- Be concise and mobile-friendly: a few short lines, light Markdown "
    "(*bold*, `code`, bullet '-'). No tables.\n"
    "- An account is RUNNING only if the CURRENT STATUS section says so. Never "
    "infer that a bot is running from log lines alone — logs may be from an "
    "earlier session. If STATUS says Idle, report it as idle/stopped.\n"
    "- If the owner asks for the recent activity / log / what it's been doing, "
    "list the most recent log lines from the RECENT ACTIVITY LOG section "
    "(newest last) as short bullets — up to ~10 lines — rather than only "
    "summarising. Quote them, don't invent them.\n"
    "- Never invent numbers; quote figures straight from the context. If the "
    "context doesn't answer it, say what you can see and suggest opening the "
    "dashboard.\n"
    "- If asked what you can do or what commands exist, summarise the "
    "capabilities from the context.\n"
    "- Never reveal API tokens, chat IDs, or file paths."
)


def resolve_openai_key(account_ids: list[str]) -> str:
    """Return the first OpenAI key found across the given accounts.

    Reuses ``follow_vision``'s resolver, which reads ``openai-api-key`` from
    each account's ``follow_vision.yml`` (falling back to ``post_reel.yml``).
    """
    try:
        from GramAddict.core.follow_vision_account import _openai_api_key
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("Could not import OpenAI key resolver: %s", exc)
        return ""
    for account_id in account_ids:
        try:
            key = _openai_api_key(account_id)
        except Exception:
            key = ""
        if key:
            return key
    return ""


def answer_question(
    question: str,
    context: str,
    api_key: str,
    model: str = DEFAULT_AI_MODEL,
) -> Optional[str]:
    """Ask the LLM the owner's question with the assembled context.

    Returns the reply text, or ``None`` if the OpenAI SDK is unavailable or the
    call fails (the caller then sends a graceful fallback message).
    """
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.debug("openai package not installed; AI assistant disabled")
        return None
    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model or DEFAULT_AI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"CONTEXT:\n{context}\n\nQUESTION: {question}",
                },
            ],
            max_tokens=MAX_REPLY_TOKENS,
            temperature=0.3,
        )
        reply = (response.choices[0].message.content or "").strip()
        return reply or None
    except Exception as exc:
        logger.warning("Telegram AI assistant call failed: %s", exc)
        return None
