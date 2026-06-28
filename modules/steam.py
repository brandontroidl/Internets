from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import BotModule, fetch_json

log = logging.getLogger("internets.steam")

_PERSONA_STATES: dict[int, tuple[str, str]] = {
    0: ("OFFLINE",          "\x0314"),
    1: ("ONLINE",           "\x0303"),
    2: ("BUSY",             "\x0304"),
    3: ("AWAY",             "\x0307"),
    4: ("SNOOZE",           "\x0307"),
    5: ("LOOKING TO TRADE", "\x0305"),
    6: ("LOOKING TO PLAY",  "\x0310"),
}


def _timeago(ts: int) -> str:
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _resolve_vanity(vanity: str, key: str, ua: str) -> str | None:
    """Resolve a Steam vanity URL name to a 64-bit Steam ID."""
    try:
        d = fetch_json(
            "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/",
            params={"key": key, "vanityurl": vanity},
            ua=ua,
            timeout=10,
        ).get("response", {})
        if d.get("success") == 1:
            return d["steamid"]
    except Exception as e:
        log.debug(f"vanity resolve: {e}")
    return None


def _get_status(steamid: str, key: str, ua: str) -> dict[str, Any]:
    players = fetch_json(
        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
        params={"key": key, "steamids": steamid},
        ua=ua,
        timeout=10,
    ).get("response", {}).get("players", [])
    if not players:
        raise ValueError("no user found")
    return players[0]


def _get_games(steamid: str, key: str, ua: str) -> dict[str, Any]:
    # Owned-games payloads can be large for power users — bump the cap
    # to 1 MB to fit accounts with hundreds of titles + appinfo metadata.
    return fetch_json(
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/",
        params={"key": key, "steamid": steamid,
                "include_played_free_games": "1",
                "include_appinfo": "1", "format": "json"},
        ua=ua,
        timeout=10,
        max_bytes=1024 * 1024,
    ).get("response", {})


def _status_sync(steamid: str, show_games: bool, key: str, ua: str) -> str:
    """Blocking Steam lookup — run via asyncio.to_thread."""
    try:
        d = _get_status(steamid, key, ua)
    except Exception as e:
        log.warning(f"Steam status: {e}")
        return "lookup failed"

    name = d.get("personaname", "?")
    state = d.get("personastate", 0)
    label, color = _PERSONA_STATES.get(state, ("UNKNOWN", "\x0314"))
    status_str = f"{color}{label}\x03"

    if show_games:
        try:
            gdata = _get_games(steamid, key, ua)
        except Exception:
            return f"\x02{name}\x02 [{status_str}] | game data unavailable (profile may be private)"
        gc = gdata.get("game_count", 0)
        if gc == 0:
            return f"\x02{name}\x02 [{status_str}] | does not own any games"
        games = gdata.get("games", [])
        total_hrs = sum(g.get("playtime_forever", 0) for g in games) / 60
        top = max(games, key=lambda g: g.get("playtime_forever", 0))
        top_name = top.get("name", f"appid {top.get('appid', '?')}")
        top_hrs = top.get("playtime_forever", 0) / 60
        return (
            f"\x02{name}\x02 [{status_str}] | "
            f"\x02Total games\x02 {gc} | "
            f"\x02Total playtime\x02 {total_hrs:,.0f} hours | "
            f"\x02Most played\x02 {top_name}, {top_hrs:,.0f} hours"
        )

    msg = f"\x02{name}\x02 [{status_str}]"
    if state == 0:
        lo = d.get("lastlogoff")
        if lo:
            msg += f" | \x02Last seen\x02 {_timeago(lo)} ago"
    else:
        gid = d.get("gameid")
        if gid:
            gname = d.get("gameextrainfo", f"appid {gid}")
            msg += f" | \x02Playing\x02 {gname}"
            gsrv = d.get("gameserverip")
            if gsrv:
                msg += f" on {gsrv}"
        else:
            msg += " | not playing anything"
    return msg


