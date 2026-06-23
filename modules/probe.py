"""Network probers — connect to a user-supplied host.

    .headers <url>          HTTP status / server / type / redirect / security headers
    .ssl <host[:port]>      TLS cert issuer, CN, days-until-expiry
    .tcp <host> <port>      TCP connect probe: open / closed / filtered + latency
    .down <host|url>        reachability check (up / down)

SECURITY: every command calls base.resolve_public() first, which refuses
private / loopback / link-local / reserved addresses, so these can't be
aimed at internal services (SSRF).  Redirects are NOT followed (.headers /
.down report the Location instead of chasing it into an internal host).
"""
from __future__ import annotations

import datetime as _dt
import logging
import socket
import ssl
import time
from urllib.parse import urlparse

import requests
from .base import BotModule, cred, help_row, resolve_public, strip_ctrl

log = logging.getLogger("internets.probe")

_TIMEOUT = 7
_SEC_HEADERS = {
    "strict-transport-security": "HSTS",
    "content-security-policy": "CSP",
    "x-frame-options": "XFO",
    "x-content-type-options": "XCTO",
}


def _tcp(host: str, port_s: str) -> str:
    try:
        port = int(port_s)
    except (ValueError, TypeError):
        return "usage: .tcp <host> <port>"
    if not (1 <= port <= 65535):
        return "port out of range (1-65535)"
    try:
        infos = resolve_public(host, port)
    except ValueError as e:
        return f"{strip_ctrl(host, 60)}: {e}"
    fam, typ, proto, _, sockaddr = infos[0]
    s = socket.socket(fam, typ, proto)
    s.settimeout(5)
    t0 = time.monotonic()
    try:
        s.connect(sockaddr)
        ms = (time.monotonic() - t0) * 1000
        return f"{strip_ctrl(host, 60)}:{port} open ({ms:.0f} ms) [{sockaddr[0]}]"
    except (socket.timeout, TimeoutError):
        return f"{strip_ctrl(host, 60)}:{port} filtered (timeout)"
    except (ConnectionRefusedError, OSError):
        return f"{strip_ctrl(host, 60)}:{port} closed"
    finally:
        s.close()


def _ssl_cert(arg: str) -> str:
    host, _, p = arg.strip().partition(":")
    port = int(p) if p.isdigit() and 1 <= int(p) <= 65535 else 443
    try:
        infos = resolve_public(host, port)
    except ValueError as e:
        return f"{strip_ctrl(host, 60)}: {e}"
    fam, typ, proto, _, sockaddr = infos[0]
    ctx = ssl.create_default_context()
    try:
        with socket.socket(fam, typ, proto) as s:
            s.settimeout(_TIMEOUT)
            s.connect(sockaddr)
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                cert = ss.getpeercert()
    except ssl.SSLCertVerificationError as e:
        return f"{strip_ctrl(host, 60)}: cert NOT valid — {strip_ctrl(getattr(e, 'verify_message', '') or 'verification failed', 60)}"
    except (ssl.SSLError, socket.timeout, TimeoutError, OSError):
        return f"{strip_ctrl(host, 60)}:{port} TLS connect failed"
    if not cert:
        return f"{strip_ctrl(host, 60)}: no certificate"
    subj = dict(x[0] for x in cert.get("subject", []) if x)
    iss = dict(x[0] for x in cert.get("issuer", []) if x)
    cn = subj.get("commonName", "?")
    issuer = iss.get("organizationName") or iss.get("commonName") or "?"
    na = cert.get("notAfter", "")
    days = "?"
    try:
        exp = _dt.datetime.strptime(na, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=_dt.timezone.utc)
        days = (exp - _dt.datetime.now(_dt.timezone.utc)).days
    except (ValueError, TypeError):
        pass
    return (f"{strip_ctrl(host, 60)}:{port} CN={strip_ctrl(cn, 50)} "
            f"issuer={strip_ctrl(issuer, 40)} expires {strip_ctrl(na, 28)} ({days}d)")


