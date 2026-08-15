# health.py - operator self-check (.health) and public uptime (.uptime)

## Purpose

`HealthModule` gives an admin a one-command per-subsystem snapshot without
shell access, and everyone else a bare uptime figure. Every probe is wrapped
so a broken subsystem degrades one line of output instead of killing the
command. Base contract: [base](base.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.health` | `modules/health.py - HealthModule.cmd_health()` | admin-only subsystem snapshot, one line per item, delivered privately via `bot.preply()` (NOTICE when invoked in a channel) so quotas/counters never spill publicly |
| `.uptime` | `HealthModule.cmd_uptime()` | public; `nick: uptime 3d 4h 12m 9s` |

Uptime is measured from the MODULE's own load time (`on_load` stores
`time.time()`), deliberately not the process start - the comment notes the
module may be loaded mid-run via `.load health`, so after a live reload the
figure resets.

## Snapshot sections (`cmd_health`)

Each probe runs through `_safe()` (swallow-and-default) so one failure cannot
abort the rest:

1. Uptime (`_fmt_duration`).
2. Modules: count + per-module `is_configured()` badge
   (`ok` / `unconfigured` / `?`).
3. Weather providers: lazy import of `weather_providers.provider_status()`
   (`_get_provider_status()`) - state, call/fail counts, health score,
   optional quota.
4. Sender queue depth: `bot._sender.queue.qsize()`.
5. Store dirty flags - see Findings: two of the three attribute names are
   wrong.
6. Geocode cache stats (lazy import, silently skipped if unavailable).
7. Authed admin count (`len(bot._authed)`).
8. Audit log chain integrity: `audit_log.default().verify()` via
   `_verify_audit()` (own method so `_safe` can swallow an ImportError) -
   `intact` / `BROKEN at record index N` / `unavailable`.
9. Bot counters from `bot._metrics` if exposed.

## Integration / configuration / state

No external services, no secrets, no persistence; `is_configured()` returns
`True`. Reads other subsystems' private attributes read-only.

## Failure behavior

`_safe()` returns a default and logs the exception type at debug. Missing
optional subsystems render as `n/a` / `(none configured)` / `unavailable`
rather than erroring.

## Findings

- defect | health.py - HealthModule.cmd_health() | the store-dirty section
  reads `store._dirty_locations` and `store._dirty_channels`
  (health.py:134-135), but `store.Store` defines `_dirty_locs` and
  `_dirty_chans` (store.py:136-137); the `getattr` default means those two
  fields print `?` forever. Only `_dirty_users` matches a real attribute.
  Known, previously verified defect - recorded, not justified.
- questionable | health.py - HealthModule.cmd_health() | two sections are both
  numbered `# 6.` in the comments (geocode cache and authed admins), so the
  comment numbering drifts one off through the rest of the method.
- test-gap | health.py - HealthModule | no `tests/test_health*` exists; the
  dirty-flag defect above is exactly the class of bug a probe-name test would
  have caught.
