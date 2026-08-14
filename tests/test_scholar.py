"""Tests for modules/scholar.py.

fetch_json is monkeypatched with canned responses (its kwargs captured so
query construction is asserted) - no real network.  Pure helpers
(parse_orcid, split_flags) are tested directly.
"""
from __future__ import annotations

from modules import scholar


# ── helpers ───────────────────────────────────────────────────────────────
def _patch_fetch(monkeypatch, value=None, *, exc=None, calls=None):
    """Stub scholar.fetch_json; append (url, kwargs) to `calls` if given."""
    def _stub(url, **k):
        if calls is not None:
            calls.append((url, k))
        if exc is not None:
            raise exc
        return value
    monkeypatch.setattr(scholar, "fetch_json", _stub)


_WORK = {
    "display_name": "Revisiting anomaly-based network intrusion detection systems",
    "publication_year": 2012,
    "doi": "https://doi.org/10.3990/1.9789036528535",
    "open_access": {"is_oa": True},
    "best_oa_location": {
        "pdf_url": "https://example.org/thesis.pdf",
        "landing_page_url": "https://example.org/thesis",
    },
    "authorships": [
        {"author": {"display_name": "Damiano Bolzoni"}},
        {"author": {"display_name": "Second Author"}},
    ],
    "primary_location": {"source": {"display_name": "University of Twente"}},
}

_WORKS_OK = {"meta": {"count": 42}, "results": [_WORK]}


