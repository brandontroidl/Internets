from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from internets import IRCBot


class BotModule:
    """
    Base class for all bot modules.

    Subclasses define COMMANDS as a dict mapping command words to method names,
    implement those methods with the signature (self, nick, reply_to, arg),
    and override help_lines() to describe them.

    on_load / on_unload are optional hooks called by the module loader.
    on_raw(line) is called for every incoming IRC line (after tag stripping)
    and lets modules react to server numerics, NOTICEs, etc.
    """

    COMMANDS: dict[str, str] = {}

    def __init__(self, bot: IRCBot) -> None:
        self.bot = bot

    def help_lines(self, prefix: str) -> list[str]:
        return []

    def on_load(self) -> None:
        pass

    def on_unload(self) -> None:
        pass

    def on_raw(self, line: str) -> None:
        pass
