"""Tests for modules/ipintel.py.

Network helpers are exercised with monkeypatched transport so NO real
network is hit:
  - DNSBL / DShield / GreyNoise / AbuseIPDB stub ``ipintel.fetch_json``.
  - Tor stubs ``ipintel._tor_fetch`` (and resets the module-level cache).
Pure helpers (_dnsbl_name, _verdict, _format, _coerce_int) are tested
directly.  The async ``cmd_ip`` path is driven via ``asyncio.run`` against
a fake bot with the source helpers stubbed.
"""

import asyncio

from modules import ipintel
from modules.ipintel import (
    IpintelModule,
    _abuseipdb_sync,
    _coerce_int,
    _dnsbl_name,
    _dnsbl_one,
    _dshield_sync,
    _format,
    _greynoise_sync,
    _tor_is_exit,
    _verdict,
)


def _raise(exc):
    def f(*a, **k):
        raise exc
    return f


def _reset_tor():
    ipintel._tor_cache["ts"] = 0.0
    ipintel._tor_cache["set"] = frozenset()


# ── _dnsbl_name ──────────────────────────────────────────────────────────
def test_dnsbl_name_ipv4():
    assert _dnsbl_name("1.2.3.4", "bl.example") == "4.3.2.1.bl.example"


def test_dnsbl_name_ipv6_none():
    assert _dnsbl_name("2001:db8::1", "bl.example") is None


def test_dnsbl_name_invalid_none():
    assert _dnsbl_name("not-an-ip", "bl.example") is None


# ── _dnsbl_one ───────────────────────────────────────────────────────────
def _doh(status=0, answers=None):
    d = {"Status": status}
    if answers is not None:
        d["Answer"] = answers
    return d


def test_dnsbl_one_listed(monkeypatch):
    monkeypatch.setattr(ipintel, "fetch_json",
                        lambda *a, **k: _doh(0, [{"type": 1, "data": "127.0.0.2"}]))
    assert _dnsbl_one("1.2.3.4", "bl.example", "UA") == 1


def test_dnsbl_one_not_listed_nxdomain(monkeypatch):
    monkeypatch.setattr(ipintel, "fetch_json", lambda *a, **k: _doh(3))
    assert _dnsbl_one("1.2.3.4", "bl.example", "UA") == 0


def test_dnsbl_one_sentinel_not_listed(monkeypatch):
    # 127.255.255.254 is the public-resolver refusal sentinel, NOT a listing.
    monkeypatch.setattr(ipintel, "fetch_json",
                        lambda *a, **k: _doh(0, [{"type": 1, "data": "127.255.255.254"}]))
    assert _dnsbl_one("1.2.3.4", "bl.example", "UA") == 0


def test_dnsbl_one_error(monkeypatch):
    import requests
    monkeypatch.setattr(ipintel, "fetch_json", _raise(requests.RequestException("down")))
    assert _dnsbl_one("1.2.3.4", "bl.example", "UA") == -1


def test_dnsbl_one_ipv6_no_request(monkeypatch):
    # _dnsbl_name -> None for IPv6, so no request is made and result is -1.
    called = {"n": 0}

    def stub(*a, **k):
        called["n"] += 1
        return _doh(0)

    monkeypatch.setattr(ipintel, "fetch_json", stub)
    assert _dnsbl_one("2001:db8::1", "bl.example", "UA") == -1
    assert called["n"] == 0


# ── _dshield_sync ────────────────────────────────────────────────────────
def test_dshield_ok(monkeypatch):
    monkeypatch.setattr(ipintel, "fetch_json",
                        lambda *a, **k: {"ip": {"count": 142, "ascountry": "US"}})
    d = _dshield_sync("1.2.3.4", "UA")
    assert d["count"] == 142


def test_dshield_non_dict_ip(monkeypatch):
    monkeypatch.setattr(ipintel, "fetch_json", lambda *a, **k: {"ip": None})
    assert _dshield_sync("1.2.3.4", "UA") is None


def test_dshield_error(monkeypatch):
    import requests
    monkeypatch.setattr(ipintel, "fetch_json", _raise(requests.RequestException("x")))
    assert _dshield_sync("1.2.3.4", "UA") is None


# ── _greynoise_sync ──────────────────────────────────────────────────────
def test_greynoise_seen(monkeypatch):
    monkeypatch.setattr(ipintel, "fetch_json",
                        lambda *a, **k: {"classification": "malicious", "name": "Mirai"})
    assert _greynoise_sync("1.2.3.4", "UA")["classification"] == "malicious"


