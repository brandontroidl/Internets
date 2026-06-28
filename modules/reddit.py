"""Top reddit post for a subreddit — wraps old.reddit.com JSON.

No API key required, but reddit aggressively 403s default User-Agents
so we use the configured weather_user_agent (which includes a contact
email).  Subreddit name is validated to ``[A-Za-z0-9_]{1,21}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.reddit")

_URL = "https://old.reddit.com/r/{sub}/top.json"
_VALID_SUB = re.compile(r"^[A-Za-z0-9_]{1,21}$")
_MAX_BODY_BYTES = 512 * 1024


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _fetch_sync(sub: str, period: str, ua: str) -> str:
    try:
        with requests.get(_URL.format(sub=sub),
                         params={"t": period, "limit": "1"},
                         headers={"User-Agent": ua, "Accept": "application/json"},
                         timeout=10, stream=True,
                         allow_redirects=False) as r:
            if r.status_code == 404:
                return f"no subreddit r/{sub}"
            if r.status_code == 403:
                return f"r/{sub} is private or quarantined"
            if r.status_code in (301, 302, 303):
                return f"r/{sub} redirected — likely private or banned"
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                return "reddit response too large"
            d = json.loads(body.decode("utf-8", errors="replace"))
            children = (d.get("data") or {}).get("children") or []
            if not children:
                return f"r/{sub} returned no posts"
            p = children[0].get("data", {})
            title = p.get("title", "?")
            score = p.get("score", 0)
            comments = p.get("num_comments", 0)
            author = p.get("author", "?")
            permalink = p.get("permalink", "")
            url = f"https://old.reddit.com{permalink}" if permalink else (p.get("url") or "")
            return _strip_ctrl(
                f"\x02r/{sub}\x02 top ({period}): {title} | "
                f"{score} pts, {comments} cmts by {author} | {url}"
            )
    except requests.RequestException as e:
        log.warning(f"reddit request: {e}")
        return "reddit unavailable"
    except Exception as e:
        log.warning(f"reddit parse: {e!r}")
        return "reddit response parse error"


class RedditModule(BotModule):
    """`.reddit <sub> [t]` — top post (t=hour/day/week/month/year/all)."""

    COMMANDS: dict[str, str] = {"reddit": "cmd_reddit", "r": "cmd_reddit"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_reddit(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}reddit <sub> [hour|day|week|month|year|all]")
            return
        parts = arg.strip().split()
        sub = parts[0].lstrip("/").removeprefix("r/")
        if not _VALID_SUB.match(sub):
            self.bot.privmsg(reply_to, f"{nick}: invalid subreddit name")
            return
        period = parts[1].lower() if len(parts) > 1 else "day"
        if period not in {"hour", "day", "week", "month", "year", "all"}:
            self.bot.privmsg(reply_to, f"{nick}: period must be hour|day|week|month|year|all")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, sub, period, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "reddit/.r <sub> [period]", "Top post from subreddit (period: hour/day/week/...)")]


def setup(bot: object) -> RedditModule:
    return RedditModule(bot)  # type: ignore[arg-type]
