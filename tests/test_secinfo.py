"""Tests for modules/secinfo.py.

Network helpers (_cve_sync, _pwn_sync) are exercised with monkeypatched
transport so NO real network is hit:
  - _cve_sync's fetch_json is replaced with a stub returning canned NVD JSON.
  - _pwn_sync's requests.get is replaced with a stub returning canned HIBP
    range text.
Pure helpers (_hashid, _cvss, _cipher, _cve_score) are tested directly.
"""

import hashlib

import pytest

from modules import secinfo
from modules.secinfo import (
    _cipher,
    _cve_score,
    _cve_sync,
    _cvss,
    _hashid,
    _pwn_sync,
)


# ── .cve ────────────────────────────────────────────────────────────────
def _nvd_payload(score=9.8, severity="CRITICAL", desc="Apache Log4j2 JNDI RCE."):
    return {
        "vulnerabilities": [
            {
                "cve": {
                    "id": "CVE-2021-44228",
                    "published": "2021-12-10T10:15:09.143",
                    "descriptions": [
                        {"lang": "es", "value": "spanish text"},
                        {"lang": "en", "value": desc},
                    ],
                    "metrics": {
                        "cvssMetricV31": [
                            {
                                "cvssData": {
                                    "baseScore": score,
                                    "baseSeverity": severity,
                                }
                            }
                        ]
                    },
                }
            }
        ]
    }


def test_cve_happy(monkeypatch):
    monkeypatch.setattr(secinfo, "fetch_json", lambda *a, **k: _nvd_payload())
    out = _cve_sync("CVE-2021-44228", "UA/1.0")
    assert "CVE-2021-44228" in out
    assert "CVSS 9.8" in out
    assert "CRITICAL" in out
    assert "Apache Log4j2 JNDI RCE." in out
    assert "published 2021-12-10" in out


def test_cve_not_found_404(monkeypatch):
    # allow_404=True -> fetch_json returns None
    monkeypatch.setattr(secinfo, "fetch_json", lambda *a, **k: None)
    out = _cve_sync("CVE-2099-0001", "UA/1.0")
    assert "not found" in out


def test_cve_empty_vulns(monkeypatch):
    monkeypatch.setattr(secinfo, "fetch_json", lambda *a, **k: {"vulnerabilities": []})
    out = _cve_sync("CVE-2099-0001", "UA/1.0")
    assert "not found" in out


def test_cve_no_metrics(monkeypatch):
    payload = _nvd_payload()
    payload["vulnerabilities"][0]["cve"]["metrics"] = {}
    monkeypatch.setattr(secinfo, "fetch_json", lambda *a, **k: payload)
    out = _cve_sync("CVE-2021-44228", "UA/1.0")
    assert "CVSS n/a" in out


