"""Interactive console for the Internets IRC bot.

Runs as an async task alongside the bot.  Reads stdin in a thread and
dispatches debug, loglevel, status, and shutdown commands.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from botlog import apply_debug, apply_loglevel, log_filter
from config import __version__

if TYPE_CHECKING:
    from internets import IRCBot

_CONSOLE_HELP = """\
  debug [on|off]            global debug toggle
  debug <sub> [off]         per-subsystem debug (e.g. debug weather)
  loglevel [LEVEL]          show or set base level (DEBUG/INFO/WARNING/ERROR)
  loglevel <logger> LEVEL   set a specific logger
  status                    show bot state (nick, channels, modules, log levels)
  shutdown [reason]         graceful shutdown
  quit                      alias for shutdown"""

log = logging.getLogger("internets")


async def run_console(bot: IRCBot) -> None:
    """Async console: reads stdin in a thread, processes commands."""
    while True:
        try:
            line = (await asyncio.to_thread(input, "> ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]

        if cmd == "help":
            print(_CONSOLE_HELP)

        elif cmd == "debug":
            apply_debug(args)

        elif cmd == "loglevel":
            if err := apply_loglevel(args):
                print(err)

        elif cmd == "status":
            _print_status(bot)

        elif cmd in ("shutdown", "quit"):
            reason = " ".join(args) if args else "Console shutdown"
            log.info(f"Console shutdown: {reason}")
            bot.request_shutdown(reason)
            break

        else:
            print(f"Unknown command: {cmd!r} — type 'help' for commands.")


def _print_status(bot: IRCBot) -> None:
    """Pretty-print the bot's current state to stdout."""
    print(f"  version  = {__version__}")
    print(f"  nick     = {bot._nick}")
    print(f"  channels = {', '.join(sorted(bot.active_channels.snapshot())) or '(none)'}")
    with bot._mod_lock:
        mods = list(bot._modules)
    print(f"  modules  = {', '.join(mods) or '(none)'}")
    with bot._auth_lock:
        admins = sorted(bot._authed)
    print(f"  admins   = {', '.join(admins) or '(none)'}")
    lvl_name = logging.getLevelName(log_filter.base_level)
    print(f"  log level = {lvl_name}"
          f"{' (global debug ON)' if log_filter.global_debug else ''}")
    if active := log_filter.active_subsystems():
        print(f"  debug subs = {', '.join(sorted(active))}")