def test_greynoise_unseen_404(monkeypatch):
    monkeypatch.setattr(ipintel, "fetch_json", lambda *a, **k: None)
    assert _greynoise_sync("1.2.3.4", "UA") == {"classification": "unseen"}


def test_greynoise_error(monkeypatch):
    import requests
    monkeypatch.setattr(ipintel, "fetch_json", _raise(requests.RequestException("x")))
    assert _greynoise_sync("1.2.3.4", "UA") is None


# ── _abuseipdb_sync ──────────────────────────────────────────────────────
def test_abuseipdb_no_key_skips():
    assert _abuseipdb_sync("1.2.3.4", "UA", "") is None


def test_abuseipdb_ok(monkeypatch):
    monkeypatch.setattr(
        ipintel, "fetch_json",
        lambda *a, **k: {"data": {"abuseConfidenceScore": 87, "totalReports": 12}})
    d = _abuseipdb_sync("1.2.3.4", "UA", "KEY")
    assert d["abuseConfidenceScore"] == 87


def test_abuseipdb_error(monkeypatch):
    import requests
    monkeypatch.setattr(ipintel, "fetch_json", _raise(requests.RequestException("x")))
    assert _abuseipdb_sync("1.2.3.4", "UA", "KEY") is None


# ── _tor_is_exit (cached) ────────────────────────────────────────────────
def test_tor_exit_member(monkeypatch):
    _reset_tor()
    monkeypatch.setattr(ipintel, "_tor_fetch", lambda ua: frozenset({"1.2.3.4"}))
    assert _tor_is_exit("1.2.3.4", "UA") == 1


def test_tor_not_member(monkeypatch):
    _reset_tor()
    monkeypatch.setattr(ipintel, "_tor_fetch", lambda ua: frozenset({"9.9.9.9"}))
    assert _tor_is_exit("1.2.3.4", "UA") == 0


def test_tor_error(monkeypatch):
    _reset_tor()
    import requests
    monkeypatch.setattr(ipintel, "_tor_fetch", _raise(requests.RequestException("x")))
    assert _tor_is_exit("1.2.3.4", "UA") == -1


def test_tor_cache_hit(monkeypatch):
    _reset_tor()
    calls = {"n": 0}

    def fake(ua):
        calls["n"] += 1
        return frozenset({"1.2.3.4"})

    monkeypatch.setattr(ipintel, "_tor_fetch", fake)
    assert _tor_is_exit("1.2.3.4", "UA") == 1
    assert _tor_is_exit("1.2.3.4", "UA") == 1
    assert calls["n"] == 1   # second call served from cache, not re-fetched


# ── _coerce_int / _verdict ───────────────────────────────────────────────
def test_coerce_int():
    assert _coerce_int("5") == 5
    assert _coerce_int(5) == 5
    assert _coerce_int(None) is None
    assert _coerce_int("x") is None


def test_verdict_malicious_two_dnsbl():
    assert _verdict(2, 0, None, None, None) == "malicious"


def test_verdict_malicious_tor():
    assert _verdict(0, 1, None, None, None) == "malicious"


def test_verdict_malicious_greynoise():
    assert _verdict(0, 0, "malicious", None, None) == "malicious"


def test_verdict_malicious_abuse_high():
    assert _verdict(0, 0, None, 87, None) == "malicious"


def test_verdict_suspicious_one_dnsbl():
    assert _verdict(1, 0, None, None, None) == "suspicious"


def test_verdict_suspicious_dshield():
    assert _verdict(0, 0, None, None, 50) == "suspicious"


def test_verdict_clean():
    assert _verdict(0, 0, "benign", 0, 0) == "clean"


# ── _format ──────────────────────────────────────────────────────────────
def test_format_malicious_line():
    r = {
        "ipv4": True, "dnsbl_listed": ["DroneBL", "SpamCop"], "dnsbl_checked": 6,
        "dshield": {"count": 142, "ascountry": "US"},
        "greynoise": {"classification": "malicious", "name": "Mirai"},
        "tor": 1,
        "abuse": {"abuseConfidenceScore": 87, "totalReports": 12},
    }
    out = _format("1.2.3.4", r)
    assert "1.2.3.4" in out
    assert "[malicious]" in out
    assert "\x02" in out          # bold emphasis preserved (not stripped away)
    assert "2/6" in out
    assert "DShield 142" in out and "US" in out
    assert "GreyNoise malicious" in out and "Mirai" in out
    assert "Tor exit" in out
    assert "87%" in out and "12 rpts" in out


