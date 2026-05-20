from __future__ import annotations

import asyncio
import html
import logging
from xml.etree import ElementTree as _stdlib_ET

import requests
from .base import BotModule

log = logging.getLogger("internets.qdb")

# ---------------------------------------------------------------------------
# XML parser hardening
# ---------------------------------------------------------------------------
# Python's stdlib ElementTree is XXE-safe in 3.8+ (external entity expansion
# is disabled by default), but it does NOT mitigate the "billion laughs"
# entity-expansion DoS — a small XML payload can decompress into gigabytes
# of internal-entity expansion.  defusedxml's ElementTree blocks both classes.
# Prefer it when available; fall back to stdlib with a loud warning so the
# operator knows about the residual DoS surface.
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring  # type: ignore[import-not-found]
    _XML_HARDENED = True
except ImportError:  # pragma: no cover — depends on env
    _xml_fromstring = _stdlib_ET.fromstring
    _XML_HARDENED = False
ParseError = _stdlib_ET.ParseError

_NS = {
    "e": "http://purl.org/rss/1.0/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}
_MAX_LINES = 5

# Cap the XML body we'll read from a third-party QDB instance.  A real
# quote payload is a few KB; anything larger than 256 KB is either an
# error page or a hostile endpoint.  Stops both unbounded reads and
# trivial decompression bombs at the HTTP boundary.
_MAX_BODY_BYTES = 256 * 1024

# IRC control bytes — any third-party-derived string we splice into a
# privmsg gets these stripped so a vandalised quote can't inject IRC
# commands (\r\n) or bot-spoof formatting (\x02 bold, \x03 color, etc.).
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    """Drop IRC control bytes and cap length."""
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


def _lookup_sync(qid: str | None, base_url: str, ua: str) -> list[str]:
    """Blocking QDB lookup — run via asyncio.to_thread.  Returns list of output lines."""
    try:
        if qid is None:
            url = f"{base_url}?action=random&fixed=0"
        else:
            url = f"{base_url}?action=quote&quote={qid}&fixed=0"
        # stream=True + bounded read prevents unbounded XML payloads.
        r = requests.get(url, headers={"User-Agent": ua},
                         timeout=10, stream=True)
        r.raise_for_status()
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            log.warning("QDB response exceeded %d bytes — refusing to parse",
                        _MAX_BODY_BYTES)
            return ["QDB response too large — endpoint may be misbehaving"]
        # Hardened parser (defusedxml when available; see _XML_HARDENED).
        root = _xml_fromstring(body)
        items = root.findall(".//e:item", _NS)
        if not items:
            return [f"quote {qid} not found" if qid else "no quotes returned"]

        item = items[0]
        title = item.findtext("e:title", "?", _NS) or "?"
        desc = item.findtext("e:description", "", _NS) or ""
        link = item.findtext("e:link", "", _NS) or ""

        # Description contains HTML with <br /> line breaks.  Strip IRC
        # control bytes from every chunk since the quote body and the
        # title both come from third-party data.
        title = _strip_ctrl(title, 80)
        link  = _strip_ctrl(link, 200)
        lines_raw = html.unescape(desc).split("<br />")
        lines = [_strip_ctrl(line.strip()) for line in lines_raw if line.strip()]

        if len(lines) > _MAX_LINES:
            return [f"[qdb {title}] quote is too long — view at {link}"]

        return [f"[qdb {title}] {line}" for line in lines]
    except ParseError:
        return [f"quote {qid} not found" if qid else "failed to parse response"]
    except Exception as e:
        log.warning(f"QDB lookup: {e}")
        return ["QDB endpoint unavailable — see [qdb] in config.ini"]


class QdbModule(BotModule):
    """Quote database lookup module (configurable QDB-compatible XML endpoint)."""

    COMMANDS: dict[str, str] = {"qdb": "cmd_qdb"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        sect = self.bot.cfg["qdb"] if "qdb" in self.bot.cfg else {}
        self._url: str = sect.get("api_url", "").strip()
        if not self._url:
            log.warning("qdb: api_url not set in [qdb] — .qdb will not work "
                        "(qdb.us is defunct; set a QDB-compatible XML endpoint)")
        if not _XML_HARDENED:
            log.warning(
                "qdb: defusedxml not installed — XML parser falls back to "
                "stdlib ElementTree.  XXE is blocked by default but "
                "billion-laughs DoS is NOT.  pip install defusedxml to fix."
            )

    def is_configured(self) -> bool:
        return bool(self._url)

    async def cmd_qdb(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Show a quote from a QDB.  Usage: .qdb [id]"""
        if not self._url:
            self.bot.privmsg(
                reply_to,
                "QDB endpoint not configured — qdb.us is defunct. "
                "Set api_url in [qdb] config to a working QDB instance.",
            )
            return
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
        return [f"  {prefix}qdb [id]               Random or specific quote from configured QDB"]


def setup(bot: object) -> QdbModule:
    """Module entry point — returns a QdbModule instance."""
    return QdbModule(bot)  # type: ignore[arg-type]
