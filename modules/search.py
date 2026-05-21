from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any
from urllib.parse import quote_plus, unquote

import requests
from .base import BotModule, fetch_json

log = logging.getLogger("internets.search")

_TAG_RE = re.compile(r"<[^>]+>")
# DuckDuckGo HTML lite result extraction
_DDG_RESULT_RE = re.compile(
    r'<a\s+rel="nofollow"\s+class="result__a"\s+href="([^"]+)"[^>]*>\s*(.*?)\s*</a>',
    re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a\s+class="result__snippet"[^>]*>\s*(.*?)\s*</a>',
    re.DOTALL,
)
# DuckDuckGo redirect URL extraction
_DDG_UDDG_RE = re.compile(r'[?&]uddg=([^&]+)')


def _strip(s: str) -> str:
    return _TAG_RE.sub("", html.unescape(s)).strip()


def _extract_ddg_url(href: str) -> str:
    """Extract the real URL from DuckDuckGo's redirect wrapper."""
    m = _DDG_UDDG_RE.search(href)
    if m:
        return unquote(m.group(1))
    return href


# ── DuckDuckGo HTML Lite (no key required) ───────────────────────────────────

def _ddg_web(query: str, ua: str) -> str:
    """Search DuckDuckGo via the HTML lite endpoint (no API key needed)."""
    try:
        # `with` releases the socket on every exit path — a stream=True
        # response left unclosed leaks the connection / FD.
        with requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "us-en"},
            headers={
                "User-Agent": ua,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
            stream=True,
        ) as r:
            r.raise_for_status()
            # Cap the HTML at 512 KB to defend against a tampered response.
            raw = r.raw.read(512 * 1024 + 1, decode_content=True)
        if len(raw) > 512 * 1024:
            return f"[DuckDuckGo] response too large for '{query}'"
        body = raw.decode("utf-8", errors="replace")

        links = _DDG_RESULT_RE.findall(body)
        snippets = _DDG_SNIPPET_RE.findall(body)

        if not links:
            return f"[DuckDuckGo] no results for '{query}'"

        href, raw_title = links[0]
        title = _strip(raw_title)
        url = _extract_ddg_url(href)
        desc = _strip(snippets[0]) if snippets else ""
        if len(desc) > 200:
            desc = desc[:197] + "..."
        return (
            f"[DuckDuckGo] \x02{title}\x02 — {url}"
            + (f" | {desc}" if desc else "")
        )
    except Exception as e:
        log.debug(f"DDG web: {e}")
        raise


# ── Brave Search (keyed provider) ────────────────────────────────────────────

def _brave_web(query: str, key: str, ua: str) -> str:
    try:
        results = fetch_json(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": "3"},
            ua=ua,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": key,
            },
            timeout=10,
        ).get("web", {}).get("results", [])
        if not results:
            return f"[Brave] no results for '{query}'"
        top = results[0]
        title = _strip(top.get("title", "?"))
        url = top.get("url", "")
        desc = _strip(top.get("description", ""))
        if len(desc) > 200:
            desc = desc[:197] + "..."
        return f"[Brave] \x02{title}\x02 — {url}" + (f" | {desc}" if desc else "")
    except Exception as e:
        log.debug(f"Brave web: {e}")
        raise


def _brave_image(query: str, key: str, ua: str) -> str:
    try:
        results = fetch_json(
            "https://api.search.brave.com/res/v1/images/search",
            params={"q": query, "count": "3"},
            ua=ua,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": key,
            },
            timeout=10,
        ).get("results", [])
        if not results:
            return f"[Brave Image] no results for '{query}'"
        top = results[0]
        title = _strip(top.get("title", "?"))
        url = top.get("url", top.get("source", ""))
        w = top.get("properties", {}).get("width", "?")
        h = top.get("properties", {}).get("height", "?")
        return f"[Brave Image] \x02{title}\x02 — {url} | {w}x{h}px"
    except Exception as e:
        log.debug(f"Brave image: {e}")
        raise


# ── Dispatcher ───────────────────────────────────────────────────────────────

def _web_sync(query: str, brave_key: str, ua: str) -> str:
    """Try Brave (if keyed), fall back to DuckDuckGo.

    Both provider failures are logged at ``warning`` (not swallowed
    silently): without a log line an operator cannot tell a bad Brave
    key from a 429 from DuckDuckGo markup drift when ``.g`` "just fails".
    """
    if brave_key:
        try:
            return _brave_web(query, brave_key, ua)
        except Exception as e:
            log.warning("search: Brave web failed (%s) — falling back to DuckDuckGo",
                        type(e).__name__)
    try:
        return _ddg_web(query, ua)
    except Exception as e:
        log.warning("search: DuckDuckGo web failed: %s", type(e).__name__)
    return f"search failed for '{query}'"


def _image_sync(query: str, brave_key: str, ua: str) -> str:
    """Image search — requires a Brave API key."""
    if not brave_key:
        return "image search requires a Brave API key — see [search] in config.ini"
    try:
        return _brave_image(query, brave_key, ua)
    except Exception as e:
        log.warning("search: Brave image failed: %s", type(e).__name__)
        return f"image search failed for '{query}'"


# ── Module ───────────────────────────────────────────────────────────────────

class SearchModule(BotModule):
    """Web and image search via DuckDuckGo (+ optional Brave Search upgrade)."""

    COMMANDS: dict[str, str] = {
        "sw": "cmd_web",
        "g":  "cmd_web",
        "si": "cmd_image",
        "gi": "cmd_image",
    }

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._brave_key: str = cred(self.bot.cfg, "brave_key", "search", "brave_key")
        src = "Brave + DuckDuckGo" if self._brave_key else "DuckDuckGo"
        log.info(f"search: active providers: {src}")

    async def cmd_web(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Web search.  Usage: .sw <query>  (aliases: .g)"""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}sw <query>  e.g. {p}sw python asyncio")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_web_sync, arg.strip(), self._brave_key, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_image(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Image search.  Usage: .si <query>  (aliases: .gi)"""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}si <query>  e.g. {p}si sunset beach")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_image_sync, arg.strip(), self._brave_key, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}sw/.g <query>          Web search (DuckDuckGo)",
            f"  {prefix}si/.gi <query>         Image search (Brave API key required)",
        ]


def setup(bot: object) -> SearchModule:
    """Module entry point — returns a SearchModule instance."""
    return SearchModule(bot)  # type: ignore[arg-type]