def _headers(url: str, ua: str) -> str:
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return "usage: .headers <url>"
    try:
        resolve_public(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except ValueError as e:
        return f"{strip_ctrl(p.hostname, 60)}: {e}"
    try:
        with requests.get(u, allow_redirects=False, stream=True, timeout=_TIMEOUT,
                          headers={"User-Agent": ua}) as r:
            h = r.headers
            parts = [f"HTTP {r.status_code}"]
            if h.get("Server"):
                parts.append(f"server {strip_ctrl(h['Server'], 40)}")
            if h.get("Content-Type"):
                parts.append(f"type {strip_ctrl(h['Content-Type'].split(';')[0], 40)}")
            if r.is_redirect and h.get("Location"):
                parts.append(f"-> {strip_ctrl(h['Location'], 80)}")
            present = [v for k, v in _SEC_HEADERS.items() if k in (n.lower() for n in h)]
            parts.append("sec: " + (",".join(present) if present else "none"))
            return strip_ctrl(" :: ".join(parts))
    except requests.RequestException:
        return f"{strip_ctrl(p.hostname, 60)}: request failed"


def _down(arg: str, ua: str) -> str:
    s = arg.strip()
    if s.startswith(("http://", "https://")):
        host = urlparse(s).hostname or ""
        url = s
    else:
        host = s.split("/")[0]
        url = f"https://{host}"
    if not host:
        return "usage: .down <host|url>"
    try:
        resolve_public(host)
    except ValueError as e:
        return f"{strip_ctrl(host, 60)}: {e}"
    try:
        with requests.head(url, allow_redirects=False, stream=True, timeout=_TIMEOUT,
                          headers={"User-Agent": ua}) as r:
            return f"{strip_ctrl(host, 60)} is UP (HTTP {r.status_code})"
    except requests.RequestException:
        # HTTP failed — fall back to a bare TCP connect on 443/80.
        for port in (443, 80):
            try:
                infos = resolve_public(host, port)
                fam, typ, proto, _, sockaddr = infos[0]
                with socket.socket(fam, typ, proto) as sk:
                    sk.settimeout(5)
                    sk.connect(sockaddr)
                    return f"{strip_ctrl(host, 60)} is UP (tcp/{port} open)"
            except (ValueError, OSError):
                continue
        return f"{strip_ctrl(host, 60)} appears DOWN (no HTTP/TCP response)"


class ProbeModule(BotModule):
    """`.headers` / `.ssl` / `.tcp` / `.down` — SSRF-guarded network probers."""

    COMMANDS: dict[str, str] = {
        "headers": "cmd_headers",
        "ssl": "cmd_ssl",
        "tcp": "cmd_tcp",
        "down": "cmd_down",
    }

    def on_load(self) -> None:
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_headers(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}headers <url>")
            return
        import asyncio
        self.bot.privmsg(reply_to, await asyncio.to_thread(_headers, arg[:300], self._ua))

    async def cmd_ssl(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}ssl <host[:port]>")
            return
        import asyncio
        self.bot.privmsg(reply_to, await asyncio.to_thread(_ssl_cert, arg[:120]))

    async def cmd_tcp(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        parts = (arg or "").split()
        if len(parts) != 2:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}tcp <host> <port>")
            return
        import asyncio
        self.bot.privmsg(reply_to, await asyncio.to_thread(_tcp, parts[0][:120], parts[1][:6]))

    async def cmd_down(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}down <host|url>")
            return
        import asyncio
        self.bot.privmsg(reply_to, await asyncio.to_thread(_down, arg[:300], self._ua))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "headers <url>", "HTTP status/server/type/redirect/security headers"),
            help_row(prefix, "ssl <host[:port]>", "TLS cert issuer/CN/days-to-expiry"),
            help_row(prefix, "tcp <host> <port>", "TCP connect probe + latency"),
            help_row(prefix, "down <host|url>", "Reachability check (up/down)"),
        ]


def setup(bot: object) -> ProbeModule:
    return ProbeModule(bot)  # type: ignore[arg-type]
