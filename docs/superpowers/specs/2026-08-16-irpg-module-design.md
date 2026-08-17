# IRPG module design

An idle-RPG game module for Internets: players register, remain idle in the
channel, and gain levels for time spent connected and quiet. Activity costs
progress. The module owns the whole game and depends on no other module, so a
deployment can unload everything else and be a dedicated game bot.

## Provenance and licensing

This is an original implementation of the game's **systems**, not a port of any
existing bot's code. Game rules, formulas, timings, and probabilities are
functional design and are implemented from the design described here. Everything
the bot says is written for this project. No source file from another
implementation is translated, restructured, or paraphrased into this module.

The one deliberate compatibility point is the on-disk player format, which is
read and written in the widely used flat-file shape so an operator can move an
existing game across. Interoperating with a data format is a functional
requirement, not an appropriation of expression.

Consequence for contributors: if you are looking at another bot's source while
working on this module, stop. Work from this document. A contribution that
reproduces another project's code or its message text cannot be accepted,
because Internets ships under ISC and the module has to be distributable under
that licence.

## Scope

In scope for the first release:

- Registration, login, logout, password change, class change.
- Idle levelling on a time-to-level curve.
- Ten equipment slots, item discovery, item levels.
- Player versus player challenges and their outcomes.
- Team battles.
- Random world events: fortune, misfortune, and rare high-value finds.
- Alignment (good, evil, neutral) and its effect on outcomes.
- Quests, with both the timed and the map-movement forms.
- Penalties for channel activity: speaking, nick change, part, quit, kick, and
  voluntary logout.
- Administrative commands for the game operator.
- Periodic state flush, backup, and an on-request status line.

Out of scope for the first release, recorded so the boundary is deliberate:
a web front end, cross-network play, and any HTTP surface. The module makes no
outbound network requests at all.

## Placement and naming

`modules/irpg.py`, registering as module name `irpg`.

`modules/idlerpg.py` already exists and stays. It is a **client** that reads a
remote game's published XML and reports a player's standing. This module is a
**server**: it runs the game. The two do not overlap in command names and can be
loaded together, though an operator running their own game will usually want
only this one.

## Command surface

The game's commands are private-message only, addressed to the bot as bare
words. Internets already makes the command prefix optional in a private message,
so `register alice hunter2 Warrior` sent to the bot dispatches without a prefix,
which is the interaction players expect. In a channel the prefix is required,
and the module answers a channel invocation by directing the player to a private
message rather than replying in public.

Player commands: `register`, `login`, `logout`, `newpass`, `align`, `whoami`,
`status`, `info`, `quest`, `removeme`.

Operator commands, all gated on game-admin status held in the player record:
`chpass`, `chuser`, `chclass`, `del`, `delold`, `mkadmin`, `deladmin`, `push`,
`jump`, `hog`, `pause`, `silent`, `backup`, `reloaddb`, `clearq`, `restart`,
`die`, `rehash`, `help`, `peval`.

`peval` evaluates an expression in the bot's own process. It is the single most
dangerous command in the module and is restricted to the configured owner, off
by default, and refused entirely unless explicitly enabled in configuration. It
is documented in the security section below rather than treated as an ordinary
admin command.

### Four names collide with Internets core

`help`, `die`, `restart`, and `rehash` are core commands. `IRCBot._handle_privmsg`
resolves `_CORE` before the module registry, so a module registering those names
is silently shadowed. This is the same defect already recorded as known issue 9,
where `modules/health.py` registers `uptime` and its handler has never run.

The module declares the names it owns:

```python
CLAIMS: frozenset[str] = frozenset({"help", "die", "restart", "rehash"})
```

and `_handle_privmsg` consults claims before `_CORE`. Unloading the module
returns the names to core with no further action. This is the only change
outside `modules/`; the design rationale, the alternatives, and the test
requirements for it are in the implementation plan.

## Game systems

### Levelling

A player who is logged in and idle accrues time toward the next level. Time to
next level grows geometrically: each level requires a fixed multiple of the
previous level's requirement, on a base interval, so early levels arrive in
minutes and later levels take days. The curve is a configuration value with the
traditional default, so an operator moving a game across keeps their pacing.

