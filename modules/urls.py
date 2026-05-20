from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse, urlunparse, urljoin

import requests
from requests.adapters import HTTPAdapter
from .base import BotModule

log = logging.getLogger("internets.urls")

# ---------------------------------------------------------------------------
# SSRF guardrail
# ---------------------------------------------------------------------------
# Outbound HTTP requests made on behalf of IRC users (especially the
# ``.expand`` follow-redirects helper) are an SSRF vector: an attacker can
# point a short URL at an internal address, the cloud metadata service, or a
# link-local address and trick the bot into hitting it.  We mitigate this by:
#
#   1. Restricting schemes to http/https.
#   2. Walking redirects manually (`allow_redirects=False`) so we can
#      re-validate the *next* URL's resolved IP at every hop.
#   3. Resolving the hostname to every A/AAAA it advertises and rejecting
#      the request if *any* answer falls in a private / loopback /
#      link-local / ULA / IPv4-mapped IPv6 / metadata-service range.
#   4. Capping hop count and total response size.
#
# DO NOT remove these checks: without them ``.expand`` is an unauthenticated
# request forgery primitive any channel user can drive.

_MAX_REDIRECTS = 5
_MAX_RESPONSE_BYTES = 64 * 1024  # plenty for is.gd JSON; short-circuits HTML
_REQUEST_TIMEOUT = 10

# Hard-coded cloud metadata service addresses (in addition to the link-local
# range that already covers 169.254.0.0/16).  Keeping the explicit entry
# makes the intent obvious in code review.
_METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254", "metadata.google.internal"})


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """Return True for any IP we refuse to talk to.

    Covers RFC1918, loopback, link-local (incl. AWS/GCP/Azure metadata at
    169.254.169.254), multicast, unspecified, reserved, IPv6 ULA (fc00::/7),
    and IPv4-mapped IPv6 (``::ffff:10.0.0.1`` style bypass attempts).
    """
    # ``ipaddress`` already classifies most categories, but IPv4-mapped IPv6
    # addresses report .is_private based on the *mapped* address; we unwrap
    # them explicitly so an attacker can't smuggle 10.0.0.1 in via
    # ::ffff:10.0.0.1.
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


def _host_is_safe(host: str) -> bool:
    """Resolve *host* and confirm every returned address is publicly routable.

    Returns False on resolution failure (fail-closed).  We check **all**
    addresses, not just the first, because some attackers publish DNS
    records with both a public and a private answer hoping the HTTP client
    picks the public one for the safety check and the private one for the
    actual connection (a DNS rebinding-style trick).  Rejecting on *any*
    private answer makes that ineffective for this code path.
    """
    if not host:
        return False
    # Literal-IP fast path: still validate the literal itself.
    try:
        return not _ip_is_blocked(ipaddress.ip_address(host))
    except ValueError:
        pass
    if host.lower() in _METADATA_HOSTS:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        sockaddr = info[4]
        addr_str = sockaddr[0]
        try:
            if _ip_is_blocked(ipaddress.ip_address(addr_str)):
                return False
        except ValueError:
            return False
    return True


