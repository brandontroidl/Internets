"""Tests for modules/ghinfo.py - _fetch_sync formatting.

The module's only network call goes through modules.base.fetch_json; we
monkeypatch the name imported into modules.ghinfo with canned responses
so the suite never touches the real network.
"""

import modules.ghinfo as ghinfo
from modules.base import ResponseTooLarge


_SAMPLE = {
    "full_name": "torvalds/linux",
    "stargazers_count": 175000,
    "forks_count": 52000,
    "open_issues_count": 320,
    "language": "C",
    "license": {"spdx_id": "GPL-2.0", "name": "GNU General Public License v2.0"},
    "pushed_at": "2024-05-01T12:34:56Z",
}


def _patch(monkeypatch, fake):
    monkeypatch.setattr(ghinfo, "fetch_json", fake)


# --- happy path -----------------------------------------------------------

def test_happy_path(monkeypatch):
    _patch(monkeypatch, lambda *a, **k: _SAMPLE)
    out = ghinfo._fetch_sync("torvalds/linux", "ua/1.0")
    assert "torvalds/linux" in out
    assert "175,000" in out
    assert "52,000" in out
    assert "320" in out
    assert "lang C" in out
    assert "GPL-2.0" in out
    assert "pushed 2024-05-01" in out  # date only, no time
    assert "T12:34" not in out


def test_passes_useragent_and_allow_404(monkeypatch):
    captured = {}

    def fake(url, **kwargs):
        captured["url"] = url
        captured["ua"] = kwargs.get("ua")
        captured["allow_404"] = kwargs.get("allow_404")
        return _SAMPLE

    _patch(monkeypatch, fake)
    ghinfo._fetch_sync("torvalds/linux", "MyUA/9.9")
    assert captured["url"] == "https://api.github.com/repos/torvalds/linux"
    assert captured["ua"] == "MyUA/9.9"
    assert captured["allow_404"] is True


# --- not found ------------------------------------------------------------

def test_not_found_404(monkeypatch):
    # allow_404=True makes fetch_json return None on a miss.
    _patch(monkeypatch, lambda *a, **k: None)
    out = ghinfo._fetch_sync("nope/nada", "ua/1.0")
    assert "not found" in out
    assert "nope/nada" in out


def test_bad_arg_no_slash(monkeypatch):
    # Should never even call fetch_json for a malformed arg.
    def boom(*a, **k):
        raise AssertionError("fetch_json must not be called")
    _patch(monkeypatch, boom)
    assert "usage" in ghinfo._fetch_sync("torvalds", "ua/1.0")
    assert "usage" in ghinfo._fetch_sync("a/b/c", "ua/1.0")
    assert "usage" in ghinfo._fetch_sync("/linux", "ua/1.0")


# --- malformed / error paths ---------------------------------------------

def test_missing_fields_use_defaults(monkeypatch):
    _patch(monkeypatch, lambda *a, **k: {"full_name": "x/y"})
    out = ghinfo._fetch_sync("x/y", "ua/1.0")
    assert "x/y" in out
    assert "lang n/a" in out
    assert "license none" in out
    assert "pushed n/a" in out


def test_license_none(monkeypatch):
    data = dict(_SAMPLE, license=None)
    _patch(monkeypatch, lambda *a, **k: data)
    assert "license none" in ghinfo._fetch_sync("torvalds/linux", "ua/1.0")


def test_non_dict_payload(monkeypatch):
    _patch(monkeypatch, lambda *a, **k: ["unexpected", "list"])
    assert "not found" in ghinfo._fetch_sync("a/b", "ua/1.0")


def test_response_too_large(monkeypatch):
    def boom(*a, **k):
        raise ResponseTooLarge("too big")
    _patch(monkeypatch, boom)
    assert ghinfo._fetch_sync("a/b", "ua/1.0") == "lookup failed"


def test_request_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    _patch(monkeypatch, boom)
    assert ghinfo._fetch_sync("a/b", "ua/1.0") == "lookup failed"


def test_control_chars_stripped(monkeypatch):
    data = dict(_SAMPLE, full_name="evil\x02\x03name/repo", language="C\x01")
    _patch(monkeypatch, lambda *a, **k: data)
    out = ghinfo._fetch_sync("a/b", "ua/1.0")
    assert "\x03" not in out
    assert "\x01" not in out
    # the leading bold marker we add ourselves is fine; upstream \x02 in the
    # name is stripped, so there's exactly one \x02 (our own prefix).
    assert out.count("\x02") == 2  # our \x02...\x02 wrapper, none from upstream
