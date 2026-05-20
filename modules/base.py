from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from internets import IRCBot


_PLACEHOLDER_MARKERS = (
    "changeme", "your-key", "placeholder", "set-in-secret-store",
    "<your-", "you@example", "example.com",
)


def cred(cfg, secret_name: str, section: str, key: str, default: str = "") -> str:
    """Pull a credential or PII field — secret_store first, config fallback.

    For new installs the keys live exclusively in the secret store
    (see ``python -m secret_store``).  The config.ini fallback path
    exists only for upgrades from 2.4.0-and-earlier where keys were
    placed directly in the ini file.  Placeholder strings from the
    template (``you@example.com``, ``set-in-secret-store``, etc.) are
    treated as unset so they never leak into outbound HTTP requests.
    """
    try:
        import secret_store  # noqa: PLC0415
        v = secret_store.get(secret_name)
        if v:
            return v
    except ImportError:
        pass
    try:
        raw = cfg.get(section, key, fallback=default).strip()
    except Exception:
        return default
    low = raw.lower()
    if any(m in low for m in _PLACEHOLDER_MARKERS):
        return default
    return raw


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

    def is_configured(self) -> bool:
        """Return True if this module has everything it needs to run.

        Modules that depend on an API key (imdb, lastfm, youtube, etc.)
        should override this to check whether the key is present.  The
        bot's ``.help`` skips modules where this returns False so the
        help output isn't cluttered with commands the user can't use.
        Module dispatch still works — admins can ``.load`` a module
        and add a key later — but it stays invisible to normal users
        until the key is in place.
        """
        return True

    def on_load(self) -> None:
        """Called after the module is registered.  Override for setup."""
        pass

    def on_unload(self) -> None:
        """Called before the module is removed.  Override for cleanup."""
        pass

    def on_raw(self, line: str) -> None:
        """Called for every incoming IRC line.  Must be fast and sync."""
        pass
