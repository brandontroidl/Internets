from __future__ import annotations

import json as _json
from configparser import ConfigParser, Error as ConfigParserError
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from internets import IRCBot


# Default per-response byte cap for the shared fetch_json helper.  Most
# JSON APIs the bot talks to fit comfortably under 256 KB; modules with
# legitimately larger payloads (poke at ~1 MB, numberfact's Wikipedia
# OnThisDay feed at ~4 MB) pass an explicit ``max_bytes=``.
_DEFAULT_MAX_JSON_BYTES = 256 * 1024


class ResponseTooLarge(Exception):
    """Raised by ``fetch_json`` when the response body exceeds ``max_bytes``.

    The bot enforces per-call byte caps on every outbound HTTP call so
    a malicious or misconfigured upstream can't OOM the process with a
    JSON-bomb or accidental large payload.
    """


def fetch_json(
    url: str,
    *,
    ua: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 10,
    max_bytes: int = _DEFAULT_MAX_JSON_BYTES,
    allow_404: bool = False,
) -> Any:
    """Fetch a JSON response with a hard size cap.

    Streams the body, caps at ``max_bytes + 1`` raw bytes, and raises
    :class:`ResponseTooLarge` if the cap is exceeded - before the body
    is decoded or parsed.  Use this in module ``_fetch_sync`` helpers
    instead of ``requests.get(...).json()`` so JSON-bomb / OOM attacks
    against a compromised upstream stay bounded.

    If ``allow_404=True``, returns ``None`` on a 404 response instead of
    raising - useful for "lookup-or-miss" semantics (e.g. dictionary
    word, pokémon name) where 404 is an expected miss, not an error.

    Raises:
        requests.RequestException - on transport / non-404 HTTP error
        ResponseTooLarge          - body exceeded ``max_bytes``
        json.JSONDecodeError      - body wasn't valid JSON
    """
    import requests  # noqa: PLC0415 - lazy import keeps base.py importable in test envs
    hdrs = {"User-Agent": ua}
    if headers:
        hdrs.update(headers)
    # `with` guarantees the socket is released on every exit path (404
    # short-circuit, raise_for_status, ResponseTooLarge, success) - a
    # stream=True response left unclosed leaks the connection / FD.
    with requests.get(url, params=params, headers=hdrs,
                      timeout=timeout, stream=True) as r:
        if allow_404 and r.status_code == 404:
            return None
        r.raise_for_status()
        body = r.raw.read(max_bytes + 1, decode_content=True)
        if len(body) > max_bytes:
            raise ResponseTooLarge(
                f"response from {url} exceeded {max_bytes} bytes")
        return _json.loads(body.decode("utf-8", errors="replace"))


