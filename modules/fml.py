from __future__ import annotations

import asyncio
import html
import logging
import random
import re

import requests
from .base import BotModule, help_row, strip_ctrl

# Bandit B311 false-positive - picking which scraped quote to print is
# not security-relevant, but routing through SystemRandom keeps scans
# clean without per-line ``# nosec``.
_rng = random.SystemRandom()

log = logging.getLogger("internets.fml")

# Match an FML article's BODY anchor on the random page.  The site
# moved to Tailwind in 2024-2025 and now renders each article with TWO
# links to the same /article/<slug>_<id>.html URL:
#
#   1. A bare category-title anchor like
#      ``<a href="/article/..._<id>.html">Magic underwear</a>``
#      (no class attribute, short curated tag-line)
#   2. The body anchor:
#      ``<a href=".._<id>.html" class="block text-blue-500 dark:text-white my-4 [spicy-hidden]">
#         Today, I ... FML
#       </a>``
#
# Anchoring the regex on the ``block text-blue-500`` class signature
# (always present on the body link, absent from the title link) ensures
# we capture the full quote, not the category tag-line.
_FML_ARTICLE = re.compile(
    r'<a\s+href="/article/[^"]*?_(\d+)\.html"\s+'
    r'class="[^"]*block text-blue-500[^"]*"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE   = re.compile(r'<[^>]+>')
_WS_RE    = re.compile(r'\s+')


def _strip_tags(s: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", html.unescape(s))).strip()


def _lookup_sync(ua: str) -> str:
    """Fetch a random FML quote - blocking, run via asyncio.to_thread."""
    try:
        # `with` releases the socket on every exit path - a stream=True
        # response left unclosed leaks the connection / FD.
        with requests.get(
            "https://www.fmylife.com/random",
            headers={
                "User-Agent": ua,
                "Accept": "text/html",
            },
            timeout=15,
            stream=True,
        ) as r:
            r.raise_for_status()
            # Cap the page at 512 KB - FML's /random is normally ~200 KB.
            body = r.raw.read(512 * 1024 + 1, decode_content=True)
        if len(body) > 512 * 1024:
            return "fmylife.com response too large"
        text = body.decode("utf-8", errors="replace")
        raw_matches = _FML_ARTICLE.findall(text)
        if not raw_matches:
            return "could not parse FML page - site layout may have changed"

        # Filter to real user posts.  FML's /random page occasionally
        # serves editorial compilation articles ("Welcome to the
        # machine", category roundups, etc.) that share the same body
        # anchor structure but lack the universal "Today, … FML" shape
        # of user submissions.  Drop anything that doesn't start with
        # "Today" (case-insensitive after strip).
        candidates: list[tuple[str, str]] = []
        for qid, raw in raw_matches:
            text = _strip_tags(raw)
            if text.lower().startswith("today"):
                candidates.append((qid, text))

        if not candidates:
            # Graceful fallback: every page should normally have at
            # least one user post.  If literally none do, return the
            # first raw match so the operator sees *something* and the
            # log records the unusual case.
            log.warning("FML page had no 'Today...' user posts among %d matches",
                        len(raw_matches))
            qid, raw = raw_matches[0]
            candidates = [(qid, _strip_tags(raw))]

        qid, text = _rng.choice(candidates)
        if len(text) > 400:
            text = text[:397] + "..."
        # Scraped quote text is third-party - strip IRC control bytes before
        # it hits the channel.  qid is digits from the regex, so it's safe.
        return f"[fml #{qid}] {strip_ctrl(text)}"
    except Exception as e:
        log.warning(f"FML lookup: {e}")
        return "fmylife.com is temporarily unavailable"


class FmlModule(BotModule):
    """FMyLife (fmylife.com) random quote module."""

    COMMANDS: dict[str, str] = {"fml": "cmd_fml"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    async def cmd_fml(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Show a random FMyLife quote."""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        result = await asyncio.to_thread(_lookup_sync, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "fml", "Random FMyLife quote")]


def setup(bot: object) -> FmlModule:
    """Module entry point - returns a FmlModule instance."""
    return FmlModule(bot)  # type: ignore[arg-type]
