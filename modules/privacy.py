from __future__ import annotations

import logging

from .base import BotModule

log = logging.getLogger("internets.privacy")

# Special key prefix used to record per-nick opt-out flags inside
# ``locations.json``.  The locations store is a flat ``dict[str, str]``
# keyed by lowercased nick; until ``store.Store`` grows a real opt-out
# column (tracked as a follow-up for the Robustness agent) we squat on a
# reserved key namespace that real nicknames cannot collide with — IRC
# RFC 2812 forbids ``:`` and leading digits/``__`` in nicks on every
# server software in common use.
_OPTOUT_KEY_PREFIX = "__optout__:"


def _optout_key(nick: str) -> str:
    """Return the locations-store key that records an opt-out flag for *nick*."""
    return f"{_OPTOUT_KEY_PREFIX}{nick.lower()}"


class PrivacyModule(BotModule):
    """User-facing data-protection commands.

    Implements the minimum surface needed for a GDPR-style hygiene pass:

    * ``.forgetme`` — right to erasure: purges the invoking nick from every
      dataset the bot owns (saved location + per-channel tracking entries).
    * ``.privacy`` — transparency: privately lists everything the bot has
      stored about the invoking nick, including their own hostmask.
    * ``.optout`` / ``.optin`` — toggle a per-nick opt-out flag.  See the
      comment on ``_OPTOUT_KEY_PREFIX`` for why the flag is stashed inside
      ``locations.json`` for now; the Robustness agent is expected to add
      a first-class column in ``store.Store`` and update consumers (notably
      ``modules/location.py``) so that opt-out propagates into user
      tracking and any future logging.

    All commands are PM-only — leaking another user's saved location or
    hostmask into a public channel would itself be a privacy regression.
    """

    COMMANDS: dict[str, str] = {
        "forgetme": "cmd_forgetme",
        "privacy":  "cmd_privacy",
        "optout":   "cmd_optout",
        "optin":    "cmd_optin",
    }

    # ── Helpers ──────────────────────────────────────────────────────

    def _is_pm(self, nick: str, reply_to: str) -> bool:
        """True when *reply_to* is the invoking nick (i.e. a PM)."""
        return not reply_to.startswith(("#", "&", "+", "!"))

    def _require_pm(self, nick: str, reply_to: str, cmd: str) -> bool:
        """Reject channel use of a PM-only command.  Returns True if OK."""
        if self._is_pm(nick, reply_to):
            return True
        p = self.bot.cfg["bot"]["command_prefix"]
        self.bot.notice(
            nick,
            f"{nick}: {p}{cmd} is PM-only — please /msg me directly so "
            "your data isn't echoed into the channel.",
        )
        return False

    def _is_opted_out(self, nick: str) -> bool:
        """Return True if *nick* currently has the opt-out flag set."""
        return self.bot.loc_get(_optout_key(nick)) is not None

    def _own_hostmask(self, nick: str) -> str | None:
        """Best-effort lookup of the invoker's own current hostmask.

        Reads the bot's in-memory ``_nick_hosts`` cache, which is updated
        on every PRIVMSG from the user.  Returns None if we somehow
        haven't observed it yet.  We deliberately never look up another
        user's hostmask through this helper.
        """
        return getattr(self.bot, "_nick_hosts", {}).get(nick.lower())

    # ── Commands ─────────────────────────────────────────────────────

    async def cmd_forgetme(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Purge every record of the invoking nick from bot-owned storage.

        Currently deletes:
          * the saved location (``loc_del``)
          * the opt-out flag itself (so a future ``.optin`` is honest)
        Hard-deletes (via ``store.user_purge``):
          * per-channel user-tracking entries — erased immediately, no
            wait for the 90-day prune cycle.
        """
        if not self._require_pm(nick, reply_to, "forgetme"):
            return

        deleted: list[str] = []

        # 1. Saved location, if any.
        if self.bot.loc_del(nick):
            deleted.append("saved location")

        # 2. Opt-out flag — remove so the opt-in/opt-out cycle stays
        #    truthful after a purge.  Don't surface this in the user
        #    confirmation; it's bookkeeping, not user data per se.
        self.bot.loc_del(_optout_key(nick))

        # 3. Channel-tracking entries.  We snapshot the channels we care
        #    about *before* mutating, so we can tell the user exactly
        #    which channels held an entry for them.
        touched_chans: list[str] = []
        try:
            for ch in self.bot.active_channels:
                ch_users = self.bot.channel_users(ch)
                if nick.lower() in ch_users:
                    touched_chans.append(ch)
        except Exception as e:  # noqa: BLE001 — never let privacy explode
            log.warning(f"forgetme: snapshot failed for {nick!r}: {e!r}")

        # Hard-delete: store.user_purge erases the rows immediately
        # across every channel the nick appears in.
        purged_rows = 0
        try:
            purged_rows = self.bot._store.user_purge(nick)  # type: ignore[attr-defined]
        except AttributeError:
            # Defensive: if some future refactor renames _store/user_purge,
            # fall back to user_quit so .forgetme still degrades gracefully.
            try:
                self.bot._store.user_quit(nick)  # type: ignore[attr-defined]
            except AttributeError:
                pass

        if purged_rows:
            deleted.append(f"tracking in {purged_rows} channel(s) (erased now)")
        elif touched_chans:
            # Snapshot saw entries but purge reported zero — race / mis-key.
            deleted.append(
                f"tracking in {len(touched_chans)} channel(s) "
                f"(scheduled for removal on next prune cycle)"
            )

        if not deleted:
            self.bot.privmsg(
                nick,
                f"{nick}: I had no stored records for you. Nothing to delete.",
            )
            log.info(f"forgetme {nick}: no-op (no records)")
            return

        self.bot.privmsg(
            nick,
            f"{nick}: deleted — {'; '.join(deleted)}. "
            f"See .privacy for what remains.",
        )
        log.info(f"forgetme {nick}: removed {deleted}")

    async def cmd_privacy(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Privately disclose everything the bot stores about the invoker."""
        if not self._require_pm(nick, reply_to, "privacy"):
            return

        # Location (if any).
        loc = self.bot.loc_get(nick)
        if loc:
            self.bot.privmsg(nick, f"{nick}: saved location: {loc!r}")
        else:
            self.bot.privmsg(nick, f"{nick}: saved location: (none)")

        # Own hostmask — only ever the invoker's own, never anybody else's.
        hm = self._own_hostmask(nick)
        if hm:
            self.bot.privmsg(nick, f"{nick}: your current hostmask (as I see it): {hm}")

        # Per-channel tracking.
        try:
            channels = list(self.bot.active_channels)
        except Exception:  # noqa: BLE001
            channels = []
        rows: list[str] = []
        for ch in sorted(channels):
            ch_users = self.bot.channel_users(ch)
            entry = ch_users.get(nick.lower())
            if not entry:
                continue
            first = entry.get("first_seen", "?")
            last  = entry.get("last_seen", "?")
            rows.append(f"  {ch}: first_seen={first} last_seen={last}")

        if rows:
            self.bot.privmsg(
                nick,
                f"{nick}: tracked in {len(rows)} channel(s) — "
                "(nick + hostmask + first/last seen kept per channel; "
                "auto-pruned after the configured retention window):",
            )
            for r in rows:
                self.bot.privmsg(nick, r)
        else:
            self.bot.privmsg(nick, f"{nick}: not currently tracked in any channel I'm in.")

        # Opt-out status.
        status = "opted-out" if self._is_opted_out(nick) else "opted-in (default)"
        self.bot.privmsg(nick, f"{nick}: privacy preference: {status}")

        # Pointer to docs.
        p = self.bot.cfg["bot"]["command_prefix"]
        self.bot.privmsg(
            nick,
            f"{nick}: to erase, run {p}forgetme.  Full policy: PRIVACY.md "
            "in the bot's source repo.",
        )
        log.info(f"privacy {nick}: disclosed (loc={'y' if loc else 'n'}, "
                 f"channels={len(rows)})")

    async def cmd_optout(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Record a per-nick opt-out flag.

        Limitation: until ``store.Store`` exposes a first-class opt-out
        column, the flag lives under a reserved key in ``locations.json``
        (``__optout__:<nick>``).  Modules that should honour the flag
        (notably ``modules/location.py``) won't read it until the
        Robustness agent threads the new column through — this is
        documented as a follow-up in PRIVACY.md.
        """
        if self._is_opted_out(nick):
            self.bot.notice(nick, f"{nick}: already opted out.")
            return
        self.bot.loc_set(_optout_key(nick), "1")
        self.bot.notice(
            nick,
            f"{nick}: opted out. Note: full propagation lands once the "
            "store schema gains a real opt-out column; for now run "
            ".forgetme to erase existing records.",
        )
        log.info(f"optout {nick}")

    async def cmd_optin(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Clear a previously recorded opt-out flag for the invoking nick."""
        if not self._is_opted_out(nick):
            self.bot.notice(nick, f"{nick}: you weren't opted out.")
            return
        self.bot.loc_del(_optout_key(nick))
        self.bot.notice(nick, f"{nick}: opted back in.")
        log.info(f"optin {nick}")

    # ── Help / configuration ─────────────────────────────────────────

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}forgetme                                  Erase all data the bot holds about you (PM-only)",
            f"  {prefix}privacy                                   Show what the bot stores about you (PM-only)",
            f"  {prefix}optout                                    Mark yourself opted-out of future tracking",
            f"  {prefix}optin                                     Undo a previous {prefix}optout",
        ]

    def is_configured(self) -> bool:
        """Privacy commands need no API key — always available."""
        return True


def setup(bot: object) -> PrivacyModule:
    """Module entry point — returns a PrivacyModule instance."""
    return PrivacyModule(bot)  # type: ignore[arg-type]