def resolve_public(host: str, port: int = 0) -> list:
    """Resolve ``host`` and refuse any non-public address (anti-SSRF).

    Returns the ``socket.getaddrinfo`` result list.  Raises ``ValueError``
    if the host is empty/oversized/unresolvable, or if ANY resolved address
    is private / loopback / link-local / multicast / reserved / unspecified.
    The network probers (.headers / .ssl / .tcp / .down) call this before
    connecting so they can't be aimed at internal services (cloud metadata
    endpoints, RFC1918 hosts, localhost, …).

    Note: this is resolve-time validation; a hostile DNS could still rebind
    between this check and a later connect (TOCTOU).  Callers that connect
    by IP should connect to an address from the returned list rather than
    re-resolving the name.
    """
    import socket  # noqa: PLC0415
    import ipaddress  # noqa: PLC0415
    host = (host or "").strip().rstrip(".")
    if not host or len(host) > 253:
        raise ValueError("invalid host")
    try:
        infos = socket.getaddrinfo(host, port or None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise ValueError("cannot resolve host")
    if not infos:
        raise ValueError("cannot resolve host")
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError("unresolvable address")
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
            raise ValueError("refusing non-public address")
    return infos


_PLACEHOLDER_MARKERS = (
    "changeme", "your-key", "placeholder", "set-in-secret-store",
    "<your-", "you@example", "example.com",
)


def cred(
    cfg: ConfigParser,
    secret_name: str,
    section: str,
    key: str,
    default: str = "",
) -> str:
    """Pull a credential or PII field - secret_store first, config fallback.

    For new installs the keys live exclusively in the secret store
    (see ``python -m secret_store``).  The config.ini fallback path
    exists only for upgrades from 2.4.0-and-earlier where keys were
    placed directly in the ini file.  Placeholder strings from the
    template (``you@example.com``, ``set-in-secret-store``, etc.) are
    treated as unset so they never leak into outbound HTTP requests.
    """
    try:
        import secret_store  # noqa: PLC0415
        v = secret_store.get(secret_name)
        if v:
            return v
    except ImportError:
        pass
    try:
        raw = cfg.get(section, key, fallback=default).strip()
    except (ConfigParserError, AttributeError):
        return default
    if any(m in raw.lower() for m in _PLACEHOLDER_MARKERS):
        return default
    return raw


# ── .help formatting ──────────────────────────────────────────────────
# Visible width of the usage column in `.help <module>` output.  Commands
# sit left of this column, descriptions right of it, so a module's lines
# align in monospace clients.  Usages wider than this fall back to a
# single separating space rather than wrapping awkwardly.
_HELP_USAGE_W = 24


def help_row(prefix: str, usage: str, desc: str, *, width: int = _HELP_USAGE_W) -> str:
    """Format one ``.help`` line consistently across all modules.

    ``usage`` is the command and any args WITHOUT the prefix - e.g.
    ``"cc <expr>"`` or ``"bofh/.excuse"`` (write aliases as ``/.alias``,
    no surrounding spaces).  The prefix is prepended once and the usage
    column padded to ``width`` so descriptions line up.  The two leading
    spaces match the indent the ``.help`` renderer expects, and the line
    is kept well under the 512-byte IRC limit (the sender truncates as a
    backstop).  Keeping every module on this one helper means `.help`
    stays uniform and `.help <cmd>` can still match on the leading token.
    """
    u = f"{prefix}{usage}"
    gap = " " if len(u) >= width else " " * (width - len(u))
    return f"  {u}{gap}{desc}"


_IRC_CTRL_RE = __import__("re").compile(r"[\x00-\x1f\x7f]")


def strip_ctrl(s: object, max_len: int = 400) -> str:
    """Drop IRC control bytes from untrusted text and cap its length.

    The single sanitizer for anything spliced into an IRC line that came
    from a third party (API titles, redirect Location headers, sensor
    names, user echoes, …).  Strips the FULL C0 range ``\\x00-\\x1f`` plus
    ``\\x7f`` (DEL) - not just CR/LF/NUL - so ``\\x02`` (bold), ``\\x03``
    (color), ``\\x16`` (reverse), ``\\x1b`` (ESC/ANSI) and ``\\x07`` (BEL)
    can't be used for bot-attributed formatting / escape / bell spoofing.
    Coerces non-``str`` input (e.g. an int or None) to ``str`` first.

    The IRC sender only strips ``\\r\\n\\x00`` as a transport backstop, so
    this is the real defense against formatting/escape injection - every
    module emitting upstream-derived text should route it through here.
    """
    text = "" if s is None else str(s)
    return _IRC_CTRL_RE.sub("", text)[:max_len]


class BotModule:
    """
    Base class for all bot modules.

    Subclasses define COMMANDS as a dict mapping command words to async method
    names.  All command handlers are coroutines::

        async def cmd_weather(self, nick: str, reply_to: str, arg: str | None) -> None:
            ...

    For blocking I/O (HTTP via requests, disk, CPU-heavy work), use::

        result = await asyncio.to_thread(requests.get, url, ...)

    Sync hooks:
        on_load()    - called after module is registered (event loop thread)
        on_unload()  - called before module is removed
        on_raw(line) - called for every incoming IRC line (must be fast, sync)

    Override help_lines() to describe commands for .help output.
    """

    COMMANDS: dict[str, str] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Validate the COMMANDS → handler contract at class-definition time.

        ``COMMANDS`` maps command words to *method-name strings*; nothing
        in the type system ties those strings to real coroutine methods.
        Checking here turns a typo (or a sync handler) into an ImportError
        at startup instead of an ``AttributeError`` / ``TypeError`` the
        first time a user runs the command in production.
        """
        super().__init_subclass__(**kwargs)
        # inspect.iscoroutinefunction (not asyncio.*) - the asyncio alias
        # is deprecated for removal in Python 3.16.
        import inspect  # noqa: PLC0415 - local keeps base.py import-light
        for word, method_name in cls.COMMANDS.items():
            handler = getattr(cls, method_name, None)
            if handler is None:
                raise TypeError(
                    f"{cls.__name__}.COMMANDS maps {word!r} → {method_name!r}, "
                    f"but {cls.__name__} defines no such method")
            if not inspect.iscoroutinefunction(handler):
                raise TypeError(
                    f"{cls.__name__}.{method_name} (command {word!r}) must be "
                    f"`async def` - every command handler is a coroutine")

    def __init__(self, bot: IRCBot) -> None:
        self.bot = bot

    def help_lines(self, prefix: str) -> list[str]:
        """Return help text lines for .help output.  Override in subclasses."""
        return []

    def is_configured(self) -> bool:
        """Return True if this module has everything it needs to run.

        Modules that depend on an API key (imdb, lastfm, youtube, etc.)
        should override this to check whether the key is present.  The
        bot's ``.help`` skips modules where this returns False so the
        help output isn't cluttered with commands the user can't use.
        Module dispatch still works - admins can ``.load`` a module
        and add a key later - but it stays invisible to normal users
        until the key is in place.
        """
        return True

    def on_load(self) -> None:
        """Called after the module is registered.  Override for setup."""
        pass

    def on_unload(self) -> None:
        """Called before the module is removed.  Override for cleanup."""
        pass

    def on_raw(self, line: str) -> None:
        """Called for every incoming IRC line.  Must be fast and sync."""
        pass

    def forget(self, nick: str) -> int:
        """Erase every record this module holds about ``nick``.

        Called by the ``.forgetme`` privacy command for each loaded
        module, so right-to-erasure covers the whole bot.  Modules that
        persist user PII (seen, tell, notes, remind, …) MUST override
        this - mutate their store, persist it, and return the number of
        records removed.  The default no-op returns 0 for modules that
        hold nothing personal.
        """
        return 0
