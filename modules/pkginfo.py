"""Package registry lookups — KEYLESS network module.

    .pypi <pkg>     PyPI: latest version, summary, license, release date, URL.
    .npm <pkg>      npm: latest version, description, license, last publish.
    .crates <name>  crates.io: max version, downloads, description, license, docs.

All outbound HTTP goes through base.fetch_json (size-capped).  Each command
runs its blocking _fetch_sync via asyncio.to_thread and returns a single
sanitised IRC line.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote

import requests

from .base import (
    BotModule,
    ResponseTooLarge,
    cred,
    fetch_json,
    help_row,
    strip_ctrl,
)

log = logging.getLogger("internets.pkginfo")

# npm registry documents can be large (full version history); cap higher.
_NPM_MAX_BYTES = 1024 * 1024

_MAX_INPUT = 120

# Conservative package-name charset.  Registry names use letters, digits,
# and a small set of separators (npm scopes use ``@scope/pkg``).  The regex
# alone admits ``..`` (every char is allowed), so the ".." check below blocks
# path traversal within the trusted registry host explicitly.  Mirrors the
# validate + quote pattern already used in ipinfo.py / ipintel.py.
_PKG_RE = re.compile(r"^[A-Za-z0-9._@/-]{1,100}$")


def _valid_pkg(name: str) -> bool:
    """True only for a conservative package name with no traversal segment."""
    return bool(_PKG_RE.match(name)) and ".." not in name


def _clip(s: object, n: int) -> str:
    """Sanitise upstream text and clip to n chars (ellipsis if truncated)."""
    text = strip_ctrl(s, 400)
    if len(text) > n:
        text = text[: n - 1].rstrip() + "…"
    return text


def _pypi_sync(pkg: str, ua: str) -> str:
    """Blocking PyPI lookup — run via asyncio.to_thread."""
    try:
        data = fetch_json(
            f"https://pypi.org/pypi/{quote(pkg, safe='')}/json",
            ua=ua,
            timeout=10,
            allow_404=True,
        )
        if data is None or not isinstance(data, dict):
            return f"pypi: '{strip_ctrl(pkg, 60)}' not found"
        info = data.get("info") or {}
        name = info.get("name") or pkg
        version = info.get("version") or "?"
        summary = info.get("summary") or ""
        license_ = info.get("license") or ""
        url = info.get("project_url") or info.get("home_page") or info.get("package_url") or ""

        # Release date = upload time of the current version's first file.
        released = ""
        releases = data.get("releases") or {}
        files = releases.get(version) or []
        if files and isinstance(files, list):
            ut = files[0].get("upload_time_iso_8601") or files[0].get("upload_time") or ""
            released = ut[:10] if ut else ""

        parts = [f"\x02{strip_ctrl(name, 60)}\x02 {strip_ctrl(version, 30)}"]
        if summary:
            parts.append(_clip(summary, 160))
        meta = []
        if license_:
            meta.append(f"license {_clip(license_, 40)}")
        if released:
            meta.append(f"released {strip_ctrl(released, 10)}")
        if meta:
            parts.append(" ".join(meta))
        if url:
            parts.append(_clip(url, 80))
        return " :: ".join(parts)
    except (requests.RequestException, ResponseTooLarge, KeyError, ValueError,
            TypeError) as e:
        log.warning("pypi lookup for %r: %s", pkg, e)
        return "pypi: lookup failed"


def _npm_sync(pkg: str, ua: str) -> str:
    """Blocking npm registry lookup — run via asyncio.to_thread."""
    try:
        data = fetch_json(
            f"https://registry.npmjs.org/{quote(pkg, safe='')}",
            ua=ua,
            timeout=10,
            max_bytes=_NPM_MAX_BYTES,
            allow_404=True,
        )
        if data is None or not isinstance(data, dict):
            return f"npm: '{strip_ctrl(pkg, 60)}' not found"
        name = data.get("name") or pkg
        latest = (data.get("dist-tags") or {}).get("latest") or "?"
        description = data.get("description") or ""

        # License may be a string or a {type: ...} dict.
        license_ = data.get("license") or ""
        if isinstance(license_, dict):
            license_ = license_.get("type") or ""

        published = ""
        times = data.get("time") or {}
        if latest and latest in times:
            published = str(times[latest])[:10]

        parts = [f"\x02{strip_ctrl(name, 60)}\x02 {strip_ctrl(latest, 30)}"]
        if description:
            parts.append(_clip(description, 160))
        meta = []
        if license_:
            meta.append(f"license {_clip(license_, 40)}")
        if published:
            meta.append(f"published {strip_ctrl(published, 10)}")
        if meta:
            parts.append(" ".join(meta))
        return " :: ".join(parts)
    except (requests.RequestException, ResponseTooLarge, KeyError, ValueError,
            TypeError) as e:
        log.warning("npm lookup for %r: %s", pkg, e)
        return "npm: lookup failed"


def _crates_sync(name: str, ua: str) -> str:
    """Blocking crates.io lookup — run via asyncio.to_thread.

    crates.io requires a descriptive User-Agent (passed via ``ua``).
    """
    try:
        data = fetch_json(
            f"https://crates.io/api/v1/crates/{quote(name, safe='')}",
            ua=ua,
            timeout=10,
            allow_404=True,
            max_bytes=2 * 1024 * 1024,  # popular crates (serde, ...) carry many versions
        )
        if data is None or not isinstance(data, dict):
            return f"crates: '{strip_ctrl(name, 60)}' not found"
        crate = data.get("crate") or {}
        crate_name = crate.get("name") or name
        max_version = crate.get("max_version") or crate.get("newest_version") or "?"
        downloads = crate.get("downloads")
        description = crate.get("description") or ""

        # License lives on the latest version, not the crate root.
        license_ = ""
        versions = data.get("versions") or []
        if versions and isinstance(versions, list):
            license_ = versions[0].get("license") or ""

        docs = crate.get("documentation") or ""
        homepage = crate.get("homepage") or crate.get("repository") or ""

        parts = [f"\x02{strip_ctrl(crate_name, 60)}\x02 {strip_ctrl(max_version, 30)}"]
        if description:
            parts.append(_clip(description, 160))
        meta = []
        if downloads is not None:
            try:
                meta.append(f"{int(downloads):,} downloads")
            except (ValueError, TypeError):
                pass
        if license_:
            meta.append(f"license {_clip(license_, 40)}")
        if meta:
            parts.append(" ".join(meta))
        link = docs or homepage
        if link:
            parts.append(_clip(link, 80))
        return " :: ".join(parts)
    except (requests.RequestException, ResponseTooLarge, KeyError, ValueError,
            TypeError) as e:
        log.warning("crates lookup for %r: %s", name, e)
        return "crates: lookup failed"


class PkginfoModule(BotModule):
    """`.pypi` / `.npm` / `.crates` — keyless package registry lookups."""

    COMMANDS: dict[str, str] = {
        "pypi": "cmd_pypi",
        "npm": "cmd_npm",
        "crates": "cmd_crates",
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

    async def cmd_pypi(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}pypi <package>")
            return
        name = arg.strip()[:_MAX_INPUT]
        if not _valid_pkg(name):
            self.bot.privmsg(reply_to, f"{nick}: invalid package name")
            return
        result = await asyncio.to_thread(_pypi_sync, name, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_npm(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}npm <package>")
            return
        name = arg.strip()[:_MAX_INPUT]
        if not _valid_pkg(name):
            self.bot.privmsg(reply_to, f"{nick}: invalid package name")
            return
        result = await asyncio.to_thread(_npm_sync, name, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_crates(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}crates <name>")
            return
        name = arg.strip()[:_MAX_INPUT]
        if not _valid_pkg(name):
            self.bot.privmsg(reply_to, f"{nick}: invalid package name")
            return
        result = await asyncio.to_thread(_crates_sync, name, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "pypi <package>", "PyPI: version, summary, license, date"),
            help_row(prefix, "npm <package>", "npm: version, description, license, date"),
            help_row(prefix, "crates <name>", "crates.io: version, downloads, license, docs"),
        ]


def setup(bot: object) -> PkginfoModule:
    return PkginfoModule(bot)  # type: ignore[arg-type]
