"""Interactive console for the Internets IRC bot.

Runs as an async task alongside the bot.  Reads stdin in a thread and
dispatches debug, loglevel, status, and shutdown commands.

SECURITY MODEL: the console grants admin-equivalent capability (debug
toggle, log-level changes, graceful shutdown) to anyone with stdin
access on the bot's host.  This is intentional - anyone with local
shell access can already kill the process, read config.ini, etc., so
the console is not an additional attack surface in that context.  But
it MUST NOT run when stdin is shared with an untrusted user.  Pass
``--no-console`` for daemonised / systemd-managed deployments, or run
the bot under a dedicated unprivileged user with no shared shell.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
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


def should_skip_console() -> bool:
    """Return True when the console should auto-skip.

    The console is unsafe when stdin isn't an interactive TTY - e.g.
    under systemd, in a Docker container without -it, or with stdin
    redirected to a file.  Skipping in those cases avoids granting
    admin equivalence to whatever piped input happens to be there.
    That is a security reason, and it is the only one: the dispatch
    loop returns on the first EOFError (see ``_console_dispatch_loop``),
    so there is no EOF loop to prevent.

    Fails safe: a missing or already-closed stdin also returns True.
    """
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        # No stdin at all, or it was closed - skip safely.
        return True


def _console_dispatch_loop(bot: IRCBot) -> None:
    """Synchronous read+dispatch loop for the console.  Runs in a
    daemon thread (see ``run_console``).

    All commands we dispatch are thread-safe to call from outside the
    asyncio loop:
      * ``apply_debug`` / ``apply_loglevel`` - touch logger state
        (RLock-guarded internally by the logging module).
      * ``_print_status`` - reads bot fields through their dedicated
        ``threading.Lock``-guarded accessors.
      * ``bot.request_shutdown`` - already uses
        ``loop.call_soon_threadsafe`` internally to set the stop event.

    Exits on EOFError (Ctrl-D / closed stdin), KeyboardInterrupt
    (Ctrl-C), ValueError (stdin closed mid-read), or "shutdown" / "quit".
    """
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt, ValueError):
            return
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
            return
        else:
            print(f"Unknown command: {cmd!r} - type 'help' for commands.")


async def run_console(bot: IRCBot) -> None:
    """Async wrapper: spawns the dispatch loop on a daemon thread and
    awaits an Event the thread sets when it exits.

    Why a daemon thread instead of ``asyncio.to_thread``: ``input()``
    parks the calling thread on a blocking ``read(0)`` syscall that
    nothing short of process death can interrupt.  If that thread
    isn't ``daemon=True``, ``asyncio.run()``'s cleanup path will
    ``loop.shutdown_default_executor()`` - which waits forever for
    the input-blocked worker to return.  Net effect of the old design:
    the whole process hung on the last shutdown log line until the
    operator hit Ctrl-C.  A daemon thread dies with the process, so
    cleanup completes and the bot exits cleanly.
    """
    # Loud warning on entry - anyone reading the log sees that the
    # console is live and grants admin equivalence to stdin.
    log.warning(
        "event=console_active stdin=tty pid=%d - "
        "the local console grants admin-equivalent capability "
        "(debug, loglevel, status, shutdown) WITHOUT authentication. "
        "Pass --no-console for daemon deployments.",
        __import__("os").getpid(),
    )
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def _wrap() -> None:
        try:
            _console_dispatch_loop(bot)
        except Exception as e:  # noqa: BLE001 - protect the loop
            log.exception(f"console thread crashed: {e!r}")
        finally:
            # call_soon_threadsafe is safe even if the loop has already
            # been closed (rare race during shutdown).
            try:
                loop.call_soon_threadsafe(done.set)
            except RuntimeError:
                pass

    t = threading.Thread(target=_wrap, daemon=True, name="console-input")
    t.start()
    try:
        await done.wait()
    except asyncio.CancelledError:
        # _main cancelled us during shutdown; nothing to clean up since
        # the dispatch thread is daemon=True.
        raise


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