def _register_sync(arg: str, key: str, ua: str) -> tuple[str | None, str]:
    """Resolve a steam ID/vanity URL and return (steamid, display_name) or (None, error)."""
    try:
        # Try as raw 64-bit ID first
        if arg.isdigit() and len(arg) >= 10:
            d = _get_status(arg, key, ua)
            return d["steamid"], d.get("personaname", arg)
        # Try vanity URL
        sid = _resolve_vanity(arg, key, ua)
        if sid:
            d = _get_status(sid, key, ua)
            return d["steamid"], d.get("personaname", arg)
        return None, "no user found"
    except Exception as e:
        log.warning(f"Steam register: {e}")
        return None, "lookup failed"


class SteamModule(BotModule):
    """Steam user status and game info module."""

    COMMANDS: dict[str, str] = {
        "steam": "cmd_steam",
        "regsteam": "cmd_regsteam",
        "register_steam": "cmd_regsteam",
    }

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._key: str = cred(self.bot.cfg, "steam_key", "steam", "steam_key")
        if not self._key:
            log.warning("steam: steam_key not set — .steam will not work")

        # Nick → steamid mapping (own JSON file — not a secret, just storage path)
        sect = self.bot.cfg["steam"] if "steam" in self.bot.cfg else {}
        self._ids_file = Path(sect.get("steamids_file", "steamids.json"))
        self._lock = threading.Lock()
        try:
            self._ids: dict[str, str] = json.loads(self._ids_file.read_text()) if self._ids_file.exists() else {}
        except Exception:
            self._ids = {}

    def _save_ids(self) -> None:
        with self._lock:
            try:
                self._ids_file.write_text(json.dumps(self._ids, indent=2))
            except Exception as e:
                log.warning(f"steam: failed to save IDs: {e}")

    def is_configured(self) -> bool:
        return bool(self._key)

    async def cmd_steam(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Show Steam user status.  Usage: .steam [user] or .steam -g [user]"""
        if not self._key:
            self.bot.privmsg(reply_to, "Steam API key not configured — see [steam] in config.ini")
            return

        show_games = False
        target = arg.strip() if arg else ""
        if target.startswith("-g"):
            show_games = True
            target = target[2:].strip()

        if not target:
            # Look up caller's registered ID
            sid = self._ids.get(nick.lower())
            if not sid:
                p = self.bot.cfg["bot"]["command_prefix"]
                self.bot.privmsg(
                    reply_to,
                    f"{nick}: no Steam ID registered — use {p}regsteam <steamid/vanityurl>",
                )
                return
        else:
            # Check if it's a registered nick
            parts = target.split()
            if parts[0].startswith("-n") and len(parts) > 1:
                lookup_nick = parts[1]
                sid = self._ids.get(lookup_nick.lower())
                if not sid:
                    self.bot.privmsg(reply_to, f"{nick}: '{lookup_nick}' has no registered Steam ID")
                    return
            else:
                sid = self._ids.get(target.lower())
                if not sid:
                    # Try as direct steamid / vanity URL
                    sid = target

        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_status_sync, sid, show_games, self._key, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_regsteam(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Register your Steam ID.  Usage: .regsteam <steamid or vanity URL>"""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}regsteam <steam64id or vanity URL>")
            return
        if not self._key:
            self.bot.privmsg(reply_to, "Steam API key not configured — see [steam] in config.ini")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        sid, display = await asyncio.to_thread(_register_sync, arg.strip().split()[0], self._key, self._ua)
        if sid:
            self._ids[nick.lower()] = sid
            await asyncio.to_thread(self._save_ids)
            self.bot.notice(nick, f"Steam ID registered — current persona: {display}")
        else:
            self.bot.notice(nick, display)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}steam [user/-g/-n nick]  Steam status/games",
            f"  {prefix}regsteam <id/vanity>     Register your Steam ID",
        ]


def setup(bot: object) -> SteamModule:
    """Module entry point — returns a SteamModule instance."""
    return SteamModule(bot)  # type: ignore[arg-type]