Only idle time counts. Any of the penalised actions below both costs time and
resets nothing else; the player simply moves further from the next level.

### Penalties

Each penalised action adds time to the player's remaining time-to-level, scaled
by the player's current level so that a high-level player pays proportionally.
The penalty types are speaking in the channel, changing nick, parting, quitting,
being kicked, logging out voluntarily, and failing a quest. Each type has its own
multiplier in configuration, and an optional ceiling caps any single penalty.

Penalties are recorded per type on the player record so `whoami` can show where a
player's time went.

### Equipment

Ten slots: ring, amulet, charm, weapon, helm, tunic, gloves, shield, leggings,
and boots. Each holds an item with an integer level; the sum across slots is the
player's item total, which decides battles.

While idle, a player periodically finds an item. The found item's level is drawn
against the player's own level, and it is equipped only if it beats what occupies
that slot. Rare finds above the normal ceiling exist and are announced.

### Battles

A challenge compares the challenger's effective total against an opponent's,
each modified by alignment, with a random component. The winner gains time toward
their next level; the loser is unaffected beyond the outcome message. A losing
challenger may suffer an additional setback on a rare secondary roll.

Team battles select two groups from the online players and resolve the same
comparison between group totals.

### Alignment

A player is good, evil, or neutral, chosen with `align` and changeable at a cost.
Good and evil confer opposing modifiers in battle, and each has an associated
periodic world event that benefits its own alignment at the expense of the other.

### World events

While at least one player is online, the module rolls periodically for each
event type. The probability of each is proportional to the number of online
players and inversely proportional to the tick interval, so a busy channel sees
events at the same real-world rate regardless of how often the clock runs. Event
types are: a large fortune, a large misfortune, a rare high-level find, a team
battle, and the two alignment events.

### Quests

Two quest forms. A timed quest sends a party of players away for a duration; on
completion every member gains time toward their next level. A map quest gives
each member a pair of coordinates to reach on a toroidal grid; members move each
tick and the quest completes when all have arrived.

A quest fails if any member is penalised while it is running. On failure the
whole party is set back and the quest ends.

Quest state is exported to a file each tick when configured, so an external
display can read it. The file is written through the same durable-write path as
the player database.

## Runtime integration

### The clock

`on_load()` starts one `asyncio.Task` running the game tick. The tick interval is
configurable with the traditional default. Every tick: advance idle time, roll
events, advance quests, and periodically flush state.

`on_unload()` cancels the task and awaits its cancellation before returning, so
`.reload irpg` cannot leave two clocks running. The task body catches and logs
its own exceptions so a single bad tick does not kill the game loop.

### Blocking work

The player database, the backup, and the quest file are written with
`asyncio.to_thread`. Nothing in the tick path performs blocking I/O on the event
loop.

### Module state and reload

Internets builds a fresh module object on every load, so nothing in memory
survives a reload. `on_load()` reads the database from disk and `on_unload()`
flushes it, which makes reload a durable round trip and gives the operator a
clean way to apply a code change to a running game.

Game state lives on the module instance rather than in module-level globals, so a
reload cannot leave a stale reference alive in a closure or a pending task.

### IRC events the game needs

Registered through `on_raw()`, which Internets calls for every inbound line
before command dispatch:

| Event | Effect |
| --- | --- |
| JOIN | Mark present; auto-login if configured and the host matches |
| PART, QUIT, KICK | Penalise, mark absent |
| NICK | Penalise, follow the rename |
| Channel PRIVMSG | Penalise by message length |
| NOTICE to the channel | Penalise by message length |

Netsplit detection is a configuration option: when enabled, a quit whose message
looks like a split is not penalised until a grace period passes without the
player returning.

## Configuration

