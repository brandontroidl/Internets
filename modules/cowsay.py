"""Cowsay - render the classic ASCII cow speaking the given text.

Pure Python: no external ``cowsay`` binary, no library.  The bubble
is built locally and the cow template is embedded below.

Command:
    .cowsay <text>   - speak <text>.  Capped at 200 chars.

Each line of the rendered cow is sent as a separate ``privmsg``; the
bot's send-queue handles flood control naturally.  Rate-limited per nick.
"""

from __future__ import annotations

import logging
import textwrap
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.cowsay")

_MAX_INPUT = 200
_WRAP = 40


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


_COW = (
    "        \\   ^__^",
    "         \\  (oo)\\_______",
    "            (__)\\       )\\/\\",
    "                ||----w |",
    "                ||     ||",
)


def _bubble(text: str) -> list[str]:
    text = text.replace("\t", "    ")
    lines: list[str] = []
    for raw_line in text.split("\n"):
        if not raw_line:
            lines.append("")
            continue
        wrapped = textwrap.wrap(raw_line, width=_WRAP) or [""]
        lines.extend(wrapped)
    if not lines:
        lines = [""]
    width = max(len(l) for l in lines)
    top = " _" + "_" * width + "_"
    bot = " -" + "-" * width + "-"
    out: list[str] = [top]
    if len(lines) == 1:
        out.append(f"< {lines[0].ljust(width)} >")
    else:
        for i, l in enumerate(lines):
            if i == 0:
                left, right = "/", "\\"
            elif i == len(lines) - 1:
                left, right = "\\", "/"
            else:
                left, right = "|", "|"
            out.append(f"{left} {l.ljust(width)} {right}")
    out.append(bot)
    return out


def _render(text: str) -> list[str]:
    return _bubble(text) + list(_COW)


class CowsayModule(BotModule):
    """`.cowsay <text>` - ASCII cow speaks the given text."""

    COMMANDS: dict[str, str] = {"cowsay": "cmd_cowsay"}

    def is_configured(self) -> bool:
        return True

    async def cmd_cowsay(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cowsay <text>")
            return
        text = arg[:_MAX_INPUT]
        for line in _render(text):
            self.bot.privmsg(reply_to, _strip_ctrl(line))

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "cowsay <text>", "ASCII cow speaks <text>")]


def setup(bot: object) -> CowsayModule:
    return CowsayModule(bot)  # type: ignore[arg-type]
