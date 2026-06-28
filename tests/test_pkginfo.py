"""Tests for modules/pkginfo.py - _pypi_sync / _npm_sync / _crates_sync.

Each test monkeypatches the module's fetch_json with a canned response so
nothing hits the real network.  We assert the one-line formatting for the
happy path, the not-found (404 -> None) path, and a malformed payload.
"""

import modules.pkginfo as pkginfo


UA = "Internets/1.0 (test)"


def _patch(monkeypatch, value=None, exc=None):
    """Replace pkginfo.fetch_json with a stub returning value (or raising exc)."""
    def fake_fetch_json(url, **kwargs):
        if exc is not None:
            raise exc
        return value
    monkeypatch.setattr(pkginfo, "fetch_json", fake_fetch_json)


# --- PyPI -----------------------------------------------------------------

def test_pypi_happy(monkeypatch):
    _patch(monkeypatch, {
        "info": {
            "name": "requests",
            "version": "2.31.0",
            "summary": "Python HTTP for Humans.",
            "license": "Apache 2.0",
            "project_url": "https://requests.readthedocs.io",
        },
        "releases": {
            "2.31.0": [{"upload_time_iso_8601": "2023-05-22T15:12:00.000000Z"}],
        },
    })
    out = pkginfo._pypi_sync("requests", UA)
    assert "requests" in out
    assert "2.31.0" in out
    assert "Python HTTP for Humans." in out
    assert "license Apache 2.0" in out
    assert "released 2023-05-22" in out
    assert "https://requests.readthedocs.io" in out


def test_pypi_not_found(monkeypatch):
    _patch(monkeypatch, None)  # allow_404 -> None
    out = pkginfo._pypi_sync("nope-no-such-pkg", UA)
    assert "not found" in out
    assert "nope-no-such-pkg" in out


def test_pypi_malformed(monkeypatch):
    _patch(monkeypatch, ["unexpected", "list"])  # not a dict
    out = pkginfo._pypi_sync("weird", UA)
    assert "not found" in out


def test_pypi_lookup_failed(monkeypatch):
    _patch(monkeypatch, exc=ValueError("bad json"))
    out = pkginfo._pypi_sync("requests", UA)
    assert out == "pypi: lookup failed"


def test_pypi_strips_control_chars(monkeypatch):
    _patch(monkeypatch, {
        "info": {"name": "evil", "version": "1.0",
                 "summary": "line\r\none\x03color"},
        "releases": {},
    })
    out = pkginfo._pypi_sync("evil", UA)
    assert "\r" not in out and "\n" not in out and "\x03" not in out


# --- npm ------------------------------------------------------------------

def test_npm_happy(monkeypatch):
    _patch(monkeypatch, {
        "name": "express",
        "dist-tags": {"latest": "4.18.2"},
        "description": "Fast, unopinionated, minimalist web framework",
        "license": "MIT",
        "time": {"4.18.2": "2022-10-08T20:00:00.000Z"},
    })
    out = pkginfo._npm_sync("express", UA)
    assert "express" in out
    assert "4.18.2" in out
    assert "Fast, unopinionated" in out
    assert "license MIT" in out
    assert "published 2022-10-08" in out


def test_npm_license_dict(monkeypatch):
    _patch(monkeypatch, {
        "name": "old-pkg",
        "dist-tags": {"latest": "1.0.0"},
        "license": {"type": "BSD-3-Clause"},
        "time": {},
    })
    out = pkginfo._npm_sync("old-pkg", UA)
    assert "license BSD-3-Clause" in out


def test_npm_not_found(monkeypatch):
    _patch(monkeypatch, None)
    out = pkginfo._npm_sync("no-such-npm-pkg", UA)
    assert "not found" in out


def test_npm_malformed(monkeypatch):
    _patch(monkeypatch, 12345)  # not a dict
    out = pkginfo._npm_sync("weird", UA)
    assert "not found" in out


def test_npm_lookup_failed(monkeypatch):
    _patch(monkeypatch, exc=KeyError("oops"))
    out = pkginfo._npm_sync("express", UA)
    assert out == "npm: lookup failed"


# --- crates.io ------------------------------------------------------------

def test_crates_happy(monkeypatch):
    _patch(monkeypatch, {
        "crate": {
            "name": "serde",
            "max_version": "1.0.193",
            "downloads": 250000000,
            "description": "A serialization framework",
            "documentation": "https://docs.rs/serde",
            "homepage": "https://serde.rs",
        },
        "versions": [{"license": "MIT OR Apache-2.0"}],
    })
    out = pkginfo._crates_sync("serde", UA)
    assert "serde" in out
    assert "1.0.193" in out
    assert "A serialization framework" in out
    assert "250,000,000 downloads" in out
    assert "license MIT OR Apache-2.0" in out
    assert "https://docs.rs/serde" in out


def test_crates_falls_back_to_homepage(monkeypatch):
    _patch(monkeypatch, {
        "crate": {
            "name": "tiny",
            "max_version": "0.1.0",
            "downloads": 5,
            "homepage": "https://example.org/tiny",
        },
        "versions": [],
    })
    out = pkginfo._crates_sync("tiny", UA)
    assert "https://example.org/tiny" in out
    assert "5 downloads" in out


def test_crates_not_found(monkeypatch):
    _patch(monkeypatch, None)
    out = pkginfo._crates_sync("no-such-crate", UA)
    assert "not found" in out


def test_crates_malformed(monkeypatch):
    _patch(monkeypatch, "not-a-dict")
    out = pkginfo._crates_sync("weird", UA)
    assert "not found" in out


def test_crates_lookup_failed(monkeypatch):
    _patch(monkeypatch, exc=pkginfo.ResponseTooLarge("too big"))
    out = pkginfo._crates_sync("serde", UA)
    assert out == "crates: lookup failed"


# --- _clip helper ---------------------------------------------------------

def test_clip_truncates_and_appends_ellipsis():
    out = pkginfo._clip("x" * 200, 50)
    assert len(out) == 50
    assert out.endswith("…")


def test_clip_strips_control_chars():
    assert "\n" not in pkginfo._clip("a\nb", 50)
