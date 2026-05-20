"""Interactive console for the Internets IRC bot.

Runs as an async task alongside the bot.  Reads stdin in a thread and
dispatches debug, loglevel, status, and shutdown commands.
"""

from __future__ import annotations

import asyncio
import logging

from config import __version__
from botlog import log_filter, apply_debug, apply_loglevel

_CONSOLE_HELP = """\
  debug [on|off]            global debug toggle
  debug <sub> [off]         per-subsystem debug (e.g. debug weather)
  loglevel [LEVEL]          show or set base level (DEBUG/INFO/WARNING/ERROR)
  loglevel <logger> LEVEL   set a specific logger
  status                    show bot state (nick, channels, modules, log levels)
  shutdown [reason]         graceful shutdown
  quit                      alias for shutdown"""

log = logging.getLogger("internets")


async def run_console(bot: object) -> None:
    """Async console: reads stdin in a thread, processes commands."""
    while True:
        try:
            line = await asyncio.to_thread(input, "> ")
            line = line.strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        parts = line.split()
        cmd   = parts[0].lower()
        args  = parts[1:]

        if cmd == "help":
            print(_CONSOLE_HELP)

        elif cmd == "debug":
            apply_debug(args)

        elif cmd == "loglevel":
            err = apply_loglevel(args)
            if err:
                print(err)

        elif cmd == "status":
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
            active = log_filter.active_subsystems()
            if active:
                print(f"  debug subs = {', '.join(sorted(active))}")

        elif cmd in ("shutdown", "quit"):
            reason = " ".join(args) if args else "Console shutdown"
            log.info(f"Console shutdown: {reason}")
            bot.request_shutdown(reason)
            break

        else:
            print(f"Unknown command: {cmd!r} — type 'help' for commands.")
