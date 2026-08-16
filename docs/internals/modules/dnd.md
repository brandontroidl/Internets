# dnd.py - D&D 5e SRD spell/monster lookup

Keyless wrapper around dnd5eapi.co (SRD data, API version pinned to
`/api/2014`). One class `DndModule` on the shared [base](base.md) contract;
blocking pipeline `_fetch_sync()` -> `_get()` -> `_fmt_spell()` /
`_fmt_monster()` via `asyncio.to_thread`.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.dnd <spell-or-monster>` | `dnd.py - DndModule.cmd_dnd()` | `.dnd fireball`, `.dnd adult red dragon` | Spell: `<Name> [L3 Evocation] | cast: ... | range: ... | duration: ... - <desc, first 240 chars>`. Monster: `<Name> [Large dragon] | CR n | AC n | HP n | speed: walk 40 ft., fly 80 ft.` |

## Integration

- Base: `https://www.dnd5eapi.co/api/2014`; tries `/spells/<slug>` first, then
  `/monsters/<slug>` (`dnd.py - _fetch_sync()`), so a worst-case miss costs two
  sequential requests. Timeout 10 s each.
- `_slug()` lowercases, collapses every non-`[a-z0-9]` run to `-`, strips edge
  hyphens, caps at 64 chars - matching the API's kebab-case index and doubling
  as URL-path injection defense (no user byte reaches the URL unTransformed).
- `_get()` is an inline `requests.get(stream=True)` with 256 KB read cap
  (sanctioned inline stream+cap form); returns the parsed dict or `None`.
- Monster AC handling covers both API shapes: list of `{value: n}` dicts and a
  bare value (`_fmt_monster()`).
- Privacy: sends only the slugged query.

## Configuration

No key; `is_configured()` True. Shared UA credential.

## Failure behavior

`_get()` returns `None` for 404, oversized body, transport error, and JSON
parse error alike. `_fetch_sync()` therefore cannot distinguish "not in the
SRD" from "API down": every failure mode reports
`no D&D 5e SRD spell or monster matched '<query>'`. Rate limit check before
the thread spawn.

## Security notes

Slug transform whitelists URL-path bytes; 256 KB cap before parse; replies
built through `_strip_ctrl()` (400-char cap).

## Findings

- questionable | dnd.py - `_get()` | collapses transport errors, oversize
  bodies, and parse errors into the same `None` as a legitimate 404, so an
  outage is reported to the channel as "no ... matched" - misleading
  not-found on infrastructure failure (every sibling module distinguishes
  "unavailable" from "no match").
