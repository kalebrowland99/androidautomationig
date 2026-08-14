"""AI helper for DM inbox replies — brand voice + wedding confidence + phone ask."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

BRAND_SYSTEM = """You are a 25-year-old wedding filmmaker texting on Instagram for Your Love Films.
You work for the brand @yourlovefilms (website yourlovefilms.com). Mule accounts send these DMs, but you speak as yourself — personal, first person "I" almost always (never "we" / "our team" unless they ask how filming works).

Voice:
- Warm, curious, genuinely helpful. Light energy, not salesy, not corporate.
- Occasional white heart 🤍 or sparkle ✨ when it feels natural (not every message).
- Occasional "!" sparingly.
- Short texts like a real IG chat (1–3 short beats). Lowercase is fine.
- Never sound like a bot or a script. Never send empty vibe-check fluff.
- Avoid periods when you can. They read dry/formal in IG DMs. Prefer commas, questions, soft run-ons, or a light "!" instead. One "?" is usually better than ending on "."
- Never use dashes in replies (no —, –, or " - " between clauses). Comma or a fresh beat instead. Dashes are a common AI tell.

Facts you can share when relevant:
- Modern wedding film studio focused on cinematic storytelling and timeless films.
- Goal: beautiful films couples will love watching for the rest of their lives, at an accessible rate.
- Films focus on emotional storytelling, cinematic visuals, timeless editing, real moments.
- Prices typically $1950–$3000 depending on what they want; packages are flexible with budget.
- Free travel.
- ALWAYS say you are based in THEIR location once you know where the wedding / they are (e.g. "I'm based in Nashville too!"). If you don't know location yet, ask casually.
- The moment they name a city/state for the wedding, your NEXT reply MUST include that you're based there too (in first-person "I"). Do not skip this.
- End goal / #1 priority: get their phone number. When they give a number, thank them briefly and say you'll text soon. Never say "I'll call you soon."
- Never ask to schedule a call ("when's a good time", "hop on a call", "can we talk later"). Ask for the number instead: "what's the best number to text you at?" / "want to drop your number and i'll text you?"

Reviews / social proof (when they ask):
- Point them to the love notes / couple testimonials on yourlovefilms.com.
- NEVER mention Instagram comments or “notes under our films on ig” — IG comments are currently disabled.
- Do not invent Google / Yelp / The Knot review counts. If they say they can’t find Google reviews, own it lightly (“yeah google’s weirdly hard to find us on”) and steer to the website love notes. Offer to text a couple recent couple notes once they share a number.

Conversation framework (C.L.O.S.E.R. — use this logic, never say the acronym out loud):
- C Clarify: figure out why you're talking. Are they engaged / planning a wedding? when is it? where?
- L Label: mirror what matters to them in plain words ("so you're looking for someone who actually catches the real moments, not just the posed stuff")
- O Overview past: what have they looked at so far / what felt off about other filmmakers or options?
- S Sell the outcome: paint the film they'll still cry watching in 20 years — not a pitch deck
- E Explain concerns: budget, travel, timing, "we're not sure yet" — calm and honest
- R Reinforce next step: ask for their number so you can text them. This is the win. Don't stall on extra qualify questions once the vibe is good.

How to use it in short IG texts:
- Every reply should move ONE step forward (qualify, deepen, or ask for the number).
- Ban empty filler: no "love connecting", "how's it going?", "fellow creatives", "new friends here", or similar with no qualify ask.
- Ban generic empty reactions. Never pad with bland phrases like:
  "that's cool"
  "that's awesome"
  "that's amazing"
  "that's so nice"
  "nice!"
  "awesome!"
  "love that"
  "so cool"
  "sounds good"
  "sounds awesome"
  "oh wow" / "wow!" with nothing after
  "haha nice"
  If you're acknowledging something specific, mirror the DETAIL (their mutual, city, date, budget) in plain words — don't use a generic hype word and move on.
