"""QR-code link generator — emits a URL only, no network fetch.

Command:
    .qr <text>   — returns a goqr.me image URL that renders <text> as a QR.

Input is capped at 1000 chars; empty or oversize input yields a usage hint.
No HTTP is performed — the URL is just constructed locally so the user
can click it.  Rate-limited per nick.
"""

from __future__ import annotations

import logging
from urllib.parse import quote
from .base import BotModule

log = logging.getLogger("internets.qr")

_MAX_INPUT = 1000
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


class QRModule(BotModule):
    """`.qr <text>` — build a QR-code image URL."""

    COMMANDS: dict[str, str] = {"qr": "cmd_qr"}

    def is_configured(self) -> bool:
        return True

    async def cmd_qr(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}qr <text>  (max 1000 chars)")
            return
        text = arg.strip()
        if len(text) > _MAX_INPUT:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}qr <text>  (max 1000 chars)")
            return
        url = (
            "https://api.qrserver.com/v1/create-qr-code/"
            f"?size=300x300&data={quote(text, safe='')}"
        )
        self.bot.privmsg(reply_to, _strip_ctrl(url))

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}qr <text>             QR-code image URL (max 1000 chars)"]


def setup(bot: object) -> QRModule:
    return QRModule(bot)  # type: ignore[arg-type]
