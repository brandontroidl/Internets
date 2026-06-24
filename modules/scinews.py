"""STEM news / journal / paper aggregator + reader — curated keyless feeds.

    .sci [topic]    latest science headlines, merged + deduped from RSS/Atom
                    feeds.  topic = all (default) / physics / cs / math / bio
                    / astro / space.
    .sci read <N>   open item N from the last list: lead paragraph + link.
    .sci sources    list the feed topics.

All feeds are keyless RSS/Atom (parsed with defusedxml).  The reader fetches
the chosen article (size-capped + SSRF-guarded via base.resolve_public) and
extracts its og:description / meta description / first paragraph — no LLM.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests
from defusedxml import ElementTree as _ET
from .base import BotModule, cred, help_row, strip_ctrl
from ._netsafe import SSRFBlocked, safe_open

log = logging.getLogger("internets.scinews")

_FEED_TIMEOUT = 6
_FEED_MAX_BYTES = 6 * 1024 * 1024   # arXiv per-archive RSS (cs/math) runs several MB
_ART_MAX_BYTES = 512 * 1024
_LIST_TTL = 600.0       # per-channel "last list" lifetime (s)
_CACHE_TTL = 120.0      # aggregate-fetch cache (s)
_MAX_ITEMS = 6
_PER_SOURCE = 2         # diversity: at most this many items from one feed
_FETCH_CONCURRENCY = 8  # cap simultaneous feed fetches (protect the thread pool)

# name -> (feed url, {topic tags})
_FEEDS: dict[str, tuple[str, set[str]]] = {
    "Nature":        ("https://www.nature.com/nature.rss",                 {"all", "bio", "physics"}),
    "Science":       ("https://www.science.org/rss/news_current.xml",      {"all"}),
    "phys.org":      ("https://phys.org/rss-feed/",                        {"all", "physics"}),
    "ScienceDaily":  ("https://www.sciencedaily.com/rss/top/science.xml",  {"all"}),
    "Quanta":        ("https://www.quantamagazine.org/feed/",              {"all", "math", "physics"}),
    "MIT News":      ("https://news.mit.edu/rss/feed",                     {"all", "cs"}),
    "Ars Science":   ("https://feeds.arstechnica.com/arstechnica/science", {"all"}),
    "arXiv cs":      ("https://rss.arxiv.org/rss/cs",                      {"cs"}),
    "arXiv physics": ("https://rss.arxiv.org/rss/physics",                 {"physics"}),
    "arXiv math":    ("https://rss.arxiv.org/rss/math",                    {"math"}),
    "arXiv q-bio":   ("https://rss.arxiv.org/rss/q-bio",                   {"bio"}),
    "arXiv astro":   ("https://rss.arxiv.org/rss/astro-ph",               {"astro", "space"}),
    "New Scientist": ("https://www.newscientist.com/feed/home/",          {"all"}),
    "Sci. American": ("https://www.scientificamerican.com/platform/syndication/rss/", {"all"}),
    "Live Science":  ("https://www.livescience.com/feeds/all",            {"all"}),
    "Eos":           ("https://eos.org/feed",                             {"all"}),
    "MIT Tech Rev":  ("https://www.technologyreview.com/feed/",           {"all", "tech", "ai"}),
    "The Register":  ("https://www.theregister.com/headlines.atom",       {"tech", "cs"}),
    "IEEE Spectrum": ("https://spectrum.ieee.org/feeds/feed.rss",         {"tech", "cs", "physics"}),
    "Ars Technica":  ("https://feeds.arstechnica.com/arstechnica/index",  {"tech"}),
    "arXiv cs.AI":   ("https://rss.arxiv.org/rss/cs.AI",                  {"cs", "ai"}),
    "arXiv cs.LG":   ("https://rss.arxiv.org/rss/cs.LG",                  {"cs", "ai"}),
    "Physics World": ("https://physicsworld.com/feed/",                   {"physics"}),
    "STAT News":     ("https://www.statnews.com/feed/",                   {"bio"}),
    "Space.com":     ("https://www.space.com/feeds/all",                  {"space", "astro"}),
    "NASA":          ("https://www.nasa.gov/feed/",                       {"space", "astro"}),
    "APS Physics":   ("https://feeds.aps.org/rss/recent/physics.xml",     {"physics"}),
    # security: infosec news, advisories, and offensive (pentest) research
    "TheHackerNews": ("https://feeds.feedburner.com/TheHackersNews",      {"sec"}),
    "BleepingComp":  ("https://www.bleepingcomputer.com/feed/",           {"sec"}),
    "Krebs":         ("https://krebsonsecurity.com/feed/",                {"sec"}),
    "Dark Reading":  ("https://www.darkreading.com/rss.xml",              {"sec"}),
    "SecurityWeek":  ("https://www.securityweek.com/feed/",               {"sec"}),
    "Schneier":      ("https://www.schneier.com/feed/atom/",              {"sec"}),
    "Reg Security":  ("https://www.theregister.com/security/headlines.atom", {"sec", "tech"}),
    "SANS ISC":      ("https://isc.sans.edu/rssfeed.xml",                 {"sec", "pentest"}),
    "CISA":          ("https://www.cisa.gov/cybersecurity-advisories/all.xml", {"sec"}),
    "Exploit-DB":    ("https://www.exploit-db.com/rss.xml",               {"sec", "pentest"}),
    "Project Zero":  ("https://googleprojectzero.blogspot.com/feeds/posts/summary?max-results=25", {"sec", "pentest"}),
    "PortSwigger":   ("https://portswigger.net/research/rss",             {"sec", "pentest"}),
    "The Record":    ("https://therecord.media/feed/",                    {"sec"}),
    "Help Net Sec":  ("https://www.helpnetsecurity.com/feed/",            {"sec"}),
    # deeper threat-intel / IR (your honeypot + DNSBL + AbuseIPDB wheelhouse)
    "DFIR Report":   ("https://thedfirreport.com/feed/",                  {"sec", "pentest"}),
    "Unit 42":       ("https://unit42.paloaltonetworks.com/feed/",        {"sec"}),
    "Cisco Talos":   ("https://blog.talosintelligence.com/rss/",          {"sec"}),
    "abuse.ch":      ("https://abuse.ch/rss/",                            {"sec"}),
    # OpenBSD
    "OpenBSD":       ("https://undeadly.org/cgi?action=rss",              {"bsd"}),
    # AI / LLM / agents
    "Simon Willison":("https://simonwillison.net/atom/everything/",       {"ai"}),
    "Hugging Face":  ("https://huggingface.co/blog/feed.xml",             {"ai"}),
    "OpenAI":        ("https://openai.com/news/rss.xml",                  {"ai"}),
    "DeepMind":      ("https://deepmind.google/blog/rss.xml",             {"ai"}),
    "Import AI":     ("https://importai.substack.com/feed",               {"ai"}),
    "Latent Space":  ("https://www.latent.space/feed",                    {"ai"}),
}
_TOPICS = sorted({t for _u, tags in _FEEDS.values() for t in tags})

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    """Strip residual HTML tags + unescape entities from feed/article text."""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s))).strip()


def _http_bytes(url: str, ua: str, max_bytes: int) -> bytes:
    """Size-capped raw GET for the hardcoded trusted feed URLs in _FEEDS.

    Feeds are operator-curated constants, so the SSRF pinning the reader
    needs (see _read_article -> _netsafe.safe_open) is not required here.
    """
    with requests.get(url, headers={"User-Agent": ua}, timeout=_FEED_TIMEOUT,
                      stream=True) as r:
        r.raise_for_status()
        raw = r.raw.read(max_bytes + 1, decode_content=True)
        if len(raw) > max_bytes:
            raise ValueError("response too large")
        return raw


def _parse_date(s: str | None) -> float:
    """Best-effort feed date -> epoch seconds (0.0 if unknown)."""
    if not s:
        return 0.0
    s = s.strip()
    try:
        return parsedate_to_datetime(s).timestamp()            # RFC 822 (RSS)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO 8601 (Atom)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_feed(raw: bytes) -> list[tuple[str, str, float]]:
    """Parse RSS or Atom bytes -> list of (title, link, ts)."""
    out: list[tuple[str, str, float]] = []
    try:
        root = _ET.fromstring(raw)
    except Exception:  # noqa: BLE001 — malformed feed
        return out
    for e in root.iter():
        if _localname(e.tag) not in ("item", "entry"):
            continue
        title = link = date = None
        for c in e:
            name = _localname(c.tag)
            if name == "title" and c.text:
                title = c.text.strip()
            elif name == "link":
                # Atom: <link href="...">; RSS: <link>text</link>
                link = c.get("href") or (c.text.strip() if c.text else None)
            elif name in ("pubdate", "published", "updated", "date") and c.text and not date:
                date = c.text.strip()
        if title and link:
            out.append((strip_ctrl(_clean(title), 160), link.strip(), _parse_date(date)))
    return out


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", t.lower()).strip()


def _fetch_one(source: str, url: str, ua: str) -> list[tuple[float, str, str, str]]:
    """Fetch+parse a single feed -> its newest items as (ts, source, title, url)."""
    try:
        raw = _http_bytes(url, ua, _FEED_MAX_BYTES)
    except (requests.RequestException, ValueError) as e:
        log.warning("scinews feed %s: %s", source, e)
        return []
    items = sorted(_parse_feed(raw), key=lambda x: x[2], reverse=True)[:10]
    return [(ts, source, title, link) for title, link, ts in items]


class _Lead(HTMLParser):
    """Pull og:description / meta description + the first real <p> from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.desc: str | None = None
        self._in_p = False
        self._p_chunks: list[str] = []
        self._p_done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("property") or a.get("name") or "").lower()
            content = a.get("content") or ""
            if content and key in ("og:description", "description", "twitter:description"):
                if self.desc is None or key == "og:description":
                    self.desc = content
        elif tag == "p" and not self._p_done and not self._p_chunks:
            self._in_p = True

    def handle_data(self, data: str) -> None:
        if self._in_p and not self._p_done:
            self._p_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._in_p:
            self._in_p = False
            if "".join(self._p_chunks).strip():
                self._p_done = True

    def lead(self) -> str:
        if self.desc and self.desc.strip():
            return self.desc.strip()
        return "".join(self._p_chunks).strip()


