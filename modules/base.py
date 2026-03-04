from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from internets import IRCBot


class BotModule:
    """
    Base class for all bot modules.

    Subclasses define COMMANDS as a dict mapping command words to async method
    names.  All command handlers are coroutines::

        async def cmd_weather(self, nick: str, reply_to: str, arg: str | None) -> None:
            ...

    For blocking I/O (HTTP via requests, disk, CPU-heavy work), use::

        result = await asyncio.to_thread(requests.get, url, ...)

    Sync hooks:
        on_load()    — called after module is registered (event loop thread)
        on_unload()  — called before module is removed
        on_raw(line) — called for every incoming IRC line (must be fast, sync)

    Override help_lines() to describe commands for .help output.
    """

    COMMANDS: dict[str, str] = {}

    def __init__(self, bot: IRCBot) -> None:
        self.bot = bot

    def help_lines(self, prefix: str) -> list[str]:
        """Return help text lines for .help output.  Override in subclasses."""
        return []

    def on_load(self) -> None:
        """Called after the module is registered.  Override for setup."""
        pass

    def on_unload(self) -> None:
        """Called before the module is removed.  Override for cleanup."""
        pass

    def on_raw(self, line: str) -> None:
        """Called for every incoming IRC line.  Must be fast and sync."""
        pass
