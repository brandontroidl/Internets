"""Shared SSRF-safe HTTP fetch with DNS-TOCTOU pinning.

Used by any module that fetches a user-influenceable URL (probe.py
.headers/.down, scinews.py article reader).  The guard:

  * resolves the host and rejects if ANY answer is private / loopback /
    link-local / metadata / ULA / IPv4-mapped (rebinding all-answers check);
  * connects to the EXACT validated IP by pinning DNS resolution for the
    calling thread, so urllib3 cannot independently re-resolve the name to a
    different (internal) address between the check and the connect.  The real
    hostname is still used for the request, so SNI / TLS verification / Host
    all work normally;
  * re-resolves + re-validates + re-pins every redirect hop.

Why thread-local DNS pinning instead of an IP-literal adapter: under
requests 2.34 / urllib3 2.7 the HTTPAdapter ``server_hostname`` override does
not propagate, so connecting to an IP literal fails TLS SNI (handshake
failure).  Pinning ``socket.getaddrinfo`` keeps the hostname intact while
still forcing the connection to the validated IP.  The global wrapper is a
no-op unless the current thread has set a pin, so it does not affect any
other code path (and aiohttp uses the loop resolver, not this).
"""
from __future__ import annotations

import logging
import socket
import threading
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

import requests

import ipaddress

log = logging.getLogger("internets.netsafe")

DEFAULT_MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 10
METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254", "metadata.google.internal"})


class SSRFBlocked(Exception):
    """Raised when a URL/host fails the SSRF guard (unsafe IP, bad scheme, hop limit)."""


def ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True for any address we refuse to connect to (RFC1918/loopback/link-local/
    multicast/reserved/unspecified/ULA + IPv4-mapped-IPv6 unwrap)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
    )


# ── thread-local DNS pin (closes the resolve/connect TOCTOU) ──────────────
_pin = threading.local()
_orig_getaddrinfo = socket.getaddrinfo


def _pinning_getaddrinfo(host, *args, **kwargs):
    pins = getattr(_pin, "map", None)
    if pins:
        forced = pins.get(host)
        if forced is not None:
            return _orig_getaddrinfo(forced, *args, **kwargs)
    return _orig_getaddrinfo(host, *args, **kwargs)


# Install once (idempotent across re-imports).
if not getattr(socket.getaddrinfo, "_netsafe_wrapped", False):
    _pinning_getaddrinfo._netsafe_wrapped = True  # type: ignore[attr-defined]
    socket.getaddrinfo = _pinning_getaddrinfo  # type: ignore[assignment]


def resolve_safe_ip(host: str) -> str | None:
    """Resolve *host* once and return one IP literal that passes ``ip_is_blocked``
    (the SAME IP we then pin the connection to), or None if any answer is unsafe
    / resolution fails."""
    if not host:
        return None
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        return None if ip_is_blocked(ip_obj) else str(ip_obj)
    if host.lower() in METADATA_HOSTS:
        return None
    try:
        infos = _orig_getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return None
    picked: str | None = None
    for info in infos:
        addr_str = info[4][0]
        if "%" in addr_str:
            addr_str = addr_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            return None
        if ip_is_blocked(ip):
            return None
        if picked is None:
            picked = str(ip)
    return picked


def url_is_safe(url: str) -> bool:
    """Scheme (http/https) + host validation for one URL — a pre-flight check
    for handing a user-supplied URL to a third party (e.g. a shortener)."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname
    if "%" in host:
        host = host.split("%", 1)[0]
    if host.lower() in METADATA_HOSTS:
        return False
    return resolve_safe_ip(host) is not None


@contextmanager
def safe_open(method: str, url: str, ua: str, *, follow_redirects: bool = True,
              timeout: int = DEFAULT_TIMEOUT, max_redirects: int = DEFAULT_MAX_REDIRECTS):
    """Context manager yielding a streaming Response fetched with per-hop SSRF
    validation + DNS pinning.  Raises ``SSRFBlocked`` for an unsafe/unresolvable
    host, bad scheme, or hop-limit overrun, or ``requests.RequestException`` on
    transport error.  Read the body INSIDE the with-block; session closed on exit.
    """
    session: requests.Session | None = None
    try:
        current = url
        for _ in range(max_redirects + 1):
            try:
                p = urlparse(current)
            except ValueError as e:
                raise SSRFBlocked("unparseable URL") from e
            if p.scheme not in ("http", "https"):
                raise SSRFBlocked(f"scheme {p.scheme!r} not allowed")
            host = p.hostname or ""
            if "%" in host:
                host = host.split("%", 1)[0]
            if host.lower() in METADATA_HOSTS:
                raise SSRFBlocked("metadata host")
            pinned = resolve_safe_ip(host)
            if pinned is None:
                raise SSRFBlocked("refusing non-public or unresolvable host")
            if session is not None:
                session.close()
            session = requests.Session()
            # Pin this thread's DNS for the request: urllib3 will resolve `host`
            # to exactly `pinned` (the validated IP), so it cannot rebind to an
            # internal address.  Cleared right after the connection is made
            # (the body read reuses the established connection).
            _pin.map = {host: pinned}
            try:
                resp = session.request(method, current, headers={"User-Agent": ua},
                                       allow_redirects=False, timeout=timeout, stream=True)
            finally:
                _pin.map = {}
            if resp.is_redirect or resp.is_permanent_redirect:
                if not follow_redirects:
                    yield resp
                    return
                loc = resp.headers.get("Location")
                resp.close()
                if not loc:
                    raise SSRFBlocked("redirect without Location")
                current = urljoin(current, loc)
                continue
            yield resp
            return
        raise SSRFBlocked("redirect limit exceeded")
    finally:
        if session is not None:
            session.close()
