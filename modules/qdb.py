"""Quote-database lookup module — scrapes bash-org-archive.com.

The classic QDB protocol (RSS-1.0 over ``process.php?action=random&fixed=0``)
is dead network-wide as of 2026: qdb.us serves an unrelated site, bash.org
is offline, the original mirrors don't resolve.  The closest living
spiritual successor is bash-org-archive.com — an HTML-only read-only
archive of the original bash.org quotes.

This module HTML-scrapes that archive.  No XML parsing, no defusedxml
dependency.  The output shape on IRC is unchanged: ``[qdb #N] line``
followed by up to ``_MAX_LINES`` quote lines, or a "too long — view at"
fallback link.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

import requests
from .base import BotModule

log = logging.getLogger("internets.qdb")

# Default endpoint.  Operators can override via [qdb] api_url in config.ini
# if a better mirror appears later.  Strip trailing slashes so the URL
# builder doesn't double them up.
_DEFAULT_URL = "https://bash-org-archive.com"

_MAX_LINES = 5
_MAX_BODY_BYTES = 256 * 1024

# Same IRC-control-byte strip used elsewhere — defends against vandalised
# quotes injecting CR/LF or IRC formatting/colour codes into the channel.
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


# HTML extractors anchored on bash-org-archive's stable markup:
#   <p class="quote">#36737</p>     ← quote header (contains the ID)
#   <p class="qt">line<br>line</p>  ← the quote body, IRC lines separated
#                                     by newlines and/or <br>
_RE_QUOTE_HEADER = re.compile(r'<p\s+class="quote"[^>]*>([^<]*)</p>',
                              re.IGNORECASE)
_RE_QUOTE_BODY   = re.compile(r'<p\s+class="qt"[^>]*>(.*?)</p>',
                              re.IGNORECASE | re.DOTALL)
_RE_TAGS         = re.compile(r'<[^>]+>')
_RE_LINE_BREAK   = re.compile(r'<br\s*/?>|\n')


def _lookup_sync(qid: str | None, base_url: str, ua: str) -> list[str]:
    """Blocking scrape — invoked via ``asyncio.to_thread``.

    ``?random1`` returns a random quote (the classic bash.org URL form).
    ``?<id>`` returns a specific numeric quote.  Returns the formatted
    output lines (already trimmed to the IRC channel width).
    """
    base = base_url.rstrip("/")
    url = f"{base}/?{'random1' if qid is None else qid}"
    try:
        r = requests.get(url, headers={"User-Agent": ua},
                         timeout=10, stream=True)
        r.raise_for_status()
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            log.warning("QDB response exceeded %d bytes — refusing to parse",
                        _MAX_BODY_BYTES)
            return ["QDB response too large — endpoint may be misbehaving"]
        text = body.decode("utf-8", errors="replace")

        m_body = _RE_QUOTE_BODY.search(text)
        if not m_body:
            return [f"quote {qid} not found" if qid else "no quote found"]
        m_id = _RE_QUOTE_HEADER.search(text)
        title = _strip_ctrl(m_id.group(1).strip(), 32) if m_id else "qdb"

        # Split on <br> or newlines, strip residual tags, drop empties.
        chunks = _RE_LINE_BREAK.split(m_body.group(1))
        lines = [
            _strip_ctrl(html.unescape(_RE_TAGS.sub("", chunk)).strip())
            for chunk in chunks
        ]
        lines = [ln for ln in lines if ln]

        if len(lines) > _MAX_LINES:
            return [f"[qdb {title}] long quote — view at {url}"]
        return [f"[qdb {title}] {ln}" for ln in lines]
    except requests.RequestException as e:
        log.warning(f"QDB request: {e}")
        return ["QDB endpoint unavailable"]
    except Exception as e:
        log.warning(f"QDB parse: {e!r}")
        return ["QDB endpoint returned unexpected content"]


class QdbModule(BotModule):
    """`.qdb [id]` — random or specific quote from bash-org-archive.com."""

    COMMANDS: dict[str, str] = {"qdb": "cmd_qdb"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        sect = self.bot.cfg["qdb"] if "qdb" in self.bot.cfg else {}
        # Empty/absent api_url → use the bash-org-archive default so the
        # module is "configured" out of the box.  Operators with a
        # different mirror can still override.
        self._url: str = sect.get("api_url", "").strip() or _DEFAULT_URL

    def is_configured(self) -> bool:
        # Always True now — there's a working default endpoint baked in.
        return bool(self._url)

    async def cmd_qdb(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Show a quote from bash-org-archive.com.  Usage: .qdb [id]"""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        qid: str | None = None
        if arg:
            arg = arg.strip().split()[0]
            if not arg.isdigit():
                self.bot.privmsg(reply_to, f"{nick}: invalid quote ID")
                return
            qid = arg
        lines = await asyncio.to_thread(_lookup_sync, qid, self._url, self._ua)
        for line in lines:
            self.bot.privmsg(reply_to, line)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}qdb [id]               Random or specific bash.org-style quote"]


def setup(bot: object) -> QdbModule:
    """Module entry point."""
    return QdbModule(bot)  # type: ignore[arg-type]
