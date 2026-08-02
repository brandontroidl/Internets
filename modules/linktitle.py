"""Passive URL title announcer - shows page titles when users post links.

Watches channel messages for http/https URLs and announces the page title.
YouTube links get enriched output (title, duration, views, likes) via the
Data API when a key is configured; otherwise falls back to the page title.

SSRF: every user-posted URL goes through _netsafe.safe_open (DNS-pinned,
redirect-validated) so the bot cannot be aimed at internal services.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from html.parser import HTMLParser

import requests
from .base import BotModule, cred, fetch_json, help_row, strip_ctrl
from ._netsafe import SSRFBlocked, safe_open

log = logging.getLogger("internets.linktitle")

_RE_PRIVMSG = re.compile(r"^:([^!\s]+)![^\s]+\s+PRIVMSG\s+(\S+)\s+:(.*)$")

_RE_URL = re.compile(r"https?://[^\s<>\x00-\x1f]+", re.IGNORECASE)

_RE_YT = re.compile(
    r"(?:youtube\.com/watch\?[^\s]*v=|youtu\.be/|youtube\.com/shorts/)"
    r"([A-Za-z0-9_-]{11})",
)

_TITLE_MAX_BYTES = 256 * 1024
_FETCH_TIMEOUT = 8
_COOLDOWN = 3.0
_DEDUP_TTL = 300.0
_MAX_URLS_PER_MSG = 3
_TITLE_MAX_LEN = 300

_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

_IGNORE_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "[::1]",
})

_IGNORE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".mp3", ".mp4", ".webm", ".ogg", ".flac", ".wav", ".avi", ".mkv", ".mov",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".exe", ".dmg", ".iso", ".bin", ".deb", ".rpm",
})


class _TitleParser(HTMLParser):
    """Extract the <title> text from an HTML document."""

    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self._chunks: list[str] = []
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self.done:
            self._in_title = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self.done = True

    @property
    def title(self) -> str:
        raw = " ".join(self._chunks)
        return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _fmt_duration(iso: str) -> str:
    m = _DURATION_RE.match(iso or "")
    if not m:
        return "?"
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def _fetch_yt_sync(vid_id: str, key: str, ua: str) -> str | None:
    """Fetch YouTube video details via Data API. Returns formatted string or None."""
    try:
        vids = fetch_json(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,contentDetails,statistics",
                "id": vid_id, "key": key,
            },
            ua=ua,
            timeout=_FETCH_TIMEOUT,
        ).get("items", [])
        if not vids:
            return None
        v = vids[0]
        title = strip_ctrl(v.get("snippet", {}).get("title", ""), _TITLE_MAX_LEN)
        if not title:
            return None
        duration = _fmt_duration(v.get("contentDetails", {}).get("duration", ""))
        stats = v.get("statistics", {})
        views = f"{int(stats.get('viewCount', 0)):,}"
        likes = f"{int(stats.get('likeCount', 0)):,}"
        channel = strip_ctrl(v.get("snippet", {}).get("channelTitle", ""), 80)
        parts = [f"\x02YouTube\x02 {title}"]
        if channel:
            parts.append(f"by {channel}")
        parts.append(f"({duration})")
        parts.append(f"\x02Views\x02 {views}")
        parts.append(f"\x0303\x02[+]\x02\x03 {likes} likes")
        return " | ".join(parts)
    except Exception as e:
        log.debug("linktitle: yt api: %s", e)
        return None


def _fetch_title_sync(url: str, ua: str) -> str | None:
    """Fetch a page and extract its <title>. Returns None on failure."""
    try:
        with safe_open("GET", url, ua, follow_redirects=True,
                       timeout=_FETCH_TIMEOUT) as resp:
            ct = resp.headers.get("content-type", "")
            if "text/html" not in ct and "application/xhtml" not in ct:
                return None
            raw = resp.raw.read(_TITLE_MAX_BYTES + 1, decode_content=True)
    except SSRFBlocked:
        return None
    except requests.RequestException as e:
        log.debug("linktitle: fetch %s: %s", url, e)
        return None
    if len(raw) > _TITLE_MAX_BYTES:
        return None
    parser = _TitleParser()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None
    title = parser.title
    if not title:
        return None
    return strip_ctrl(title, _TITLE_MAX_LEN)


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _ext_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path.lower()
        dot = path.rfind(".")
        if dot != -1:
            return path[dot:]
    except Exception:
        pass
    return ""


class LinkTitleModule(BotModule):
    """Passive URL title announcer for channel messages."""

    COMMANDS: dict[str, str] = {}

    def on_load(self) -> None:
        from .base import cred as _cred
        self._ua: str = _cred(self.bot.cfg, "weather_user_agent",
                              "weather", "user_agent", "Internets/1.0")
        self._yt_key: str = _cred(self.bot.cfg, "youtube_key", "youtube", "youtube_key")
        self._prefix: str = self.bot.cfg["bot"].get("command_prefix", ".")
        self._last: dict[str, float] = {}
        self._seen_urls: dict[str, float] = {}

    def _should_skip(self, channel: str, url: str) -> bool:
        now = time.monotonic()
        if now - self._last.get(channel, 0) < _COOLDOWN:
            return True
        url_key = f"{channel}\0{url}"
        if now - self._seen_urls.get(url_key, 0) < _DEDUP_TTL:
            return True
        if _host_of(url) in _IGNORE_HOSTS:
            return True
        if _ext_of(url) in _IGNORE_EXTS:
            return True
        return False

    def _mark(self, channel: str, url: str) -> None:
        now = time.monotonic()
        self._last[channel] = now
        self._seen_urls[f"{channel}\0{url}"] = now
        if len(self._seen_urls) > 500:
            cutoff = now - _DEDUP_TTL
            self._seen_urls = {k: v for k, v in self._seen_urls.items()
                               if v > cutoff}

    def on_raw(self, line: str) -> None:
        try:
            m = _RE_PRIVMSG.match(line)
            if not m:
                return
            nick, target, text = m.group(1), m.group(2), m.group(3)
            if not target.startswith(("#", "&", "+", "!")):
                return
            if nick.lower() == self.bot._nick.lower():
                return
            if text.startswith(self._prefix):
                return
            if text.startswith("\x01"):
                return
            urls = _RE_URL.findall(text)
            if not urls:
                return
            for url in urls[:_MAX_URLS_PER_MSG]:
                if self._should_skip(target, url):
                    continue
                self._mark(target, url)
                asyncio.ensure_future(self._announce(target, url))
        except Exception as e:
            log.debug("linktitle: on_raw: %s", e)

    async def _announce(self, channel: str, url: str) -> None:
        yt_match = _RE_YT.search(url)
        if yt_match and self._yt_key:
            result = await asyncio.to_thread(
                _fetch_yt_sync, yt_match.group(1), self._yt_key, self._ua)
            if result:
                self.bot.privmsg(channel, result)
                return
        title = await asyncio.to_thread(_fetch_title_sync, url, self._ua)
        if title:
            self.bot.privmsg(channel, f"\x02Link\x02 {title}")

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "(passive)", "Shows page titles for URLs posted in channels")]


def setup(bot: object) -> LinkTitleModule:
    return LinkTitleModule(bot)  # type: ignore[arg-type]
