from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import requests
from .base import BotModule, fetch_json

log = logging.getLogger("internets.twitch")


class _TwitchAPI:
    """Minimal Twitch Helix API client with automatic OAuth token management."""

    def __init__(self, client_id: str, client_secret: str, ua: str) -> None:
        self._cid = client_id
        self._secret = client_secret
        self._ua = ua
        self._token: str = ""
        self._expires: float = 0

    def _refresh_token(self) -> None:
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": self._cid,
                "client_secret": self._secret,
                "grant_type": "client_credentials",
            },
            headers={"User-Agent": self._ua},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._expires = time.time() + d.get("expires_in", 3600) - 60

    def _headers(self) -> dict[str, str]:
        if not self._token or time.time() >= self._expires:
            self._refresh_token()
        return {
            "Client-ID": self._cid,
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._ua,
        }

    def get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        # Use the shared size-capped helper to defend against an OOM via a
        # tampered upstream — even Twitch responses get a 256 KB ceiling.
        hdrs = self._headers()
        ua = hdrs.pop("User-Agent")
        return fetch_json(
            f"https://api.twitch.tv/helix/{endpoint}",
            params=params or {},
            ua=ua,
            headers=hdrs,
            timeout=10,
        )

    # ── convenience methods ──────────────────────────────────────────

    def search_channels(self, query: str, limit: int = 5) -> list[dict]:
        d = self.get("search/channels", {"query": query, "first": str(limit), "live_only": "false"})
        return d.get("data", [])

    def get_streams(self, limit: int = 5) -> list[dict]:
        d = self.get("streams", {"first": str(limit)})
        return d.get("data", [])

    def search_streams(self, game_name: str, limit: int = 5) -> list[dict]:
        # Search categories first to get game_id
        cats = self.get("search/categories", {"query": game_name, "first": "1"})
        if not cats.get("data"):
            return []
        gid = cats["data"][0]["id"]
        d = self.get("streams", {"game_id": gid, "first": str(limit)})
        return d.get("data", [])

    def get_channel_info(self, broadcaster: str) -> dict | None:
        # Resolve login to user ID
        users = self.get("users", {"login": broadcaster})
        if not users.get("data"):
            return None
        u = users["data"][0]
        info = self.get("channels", {"broadcaster_id": u["id"]})
        ch = info["data"][0] if info.get("data") else {}
        ch["_user"] = u
        return ch

    def search_games(self, query: str, limit: int = 5) -> list[dict]:
        d = self.get("search/categories", {"query": query, "first": str(limit)})
        return d.get("data", [])


def _fmt_viewers(n: int | str) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _dispatch_sync(api: _TwitchAPI, subcmd: str, arg: str) -> str:
    """Route subcommands — blocking, run via to_thread."""
    try:
        if subcmd in ("s", "stream", ""):
            if arg:
                chans = api.search_channels(arg, 3)
                if not chans:
                    return f"no channels found for '{arg}'"
                lines = []
                for i, ch in enumerate(chans, 1):
                    live = "\x0303LIVE\x03" if ch.get("is_live") else "\x0314offline\x03"
                    lines.append(
                        f"[{i}/{len(chans)}] \x02{ch['display_name']}\x02 [{live}] | "
                        f"https://twitch.tv/{ch['broadcaster_login']}"
                    )
                return " | ".join(lines) if len(lines) <= 2 else "\n".join(lines)
            else:
                streams = api.get_streams(5)
                if not streams:
                    return "no live streams"
                lines = []
                for i, s in enumerate(streams, 1):
                    lines.append(
                        f"[{i}] \x02{s.get('game_name', '?')}\x02 — "
                        f"{s['user_name']} ({_fmt_viewers(s.get('viewer_count', 0))} viewers)"
                    )
                return " | ".join(lines)

        elif subcmd in ("c", "channel"):
            if not arg:
                return "usage: .tw -c <channel>"
            info = api.get_channel_info(arg)
            if not info:
                return f"channel '{arg}' not found"
            u = info.get("_user", {})
            return (
                f"\x02{info.get('broadcaster_name', arg)}\x02 | "
                f"\x02Game\x02 {info.get('game_name', 'N/A')} | "
                f"\x02Title\x02 {info.get('title', 'N/A')} | "
                f"\x02Views\x02 {_fmt_viewers(u.get('view_count', 0))} | "
                f"https://twitch.tv/{info.get('broadcaster_login', arg)}"
            )

        elif subcmd in ("g", "game"):
            if not arg:
                return "usage: .tw -g <game>"
            games = api.search_games(arg, 5)
            if not games:
                return f"no games found for '{arg}'"
            lines = []
            for i, g in enumerate(games, 1):
                lines.append(
                    f"[{i}/{len(games)}] \x02{g['name']}\x02 | "
                    f"https://www.twitch.tv/directory/category/{g.get('id', '')}"
                )
            return " | ".join(lines) if len(lines) <= 3 else "\n".join(lines)

        else:
            return "usage: .tw [-s query] [-c channel] [-g game]"

    except Exception as e:
        log.warning(f"Twitch lookup: {e}")
        return "lookup failed"


class TwitchModule(BotModule):
    """Twitch stream, channel, and game lookup module (Helix API)."""

    COMMANDS: dict[str, str] = {"tw": "cmd_twitch", "twitch": "cmd_twitch"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        cid    = cred(self.bot.cfg, "twitch_client_id",     "twitch", "twitch_client_id")
        secret = cred(self.bot.cfg, "twitch_client_secret", "twitch", "twitch_client_secret")
        self._api: _TwitchAPI | None = None
        if cid and secret:
            self._api = _TwitchAPI(cid, secret, self._ua)
        else:
            log.warning("twitch: client_id/client_secret not set — .tw will not work")

    def is_configured(self) -> bool:
        return self._api is not None

    async def cmd_twitch(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up Twitch streams, channels, or games."""
        if not self._api:
            self.bot.privmsg(reply_to, "Twitch API not configured — see [twitch] in config.ini")
            return

        raw = (arg or "").strip()
        if not raw:
            # Default: show top streams
            subcmd, query = "", ""
        elif raw.startswith("-"):
            parts = raw.split(None, 1)
            flag = parts[0].lstrip("-")
            query = parts[1] if len(parts) > 1 else ""
            subcmd = flag
        else:
            subcmd, query = "s", raw

        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_dispatch_sync, self._api, subcmd, query)
        for line in result.split("\n"):
            self.bot.privmsg(reply_to, line)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}tw/.twitch [-s query]    Search streams (default: top live)",
            f"  {prefix}tw -c <channel>          Channel info",
            f"  {prefix}tw -g <game>             Search games",
        ]


def setup(bot: object) -> TwitchModule:
    """Module entry point — returns a TwitchModule instance."""
    return TwitchModule(bot)  # type: ignore[arg-type]