def test_cve_request_error(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.RequestException("network down")

    monkeypatch.setattr(secinfo, "fetch_json", boom)
    assert _cve_sync("CVE-2021-44228", "UA/1.0") == "lookup failed"


def test_cve_malformed(monkeypatch):
    # not a dict -> .get triggers AttributeError caught as TypeError path? use list
    monkeypatch.setattr(secinfo, "fetch_json", lambda *a, **k: {"vulnerabilities": "bogus"})
    out = _cve_sync("CVE-2021-44228", "UA/1.0")
    assert out == "lookup failed"


def test_cve_score_prefers_v31():
    metrics = {
        "cvssMetricV2": [{"cvssData": {"baseScore": 5.0}, "baseSeverity": "MEDIUM"}],
        "cvssMetricV31": [{"cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}],
    }
    score, sev = _cve_score(metrics)
    assert score == 9.8
    assert sev == "CRITICAL"


def test_cve_score_falls_back_to_v2():
    metrics = {"cvssMetricV2": [{"cvssData": {"baseScore": 7.5}, "baseSeverity": "HIGH"}]}
    score, sev = _cve_score(metrics)
    assert score == 7.5
    assert sev == "HIGH"


def test_cve_score_none():
    assert _cve_score({}) == (None, "")


# ── .pwn ────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, text):
        self._bytes = text.encode("utf-8")
        self.raw = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def read(self, n, decode_content=True):
        return self._bytes


def _patch_hibp(monkeypatch, text):
    monkeypatch.setattr(secinfo.requests, "get", lambda *a, **k: _FakeResp(text))


def test_pwn_found(monkeypatch):
    pw = "password"
    sha1 = hashlib.sha1(pw.encode()).hexdigest().upper()
    suffix = sha1[5:]
    body = f"0000000000000000000000000000000000A:3\r\n{suffix}:12345\r\nFFFF:1"
    _patch_hibp(monkeypatch, body)
    out = _pwn_sync(pw, "UA/1.0")
    assert "pwned" in out
    assert "12,345" in out


def test_pwn_only_prefix_sent(monkeypatch):
    """Ensure only the 5-char SHA-1 prefix is ever placed in the URL."""
    pw = "hunter2"
    sha1 = hashlib.sha1(pw.encode()).hexdigest().upper()
    captured = {}

    def fake_get(url, *a, **k):
        captured["url"] = url
        return _FakeResp(f"{sha1[5:]}:7")

    monkeypatch.setattr(secinfo.requests, "get", fake_get)
    _pwn_sync(pw, "UA/1.0")
    assert captured["url"].endswith(sha1[:5])
    # full hash / suffix must NOT appear in the outbound URL
    assert sha1[5:] not in captured["url"]


def test_pwn_not_found(monkeypatch):
    pw = "a-very-unique-passphrase-xyz"
    body = "ABCDEF0123456789ABCDEF0123456789ABC:9\r\nFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF:2"
    _patch_hibp(monkeypatch, body)
    out = _pwn_sync(pw, "UA/1.0")
    assert "not found" in out


def test_pwn_request_error(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.RequestException("down")

    monkeypatch.setattr(secinfo.requests, "get", boom)
    assert _pwn_sync("x", "UA/1.0") == "lookup failed"


# ── .hashid ─────────────────────────────────────────────────────────────
def test_hashid_md5():
    assert "MD5" in _hashid("5f4dcc3b5aa765d61d8327deb882cf99")


def test_hashid_sha1():
    assert "SHA-1" in _hashid("a" * 40)


def test_hashid_sha256():
    assert "SHA-256" in _hashid("a" * 64)


def test_hashid_bcrypt():
    assert "bcrypt" in _hashid("$2b$12$" + "a" * 53)


def test_hashid_argon2():
    assert "Argon2" in _hashid("$argon2id$v=19$m=65536,t=3,p=4$abc$def")


def test_hashid_sha512crypt():
    assert "sha512crypt" in _hashid("$6$rounds=5000$salt$hashvalue")


def test_hashid_unknown_hex():
    assert "unknown hex" in _hashid("abcdef")


def test_hashid_unrecognised():
    assert "unrecognised" in _hashid("not a hash at all!!!")


def test_hashid_empty():
    assert "usage" in _hashid("")


# ── .cvss ───────────────────────────────────────────────────────────────
def test_cvss_log4shell_critical():
    out = _cvss("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H")
    assert "10.0" in out
    assert "Critical" in out


def test_cvss_scope_unchanged_98():
    out = _cvss("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    assert "9.8" in out
    assert "Critical" in out


def test_cvss_none_score():
    out = _cvss("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N")
    assert "0.0" in out
    assert "None" in out


def test_cvss_low():
    out = _cvss("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N")
    assert "Low" in out


def test_cvss_no_prefix_ok():
    # vector without leading CVSS:3.1 token still parses
    out = _cvss("AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    assert "9.8" in out


def test_cvss_incomplete():
    assert "invalid" in _cvss("CVSS:3.1/AV:N/AC:L").lower()


def test_cvss_wrong_version():
    assert "v3" in _cvss("CVSS:2.0/AV:N/AC:L/Au:N/C:P/I:P/A:P")


def test_cvss_empty():
    assert "usage" in _cvss("")


# ── .cipher ─────────────────────────────────────────────────────────────
def test_cipher_aes_secure():
    out = _cipher("aes")
    assert "AES" in out
    assert "secure" in out


def test_cipher_des_broken():
    assert "broken" in _cipher("des")


def test_cipher_rc4_broken():
    assert "broken" in _cipher("rc4")


def test_cipher_md5_broken():
    assert "broken" in _cipher("md5")


def test_cipher_normalized_lookup():
    # "SHA 256" should resolve via the separator-stripping fallback
    assert "SHA-256" in _cipher("SHA 256")


def test_cipher_unknown():
    assert "no reference" in _cipher("frobnicate")


def test_cipher_empty():
    assert "usage" in _cipher("")
