"""`.health` and `.uptime` — operator introspection for the bot.

`.health` is admin-only and surfaces every subsystem we can cheaply
reach from a module: load list, weather provider states, sender queue
depth, store dirty flags, and audit log chain integrity.  Each piece is
emitted as its own privmsg so we never butt up against IRC's 512-byte
line cap.

`.uptime` is the public companion: just process uptime, no internals.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import BotModule

log = logging.getLogger("internets.health")


def _fmt_duration(seconds: float) -> str:
    """Render an elapsed duration as ``Xd Yh Zm Ws`` (compact)."""
    seconds = max(0, int(seconds))
    days,  rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins,  secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if mins or hours or days:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _safe(callable_, default=None):
    """Run ``callable_`` and swallow exceptions, returning ``default``."""
    try:
        return callable_()
    except Exception as e:
        log.debug("health: probe failed: %s", type(e).__name__)
        return default


class HealthModule(BotModule):
    """Operator-facing subsystem inspector."""

    COMMANDS: dict[str, str] = {
        "health": "cmd_health",
        "uptime": "cmd_uptime",
    }

    def on_load(self) -> None:
        # We record our own start time rather than rely on the bot
        # exposing one — health may be loaded mid-run via `.load health`.
        self._started_at: float = time.time()

    def is_configured(self) -> bool:
        return True

    # ── public command ─────────────────────────────────────────────

    async def cmd_uptime(self, nick: str, reply_to: str,
                         arg: str | None) -> None:
        """Public: bot uptime, no internals."""
        up = _fmt_duration(time.time() - self._started_at)
        self.bot.privmsg(reply_to, f"{nick}: uptime {up}")

    # ── admin command ──────────────────────────────────────────────

    async def cmd_health(self, nick: str, reply_to: str,
                         arg: str | None) -> None:
        """Admin: per-subsystem state snapshot, one privmsg per line."""
        if not self.bot.is_admin(nick):
            self.bot.privmsg(reply_to,
                f"{nick}: .health is admin-only — try .uptime instead.")
            return

        # Replies go via notice to the requester to keep channel noise
        # down; if they invoked it in PM, that's where it lands anyway.
        target = nick if not reply_to.startswith(("#", "&", "+", "!")) else reply_to
        send = lambda msg: self.bot.privmsg(target, msg)

        # 1. Uptime ────────────────────────────────────────────────
        up = _fmt_duration(time.time() - self._started_at)
        send(f"[health] uptime: {up}")

        # 2. Modules ───────────────────────────────────────────────
        mods = _safe(lambda: dict(self.bot._modules), {}) or {}
        if mods:
            send(f"[health] modules loaded: {len(mods)}")
            for name in sorted(mods):
                inst = mods[name]
                cfg_ok = _safe(lambda i=inst: bool(i.is_configured()), None)
                badge = ("ok" if cfg_ok is True
                         else "unconfigured" if cfg_ok is False
                         else "?")
                send(f"[health]   module {name}: {badge}")
        else:
            send("[health] modules loaded: 0")

        # 3. Weather providers ────────────────────────────────────
        statuses = _safe(self._get_provider_status, []) or []
        if statuses:
            send(f"[health] weather providers: {len(statuses)}")
            for s in statuses:
                pid    = s.get("id", "?")
                state  = s.get("state", "?")
                calls  = s.get("calls", 0)
                fails  = s.get("fails", 0)
                hs     = s.get("health_score", 0.0)
                quota  = s.get("quota")  # Robustness agent adds this
                line = (f"[health]   provider {pid}: state={state} "
                        f"calls={calls} fails={fails} health={hs:.2f}")
                if quota is not None:
                    line += f" quota={quota}"
                send(line)
        else:
            send("[health] weather providers: (none configured)")

        # 4. Sender queue depth ──────────────────────────────────
        sender = getattr(self.bot, "_sender", None)
        queue  = getattr(sender, "queue", None) if sender is not None else None
        qsize  = _safe(lambda: queue.qsize(), None) if queue is not None else None
        send(f"[health] sender queue depth: "
             f"{qsize if qsize is not None else 'n/a'}")

        # 5. Store dirty flags ─────────────────────────────────────
        store = getattr(self.bot, "_store", None)
        dirty_loc  = getattr(store, "_dirty_locations", "?")
        dirty_chan = getattr(store, "_dirty_channels",  "?")
        dirty_user = getattr(store, "_dirty_users",     "?")
        send(f"[health] store dirty: locations={dirty_loc} "
             f"channels={dirty_chan} users={dirty_user}")

        # 6. Authed admins ─────────────────────────────────────────
        authed = _safe(lambda: len(self.bot._authed), None)
        send(f"[health] authed admins: "
             f"{authed if authed is not None else 'n/a'}")

        # 7. Audit log integrity ───────────────────────────────────
        intact, broken_idx = _safe(self._verify_audit, (None, -1))
        if intact is True:
            send("[health] audit log: intact")
        elif intact is False:
            send(f"[health] audit log: BROKEN at record index {broken_idx}")
        else:
            send("[health] audit log: unavailable")

        # 8. Bot counters (if exposed) ─────────────────────────────
        m = getattr(self.bot, "_metrics", None)
        if isinstance(m, dict) and m:
            counters = ", ".join(f"{k}={v}" for k, v in sorted(m.items()))
            send(f"[health] counters: {counters}")

    # ── helpers ────────────────────────────────────────────────────

    def _get_provider_status(self) -> list[dict[str, Any]]:
        """Pull provider_status() lazily — weather_providers is optional."""
        from weather_providers import provider_status  # noqa: PLC0415
        return provider_status()

    def _verify_audit(self) -> tuple[bool, int]:
        """Run AuditLog.verify(); return its tuple, or (None, -1) on error.

        Wrapped in its own method so ``_safe`` can swallow ImportError if
        someone removes audit_log.py.
        """
        import audit_log  # noqa: PLC0415
        return audit_log.default().verify()

    # ── help ───────────────────────────────────────────────────────

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}health           Per-subsystem health snapshot  [admin]",
            f"  {prefix}uptime           Show bot uptime",
        ]


def setup(bot: object) -> HealthModule:
    """Module entry point."""
    return HealthModule(bot)  # type: ignore[arg-type]