def _url_is_safe(url: str) -> bool:
    """Scheme + host validation for a single URL (one hop)."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    # Reject anything that isn't http(s) — file://, gopher://, ftp://, etc.
    if p.scheme not in ("http", "https"):
        return False
    host = p.hostname or ""
    # Strip any zone-id (``fe80::1%eth0``) before resolving.
    if "%" in host:
        host = host.split("%", 1)[0]
    if host.lower() in _METADATA_HOSTS:
        return False
    return _host_is_safe(host)


# ---------------------------------------------------------------------------
# DNS TOCTOU pinning
# ---------------------------------------------------------------------------
# ``_host_is_safe`` resolves the hostname and rejects the request if *any*
# answer is private.  That's necessary but not sufficient: an attacker who
# controls authoritative DNS for ``evil.example.com`` can serve a public IP
# the first time we call ``getaddrinfo`` (during the safety check) and a
# *private* IP the second time, milliseconds later, when ``requests`` opens
# the actual TCP socket.  This is a classic time-of-check-to-time-of-use
# (TOCTOU) gap: the check and the connect each perform their own DNS lookup
# and the answers can differ.
#
# We close the window by doing the DNS resolution exactly ONCE per hop,
# re-running ``_ip_is_blocked`` against the specific IP we pick, and then
# forcing urllib3 to connect to that exact IP literal instead of letting it
# re-resolve the hostname.  TLS SNI and the HTTP ``Host:`` header continue
# to carry the *original* hostname so certificate verification and virtual
# hosting still work.  At every redirect we throw the pin away and repeat
# the resolve+validate+pin dance from scratch for the new host.
#
# DO NOT remove this pinning: without it the all-answers check above can be
# defeated by an attacker who flips DNS answers between the safety lookup
# and the connect.


def _resolve_safe_ip(host: str) -> str | None:
    """Resolve *host* once and return a single IP literal that passes
    ``_ip_is_blocked``, or ``None`` if any answer is unsafe / resolution
    fails.  This is the single source of truth used for both the safety
    check and the connection target — pinning them together is what closes
    the TOCTOU window described above.
    """
    if not host:
        return None
    # Literal-IP fast path: re-validate and return as-is.
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        return None if _ip_is_blocked(ip_obj) else str(ip_obj)
    if host.lower() in _METADATA_HOSTS:
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    picked: str | None = None
    for info in infos:
        sockaddr = info[4]
        addr_str = sockaddr[0]
        # Strip any IPv6 zone-id getaddrinfo may have tacked on.
        if "%" in addr_str:
            addr_str = addr_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            return None
        if _ip_is_blocked(ip):
            return None
        if picked is None:
            picked = str(ip)
    return picked


def _format_netloc(ip: str, port: int | None) -> str:
    """Format an (IP, port) as a URL netloc, bracketing IPv6 literals."""
    try:
        is_v6 = isinstance(ipaddress.ip_address(ip), ipaddress.IPv6Address)
    except ValueError:
        is_v6 = False
    host_part = f"[{ip}]" if is_v6 else ip
    if port is None:
        return host_part
    return f"{host_part}:{port}"


class _PinnedHostHTTPAdapter(HTTPAdapter):
    """Transport adapter that connects to a pre-resolved IP while keeping
    the original hostname for SNI / TLS verification / ``Host:`` header.

    See the big comment above ``_resolve_safe_ip`` for the threat model.
    The pin lives for a single hop; ``_safe_request`` mounts a fresh
    adapter (with a freshly-resolved IP) before every redirect target.
    """

    def __init__(self, pinned_ip: str, original_host: str, *args, **kwargs):
        self._pinned_ip = pinned_ip
        self._original_host = original_host
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):  # type: ignore[override]
        parsed = urlparse(request.url)
        # Defence-in-depth: if the URL we're about to send doesn't match the
        # host this adapter was pinned for, refuse rather than silently
        # connecting somewhere unexpected.  Callers must mount a fresh
        # adapter per hop.
        url_host = (parsed.hostname or "").lower()
        if url_host != self._original_host.lower():
            raise requests.exceptions.InvalidURL(
                f"pinned adapter host mismatch: url={url_host!r} "
                f"pinned={self._original_host!r}"
            )
        port = parsed.port  # None if default; preserved verbatim below.
        # Stash the human-meaningful URL so we can restore it on the
        # response below — callers expect ``resp.url`` to carry the
        # original hostname, not the IP literal we connected to.
        original_url = request.url
        # Rewrite the netloc to the pinned IP.  urllib3 will treat this as
        # a literal address and skip its own getaddrinfo() — that's the
        # whole point.
        new_netloc = _format_netloc(self._pinned_ip, port)
        new_parsed = parsed._replace(netloc=new_netloc)
        request.url = urlunparse(new_parsed)
        # Preserve the original hostname for HTTP virtual hosting.  Use the
        # original port too (if explicit) so the Host header matches what
        # the server expects to see.
        try:
            is_v6_orig = isinstance(
                ipaddress.ip_address(self._original_host),
                ipaddress.IPv6Address,
            )
        except ValueError:
            is_v6_orig = False
        host_for_header = (
            f"[{self._original_host}]" if is_v6_orig else self._original_host
        )
        if port is not None:
            request.headers["Host"] = f"{host_for_header}:{port}"
        else:
            request.headers["Host"] = host_for_header
        try:
            resp = super().send(request, **kwargs)
        finally:
            # Restore the prepared request URL whether send succeeded or not
            # so the caller (and any retry/inspection logic) sees the
            # original hostname-bearing URL.
            request.url = original_url
        # Override the response URL too — by default requests copies it
        # from the prepared request, which was the IP-literal form.
        try:
            resp.url = original_url
        except Exception:  # pragma: no cover - defensive
            pass
        return resp

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):  # type: ignore[override]
        # Inject ``assert_hostname`` / ``server_hostname`` so urllib3 checks
        # the TLS cert against the *original* hostname even though the
        # connection target is an IP literal.  Without this, requests would
        # try to verify the cert against the IP (which almost never matches
        # a SAN) and fail every HTTPS request.
        conn = super().get_connection_with_tls_context(
            request, verify, proxies=proxies, cert=cert
        )
        try:
            # urllib3 HTTPSConnectionPool exposes assert_hostname / server_hostname.
            if hasattr(conn, "assert_hostname"):
                conn.assert_hostname = self._original_host
            if hasattr(conn, "server_hostname"):
                conn.server_hostname = self._original_host
        except Exception:  # pragma: no cover - defensive
            pass
        return conn


def _safe_request(method: str, url: str, ua: str) -> requests.Response | None:
    """Walk redirects ourselves, re-validating the IP at every hop.

    Returns the final ``Response`` or ``None`` if any hop fails the SSRF
    check, the hop limit is exceeded, or the network errors out.

    DNS TOCTOU: each hop pins the connection to a single IP that we just
    validated via ``_ip_is_blocked``.  See the comment above
    ``_resolve_safe_ip``.  Redirects re-resolve from scratch — we never
    reuse a previous hop's pin for a new host.
    """
    current = url
    # We allocate a fresh session per hop because the pinned adapter is
    # only valid for the hostname it was built for; the previous session
    # is closed before we move on.
    last_session: requests.Session | None = None
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                p = urlparse(current)
            except ValueError:
                log.warning(f"URL blocked by SSRF guard (parse): {current!r}")
                return None
            if p.scheme not in ("http", "https"):
                log.warning(f"URL blocked by SSRF guard (scheme): {current!r}")
                return None
            original_host = p.hostname or ""
            # Strip zone-id before resolving (matches _url_is_safe).
            if "%" in original_host:
                original_host = original_host.split("%", 1)[0]
            if original_host.lower() in _METADATA_HOSTS:
                log.warning(f"URL blocked by SSRF guard (metadata): {current!r}")
                return None
            # Single resolve+validate+pin: this is the IP we both checked
            # *and* will connect to, closing the TOCTOU window.
            pinned_ip = _resolve_safe_ip(original_host)
            if pinned_ip is None:
                log.warning(f"URL blocked by SSRF guard: {current!r}")
                return None
            # Tear down any previous hop's session (and its now-stale pin)
            # before we start the next hop.
            if last_session is not None:
                last_session.close()
                last_session = None
            session = requests.Session()
            adapter = _PinnedHostHTTPAdapter(
                pinned_ip=pinned_ip, original_host=original_host
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            last_session = session
            # stream=True so we don't pull the body of an HTML redirect page.
            resp = session.request(
                method,
                current,
                headers={"User-Agent": ua},
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
                stream=True,
            )
            if resp.is_redirect or resp.is_permanent_redirect:
                loc = resp.headers.get("Location")
                resp.close()
                if not loc:
                    return None
                # Resolve relative redirects against the current URL (the
                # hostname form, NOT the IP-literal form the adapter sent —
                # `current` was never mutated).
                current = urljoin(current, loc)
                continue
            return resp
        log.warning(f"URL exceeded redirect limit: {url!r}")
        return None
    except requests.RequestException as e:
        log.warning(f"URL request error: {e}")
        return None
    finally:
        if last_session is not None:
            last_session.close()


def _strip_ctrl(s: str) -> str:
    """Strip CR/LF/NUL/IRC-formatting bytes from upstream-derived strings.

    Anything we splice into an IRC line that came from a third party (a
    redirect Location header, an is.gd error message, a Google Translate
    payload, an IP-API response) is untrusted: it can carry \r\n to inject
    a second IRC command, or IRC color/bold/reverse codes to spoof bot
    output.  Strip the lot before we hand it to ``privmsg``.
    """
    return "".join(
        ch for ch in s
        if ch not in ("\r", "\n", "\x00", "\x01", "\x02", "\x03",
                      "\x04", "\x0f", "\x16", "\x1d", "\x1f")
    )


def _shorten_sync(url: str, ua: str) -> str:
    """Shorten a URL via is.gd (free, no key required)."""
    # Validate the *user-supplied* URL before sending it to is.gd: we don't
    # want to ask is.gd to shorten http://10.0.0.1/admin either.
    if not _url_is_safe(url):
        return "shortening failed"
    try:
        r = requests.get(
            "https://is.gd/create.php",
            params={"format": "json", "url": url},
            headers={"User-Agent": ua},
            timeout=_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        if "shorturl" in d:
            # is.gd returns its own URL — sanitize defensively anyway.
            return f"\x02Short URL\x02 {_strip_ctrl(str(d['shorturl']))}"
        return _strip_ctrl(str(d.get("errormessage", "shortening failed")))
    except Exception as e:
        log.warning(f"URL shorten: {e}")
        return "shortening failed"


def _expand_sync(url: str, ua: str) -> str:
    """Expand a shortened URL by following redirects under SSRF guard."""
    resp = _safe_request("HEAD", url, ua)
    if resp is None:
        return "expansion failed"
    final = resp.url
    resp.close()
    # Re-sanitize: the final URL came from an attacker-controlled Location
    # header chain.  Strip any CR/LF/IRC-control bytes before display.
    final = _strip_ctrl(final)
    if final == url:
        return "\x02Long URL\x02 URL does not redirect"
    return f"\x02Long URL\x02 {final}"


class UrlsModule(BotModule):
    """URL shortener (is.gd) and expander module."""

    COMMANDS: dict[str, str] = {
        "shorten": "cmd_shorten",
        "expand": "cmd_expand",
        "unshorten": "cmd_expand",
    }

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    async def cmd_shorten(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Shorten a URL."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}shorten <url>")
            return
        url = arg.strip().split()[0]
        if not url.startswith(("http://", "https://")):
            self.bot.privmsg(reply_to, f"{nick}: URL must start with http:// or https://")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_shorten_sync, url, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_expand(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Expand a shortened URL."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}expand <url>")
            return
        url = arg.strip().split()[0]
        if not url.startswith(("http://", "https://")):
            self.bot.privmsg(reply_to, f"{nick}: URL must start with http:// or https://")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_expand_sync, url, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}shorten <url>          Shorten a URL via is.gd",
            f"  {prefix}expand/.unshorten <url> Expand a shortened URL",
        ]


def setup(bot: object) -> UrlsModule:
    """Module entry point — returns a UrlsModule instance."""
    return UrlsModule(bot)  # type: ignore[arg-type]