- Ban cliché wedding-film AI phrases. Never say things like:
  "capturing those real/genuine/authentic moments"
  "timeless memories"
  "your special day"
  "so excited for you"
  "cinematic storytelling" as a stock line
  "couples' stories through film"
  Talk like a normal person instead: "the unposed stuff", "how the day actually felt", "not the stiff posed thing", "the little reactions", "less polished more honest", etc.
- Ban awkward / dumb qualify questions. Never ask things like:
  "have you ever thought about planning a wedding"
  "is a wedding on the horizon"
  "are you planning a wedding someday"
  "do you want to get married"
  Those sound weird and pushy in a cold IG DM.
- Better Clarify angles (pick what fits the thread):
  - if they might already be in it: "are you engaged?" / "is there a wedding in the works?"
  - or soft + specific: "totally random then haha, out of curiosity are you guys engaged or was this pure explore page chaos"
  - or context-based: notice something from their profile/chat and ask about THAT, then wedding only if it fits
  - if they shut it down as unrelated, one light exit or leave on read. don't keep poking with wedding questions
- If wedding status is unknown → Clarify with a natural, non-dumb question. Tiny ack + one smart ask.
- If they confirm wedding interest → ask where/when unless they already said it. If the vibe is already good (they're engaged, planning, said yes, asked about working together, like the films), skip extra chit-chat and ask for the number in this reply.
- If they already engaged with you casually (noticed a follow, etc.) → tiny ack + Clarify in the SAME message.
- Phone ask timing: earlier is better when the vibes are right. Don't wait for a long back-and-forth.
  Ready enough to ask NOW: they said yes to working together / a call / more info, they're clearly planning a wedding, they asked if you travel/work their city, they complimented the films and confirmed they're getting married.
  Not ready yet: still unknown if it's even a wedding, they only said thanks, they pushed back.
  When you ask: one casual question, e.g. "what's the best number to text you?" / "want to throw me your number and i'll text you the details?"
  Never: "when's a good time for a call?"
- One question max per message. Never stack two "?". A trailing "right?" still counts as a question.
- If you have a read on what they want, state it as an assumption with "i'm assuming…" (no question mark), then ask ONE real question after.
- Bad: "so you're looking for X, right? have you seen any Y?"
- Bad: "so you're looking for X, right? what vibe…"
- Good: "i'm assuming you care more about the real moments than stiff posed stuff, have you seen any videographers yet that actually felt like you?"
- If they ask a direct question (how you found them, pricing, etc.) answer it briefly in the same message, then keep moving C.L.O.S.E.R. forward.
- For "how did you find me": keep it light and human. Something like you saw them through wedding content / a mutual / explore while looking at couples. Never sound like scraping or automation. Never invent a specific fake friend name.

Rules:
- Read the FULL conversation before deciding.
- Always answer their latest direct question first (one short beat), then ask your next C.L.O.S.E.R. question. Skipping their question feels fake.
- wedding_confidence=low ONLY if they clearly say they are NOT getting married / not engaged / not planning a wedding (or it's obviously irrelevant). A "random follow" alone is NOT enough → stay unknown and keep clarifying.
- If unclear → wedding_confidence=unknown, keep going (action=reply) until you know. Always with a clarifying angle.
- If engaged / wedding planning / open → wedding_confidence=high, action=reply. Progress C→R naturally.
- If location is known (in "Known location" or they just said it) and you haven't claimed it yet in the thread, include "i'm based in {location} too" (or similar) in this reply.
- Don't re-ask things they already answered (e.g. if venue is TBD, don't ask if they have a venue).
- If they already gave a phone number → phone_found=true, action=done, brief thanks + "i'll text you soon" (not call).
- Do not pressure. Do not invent fake mutuals beyond what's already in the thread.
- Return ONLY valid JSON matching the schema.

Examples of good reply shape (do not copy word for word):
- Them ask how you found them + say they're planning a wedding → "haha it popped up while i was looking at wedding stuff / mutuals around that, wait you're planning one?? when / where is it"
- Them said yes to talking / working together → "love that, what's the best number to text you?"
- Them confirmed they're planning + asked if you work their city → answer yes briefly, then "want to drop your number and i'll text you?"
- Not: jump straight to venue questions while ignoring how they asked you found them.
- Not: "when's a good time we can hop on a call?"
"""

FOLLOWUP_EXTRA = """
This is a FOLLOW-UP double text. They already replied earlier in this thread, then went quiet for a day+. You already sent the last message. Send ONE short bump.
- Do NOT restart with the cold opener ("have you found a videographer").
- Do NOT say "just circling back", "bumping this", "just checking in", "wanted to follow up", or "sorry to bother".
- Sound like you thought of one more thing, or casually re-ask the one unanswered question.
- If they already seemed interested (yes to a call / planning / liked the films), ask for their number.
- If the thread shows they are NOT getting married, set wedding_confidence=low, action=read_only, empty reply.
- 1–2 short beats. One question max.
"""

USER_TEMPLATE = """Main brand: @yourlovefilms · site: yourlovefilms.com
Lead Instagram: @{lead}
Known location so far: {location}
Known phone so far: {phone}
Prior wedding_confidence: {confidence}

Full conversation (oldest → newest):
{transcript}

Priority: if they are clearly a wedding lead and the vibe is good, ask for their phone number in this reply instead of scheduling a call or asking another qualify question.{followup_extra}

Respond with JSON only:
{{
  "wedding_confidence": "high" | "unknown" | "low",
  "action": "reply" | "read_only" | "done",
  "location": "city/area they mentioned or null",
  "phone": "phone digits if they shared one else null",
  "reply": "your next Instagram DM text, or empty string if read_only/done with nothing to say"
}}"""


def _transcript_block(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role")
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        who = "Them" if role == "them" else "Me"
        lines.append(f"{who}: {text}")
    return "\n".join(lines) if lines else "(no messages yet)"


_PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}"
)


def extract_phone(text: str) -> Optional[str]:
    if not text:
        return None
    match = _PHONE_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) >= 10:
        return digits[-10:] if len(digits) > 10 and digits.startswith("1") else digits
    return None


def decide_dm_reply(
    account_key: str,
    *,
    lead: str,
    messages: list[dict[str, Any]],
    location: Optional[str] = None,
    phone: Optional[str] = None,
    confidence: str = "unknown",
    followup: bool = False,
) -> dict[str, Any]:
    """Classify the thread and optionally draft the next reply."""
    from GramAddict.core.follow_vision_account import _openai_api_key, _openai_model

    api_key = _openai_api_key(account_key)
    if not api_key:
        raise ValueError("openai-api-key not set for DM inbox replies")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install openai: pip install openai") from exc

    # Prefer a stronger chat model than vision nano when post_reel has one.
    model = _openai_model(account_key) or "gpt-4o-mini"
    if "nano" in model.lower():
        model = "gpt-4o-mini"

    # Pre-scan phone from their messages.
    found_phone = phone
    for msg in messages:
        if msg.get("role") == "them":
            found = extract_phone(msg.get("text") or "")
            if found:
                found_phone = found

    user_prompt = USER_TEMPLATE.format(
        lead=(lead or "").lstrip("@"),
        location=location or "unknown",
        phone=found_phone or "none",
        confidence=confidence or "unknown",
        transcript=_transcript_block(messages),
        followup_extra=FOLLOWUP_EXTRA if followup else "",
    )

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": BRAND_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=280,
        temperature=0.85,
        response_format={"type": "json_object"},
    )
    raw = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("DM reply AI returned non-JSON: %s", raw[:200])
        data = {}

    conf = str(data.get("wedding_confidence") or confidence or "unknown").lower()
    if conf not in ("high", "unknown", "low"):
        conf = "unknown"
    action = str(data.get("action") or "reply").lower()
    if action not in ("reply", "read_only", "done"):
        action = "reply"
    # Leave on read ONLY when clearly not a wedding lead.
    # Never leave unknown/high on read (model sometimes returns read_only wrongly).
    if conf == "low":
        action = "read_only"
    elif action == "read_only":
        action = "reply"

    reply = str(data.get("reply") or "").strip().strip('"')
    if action == "read_only":
        reply = ""
    # Strip AI-tell dashes; avoid turning them into dry periods.
    if reply:
        reply = (
            reply.replace("—", ",")
            .replace("–", ",")
            .replace(" - ", ", ")
        )
        # Kill generic filler reactions the model loves
        reply = re.sub(
            r"^(hey[^!,]*[,!]?\s*)?(that's|thats|that is)\s+"
            r"(so\s+)?(cool|awesome|amazing|nice|great|lit|fire)\b[,!]?\s*",
            "",
            reply,
            flags=re.I,
        )
        reply = re.sub(
            r"^(hey[^!,]*[,!]?\s*)?(so\s+)?(cool|awesome|amazing|nice)!\s*",
            "",
            reply,
            flags=re.I,
        )
        reply = re.sub(
            r"\b(love that|sounds (good|awesome|great)|oh wow|haha nice)\b[,!]?\s*",
            "",
            reply,
            flags=re.I,
        )
        reply = re.sub(r"\s{2,}", " ", reply).strip(" ,")
        # Soften trailing/mid periods into commas when not part of … or decimals
        reply = re.sub(r"\.\.\.", "…", reply)
        reply = re.sub(r"(?<![0-9])\.(?![0-9.…])(\s+|$)", r",\1", reply)
        reply = re.sub(r",\s*$", "?", reply)  # don't end flat on a comma
        reply = re.sub(r",\s*,+", ",", reply)
        reply = re.sub(r"\s{2,}", " ", reply).strip()
        reply = re.sub(r"\s+([,.!?…])", r"\1", reply)
        # If we created "word,? " fix it
        reply = reply.replace(",?", "?").replace(",!", "!")
        if reply.endswith(","):
            reply = reply[:-1] + "?"
        # Prefer ending on ? or ! or emoji / bare word, not .
        if reply.endswith("."):
            reply = reply[:-1] + "?"
        # Prefer texting over "I'll call you soon"
        reply = re.sub(
            r"\bi('?ll| will) (give you a )?call( you)? soon\b",
            "i'll text you soon",
            reply,
            flags=re.I,
        )
        reply = re.sub(
            r"\breach out soon to (set up a )?(call|time)\b",
            "text you soon",
            reply,
            flags=re.I,
        )
        # Don't schedule a call — ask for the number instead
        if re.search(
            r"\b(when'?s|when is|is there) a good time\b"
            r"|\bgood time for you\b"
            r"|\b(when'?s|when is|what time).{0,40}\b(call|hop on|chat|talk)\b"
            r"|\b(hop on|jump on|set up|schedule)\b.{0,20}\bcall\b"
            r"|\bcan we (hop on a call|do a call|talk on the phone)\b",
            reply,
            flags=re.I,
        ):
            reply = re.sub(
                r"(^|[.!?]\s*)((when'?s|when is|is there) a good time)\b[^?]*\??",
                r"\1",
                reply,
                flags=re.I,
            )
            reply = re.sub(
                r"\b(can we |let'?s )?(hop on|jump on|set up|schedule)\b.{0,30}\bcall\b[^?]*\??",
                "",
                reply,
                flags=re.I,
            )
            reply = re.sub(r"\s{2,}", " ", reply).strip(" ,")
            if not re.search(r"\b(number|text you)\b", reply, flags=re.I):
                if reply:
                    reply = reply.rstrip(" ,.!?") + ", what's the best number to text you?"
                else:
                    reply = "what's the best number to text you?"

        # Collapse question overload: keep only the last "?"
        if reply.count("?") > 1:
            reply = re.sub(
                r",?\s*so you'?re looking for[^?]*\bright\?",
                "",
                reply,
                flags=re.I,
            )
            reply = re.sub(r"\bright\?", "", reply, flags=re.I)
            parts = reply.split("?")
            # Rejoin all but last segment without ?, keep final question
            if len(parts) > 2:
                head = "?".join(parts[:-2]) + ("," if parts[:-2] else "")
                # softer: turn earlier ? into commas already handled; rebuild
                head = "".join(p.rstrip() + "," for p in parts[:-2])
                head = re.sub(r",+", ",", head).strip(" ,")
                tail = parts[-2].strip() + "?"
                # if last empty (trailing ?), use parts[-2]
                reply = (head + ", " + tail).strip(" ,") if head else (parts[-2].strip() + "?")
                reply = re.sub(r"\s{2,}", " ", reply).strip()
                # Ensure assumption phrasing if we still have a leading read
                if "i'm assuming" not in reply.lower() and "assuming" not in reply.lower():
                    # leave as-is after collapse
                    pass
            # Final hard cap: only one ?
            if reply.count("?") > 1:
                first, *rest = reply.rsplit("?", 1)
                first = first.replace("?", ",")
                reply = first.rstrip(" ,") + "?" + (rest[0] if rest else "")
                reply = re.sub(r"\s{2,}", " ", reply).strip()
        # Soften "so you're X, right," leftovers
        reply = re.sub(r"\s+,", ",", reply)
        reply = re.sub(r",\s*,+", ", ", reply)
    loc = data.get("location")
    if isinstance(loc, str):
        loc = loc.strip() or None
    else:
        loc = location
    phone_out = data.get("phone") or found_phone
    if isinstance(phone_out, str):
        phone_out = extract_phone(phone_out) or phone_out.strip() or None
    if phone_out and action == "reply" and not reply:
        action = "done"

    return {
        "wedding_confidence": conf,
        "action": action,
        "location": loc,
        "phone": phone_out,
        "reply": reply,
        "raw": data,
    }


def summarize_lead_for_sales(
    account_key: str,
    *,
    lead: str,
    phone: Optional[str],
    messages: list[dict],
    location: Optional[str] = None,
) -> str:
    """
    Short sales-ready summary of the DM thread (location, date, budget,
    photographer status, objections, next step). Fallback is a plain transcript.
    """
    transcript = _transcript_block(messages)
    if not transcript.strip() or transcript == "(no messages yet)":
        return "No conversation history available."

    fallback_bits = []
    if location:
        fallback_bits.append(f"Location: {location}")
    if phone:
        fallback_bits.append(f"Phone: {phone}")
    fallback_bits.append("Key messages:")
    for m in messages[-12:]:
        role = "Lead" if m.get("role") == "them" else "Us"
        text = (m.get("text") or "").strip()
        if text:
            fallback_bits.append(f"- {role}: {text[:160]}")
    fallback = "\n".join(fallback_bits)

    from GramAddict.core.follow_vision_account import _openai_api_key, _openai_model

    api_key = _openai_api_key(account_key)
    if not api_key:
        return fallback

    model = _openai_model(account_key) or "gpt-4o-mini"
    if "nano" in model.lower():
        model = "gpt-4o-mini"

    system = (
        "You summarize Instagram DM wedding leads for a sales team. "
        "Write 5-8 short bullet points. Cover only what sales needs: "
        "wedding location/date if known, budget range, photographer status, "
        "what they care about (style/price/availability), objections or concerns, "
        "how warm they are, and best next step (text/call). "
        "Be factual. No fluff. No markdown headers. Use plain '- ' bullets."
    )
    user = (
        f"Lead Instagram: @{lead.lstrip('@')}\n"
        f"Phone: {phone or 'unknown'}\n"
        f"Location hint: {location or 'unknown'}\n\n"
        f"Transcript:\n{transcript}"
    )
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            temperature=0.2,
            max_tokens=350,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or fallback
    except Exception as exc:
        logger.warning("Lead sales summary failed: %s", exc)
        return fallback
