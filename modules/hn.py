"""Hacker News top-stories lookup - wraps the Firebase HN API.

No API key required.  Two-step:
  - GET /v0/topstories.json     → array of story IDs
  - GET /v0/item/<id>.json      → story metadata

We fetch the first N IDs and pull the top story.
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.hn")

_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
_MAX_BODY_BYTES = 64 * 1024


def _strip_ctrl(s, max_len=400):
    return strip_ctrl(s, max_len)


def _get_json(url: str, ua: str) -> object | None:
    try:
        with requests.get(url, headers={"User-Agent": ua},
                          timeout=10, stream=True) as r:
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                return None
            return json.loads(body.decode("utf-8", errors="replace"))
    except (requests.RequestException, ValueError):
        return None


def _fetch_sync(rank: int, ua: str) -> str:
    ids = _get_json(_TOP, ua)
    if not isinstance(ids, list) or not ids:
        return "Hacker News unavailable"
    if rank < 1 or rank > len(ids):
        return f"rank out of range (1–{min(len(ids), 30)})"
    item = _get_json(_ITEM.format(id=ids[rank - 1]), ua)
    if not isinstance(item, dict):
        return "Hacker News item fetch failed"
    title = item.get("title", "?")
    url   = item.get("url", "") or f"https://news.ycombinator.com/item?id={item.get('id')}"
    by    = item.get("by", "?")
    score = item.get("score", 0)
    descs = item.get("descendants", 0)
    return _strip_ctrl(
        f"\x02HN #{rank}\x02 {title} | {score} pts, {descs} comments by {by} | {url}"
    )


class HnModule(BotModule):
    """`.hn [rank]` - top Hacker News story (rank 1–30, default 1)."""

    COMMANDS: dict[str, str] = {"hn": "cmd_hn"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_hn(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        rank = 1
        if arg and arg.strip():
            token = arg.strip().split()[0]
            if not token.isdigit():
                self.bot.privmsg(reply_to, f"{nick}: hn [rank]")
                return
            rank = int(token)
            if rank < 1 or rank > 30:
                self.bot.privmsg(reply_to, f"{nick}: rank must be 1–30")
                return
        text = await asyncio.to_thread(_fetch_sync, rank, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "hn [rank]", "Top Hacker News story (1–30)")]


def setup(bot: object) -> HnModule:
    return HnModule(bot)  # type: ignore[arg-type]
