from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from .base import BotModule, fetch_json

log = logging.getLogger("internets.lastfm")


def _fmt_thousand(n: int) -> str:
    return f"{n:,}"


def _timeago(ts: int) -> str:
    """Human-readable time-ago from a unix timestamp."""
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _lookup_sync(username: str, key: str, ua: str) -> str:
    """Blocking Last.fm lookup — run via asyncio.to_thread."""
    base = "https://ws.audioscrobbler.com/2.0/"
    try:
        # Get user info
        data = fetch_json(
            base,
            params={"method": "user.getinfo", "user": username,
                    "api_key": key, "format": "json"},
            ua=ua,
            timeout=10,
        )
        if "error" in data:
            return f"{data.get('message', 'user not found')}"
        user = data["user"]

        # Get recent tracks
        recent = fetch_json(
            base,
            params={"method": "user.getrecenttracks", "user": username,
                    "limit": "1", "api_key": key, "format": "json"},
            ua=ua,
            timeout=10,
        )

        # Build user info string
        parts: list[str] = []
        rn = user.get("realname", "")
        if rn:
            parts.append(rn)
        country = user.get("country", "")
        if country and country != "None":
            parts.append(country)
        info_str = f" [{', '.join(parts)}]" if parts else ""

        plays = _fmt_thousand(int(user.get("playcount", 0)))
        reg_ts = int(user.get("registered", {}).get("unixtime", 0))
        reg_date = datetime.fromtimestamp(reg_ts, tz=timezone.utc).strftime("%Y-%m-%d") if reg_ts else "?"

        # Build now-playing / latest track
        track_str = ""
        tracks = recent.get("recenttracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        if tracks:
            t = tracks[0]
            artist = t.get("artist", {}).get("#text", "?")
            name = t.get("name", "?")
            now_playing = t.get("@attr", {}).get("nowplaying", "false") == "true"
            if now_playing:
                track_str = f" | \x02Now playing\x02 {artist} — {name}"
            elif "date" in t:
                ago = _timeago(int(t["date"]["uts"]))
                track_str = f" | \x02Latest\x02 {artist} — {name} ({ago} ago)"

        return (
            f"\x02{user['name']}\x02{info_str} | "
            f"\x02Plays\x02 {plays} since {reg_date} | "
            f"\x02Link\x02 {user.get('url', '')}"
            f"{track_str}"
        )
    except Exception as e:
        log.warning(f"Last.fm lookup: {e}")
        return "lookup failed"


class LastfmModule(BotModule):
    """Last.fm user lookup module."""

    COMMANDS: dict[str, str] = {"lastfm": "cmd_lastfm"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._key: str = cred(self.bot.cfg, "lastfm_key", "lastfm", "lastfm_key")
        if not self._key:
            log.warning("lastfm: lastfm_key not set — .lastfm will not work")

    def is_configured(self) -> bool:
        return bool(self._key)

    async def cmd_lastfm(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up a Last.fm user profile and recent track."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}lastfm <username>  e.g. {p}lastfm RJ")
            return
        if not self._key:
            self.bot.privmsg(reply_to, "Last.fm API key not configured — see [lastfm] in config.ini")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_lookup_sync, arg.strip().split()[0], self._key, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}lastfm <user>          Last.fm profile + now playing"]


def setup(bot: object) -> LastfmModule:
    """Module entry point — returns a LastfmModule instance."""
    return LastfmModule(bot)  # type: ignore[arg-type]