`[irpg]` in `config.ini`, read at `on_load()` so `.reload irpg` picks up changes.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enable` | bool | `false` | Master switch; the module loads inert when false |
| `channel` | str | none | Channel the game runs in; required when enabled |
| `db_file` | path | `irpg_players.db` | Player database |
| `quest_file` | path | unset | Quest export; disabled when unset |
| `tick_seconds` | int | `3` | Game clock interval |
| `base_ttl` | int | `600` | Seconds to level 1 |
| `ttl_factor` | float | `1.16` | Geometric growth per level |
| `pen_*` | int | per type | Penalty multipliers |
| `pen_cap` | int | unset | Ceiling on a single penalty |
| `map_size` | int pair | `500x500` | Map quest grid |
| `owner` | str | unset | Account permitted to use `peval` |
| `allow_peval` | bool | `false` | Must be true for `peval` to run at all |
| `auto_login` | bool | `false` | Log a player in on join from a known host |
| `voice_on_login` | bool | `false` | Grant voice to a logged-in player |
| `detect_splits` | bool | `true` | Suppress quit penalties during a netsplit |

The module is inert until `enable` is true and `channel` is set, so loading it on
a bot that is not running a game does nothing.

## Persistence

One flat file of player records, one field per column, matching the traditional
layout so an existing game's file can be used unchanged. Fields: account, hashed
password, admin flag, class, level, remaining time to level, alignment, host
mask, online flag, idle time, the map coordinates, the ten item levels, the
per-type penalty totals, and the creation and last-login timestamps.

Passwords are stored hashed and salted. New records use the project's existing
hashing helper; records carrying the legacy hash from an imported file are
verified against that scheme and transparently upgraded on the player's next
successful login.

Writes go through a temporary file and an atomic replace, with the previous
version retained, matching how `store.py` protects its datasets. A corrupt or
truncated read is quarantined rather than overwritten, so a bad file never
destroys the only copy.

## Security

The module accepts untrusted input from any user in the channel and stores
credentials, so it carries a real security surface.

- **Passwords never reach a log.** `register`, `login`, `newpass`, and `chpass`
  all take a password as an argument. The module declares
  `SECRET_ARGS = frozenset({"register", "login", "newpass", "chpass"})`, which
  the dispatcher already honours, so the argument is masked in the command log.
  This mechanism exists because `.pwn` wrote passwords to disk; see known issue
  22.
- **All output is sanitised.** Player-chosen names, classes, and item names are
  attacker-controlled and are passed through `strip_ctrl` before reaching IRC, so
  a crafted name cannot inject formatting or forge the shape of a line.
- **`peval` is off unless explicitly enabled** and restricted to the configured
  owner. It executes arbitrary code in the bot's process, which means full
  compromise of every credential the bot holds. The default is off, the
  configuration key is named so it cannot be enabled by accident, and enabling it
  is logged at startup.
- **Admin status is stored in the player record**, not derived from Internets'
  admin session. A game admin is not a bot admin and cannot reach any core
  command.
- **Rate limiting** uses the standard module gate before any command that writes
  state, so registration and login cannot be used to flood the disk.

## Testing

The module is testable without a network, because nothing in it makes outbound
requests.

- Levelling: a player at a known level and time advances to exactly the expected
  level after a known number of ticks.
- Penalties: each type applies its multiplier, scales with level, and respects the
  ceiling.
- Battles: with the random component pinned, a stronger player wins; alignment
  modifiers move the outcome in the documented direction.
- Items: a found item replaces a weaker item in its slot and never a stronger one.
- Quests: both forms complete; a penalty during a quest fails it and sets the
  party back.
- Persistence: a written database reads back identically; a truncated file is
  quarantined rather than overwritten; a legacy password hash verifies and
  upgrades.
- Reload: loading, unloading, and reloading leaves exactly one clock task, and
  state survives the round trip.
- Self-sufficiency: with `irpg` as the only loaded module, registration, login,
  levelling, and a battle all work. This is what makes the "unload everything
  else and it is a game bot" claim testable rather than aspirational.
- Claims: the four claimed names reach the module while it is loaded and return
  to core when it is unloaded.

## Open questions for the implementer

None. Every behavioural choice above is resolved. Where a value is
configuration, its default is given; where a mechanism is shared with the rest
of the bot, the existing helper is named.
