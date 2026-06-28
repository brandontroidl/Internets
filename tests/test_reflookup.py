"""Tests for modules/reflookup.py.

Every network-touching helper has its fetch_json (or the arXiv raw fetcher)
monkeypatched with canned responses - no real network is hit.  Pure helpers
(element_lookup) are tested directly.
"""
from __future__ import annotations

import pytest

from modules import reflookup


# ── helpers ───────────────────────────────────────────────────────────────
def _patch_fetch(monkeypatch, value=None, *, exc=None):
    """Replace reflookup.fetch_json with a stub returning `value` (or raising)."""
    def _stub(*a, **k):
        if exc is not None:
            raise exc
        return value
    monkeypatch.setattr(reflookup, "fetch_json", _stub)


# ── element (pure, no network) ─────────────────────────────────────────────
class TestElement:
    def test_by_symbol(self):
        out = reflookup.element_lookup("Fe")
        assert "Iron" in out and "Z=26" in out and "transition metal" in out

    def test_by_name_case_insensitive(self):
        out = reflookup.element_lookup("gold")
        assert "Gold" in out and "(Au)" in out and "Z=79" in out

    def test_by_atomic_number(self):
        out = reflookup.element_lookup("8")
        assert "Oxygen" in out and "Z=8" in out

    def test_period_and_group(self):
        out = reflookup.element_lookup("H")
        assert "period 1" in out and "group 1" in out

    def test_lanthanide_no_group(self):
        out = reflookup.element_lookup("Ce")
        assert "Cerium" in out and "group -" in out

    def test_not_found(self):
        assert "no element" in reflookup.element_lookup("Xx")

    def test_empty(self):
        assert "usage" in reflookup.element_lookup("")


# ── wiki ───────────────────────────────────────────────────────────────────
class TestWiki:
    def test_happy(self, monkeypatch):
        _patch_fetch(monkeypatch, {
            "type": "standard",
            "title": "Python (programming language)",
            "extract": "Python is a high-level language.",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Python"}},
        })
        out = reflookup._wiki_sync("Python", "UA")
        assert "Python is a high-level language." in out
        assert "https://en.wikipedia.org/wiki/Python" in out

    def test_disambiguation(self, monkeypatch):
        _patch_fetch(monkeypatch, {
            "type": "disambiguation",
            "title": "Mercury",
            "extract": "Mercury may refer to:",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Mercury"}},
        })
        out = reflookup._wiki_sync("Mercury", "UA")
        assert "disambiguation" in out

    def test_not_found(self, monkeypatch):
        _patch_fetch(monkeypatch, None)
        assert "no Wikipedia article" in reflookup._wiki_sync("zzqqxx", "UA")

    def test_malformed(self, monkeypatch):
        _patch_fetch(monkeypatch, "not a dict")
        assert "no Wikipedia article" in reflookup._wiki_sync("x", "UA")

    def test_exception(self, monkeypatch):
        _patch_fetch(monkeypatch, exc=ValueError("boom"))
        assert reflookup._wiki_sync("x", "UA") == "lookup failed"


# ── doi ────────────────────────────────────────────────────────────────────
class TestDoi:
    def test_happy(self, monkeypatch):
        _patch_fetch(monkeypatch, {"message": {
            "title": ["A Great Paper"],
            "author": [{"given": "Ada", "family": "Lovelace"},
                       {"given": "Alan", "family": "Turing"}],
            "container-title": ["Nature"],
            "issued": {"date-parts": [[2013]]},
        }})
        out = reflookup._doi_sync("10.1/x", "UA")
        assert "A Great Paper" in out
        assert "Ada Lovelace" in out
        assert "Nature" in out
        assert "2013" in out

    def test_not_found(self, monkeypatch):
        _patch_fetch(monkeypatch, None)
        assert "no Crossref record" in reflookup._doi_sync("10.bad", "UA")

    def test_malformed_missing_fields(self, monkeypatch):
        _patch_fetch(monkeypatch, {"message": {}})
        out = reflookup._doi_sync("10.1/x", "UA")
        assert "(untitled)" in out

    def test_exception(self, monkeypatch):
        _patch_fetch(monkeypatch, exc=ValueError("boom"))
        assert reflookup._doi_sync("x", "UA") == "lookup failed"


# ── isbn ───────────────────────────────────────────────────────────────────
class TestIsbn:
    def test_happy(self, monkeypatch):
        _patch_fetch(monkeypatch, {"ISBN:9780131103627": {
            "title": "The C Programming Language",
            "authors": [{"name": "Brian Kernighan"}, {"name": "Dennis Ritchie"}],
            "publish_date": "1988",
            "publishers": [{"name": "Prentice Hall"}],
        }})
        out = reflookup._isbn_sync("978-0-13-110362-7", "UA")
        assert "The C Programming Language" in out
        assert "Brian Kernighan" in out
        assert "1988" in out
        assert "Prentice Hall" in out

    def test_not_found(self, monkeypatch):
        _patch_fetch(monkeypatch, {})
        assert "no book found" in reflookup._isbn_sync("000", "UA")

    def test_malformed(self, monkeypatch):
        _patch_fetch(monkeypatch, {"ISBN:123": {}})
        out = reflookup._isbn_sync("123", "UA")
        assert "(untitled)" in out

    def test_exception(self, monkeypatch):
        _patch_fetch(monkeypatch, exc=ValueError("boom"))
        assert reflookup._isbn_sync("x", "UA") == "lookup failed"


