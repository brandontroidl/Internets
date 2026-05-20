from __future__ import annotations

import asyncio
import html
import logging
import re
from xml.etree import ElementTree

import requests
from .base import BotModule

log = logging.getLogger("internets.qdb")

_NS = {
    "e": "http://purl.org/rss/1.0/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}
_MAX_LINES = 5


def _lookup_sync(qid: str | None, base_url: str, ua: str) -> list[str]:
    """Blocking QDB lookup — run via asyncio.to_thread.  Returns list of output lines."""
    try:
        if qid is None:
            url = f"{base_url}?action=random&fixed=0"
        else:
            url = f"{base_url}?action=quote&quote={qid}&fixed=0"
        r = requests.get(url, headers={"User-Agent": ua}, timeout=10)
        r.raise_for_status()
        root = ElementTree.fromstring(r.content)
        items = root.findall(".//e:item", _NS)
        if not items:
            return [f"quote {qid} not found" if qid else "no quotes returned"]

        item = items[0]
        title = item.findtext("e:title", "?", _NS)
        desc = item.findtext("e:description", "", _NS)
        link = item.findtext("e:link", "", _NS)

        # Description contains HTML with <br /> line breaks
        lines_raw = html.unescape(desc).split("<br />")
        lines = [l.strip() for l in lines_raw if l.strip()]

        if len(lines) > _MAX_LINES:
            return [f"[qdb {title}] quote is too long — view at {link}"]

        return [f"[qdb {title}] {line}" for line in lines]
    except ElementTree.ParseError:
        return [f"quote {qid} not found" if qid else "failed to parse response"]
    except Exception as e:
        log.warning(f"QDB lookup: {e}")
        return ["QDB endpoint unavailable — see [qdb] in config.ini"]


class QdbModule(BotModule):
    """Quote database lookup module (configurable QDB-compatible XML endpoint)."""

    COMMANDS: dict[str, str] = {"qdb": "cmd_qdb"}

    def on_load(self) -> None:
        try:
            self._ua: str = self.bot.cfg["weather"]["user_agent"]
        except KeyError:
            self._ua = "Internets/1.0"
        sect = self.bot.cfg["qdb"] if "qdb" in self.bot.cfg else {}
        self._url: str = sect.get("api_url", "").strip()
        if not self._url:
            log.warning("qdb: api_url not set in [qdb] — .qdb will not work "
                        "(qdb.us is defunct; set a QDB-compatible XML endpoint)")

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
