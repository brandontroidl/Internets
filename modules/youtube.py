from __future__ import annotations

import asyncio
import logging
import re
from .base import BotModule, fetch_json, help_row, strip_ctrl

log = logging.getLogger("internets.youtube")

_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _fmt_duration(iso: str) -> str:
    """Convert ISO 8601 duration (PT1H2M3S) to h:mm:ss or m:ss."""
    m = _DURATION_RE.match(iso or "")
    if not m:
        return "?"
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def _fmt_thousand(n: int) -> str:
    return f"{n:,}"


def _search_sync(query: str, key: str, ua: str) -> str:
    """Blocking YouTube search — run via asyncio.to_thread."""
    try:
        # Step 1: search
        items = fetch_json(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet", "order": "relevance", "type": "video",
                "maxResults": "1", "q": query, "key": key,
            },
            ua=ua,
            timeout=10,
        ).get("items", [])
        if not items:
            return f"no results for '{strip_ctrl(query)}'"

        vid_id = items[0]["id"]["videoId"]
        title = strip_ctrl(items[0]["snippet"]["title"])

        # Step 2: get video details (duration, view count, likes)
        vids = fetch_json(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "contentDetails,statistics", "id": vid_id, "key": key,
            },
            ua=ua,
            timeout=10,
        ).get("items", [])
        if not vids:
            return f"\x02YouTube\x02 {title} | https://www.youtube.com/watch?v={vid_id}"

        v = vids[0]
        duration = _fmt_duration(v.get("contentDetails", {}).get("duration", ""))
        stats = v.get("statistics", {})
        views = _fmt_thousand(int(stats.get("viewCount", 0)))
        likes = _fmt_thousand(int(stats.get("likeCount", 0)))

        return (
            f"\x02YouTube\x02 {title} | "
            f"https://www.youtube.com/watch?v={vid_id} ({duration}) | "
            f"\x02Views\x02 {views} | "
            f"\x0303\x02[+]\x02\x03 {likes} likes"
        )
    except Exception as e:
        log.warning(f"YouTube search: {e}")
        return "search failed"


class YoutubeModule(BotModule):
    """YouTube video search module."""

    COMMANDS: dict[str, str] = {"yt": "cmd_yt", "youtube": "cmd_yt"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._key: str = cred(self.bot.cfg, "youtube_key", "youtube", "youtube_key")
        if not self._key:
            log.warning("youtube: youtube_key not set — .yt will not work")

    def is_configured(self) -> bool:
        return bool(self._key)

    async def cmd_yt(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Search YouTube for a video."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}yt <search>  e.g. {p}yt never gonna give you up")
            return
        if not self._key:
            self.bot.privmsg(reply_to, "YouTube API key not configured — see [youtube] in config.ini")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_search_sync, arg.strip(), self._key, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "yt/.youtube <search>", f"YouTube search  e.g. {prefix}yt cat videos")]


def setup(bot: object) -> YoutubeModule:
    """Module entry point — returns a YoutubeModule instance."""
    return YoutubeModule(bot)  # type: ignore[arg-type]
