"""Tests for the STEM news aggregator + reader (network mocked)."""
from __future__ import annotations

import asyncio
from collections import Counter
from configparser import ConfigParser

import modules.scinews as sn

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>First &amp; &lt;b&gt;bold&lt;/b&gt;</title><link>https://a.test/1</link>
<pubDate>Mon, 23 Jun 2026 10:00:00 GMT</pubDate></item>
<item><title>Second</title><link>https://a.test/2</link>
<pubDate>Mon, 23 Jun 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom One</title><link href="https://b.test/1"/>
<updated>2026-06-23T10:00:00Z</updated></entry></feed>"""


def test_parse_rss():
    items = sn._parse_feed(RSS)
    assert len(items) == 2
    assert items[0][0] == "First & bold"        # HTML cleaned
    assert items[0][1] == "https://a.test/1"
    assert items[0][2] > 0


def test_parse_atom_href():
    items = sn._parse_feed(ATOM)
    assert items[0][1] == "https://b.test/1"     # link href, not text


def test_parse_garbage_is_empty():
    assert sn._parse_feed(b"<<<not xml") == []


def test_clean():
    assert sn._clean("a &amp; <i>b</i>") == "a & b"


def test_parse_date():
    assert sn._parse_date("Mon, 23 Jun 2026 10:00:00 GMT") > 0
    assert sn._parse_date("2026-06-23T10:00:00Z") > 0
    assert sn._parse_date("garbage") == 0.0
    assert sn._parse_date(None) == 0.0


def test_lead_prefers_og():
    p = sn._Lead()
    p.feed('<meta property="og:description" content="Hello desc"><p>body</p>')
    assert p.lead() == "Hello desc"


def test_lead_first_paragraph():
    p = sn._Lead()
    p.feed("<p>First para</p><p>Second</p>")
    assert "First para" in p.lead()


class FakeBot:
    def __init__(self):
        self.cfg = ConfigParser()
        self.cfg.read_dict({"bot": {"command_prefix": "."}, "weather": {"user_agent": "t"}})
        self.out: list[str] = []
    def rate_limited(self, n): return False
    def notice(self, n, m): self.out.append(m)
    def privmsg(self, t, m): self.out.append(m)


def _mod():
    m = sn.ScinewsModule(FakeBot())
    m.on_load()
    return m


def test_diversity_and_order(monkeypatch):
    # 4 sources so the per-source cap can fill _MAX_ITEMS without relaxing.
    bases = {"A": 100, "B": 90, "C": 80, "D": 70}
    def fake(source, url, ua):
        b = bases[source]
        return [(b - i, source, f"{source}{i}", f"https://{source}/{i}") for i in range(3)]
    monkeypatch.setattr(sn, "_fetch_one", fake)
    monkeypatch.setattr(sn, "_FEEDS", {s: ("u", {"all"}) for s in bases})
    m = _mod()
    items = asyncio.run(m._get_items("all"))
    counts = Counter(s for s, _t, _u in items)
    assert all(v <= sn._PER_SOURCE for v in counts.values())   # per-source cap holds
    assert items[0][0] == "A"            # newest (ts 100) first
    assert len(items) == sn._MAX_ITEMS


def test_cmd_list_then_read(monkeypatch):
    m = _mod()
    async def fake_items(topic):
        return [("Nature", "T1", "https://n/1"), ("MIT", "T2", "https://m/2")]
    monkeypatch.setattr(m, "_get_items", fake_items)
    monkeypatch.setattr(sn, "_read_article", lambda url, ua: "LEAD TEXT")
    asyncio.run(m.cmd_sci("u", "#c", ""))
    assert any("1. [Nature] T1" in x for x in m.bot.out)
    m.bot.out = []
    asyncio.run(m.cmd_sci("u", "#c", "read 2"))
    assert any("[MIT] T2" in x for x in m.bot.out)
    assert any("LEAD TEXT" in x for x in m.bot.out)


def test_read_without_list():
    m = _mod()
    asyncio.run(m.cmd_sci("u", "#c", "read 1"))
    assert any("sci first" in x for x in m.bot.out)


def test_bad_topic():
    m = _mod()
    asyncio.run(m.cmd_sci("u", "#c", "zzz"))
    assert any("unknown topic" in x for x in m.bot.out)


def test_sources():
    m = _mod()
    asyncio.run(m.cmd_sci("u", "#c", "sources"))
    assert any("topics:" in x for x in m.bot.out)


def test_read_article_refuses_internal():
    # SSRF guard wired into the reader.
    out = sn._read_article("http://127.0.0.1/x", "ua")
    assert "non-public" in out or "can't read" in out


def test_reader_blocks_redirect_to_internal(monkeypatch):
    # A public article that 302-redirects to the cloud-metadata IP must be
    # refused — the guard re-checks each hop, not just the initial host.
    import socket

    def fake_getaddrinfo(host, *a, **k):
        ip = "93.184.216.34" if host == "example.com" else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    class Redir:
        is_redirect = True
        status_code = 302
        headers = {"Location": "http://169.254.169.254/latest/meta-data/"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
    monkeypatch.setattr(sn.requests, "get", lambda *a, **k: Redir())

    out = sn._read_article("https://example.com/article", "ua")
    assert "can't read" in out and "non-public" in out
