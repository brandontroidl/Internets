"""HTTP status-code lookup — pure local table, no API.

Command:
    .http <code>  — look up a 3-digit HTTP status code.

Covers the common 1xx/2xx/3xx/4xx/5xx codes plus the WebDAV/RFC-7540
extras (102, 207, 208, 226, 421, 423, 424, 425, 426, 428, 429, 431,
451, 506, 507, 510, 511) and the 418 teapot.  Reject anything that
isn't a literal 3-digit string.  Rate-limited per nick.
"""

from __future__ import annotations

import logging
from .base import BotModule

log = logging.getLogger("internets.httpcode")

_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


_CODES: dict[int, tuple[str, str]] = {
    100: ("Continue", "Client should continue sending the request body."),
    101: ("Switching Protocols", "Server is switching to the protocol the client requested."),
    102: ("Processing", "Server has received and is processing the request (WebDAV)."),
    103: ("Early Hints", "Preliminary response with header hints before the final response."),
    200: ("OK", "Request succeeded."),
    201: ("Created", "Request succeeded and a new resource was created."),
    202: ("Accepted", "Request accepted for processing but not yet completed."),
    203: ("Non-Authoritative Information", "Returned metadata is from a third-party copy."),
    204: ("No Content", "Request succeeded; no body to return."),
    205: ("Reset Content", "Request succeeded; client should reset its view."),
    206: ("Partial Content", "Server is returning part of the resource (range request)."),
    207: ("Multi-Status", "Body contains multiple status codes (WebDAV)."),
    208: ("Already Reported", "Members of a DAV binding already enumerated (WebDAV)."),
    226: ("IM Used", "Response is the result of one or more instance-manipulations."),
    300: ("Multiple Choices", "Multiple options for the resource are available."),
    301: ("Moved Permanently", "Resource has been permanently moved to a new URI."),
    302: ("Found", "Resource resides temporarily at a different URI."),
    303: ("See Other", "Response can be found at another URI via GET."),
    304: ("Not Modified", "Cached copy is still valid."),
    305: ("Use Proxy", "Resource must be accessed through the indicated proxy."),
    307: ("Temporary Redirect", "Resource temporarily at another URI; method must not change."),
    308: ("Permanent Redirect", "Resource permanently at another URI; method must not change."),
    400: ("Bad Request", "Server cannot process the request due to client error."),
    401: ("Unauthorized", "Authentication is required and has failed or not been provided."),
    402: ("Payment Required", "Reserved for future use."),
    403: ("Forbidden", "Server understood but refuses to authorize the request."),
    404: ("Not Found", "Resource could not be found."),
    405: ("Method Not Allowed", "Request method not supported for the target resource."),
    406: ("Not Acceptable", "No representation matches the Accept headers."),
    407: ("Proxy Authentication Required", "Client must authenticate with the proxy."),
    408: ("Request Timeout", "Server timed out waiting for the request."),
    409: ("Conflict", "Request conflicts with the current state of the resource."),
    410: ("Gone", "Resource is no longer available and will not return."),
    411: ("Length Required", "Content-Length header is required."),
    412: ("Precondition Failed", "Precondition in headers evaluated to false."),
    413: ("Payload Too Large", "Request body is larger than the server is willing to accept."),
    414: ("URI Too Long", "Request URI is longer than the server can interpret."),
    415: ("Unsupported Media Type", "Request body media type is not supported."),
    416: ("Range Not Satisfiable", "Requested range cannot be satisfied."),
    417: ("Expectation Failed", "Server cannot meet the Expect request-header field."),
    418: ("I'm a teapot", "Server refuses to brew coffee because it is a teapot."),
    421: ("Misdirected Request", "Request was directed at a server that cannot produce a response."),
    422: ("Unprocessable Entity", "Request is well-formed but semantically incorrect."),
    423: ("Locked", "Resource being accessed is locked (WebDAV)."),
    424: ("Failed Dependency", "Request failed because a previous request failed (WebDAV)."),
    425: ("Too Early", "Server is unwilling to risk processing a possibly replayed request."),
    426: ("Upgrade Required", "Client must switch to a different protocol."),
    428: ("Precondition Required", "Origin server requires the request to be conditional."),
    429: ("Too Many Requests", "Client has sent too many requests in a given time."),
    431: ("Request Header Fields Too Large", "Header fields are too large to be processed."),
    451: ("Unavailable For Legal Reasons", "Resource is unavailable for legal reasons."),
    500: ("Internal Server Error", "Generic server-side failure."),
    501: ("Not Implemented", "Server does not support the functionality required."),
    502: ("Bad Gateway", "Upstream server returned an invalid response."),
    503: ("Service Unavailable", "Server is overloaded or down for maintenance."),
    504: ("Gateway Timeout", "Upstream server did not respond in time."),
    505: ("HTTP Version Not Supported", "HTTP version used is not supported."),
    506: ("Variant Also Negotiates", "Content negotiation configuration error."),
    507: ("Insufficient Storage", "Server unable to store representation (WebDAV)."),
    510: ("Not Extended", "Further extensions to the request are required."),
    511: ("Network Authentication Required", "Client must authenticate to gain network access."),
}


class HttpcodeModule(BotModule):
    """`.http <code>` — look up an HTTP status code locally."""

    COMMANDS: dict[str, str] = {"http": "cmd_http"}

    def is_configured(self) -> bool:
        return True

    async def cmd_http(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}http <code>  e.g. {p}http 404")
            return
        s = arg.strip()
        if len(s) != 3 or not s.isdigit():
            self.bot.privmsg(reply_to, f"{nick}: code must be 3 digits (100-599)")
            return
        code = int(s)
        entry = _CODES.get(code)
        if entry is None:
            self.bot.privmsg(reply_to, f"{nick}: unknown status code {code}")
            return
        reason, desc = entry
        self.bot.privmsg(reply_to, _strip_ctrl(f"\x02{code}\x02 {reason} — {desc}"))

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}http <code>           HTTP status code lookup"]


def setup(bot: object) -> HttpcodeModule:
    return HttpcodeModule(bot)  # type: ignore[arg-type]
