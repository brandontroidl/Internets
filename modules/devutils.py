"""Developer utilities - pure local text codecs and time helpers.

No network.  Commands (all rate-limited):
    .b64 <text>       - base64 encode UTF-8 input
    .unb64 <text>     - base64 decode (replies "binary or invalid utf-8"
                        if the result isn't decodable text)
    .hex <text>       - auto-detect: hex-only input decodes, otherwise encodes
    .morse <text>     - auto-detect: encode plain text, or decode if input
                        is only ``.``, ``-``, ``/``, and spaces
                        (``/`` is the word separator)
    .uuid             - emit a random UUIDv4
    .epoch [arg]      - no arg = current epoch; numeric arg = epoch -> ISO 8601
                        UTC; ISO 8601 datetime string = parsed -> epoch
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import logging
import re
import time
import uuid
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.devutils")

_MAX_INPUT = 400


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


_HEX_RE = re.compile(r"^[a-fA-F0-9]+$")
_MORSE_RE = re.compile(r"^[\.\-/ ]+$")

_MORSE_MAP: dict[str, str] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "'": ".----.", "!": "-.-.--",
    "/": "-..-.", "(": "-.--.", ")": "-.--.-", "&": ".-...", ":": "---...",
    ";": "-.-.-.", "=": "-...-", "+": ".-.-.", "-": "-....-", "_": "..--.-",
    "\"": ".-..-.", "$": "...-..-", "@": ".--.-.",
}
_MORSE_REV: dict[str, str] = {v: k for k, v in _MORSE_MAP.items()}


def _morse_encode(text: str) -> str:
    out: list[str] = []
    for word in text.upper().split():
        letters = [_MORSE_MAP[c] for c in word if c in _MORSE_MAP]
        if letters:
            out.append(" ".join(letters))
    return " / ".join(out)


def _morse_decode(text: str) -> str:
    out: list[str] = []
    for word in text.strip().split("/"):
        letters = [_MORSE_REV.get(code, "") for code in word.strip().split() if code]
        out.append("".join(letters))
    return " ".join(w for w in out if w)


class DevutilsModule(BotModule):
    """`.b64` / `.unb64` / `.hex` / `.morse` / `.uuid` / `.epoch` - local dev utils."""

    COMMANDS: dict[str, str] = {
        "b64": "cmd_b64",
        "unb64": "cmd_unb64",
        "hex": "cmd_hex",
        "morse": "cmd_morse",
        "uuid": "cmd_uuid",
        "epoch": "cmd_epoch",
    }

    def is_configured(self) -> bool:
        return True

    async def cmd_b64(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}b64 <text>")
            return
        text = arg[:_MAX_INPUT]
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self.bot.privmsg(reply_to, _strip_ctrl(encoded))

    async def cmd_unb64(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}unb64 <text>")
            return
        text = arg.strip()[:_MAX_INPUT]
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            self.bot.privmsg(reply_to, "invalid base64")
            return
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.bot.privmsg(reply_to, "binary or invalid utf-8")
            return
        self.bot.privmsg(reply_to, _strip_ctrl(decoded))

    async def cmd_hex(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}hex <text>")
            return
        text = arg.strip()[:_MAX_INPUT]
        if _HEX_RE.match(text) and len(text) % 2 == 0:
            try:
                raw = bytes.fromhex(text)
                decoded = raw.decode("utf-8")
                self.bot.privmsg(reply_to, _strip_ctrl(decoded))
                return
            except (ValueError, UnicodeDecodeError):
                self.bot.privmsg(reply_to, "binary or invalid utf-8")
                return
        encoded = arg[:_MAX_INPUT].encode("utf-8").hex()
        self.bot.privmsg(reply_to, _strip_ctrl(encoded))

    async def cmd_morse(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}morse <text>")
            return
        text = arg[:_MAX_INPUT]
        if _MORSE_RE.match(text.strip()):
            out = _morse_decode(text)
        else:
            out = _morse_encode(text)
        self.bot.privmsg(reply_to, _strip_ctrl(out) or "no output")

    async def cmd_uuid(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        self.bot.privmsg(reply_to, str(uuid.uuid4()))

    async def cmd_epoch(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        if not arg or not arg.strip():
            self.bot.privmsg(reply_to, str(int(time.time())))
            return
        s = arg.strip()[:_MAX_INPUT]
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            try:
                ts = float(s)
                iso = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
                self.bot.privmsg(reply_to, _strip_ctrl(iso))
                return
            except (ValueError, OSError, OverflowError):
                self.bot.privmsg(reply_to, "invalid epoch")
                return
        try:
            iso_in = s.replace("Z", "+00:00")
            dt = _dt.datetime.fromisoformat(iso_in)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            self.bot.privmsg(reply_to, str(int(dt.timestamp())))
        except ValueError:
            self.bot.privmsg(reply_to, "invalid datetime - try ISO 8601 or epoch seconds")

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "b64 <text>", "Base64 encode"),
            help_row(prefix, "unb64 <text>", "Base64 decode"),
            help_row(prefix, "hex <text>", "Hex encode/decode (auto)"),
            help_row(prefix, "morse <text>", "Morse encode/decode (auto, / = word break)"),
            help_row(prefix, "uuid", "Random UUID4"),
            help_row(prefix, "epoch [arg]", "Epoch <-> ISO 8601 UTC"),
        ]


def setup(bot: object) -> DevutilsModule:
    return DevutilsModule(bot)  # type: ignore[arg-type]