# ── parse_orcid (pure) ────────────────────────────────────────────────────
class TestParseOrcid:
    def test_bare_id(self):
        assert scholar.parse_orcid("0000-0002-1825-0097") == "0000-0002-1825-0097"

    def test_url_form(self):
        assert scholar.parse_orcid(
            "https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"

    def test_x_checksum_uppercased(self):
        assert scholar.parse_orcid("0000-0002-1694-233x") == "0000-0002-1694-233X"

    def test_not_an_id(self):
        assert scholar.parse_orcid("intrusion detection") is None

    def test_empty(self):
        assert scholar.parse_orcid("") is None


# ── split_flags (pure) ────────────────────────────────────────────────────
class TestSplitFlags:
    def test_flag_anywhere(self):
        assert scholar.split_flags("-oa quantum") == ("quantum", {"oa"})
        assert scholar.split_flags("quantum -oa") == ("quantum", {"oa"})
        assert scholar.split_flags("deep -oa learning") == ("deep learning", {"oa"})

    def test_no_flags(self):
        assert scholar.split_flags("plain query") == ("plain query", set())

    def test_empty(self):
        assert scholar.split_flags("") == ("", set())


# ── .papers ───────────────────────────────────────────────────────────────
class TestPapers:
    def test_orcid_arg_builds_author_filter(self, monkeypatch):
        calls = []
        _patch_fetch(monkeypatch, _WORKS_OK, calls=calls)
        out = scholar._papers_sync("0000-0002-1825-0097", "UA")
        params = calls[0][1]["params"]
        assert params["filter"] == "author.orcid:0000-0002-1825-0097"
        assert params["sort"] == "publication_date:desc"
        assert "search" not in params
        assert "ORCID 0000-0002-1825-0097" in out[0]
        assert "42 works" in out[0]

    def test_topic_arg_uses_search_param(self, monkeypatch):
        calls = []
        _patch_fetch(monkeypatch, _WORKS_OK, calls=calls)
        scholar._papers_sync("intrusion detection", "UA")
        params = calls[0][1]["params"]
        assert params["search"] == "intrusion detection"
        assert "filter" not in params

    def test_oa_flag_adds_filter(self, monkeypatch):
        calls = []
        _patch_fetch(monkeypatch, _WORKS_OK, calls=calls)
        out = scholar._papers_sync("quantum -oa", "UA")
        assert calls[0][1]["params"]["filter"] == "open_access.is_oa:true"
        assert "[open access]" in out[0]

    def test_result_line_formatting(self, monkeypatch):
        _patch_fetch(monkeypatch, _WORKS_OK)
        out = scholar._papers_sync("x", "UA")
        line = out[1]
        assert "Revisiting anomaly-based" in line
        assert "(2012)" in line
        assert "[OA]" in line
        assert "Damiano Bolzoni et al." in line
        assert "University of Twente" in line
        # OA PDF preferred over landing page and DOI
        assert "https://example.org/thesis.pdf" in line

    def test_link_fallback_to_doi(self, monkeypatch):
        w = dict(_WORK, best_oa_location=None)
        _patch_fetch(monkeypatch, {"meta": {"count": 1}, "results": [w]})
        out = scholar._papers_sync("x", "UA")
        assert "https://doi.org/10.3990/1.9789036528535" in out[1]

    def test_no_results(self, monkeypatch):
        _patch_fetch(monkeypatch, {"meta": {"count": 0}, "results": []})
        assert "no results" in scholar._papers_sync("zzqqxx", "UA")[0]

    def test_malformed(self, monkeypatch):
        _patch_fetch(monkeypatch, "not a dict")
        assert scholar._papers_sync("x", "UA") == ["lookup failed"]

    def test_exception(self, monkeypatch):
        _patch_fetch(monkeypatch, exc=ValueError("boom"))
        assert scholar._papers_sync("x", "UA") == ["lookup failed"]


# ── .thesis ───────────────────────────────────────────────────────────────
class TestThesis:
    def test_dissertation_filter_always_present(self, monkeypatch):
        calls = []
        _patch_fetch(monkeypatch, _WORKS_OK, calls=calls)
        out = scholar._thesis_sync("intrusion detection", "UA")
        params = calls[0][1]["params"]
        assert params["filter"] == "type:dissertation"
        assert params["search"] == "intrusion detection"
        assert "dissertations" in out[0]

    def test_oa_flag_appends_filter(self, monkeypatch):
        calls = []
        _patch_fetch(monkeypatch, _WORKS_OK, calls=calls)
        scholar._thesis_sync("-oa quantum", "UA")
        assert calls[0][1]["params"]["filter"] == \
            "type:dissertation,open_access.is_oa:true"

    def test_exception(self, monkeypatch):
        _patch_fetch(monkeypatch, exc=ValueError("boom"))
        assert scholar._thesis_sync("x", "UA") == ["lookup failed"]


# ── .scholar ──────────────────────────────────────────────────────────────
_ORCID_OK = {
    "num-found": 18101,
    "expanded-result": [
        {"orcid-id": "0000-0003-2733-6033", "given-names": "Mahdi",
         "family-names": "Mahdavi Oliaee",
         "institution-name": ["KU Leuven", "Shahid Beheshti University",
                              "Third Uni"]},
    ],
}


class TestScholar:
    def test_happy(self, monkeypatch):
        calls = []
        _patch_fetch(monkeypatch, _ORCID_OK, calls=calls)
        out = scholar._scholar_sync("cryptography", "UA")
        assert calls[0][1]["headers"] == {"Accept": "application/json"}
        assert "18101 found" in out[0]
        line = out[1]
        assert "Mahdi Mahdavi Oliaee" in line
        assert "KU Leuven" in line
        assert "Third Uni" not in line  # capped at 2 institutions
        assert "https://orcid.org/0000-0003-2733-6033" in line

    def test_no_results(self, monkeypatch):
        _patch_fetch(monkeypatch, {"num-found": 0, "expanded-result": []})
        assert "no ORCID researchers" in scholar._scholar_sync("zzz", "UA")[0]

    def test_exception(self, monkeypatch):
        _patch_fetch(monkeypatch, exc=ValueError("boom"))
        assert scholar._scholar_sync("x", "UA") == ["lookup failed"]


# ── module wiring ─────────────────────────────────────────────────────────
def test_commands_map_to_coroutines():
    import inspect
    for word, method in scholar.ScholarModule.COMMANDS.items():
        handler = getattr(scholar.ScholarModule, method, None)
        assert handler is not None, word
        assert inspect.iscoroutinefunction(handler), word


def test_setup_returns_module():
    class _Bot:
        cfg = {"bot": {"command_prefix": "."}}
    m = scholar.setup(_Bot())
    assert isinstance(m, scholar.ScholarModule)


def test_help_lines_one_per_command():
    class _Bot:
        cfg = {"bot": {"command_prefix": "."}}
    m = scholar.setup(_Bot())
    assert len(m.help_lines(".")) == len(scholar.ScholarModule.COMMANDS)
