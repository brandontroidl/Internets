"""Natural-language parsing for modules/claude.py.

Not a loadable module. Pure functions over strings: deciding whether a channel
line is addressed to the bot, and recognising the handful of requests the
module answers locally (facts, personas, timezone, clock) instead of sending
them to the API. Everything here is deterministic, so it is tested directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── addressing ───────────────────────────────────────────────────────


def addressed_text(line: str, botnick: str, mode: str) -> str | None:
    """Return the message body if *line* is addressed to *botnick*, else None.

    ``heuristic``: the nick must open the line ("Bot: hi", "Bot, hi",
    "Bot hi"). A mention anywhere else is talk *about* the bot and is ignored.
    ``off``: any mention of the nick counts, and the whole line is the body.
    """
    if not botnick:
        return None
    nick = re.escape(botnick)
    m = re.match(rf"^\s*@?{nick}(?:[:,>]|\s)\s*(.*)$", line, re.I)
    if m:
        return m.group(1).strip()
    if mode == "off" and re.search(rf"(?<![\w-]){nick}(?![\w-])", line, re.I):
        return line.strip()
    return None


_EMOTE_VERBS = (
    "pets", "pats", "hugs", "cuddles", "snuggles", "pokes", "boops", "kisses",
    "slaps", "smacks", "highfives", "high-fives", "waves", "winks", "dances",
    "twirls", "tickles", "headpats", "feeds", "nudges",
)
_EMOTE_RE = re.compile(r"^\s*(?:\*\s*\S+\s+)?(" + "|".join(_EMOTE_VERBS) + r")\b", re.I)


def emote_verb(action: str, botnick: str) -> str | None:
    """Return the verb if an action text ("pets Bot") targets the bot."""
    if not botnick or not re.search(rf"(?<![\w-]){re.escape(botnick)}(?![\w-])", action, re.I):
        return None
    m = _EMOTE_RE.match(action)
    return m.group(1).lower() if m else None


def plain_emote(line: str, botnick: str) -> str | None:
    """Recognise the text form "* User hugs Bot"; return the action text."""
    m = re.match(r"^\s*\*\s+\S+\s+(.+)$", line)
    if m and emote_verb(m.group(1), botnick):
        return m.group(1)
    return None


# ── local intents ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Intent:
    kind: str
    value: str = ""
    target: str = ""


_REMEMBER = re.compile(r"^remember\s+(?!when\b|how\b|that\b|the time\b)(.+)$", re.I | re.S)
_FORGET_ALL = re.compile(r"^forget\s+(?:everything|all)(?:\s+about\s+me)?\W*$", re.I)
_FORGET = re.compile(r"^forget\s+(?:about\s+)?(?!it\b|that\b)(.+?)\W*$", re.I)
_LIST_FACTS = re.compile(
    r"^what\s+do\s+you\s+(?:remember|know)\s+about\s+(\S+?)\W*$", re.I)
_PERSONA_ME = re.compile(
    r"^(?:reply|respond|talk|speak)\s+to\s+me\s+(?:as|like)\s+(.+)$", re.I)
_PERSONA_TARGET = re.compile(
    r"^(?:reply|respond|talk|speak)\s+to\s+(\S+)\s+(?:as|like)\s+(.+)$", re.I)
_PERSONA_CHANNEL = re.compile(
    r"^(?:(?:reply|respond|talk|speak)\s+(?:as|like)|from\s+now\s+on,?\s+"
    r"(?:be|act\s+like|talk\s+like|you\s+are))\s+(.+)$", re.I)
_PERSONA_RESET = re.compile(
    r"^(?:reset\s+(?:your\s+)?personality|stop\s+acting|be\s+yourself)"
    r"(?:\s+(?:in\s+this\s+channel|for\s+everyone))?\W*$", re.I)
_EVERYONE = re.compile(r"\s+(?:in\s+this\s+channel|for\s+everyone)\W*$", re.I)
_TZ = re.compile(
    r"^(?:i'?m\s+in|i\s+am\s+in|i\s+live\s+in|set\s+my\s+time\s*zone\s+to|"
    r"my\s+time\s*zone\s+is)\s+(?:the\s+)?([\w/+-]+)(?:\s+time(?:\s*zone)?)?\W*$", re.I)
_CLOCK = re.compile(
    r"^(?:i\s+prefer|use|set\s+(?:my\s+)?clock\s+to)\s+(12|24)\s*-?\s*h(?:ou)?r?s?"
    r"(?:\s+(?:time|clock))?\W*$", re.I)
_TIME_Q = re.compile(
    r"\b(?:what\s+time\s+is\s+it|what'?s\s+the\s+time|current\s+time|"
    r"what'?s\s+the\s+date|what\s+day\s+is\s+it|today'?s\s+date|"
    r"what\s+is\s+the\s+(?:time|date))\b", re.I)
_REVIEW = re.compile(
    r"^\s*\^\^\s*$|\b(?:thoughts|opinion|what\s+do\s+you\s+think|summari[sz]e|"
    r"give\s+me\s+your\s+take|opine|what'?s\s+being\s+discussed|"
    r"what'?s\s+(?:happening|going\s+on)|catch\s+me\s+up|fill\s+me\s+in|"
    r"what\s+did\s+i\s+miss|recap|tl;?dr|what\s+happened)\b", re.I)
_SOURCES = re.compile(
    r"\b(?:show\s+me\s+the\s+links|sources|citations|references|urls|include\s+links)\b",
    re.I)

TZ_WORDS = {"eastern": "est", "central": "cst", "mountain": "mst", "pacific": "pst"}


def local_intent(text: str) -> Intent | None:
    """Classify the requests the module handles itself. None means: ask Claude."""
    t = " ".join(text.split())
    if _FORGET_ALL.match(t):
        return Intent("forget_all")
    m = _LIST_FACTS.match(t)
    if m:
        return Intent("list_facts", target=m.group(1))
    m = _REMEMBER.match(t)
    if m and len(m.group(1).strip()) >= 5:
        return Intent("remember", m.group(1).strip())
    m = _FORGET.match(t)
    if m and len(m.group(1).strip()) >= 3:
        return Intent("forget", m.group(1).strip())
    if _PERSONA_RESET.match(t):
        return Intent("persona_reset", target="channel" if _EVERYONE.search(t) else "")
    m = _PERSONA_ME.match(t)
    if m:
        return Intent("persona_user", _EVERYONE.sub("", m.group(1)).strip())
    m = _PERSONA_TARGET.match(t)
    if m and m.group(1).lower() not in ("me", "everyone", "us"):
        return Intent("persona_target", m.group(2).strip(), target=m.group(1))
    m = _PERSONA_CHANNEL.match(t)
    if m:
        scope = "channel" if _EVERYONE.search(t) else ""
        return Intent("persona_channel", _EVERYONE.sub("", m.group(1)).strip(), target=scope)
    m = _TZ.match(t)
    if m:
        return Intent("timezone", m.group(1))
    m = _CLOCK.match(t)
    if m:
        return Intent("clock", m.group(1))
    if len(t.split()) <= 8 and _TIME_Q.search(t):
        return Intent("time")
    return None


def is_review(text: str) -> bool:
    return bool(_REVIEW.search(text))


def wants_sources(text: str) -> bool:
    return bool(_SOURCES.search(text))


# ── chime-in filters ──────────────────────────────────────────────────

_LAUGH = re.compile(r"\b(?:lol|lmao|lmfao|rofl|haha+|omg|wtf|kek)\b", re.I)
_GREETING = re.compile(r"^\s*(?:hi|hey|hello|yo|sup|hiya|howdy|morning|o/)\b[\s,]*\S+\W*$", re.I)


def is_laughter(text: str) -> bool:
    return bool(_LAUGH.search(text))


def is_greeting(text: str) -> bool:
    """A short greeting aimed at someone ("hey owo") - never chime in on it."""
    return bool(_GREETING.match(text))