def test_format_clean_line():
    r = {"ipv4": True, "dnsbl_listed": [], "dnsbl_checked": 6,
         "dshield": None, "greynoise": {"classification": "unseen"},
         "tor": 0, "abuse": None}
    out = _format("8.8.8.8", r)
    assert "[clean]" in out
    assert "DNSBL clean (0/6)" in out
    assert "GreyNoise unseen" in out
    assert "Tor no" in out
    assert "AbuseIPDB" not in out


def test_format_ipv6_dnsbl_na():
    r = {"ipv4": False, "dnsbl_listed": [], "dnsbl_checked": 0,
         "dshield": None, "greynoise": None, "tor": -1, "abuse": None}
    out = _format("2001:db8::1", r)
    assert "DNSBL n/a (IPv6)" in out


def test_format_strips_control_bytes():
    # A poisoned upstream name must not inject CR/LF/formatting into the line.
    r = {"ipv4": True, "dnsbl_listed": [], "dnsbl_checked": 1, "dshield": None,
         "greynoise": {"classification": "malicious",
                       "name": "evil\r\nPRIVMSG #x :pwned"},
         "tor": -1, "abuse": None}
    out = _format("1.2.3.4", r)
    assert "\r" not in out and "\n" not in out


# ── async cmd_ip ─────────────────────────────────────────────────────────
class _FakeBot:
    def __init__(self):
        self.cfg = {"bot": {"command_prefix": "."}}
        self.sent: list[tuple[str, str]] = []

    def rate_limited(self, nick):
        return False

    def notice(self, nick, msg):
        self.sent.append(("notice", msg))

    def privmsg(self, target, msg):
        self.sent.append(("privmsg", msg))


def _make_mod():
    bot = _FakeBot()
    mod = IpintelModule(bot)
    mod._ua = "UA"
    mod._abuse_key = ""
    return bot, mod


def test_cmd_ip_happy(monkeypatch):
    _reset_tor()
    bot, mod = _make_mod()
    monkeypatch.setattr(ipintel, "resolve_safe_ip", lambda t: "1.2.3.4")
    monkeypatch.setattr(ipintel, "_dnsbl_one",
                        lambda ip, zone, ua: 1 if zone.startswith("dnsbl.dronebl") else 0)
    monkeypatch.setattr(ipintel, "_dshield_sync", lambda ip, ua: {"count": 5})
    monkeypatch.setattr(ipintel, "_greynoise_sync", lambda ip, ua: {"classification": "unknown"})
    monkeypatch.setattr(ipintel, "_tor_is_exit", lambda ip, ua: 0)
    asyncio.run(mod.cmd_ip("nick", "#chan", "1.2.3.4"))
    assert bot.sent and bot.sent[-1][0] == "privmsg"
    assert "1.2.3.4" in bot.sent[-1][1]


def test_cmd_ip_rejects_non_public(monkeypatch):
    bot, mod = _make_mod()
    monkeypatch.setattr(ipintel, "resolve_safe_ip", lambda t: None)
    asyncio.run(mod.cmd_ip("nick", "#chan", "10.0.0.1"))
    assert any("refusing" in m for _, m in bot.sent)


def test_cmd_ip_usage_when_empty():
    bot, mod = _make_mod()
    asyncio.run(mod.cmd_ip("nick", "#chan", None))
    assert any(".ip <ip" in m for _, m in bot.sent)


def test_cmd_ip_invalid_target():
    bot, mod = _make_mod()
    asyncio.run(mod.cmd_ip("nick", "#chan", "1.2.3.4/foo"))
    assert any("invalid" in m for _, m in bot.sent)


def test_cmd_ip_survives_unexpected_exception(monkeypatch):
    # A source raising an UNFORESEEN error type (e.g. a raw urllib3 read
    # error, not in any helper's catch tuple) must degrade to that source's
    # sentinel via gather(return_exceptions=True), never abort the reply.
    _reset_tor()
    bot, mod = _make_mod()
    monkeypatch.setattr(ipintel, "resolve_safe_ip", lambda t: "1.2.3.4")

    def boom(*a, **k):
        raise RuntimeError("unexpected read error")

    monkeypatch.setattr(ipintel, "_dshield_sync", boom)
    monkeypatch.setattr(ipintel, "_dnsbl_one", lambda ip, zone, ua: 0)
    monkeypatch.setattr(ipintel, "_greynoise_sync", lambda ip, ua: {"classification": "benign"})
    monkeypatch.setattr(ipintel, "_tor_is_exit", lambda ip, ua: 0)
    asyncio.run(mod.cmd_ip("nick", "#chan", "1.2.3.4"))
    assert bot.sent and bot.sent[-1][0] == "privmsg"
    out = bot.sent[-1][1]
    assert "1.2.3.4" in out
    assert "\x02" in out          # reply still assembled with emphasis intact