# ── so ─────────────────────────────────────────────────────────────────────
class TestSo:
    def test_happy(self, monkeypatch):
        _patch_fetch(monkeypatch, {"items": [{
            "title": "How do I exit Vim?",
            "score": 4242,
            "is_answered": True,
            "link": "https://stackoverflow.com/q/11828270",
        }]})
        out = reflookup._so_sync("exit vim", "UA")
        assert "How do I exit Vim?" in out
        assert "score 4242" in out
        assert "answered" in out
        assert "stackoverflow.com" in out

    def test_unanswered(self, monkeypatch):
        _patch_fetch(monkeypatch, {"items": [{
            "title": "Q", "score": 0, "is_answered": False, "link": "http://x"}]})
        assert "unanswered" in reflookup._so_sync("q", "UA")

    def test_no_results(self, monkeypatch):
        _patch_fetch(monkeypatch, {"items": []})
        assert "no Stack Overflow results" in reflookup._so_sync("zzz", "UA")

    def test_exception(self, monkeypatch):
        _patch_fetch(monkeypatch, exc=ValueError("boom"))
        assert reflookup._so_sync("x", "UA") == "lookup failed"


# ── rfc ────────────────────────────────────────────────────────────────────
class TestRfc:
    def test_happy(self, monkeypatch):
        _patch_fetch(monkeypatch, {
            "title": "Hypertext Transfer Protocol -- HTTP/1.1",
            "status": "PROPOSED STANDARD",
            "month": "June",
            "year": 1999,
        })
        out = reflookup._rfc_sync("2616", "UA")
        assert "RFC 2616" in out
        assert "Hypertext Transfer Protocol" in out
        assert "PROPOSED STANDARD" in out
        assert "June 1999" in out

    def test_non_numeric(self, monkeypatch):
        # non-numeric arg now triggers a datatracker title search (not a usage
        # error); with no search hits it reports no match.
        _patch_fetch(monkeypatch, None)
        assert "no RFC matching" in reflookup._rfc_sync("abc", "UA")

    def test_not_found(self, monkeypatch):
        _patch_fetch(monkeypatch, None)
        assert "no RFC" in reflookup._rfc_sync("99999", "UA")

    def test_exception(self, monkeypatch):
        _patch_fetch(monkeypatch, exc=ValueError("boom"))
        assert reflookup._rfc_sync("1", "UA") == "lookup failed"


# ── arxiv (XML) ────────────────────────────────────────────────────────────
_ATOM_OK = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1234.5678v1</id>
    <published>2012-01-15T00:00:00Z</published>
    <title>Attention Is All You Need</title>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
  </entry>
</feed>"""

_ATOM_EMPTY = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>"""


class TestArxiv:
    def test_happy(self):
        out = reflookup._arxiv_sync("1234.5678", "UA", fetch=lambda q, ua: _ATOM_OK)
        assert "Attention Is All You Need" in out
        assert "Ashish Vaswani" in out
        assert "2012-01-15" in out
        assert "arxiv.org/abs/1234.5678" in out

    def test_no_results(self):
        out = reflookup._arxiv_sync("zzz", "UA", fetch=lambda q, ua: _ATOM_EMPTY)
        assert "no arXiv result" in out

    def test_malformed_xml(self):
        out = reflookup._arxiv_sync("x", "UA", fetch=lambda q, ua: "<not valid")
        assert out == "lookup failed"

    def test_fetch_raises(self):
        def _boom(q, ua):
            raise reflookup.ResponseTooLarge("too big")
        assert reflookup._arxiv_sync("x", "UA", fetch=_boom) == "lookup failed"


# ── module wiring ──────────────────────────────────────────────────────────
def test_commands_map_to_coroutines():
    import inspect
    for word, method in reflookup.RefLookupModule.COMMANDS.items():
        handler = getattr(reflookup.RefLookupModule, method, None)
        assert handler is not None, word
        assert inspect.iscoroutinefunction(handler), word


def test_setup_returns_module():
    class _Bot:
        cfg = {"bot": {"command_prefix": "."}}
    m = reflookup.setup(_Bot())
    assert isinstance(m, reflookup.RefLookupModule)


def test_help_lines_one_per_command():
    class _Bot:
        cfg = {"bot": {"command_prefix": "."}}
    m = reflookup.setup(_Bot())
    lines = m.help_lines(".")
    assert len(lines) == len(reflookup.RefLookupModule.COMMANDS)
