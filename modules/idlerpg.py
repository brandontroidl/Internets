from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from defusedxml import ElementTree  # XML from a 3rd-party HTTP endpoint — defuse XXE/billion-laughs.

import requests
from .base import BotModule

log = logging.getLogger("internets.idlerpg")

_ALIGNMENTS = {"g": "Good", "e": "Evil", "n": "Neutral"}


def _lookup_sync(player: str, base_url: str, ua: str) -> str:
    """Blocking IdleRPG lookup — run via asyncio.to_thread."""
    try:
        r = requests.get(
            base_url,
            params={"player": player},
            headers={"User-Agent": ua},
            timeout=10,
        )
        r.raise_for_status()
        # Strip IRC control codes that the XML may contain
        text = r.text
        for ch in ("\x02", "\x03", "\x0f", "\x1f"):
            text = text.replace(ch, "")

        root = ElementTree.fromstring(text)
        name = root.findtext("username", "")
        if not name:
            return f"player '{player}' not found (\x02note:\x02 names are case sensitive)"

        level = root.findtext("level", "0")
        classe = root.findtext("class", "?")
        ttl = int(root.findtext("ttl", "0"))
        idled = int(root.findtext("totalidled", "0"))
        online = root.findtext("online", "0") == "1"
        alignment = _ALIGNMENTS.get(root.findtext("alignment", "n"), "Neutral")
        status = "\x0303ON\x03" if online else "\x0304OFF\x03"

        return (
            f"\x02{name}\x02 [{status}] | "
            f"\x02Level\x02 {level} {classe} | "
            f"\x02Next level\x02 {timedelta(seconds=ttl)} | "
            f"\x02Idled\x02 {timedelta(seconds=idled)} | "
            f"\x02Alignment\x02 {alignment}"
        )
    except Exception as e:
        log.warning(f"IdleRPG lookup: {e}")
        return "lookup failed"


class IdlerpgModule(BotModule):
    """IdleRPG player lookup module."""

    COMMANDS: dict[str, str] = {"irpg": "cmd_irpg", "idlerpg": "cmd_irpg"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        sect = self.bot.cfg["idlerpg"] if "idlerpg" in self.bot.cfg else {}
        self._url: str = sect.get(
            "api_url", "http://idlerpg.rizon.net/xml.php"
        ).strip()

    async def cmd_irpg(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up an IdleRPG player."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}irpg <player>  (names are case sensitive)")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(
            _lookup_sync, arg.strip().split()[0], self._url, self._ua
        )
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}irpg/.idlerpg <player>  IdleRPG player info"]


def setup(bot: object) -> IdlerpgModule:
    """Module entry point — returns an IdlerpgModule instance."""
    return IdlerpgModule(bot)  # type: ignore[arg-type]
