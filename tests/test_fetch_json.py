"""Tests for modules.base.fetch_json - the shared HTTP size-cap helper.

fetch_json guards every outbound JSON call in the bot: it streams the
response, caps the body at ``max_bytes``, and raises before parsing.
These tests pin the cap boundary and the 404 / malformed-JSON paths so
an off-by-one regression can't silently disable the OOM guard.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
import requests

from modules.base import ResponseTooLarge, fetch_json


# ── Fakes ────────────────────────────────────────────────────────────────

class _FakeRaw:
    """Stand-in for ``response.raw`` - read() returns at most ``amt`` bytes."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self, amt: int, decode_content: bool = True) -> bytes:
        return self._data[:amt]


class _FakeResponse:
    """Minimal ``requests.Response`` stand-in - and a context manager,
    since fetch_json uses ``with requests.get(...) as r``."""

    def __init__(self, *, status: int = 200, body: bytes = b"",
                 reason: str = "OK"):
        self.status_code = status
        self.reason = reason
        self.raw = _FakeRaw(body)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} {self.reason}")


def _call(resp: _FakeResponse, **kwargs: object):
    """Invoke fetch_json with ``requests.get`` patched to yield ``resp``."""
    with mock.patch("requests.get", return_value=resp) as getter:
        result = fetch_json("https://example.test/api", ua="test/1.0", **kwargs)
    return result, getter


# ── Size-cap boundary ────────────────────────────────────────────────────

class TestSizeCap:
    def test_body_exactly_at_cap_passes(self):
        body = json.dumps({"ok": True, "pad": "z" * 500}).encode()
        result, _ = _call(_FakeResponse(body=body), max_bytes=len(body))
        assert result == {"ok": True, "pad": "z" * 500}

    def test_body_one_over_cap_raises(self):
        body = json.dumps({"ok": True, "pad": "z" * 500}).encode()
        # max_bytes one short of the body → must raise.
        with pytest.raises(ResponseTooLarge):
            _call(_FakeResponse(body=body), max_bytes=len(body) - 1)

    def test_small_body_well_under_cap_passes(self):
        body = b'{"v": 1}'
        result, _ = _call(_FakeResponse(body=body), max_bytes=256 * 1024)
        assert result == {"v": 1}


# ── 404 handling ─────────────────────────────────────────────────────────

class TestNotFound:
    def test_allow_404_true_returns_none(self):
        result, _ = _call(_FakeResponse(status=404, body=b""), allow_404=True)
        assert result is None

    def test_allow_404_false_raises(self):
        with pytest.raises(requests.HTTPError):
            _call(_FakeResponse(status=404, body=b""), allow_404=False)

    def test_404_is_default_behaviour_raises(self):
        # allow_404 defaults to False.
        with pytest.raises(requests.HTTPError):
            _call(_FakeResponse(status=404, body=b""))

    def test_500_always_raises_even_with_allow_404(self):
        # allow_404 only short-circuits 404 - a 500 must still raise.
        with pytest.raises(requests.HTTPError):
            _call(_FakeResponse(status=500, body=b"", reason="Server Error"),
                  allow_404=True)


# ── Malformed body ───────────────────────────────────────────────────────

class TestMalformedJSON:
    def test_non_json_body_raises_decode_error(self):
        with pytest.raises(json.JSONDecodeError):
            _call(_FakeResponse(body=b"<html>not json</html>"))

    def test_empty_body_raises_decode_error(self):
        with pytest.raises(json.JSONDecodeError):
            _call(_FakeResponse(body=b""))


# ── Request wiring ───────────────────────────────────────────────────────

class TestRequestWiring:
    def test_user_agent_and_headers_passed(self):
        resp = _FakeResponse(body=b'{"x": 1}')
        with mock.patch("requests.get", return_value=resp) as getter:
            fetch_json("https://example.test/api", ua="myua/9.9",
                       headers={"Accept": "application/json"},
                       params={"q": "term"})
        _, kwargs = getter.call_args
        assert kwargs["headers"]["User-Agent"] == "myua/9.9"
        assert kwargs["headers"]["Accept"] == "application/json"
        assert kwargs["params"] == {"q": "term"}
        assert kwargs["stream"] is True

    def test_caller_headers_do_not_mutate_across_calls(self):
        # A header dict is rebuilt per call - no cross-call leakage.
        resp = _FakeResponse(body=b'{"x": 1}')
        with mock.patch("requests.get", return_value=resp) as getter:
            fetch_json("https://example.test", ua="a/1")
            fetch_json("https://example.test", ua="b/2")
        first = getter.call_args_list[0].kwargs["headers"]
        second = getter.call_args_list[1].kwargs["headers"]
        assert first["User-Agent"] == "a/1"
        assert second["User-Agent"] == "b/2"
