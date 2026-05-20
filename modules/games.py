"""Games — small chance/choice commands.

Pure local, no network.  Commands:
    .coin                 — flip a coin
    .8ball <question>     — classic Magic-8-Ball answer
    .rps <choice>         — rock / paper / scissors vs the bot
    .choose A, B, C, ...  — pick one of a comma-separated list

Uses ``random.SystemRandom`` for unpredictable picks.  All commands
are rate-limited per nick via ``self.bot.rate_limited``.
"""

from __future__ import annotations

import logging
import random
from .base import BotModule

log = logging.getLogger("internets.games")

_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


_RNG = random.SystemRandom()

_8BALL_ANSWERS: tuple[str, ...] = (
    "It is certain.",
    "It is decidedly so.",
    "Without a doubt.",
    "Yes definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Yes.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
)

_RPS_BEATS: dict[str, str] = {
    "rock": "scissors",
    "paper": "rock",
    "scissors": "paper",
}


class GamesModule(BotModule):
    """`.coin` / `.8ball` / `.rps` / `.choose` — small chance games."""

    COMMANDS: dict[str, str] = {
        "coin": "cmd_coin",
        "8ball": "cmd_8ball",
        "rps": "cmd_rps",
        "choose": "cmd_choose",
    }

    def is_configured(self) -> bool:
        return True

    async def cmd_coin(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        self.bot.privmsg(reply_to, _RNG.choice(("Heads", "Tails")))

    async def cmd_8ball(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}8ball <question>")
            return
        self.bot.privmsg(reply_to, _strip_ctrl(f"{nick}: {_RNG.choice(_8BALL_ANSWERS)}"))

    async def cmd_rps(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}rps <rock|paper|scissors>")
            return
        choice = arg.strip().lower()
        if choice not in _RPS_BEATS:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}rps <rock|paper|scissors>")
            return
        bot_pick = _RNG.choice(tuple(_RPS_BEATS.keys()))
        if bot_pick == choice:
            outcome = "tie"
        elif _RPS_BEATS[choice] == bot_pick:
            outcome = "you win"
        else:
            outcome = "you lose"
        self.bot.privmsg(reply_to, _strip_ctrl(
            f"you: {choice}, bot: {bot_pick} — {outcome}"
        ))

    async def cmd_choose(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        if not arg or "," not in arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}choose A, B, C, ...")
            return
        options = [o.strip() for o in arg.split(",")]
        options = [o for o in options if o]
        if len(options) < 2:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}choose A, B, C, ...")
            return
        if len(options) > 20:
            self.bot.privmsg(reply_to, f"{nick}: max 20 options")
            return
        if any(len(o) > 60 for o in options):
            self.bot.privmsg(reply_to, f"{nick}: each option max 60 chars")
            return
        self.bot.privmsg(reply_to, _strip_ctrl(f"{nick}: {_RNG.choice(options)}"))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}coin                  Flip a coin",
            f"  {prefix}8ball <question>      Magic 8-ball",
            f"  {prefix}rps <choice>          Rock/paper/scissors",
            f"  {prefix}choose A, B, C, ...   Pick one at random",
        ]


def setup(bot: object) -> GamesModule:
    return GamesModule(bot)  # type: ignore[arg-type]
