"""Example module - a copy-and-fill skeleton for a new command.

A complete, loadable BotModule. The default `.example` does one trivial thing
(echo its argument back, uppercased) with no external dependency, so you can
`.load example` and watch it run, then replace the body. The commented network
variant below shows the canonical shape for a command that talks to an HTTP API
- the pattern 50+ modules use.

This covers the common module shape and the parts of the BotModule contract you
usually touch. Two passive hooks are not shown because most modules don't need
them: on_raw(self, line) runs SYNC in the IRC read path for every incoming line
(must be fast and never raise - wrap the body, log at debug; see seen.py), and
on_unload(self) cancels tasks / final-flushes before removal.

Quick start:
  1. Copy this file to modules/<name>.py; rename the class and the logger.
  2. Set COMMANDS to your command words.
  3. Write the cmd_* coroutine(s).
  4. Add <name> to [bot] autoload in config.ini, or `.load <name>` at runtime.

Contract (enforced at class-definition by BotModule.__init_subclass__): COMMANDS
maps each command word to the NAME of an `async def cmd_*` method; a typo or a
non-async handler raises TypeError at import, not in production.
"""

from __future__ import annotations

import logging

from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.example")

# Bound user input before interpolating it into a URL/identifier or echoing it.
_MAX_INPUT = 200


# ---- canonical network shape (reference; the default command is offline) ----
# Outbound HTTP and CPU-heavy work MUST run off the event loop or it stalls
# every other user. The convention in 50+ modules: a MODULE-LEVEL sync function
# does the whole fetch + parse + format and ALWAYS returns a finished string,
# catching every error so the coroutine never raises; the handler just awaits it
# via asyncio.to_thread and emits the string. Uncomment, add `import asyncio` and
# `import requests`, and point it at a real API:
#
#     from .base import fetch_json, ResponseTooLarge
#
#     def _fetch_sync(arg: str, ua: str) -> str:
#         try:
#             # fetch_json streams + hard-caps the body (default 256 KB; pass
#             # max_bytes= for legitimately larger APIs). NEVER a bare
#             # requests.json() - that has no size cap.
#             data = fetch_json("https://api.example.com/thing",
#                               params={"q": arg}, ua=ua)
#             if not isinstance(data, dict):     # may be None (allow_404) or a list
#                 return "example: no result"
#             return strip_ctrl(str(data.get("field", "")), _MAX_INPUT)
#         except ResponseTooLarge:
#             log.warning("example: response too large")
#             return "example: upstream response too large"
#         except requests.RequestException as e:
#             log.warning(f"example: request: {e}")
#             return "example: lookup failed"
#         except Exception as e:                 # parse / unexpected shape
#             log.warning(f"example: parse: {e!r}")
#             return "example: lookup failed"
#
# SSRF: fetch_json only size-caps; it does NOT validate the destination. If the
# host/URL is derived from user or feed input (a `.fetch <url>` command), DO NOT
# pass it to fetch_json - route it through base.resolve_public() or
# _netsafe.safe_open() (pins the DNS result across redirects). See probe.py /
# urls.py / scinews.py. A fixed, trusted host like the one above is fine.
# -----------------------------------------------------------------------------


class ExampleModule(BotModule):
    """`.example <text>` - echo the text back uppercased (replace this)."""

    # Command word -> async method name. Point several words at one method to
    # make aliases, e.g. {"example": "cmd_example", "ex": "cmd_example"}.
    COMMANDS: dict[str, str] = {"example": "cmd_example"}

    # on_load() runs once after the module is registered, on the event-loop
    # thread - read config and secrets here ONCE, not per command. Delete it if
    # you need nothing. cred() checks secret_store first, then config.ini, then
    # the default; it never raises on a fresh install, so the module degrades
    # instead of crashing at load. Network modules share ONE outbound UA - do
    # not invent a per-module section unless you genuinely need a distinct one:
    #
    #     def on_load(self) -> None:
    #         from .base import cred
    #         self._ua  = cred(self.bot.cfg, "weather_user_agent",
    #                          "weather", "user_agent", "Internets/1.0")
    #         self._key = cred(self.bot.cfg, "example_key", "example", "api_key")
    #
    # is_configured() gates VISIBILITY in .help: return False when a required key
    # is missing so the command hides from normal users (an admin can still
    # .load it and add the key later). Default True; override only if keyed:
    #
    #     def is_configured(self) -> bool:
    #         return bool(getattr(self, "_key", ""))

    async def cmd_example(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Every handler is `async def (self, nick, reply_to, arg)`.

        nick      who invoked the command.
        reply_to  where to reply: a channel name, or the nick for a PM.
        arg       everything after the command word, or None if there was none.
        """
        # Usage first, so an empty command does not burn a flood token.
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}example <text>")
            return
        # Rate-limit any command that does real work (network, CPU). Per-nick;
        # admins bypass the flood gate. The notice is a private reply to nick.
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        # Admin-only command? gate it (see weather.py cmd_providers):
        #     if not self.bot.is_admin(nick):
        #         self.bot.notice(nick, f"{nick}: admins only"); return

        # Strip control bytes from any user/upstream text before it enters a
        # bot-emitted line - the canonical defense against IRC colour/CTCP/BEL
        # injection. The second arg caps the length.
        text = strip_ctrl(arg, _MAX_INPUT)
        self.bot.privmsg(reply_to, f"{nick}: {text.upper()}")

        # Network variant: result = await asyncio.to_thread(_fetch_sync, arg, self._ua)
        #                  self.bot.privmsg(reply_to, f"{nick}: {result}")
        # (see the _fetch_sync reference above; needs `import asyncio`.)

    def help_lines(self, prefix: str) -> list[str]:
        """One help_row per user-facing command; shown in .help. For an alias,
        use the `cmd/.alias <arg>` form, e.g.
        help_row(prefix, "example/.ex <text>", "...")."""
        return [help_row(prefix, "example <text>", "Echo the text back uppercased")]

    # forget(self, nick) -> int: override ONLY if you store data keyed by nick
    # (seen/tell/notes do). The .forgetme command calls it for every module so
    # right-to-erasure covers the whole bot: mutate your store, persist it
    # atomically (mkstemp + os.replace + chmod 0o600 under a Lock; see seen.py),
    # and return the count removed. This module holds no per-user data, so the
    # BotModule default (return 0) is correct and is not overridden.


# ---- patterns beyond this skeleton (read the cited module when you need one) -
# Multi-flag command (-l, -p <provider>, -n <nick>):  weather.py _parse_weather_flags
# Thread-safe TTL + LRU cache for repeated queries:    geocode.py (OrderedDict + Lock + expiry)
# DRY many near-identical commands via one helper:     weather.py _weather_cmd
# Parsing UNTRUSTED XML (XXE / billion-laughs safe):   reflookup.py (from defusedxml import ElementTree)
# Multi-line output (a header then a line per item):   loop self.bot.privmsg(reply_to, line)
# -----------------------------------------------------------------------------


def setup(bot: object) -> ExampleModule:
    """Module entry point - the loader calls setup(bot) and expects a BotModule
    instance back. Keep this function at the bottom of the file."""
    return ExampleModule(bot)  # type: ignore[arg-type]
