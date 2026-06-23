"""GitHub repository info — keyless (unauthenticated, 60 req/hr).

    .gh <owner/repo>   stars / forks / open issues / language / license / last push
"""
from __future__ import annotations

import asyncio
import logging

from .base import BotModule, fetch_json, help_row, strip_ctrl, ResponseTooLarge

log = logging.getLogger("internets.ghinfo")

_MAX_INPUT = 120


def _fetch_sync(repo: str, ua: str) -> str:
    """Blocking GitHub repo lookup — run via asyncio.to_thread.

    Hits the public, unauthenticated REST API (60 req/hr, no key).
    GitHub requires a User-Agent header.  Returns a friendly one-line
    string on every failure path — never raises to the caller.
    """
    repo = repo.strip().strip("/")
    if repo.count("/") != 1 or not all(repo.split("/")):
        return "usage: .gh <owner/repo>  e.g. .gh torvalds/linux"
    owner, name = repo.split("/")
    try:
        data = fetch_json(
            f"https://api.github.com/repos/{owner}/{name}",
            ua=ua,
            timeout=10,
            allow_404=True,
            headers={"Accept": "application/vnd.github+json"},
        )
        if not data or not isinstance(data, dict):
            return f"repo not found: {strip_ctrl(repo, 60)}"

        full = strip_ctrl(data.get("full_name") or repo, 80)
        stars = data.get("stargazers_count", 0)
        forks = data.get("forks_count", 0)
        issues = data.get("open_issues_count", 0)
        lang = strip_ctrl(data.get("language") or "n/a", 30)

        lic = data.get("license")
        lic_name = "none"
        if isinstance(lic, dict):
            lic_name = strip_ctrl(lic.get("spdx_id") or lic.get("name") or "none", 30)

        pushed = data.get("pushed_at") or ""
        # "2024-05-01T12:34:56Z" -> "2024-05-01"
        pushed_date = strip_ctrl(pushed[:10] if pushed else "n/a", 10)

        return (
            f"\x02{full}\x02 :: ★ {stars:,} :: forks {forks:,} :: "
            f"issues {issues:,} :: lang {lang} :: license {lic_name} :: "
            f"pushed {pushed_date}"
        )
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f"GitHub lookup ({repo}): {e}")
        return "lookup failed"
    except Exception as e:  # requests.RequestException et al.
        log.warning(f"GitHub lookup ({repo}): {e}")
        return "lookup failed"


class GhinfoModule(BotModule):
    """`.gh <owner/repo>` — public GitHub repository info (keyless)."""

    COMMANDS: dict[str, str] = {"gh": "cmd_gh"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_gh(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up a public GitHub repository."""
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}gh <owner/repo>  e.g. {p}gh torvalds/linux")
            return
        result = await asyncio.to_thread(_fetch_sync, arg[:_MAX_INPUT], self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "gh <owner/repo>", f"GitHub repo info  e.g. {prefix}gh torvalds/linux"),
        ]


def setup(bot: object) -> GhinfoModule:
    """Module entry point — returns a GhinfoModule instance."""
    return GhinfoModule(bot)  # type: ignore[arg-type]