def _read_article(url: str, ua: str) -> str:
    """Fetch an article lead via the SSRF-safe pinned fetch.

    Article links come from feed items (attacker-influenceable), so this uses
    _netsafe.safe_open, which resolves+validates+pins the IP for the initial
    host AND every redirect hop (closing the DNS-rebinding TOCTOU).
    """
    try:
        with safe_open("GET", url, ua, follow_redirects=True, timeout=_FEED_TIMEOUT) as resp:
            resp.raise_for_status()
            raw = resp.raw.read(_ART_MAX_BYTES + 1, decode_content=True)
    except SSRFBlocked as e:
        return f"can't read that article ({e})"
    except requests.RequestException as e:
        log.warning("scinews read %s: %s", url, e)
        return "could not fetch article"
    if len(raw) > _ART_MAX_BYTES:
        return "could not fetch article (too large)"
    parser = _Lead()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — malformed HTML
        pass
    lead = _clean(parser.lead())
    if not lead:
        return "(no preview available)"
    if len(lead) > 320:
        lead = lead[:317] + "..."
    return strip_ctrl(lead, 340)


class ScinewsModule(BotModule):
    """`.sci` — STEM news aggregator + keyless article reader."""

    COMMANDS: dict[str, str] = {"sci": "cmd_sci"}

    def on_load(self) -> None:
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        # per-channel last list: reply_to -> (ts, [(source, title, url), ...])
        self._last: dict[str, tuple[float, list[tuple[str, str, str]]]] = {}
        # aggregate cache: topic -> (ts, [(source, title, url), ...])
        self._cache: dict[str, tuple[float, list[tuple[str, str, str]]]] = {}

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def _get_items(self, topic: str) -> list[tuple[str, str, str]]:
        now = time.monotonic()
        cached = self._cache.get(topic)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]
        feeds = [(name, url) for name, (url, tags) in _FEEDS.items() if topic in tags]
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _fetch(name: str, url: str):
            async with sem:
                return await asyncio.to_thread(_fetch_one, name, url, self._ua)

        results = await asyncio.gather(
            *[_fetch(name, url) for name, url in feeds],
            return_exceptions=True,
        )
        flat = [it for r in results if isinstance(r, list) for it in r]
        flat.sort(key=lambda x: x[0], reverse=True)   # newest first, globally

        merged: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        per_src: dict[str, int] = {}
        # First pass: newest-first with a per-source cap for diversity.
        for _ts, src, title, url in flat:
            k = _norm_title(title)
            if k in seen or per_src.get(src, 0) >= _PER_SOURCE:
                continue
            seen.add(k)
            per_src[src] = per_src.get(src, 0) + 1
            merged.append((src, title, url))
            if len(merged) >= _MAX_ITEMS:
                break
        # Second pass: if few sources left us short, fill with the rest.
        if len(merged) < _MAX_ITEMS:
            for _ts, src, title, url in flat:
                k = _norm_title(title)
                if k in seen:
                    continue
                seen.add(k)
                merged.append((src, title, url))
                if len(merged) >= _MAX_ITEMS:
                    break

        self._cache[topic] = (now, merged)
        return merged

    async def cmd_sci(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        p = self.bot.cfg["bot"]["command_prefix"]
        parts = (arg or "").split()

        if parts and parts[0].lower() == "sources":
            self.bot.privmsg(reply_to, "topics: " + ", ".join(_TOPICS)
                             + f"  ({len(_FEEDS)} feeds)")
            return

        if parts and parts[0].lower() == "read":
            if len(parts) < 2 or not parts[1].isdigit():
                self.bot.privmsg(reply_to, f"{nick}: {p}sci read <N>")
                return
            entry = self._last.get(reply_to)
            if not entry or time.monotonic() - entry[0] > _LIST_TTL:
                self.bot.privmsg(reply_to, f"{nick}: run {p}sci first (then {p}sci read <N>)")
                return
            idx = int(parts[1]) - 1
            items = entry[1]
            if not (0 <= idx < len(items)):
                self.bot.privmsg(reply_to, f"{nick}: pick 1-{len(items)}")
                return
            src, title, url = items[idx]
            lead = await asyncio.to_thread(_read_article, url, self._ua)
            self.bot.privmsg(reply_to, f"[{strip_ctrl(src, 20)}] {strip_ctrl(title, 120)}")
            self.bot.privmsg(reply_to, f"  {lead}")
            self.bot.privmsg(reply_to, f"  {strip_ctrl(url, 200)}")
            return

        topic = parts[0].lower() if parts else "all"
        if topic not in _TOPICS:
            self.bot.privmsg(reply_to, f"{nick}: unknown topic — try: {', '.join(_TOPICS)}")
            return
        items = await self._get_items(topic)
        if not items:
            self.bot.privmsg(reply_to, "no headlines right now (feeds unreachable?)")
            return
        now = time.monotonic()
        # Evict stale entries so this map can't grow unbounded across many
        # channels/nicks (PM keys are attacker-controlled).
        self._last = {k: v for k, v in self._last.items() if now - v[0] <= _LIST_TTL}
        self._last[reply_to] = (now, items)
        self.bot.privmsg(reply_to, f":: STEM news ({topic}) — {p}sci read <N> for details ::")
        for i, (src, title, _url) in enumerate(items, 1):
            self.bot.privmsg(reply_to, f"  {i}. [{strip_ctrl(src, 18)}] {strip_ctrl(title, 150)}")

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "sci [topic]", "Science/infosec/AI/BSD headlines; topics via .sci sources"),
            help_row(prefix, "sci read <N>", "Read item N from the last list (lead + link)"),
            help_row(prefix, "sci sources", "List feed topics"),
        ]


def setup(bot: object) -> ScinewsModule:
    return ScinewsModule(bot)  # type: ignore[arg-type]
