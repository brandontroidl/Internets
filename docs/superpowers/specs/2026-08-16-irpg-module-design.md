# IRPG module design

An idle-RPG game module for Internets: players register, stay connected and
quiet, and gain levels for idle time. Activity costs progress. The module owns
the whole game and depends on no other module, so a deployment can unload
everything else and be a dedicated game bot.

Revision 2. Revision 1 claimed to have no open questions; an adversarial review
found that its lifecycle, identity, presence, and persistence-format decisions
were unresolved prerequisites rather than implementation details, and that two
of its claims about this codebase were simply wrong. Both errors are corrected
below and the work is now phased so the foundation is proven before game logic
couples to it.

## Provenance and licensing

This is an original implementation of the game's **systems**, not a port of any
existing bot. Rules, formulas, timings, and probabilities are functional design.
Everything the bot says is written for this project. No source file from another
implementation is translated, restructured, or paraphrased.

Contributor rule: work from this document. If you are reading another bot's
source while implementing, stop. A contribution reproducing another project's
code or message text cannot be accepted, because Internets ships under ISC and
this module must be distributable under it.

The one compatibility point is the on-disk player format, so an operator can
move an existing game across. Interoperating with a data format is functional.
That format is not yet specified; see phase 2 and the open questions.

## Corrections to revision 1

Two claims about this codebase were wrong and are recorded so they are not
reintroduced:

- **There is no "standard module gate before commands that write state."**
  `internets.py - IRCBot._handle_privmsg()` applies `flood_limited()` to every
  accepted command before dispatch. `IRCBot.rate_limited()` is a separate API
  cooldown a module calls explicitly. Game-specific limits on registration and
  login must therefore be built, not assumed.
- **`store.py` exposes no reusable durable-write path.**
  `Store._write()` is private, static, and writes a JSON envelope for Store's
  own datasets. This module needs its own atomic-write implementation for a
  flat text format, built to the same standard rather than inheriting it.

## Phasing

Revision 1 treated this as one unit. It is not: it combines a core dispatch
change, an account system, a legacy data format, presence tracking, a scheduler,
a persistence engine, and roughly six game subsystems. Foundation errors would
be found only after game logic depended on them.

Each phase lands independently, with tests, and leaves the bot working.

| Phase | Delivers | Why this seam |
| --- | --- | --- |
| 1 | Core prerequisites: module command claims, module-declared secret arguments already present, and a cancellable module-owned task helper | Nothing game-specific; usable by any module and testable alone |
| 2 | Foundation: player record, persistence format and atomic writes, session and identity model, presence reconciliation, monotonic clock, tick supervision, status | The parts that are expensive to change once real databases exist |
| 3 | Minimal playable game: register, login, logout, levelling, speech and membership penalties | First point a player can use it; proves the foundation under real play |
| 4 | Equipment and single-player events | Additive on a proven core |
| 5 | Challenges, team battles, alignment | Depends on equipment totals |
| 6 | Quests, then operator commands | Quests depend on presence being reliable; operator commands need the audit behaviour from phase 2 |

Only phase 1 and phase 2 are specified to implementation depth here. Later
phases are scoped, not detailed, and get their own specs. That is deliberate:
writing decision-free detail for phase 5 before phase 2 exists would be
inventing constraints for a foundation that has not been built.

## Placement and naming

`modules/irpg.py`, module name `irpg`. `modules/idlerpg.py` stays; it is a
client that reads a remote game's published XML. This is the server. They share
no command names.

## Phase 1: core prerequisites

### Command claims

The game's commands are private-message bare words, which Internets already
supports because the prefix is optional in a private message. Four canonical
names collide with core: `help`, `die`, `restart`, `rehash`.
`IRCBot._handle_privmsg()` resolves `_CORE` before the module registry, so a
module registering them is silently shadowed. This is known issue 9, where
`modules/health.py` registers `uptime` and its handler has never run.

A module declares the names it owns:

```python
CLAIMS: frozenset[str] = frozenset({"help", "die", "restart", "rehash"})
```

`_handle_privmsg()` consults claims before `_CORE`. Requirements:

- A claim is honoured only while the declaring module is loaded; unloading
  returns the name to core with no further action.
- Two modules claiming one name is a load error, refused the same way a
  duplicate command registration is refused today.
- A claimed name is logged at load, because silently taking `die` from core is
  exactly the kind of thing an operator must be able to see.
- Claiming does not confer privilege. A claimed `restart` reaching the module
  runs the module's handler with the module's own authorization, and cannot
  restart the bot.

### Module-owned background tasks

Phase 2 needs a periodic task, and getting cancellation wrong leaves two clocks
after a reload. Rather than each module reimplementing it, `modules/base.py`
gains a helper that owns the task lifecycle: start on load, cancel and await on
unload, restart-on-crash with backoff, and a health record the module can
report. Tested against reload, unload during a tick, and a task that raises
every time.

### Secret arguments

Already present. `BotModule.SECRET_ARGS` masks a command's argument in the
dispatch log. This module declares `register`, `login`, `newpass`, and `chpass`.
No phase-1 work beyond using it.

## Phase 2: foundation

### Time

All progression uses a monotonic clock. Wall-clock timestamps are stored only
for display and audit. Reasons: sleep and resume, NTP corrections, and event
loop stalls otherwise grant or destroy progress.

The tick advances the game by *measured elapsed monotonic time*, not by
assuming the nominal interval passed. A tick delayed by ten seconds advances the
game by ten seconds. Elapsed time beyond a configurable ceiling is clamped and
the clamp is logged, so a laptop resuming from suspend does not hand every
online player an hour of progress.

Event probability is a hazard process over elapsed time and online population,
`p = 1 - exp(-rate * elapsed * online)`, which is well defined for any elapsed
value and cannot exceed 1. Revision 1's "proportional to online, inversely
proportional to interval" was undefined for a long tick.

The clock and the random source are injected, so tests are deterministic and a
tick can be driven directly.

### Presence

Online status is derived from observed IRC state, never trusted from the
database. On load every player is offline until seen.

| Signal | Effect |
| --- | --- |
| Bot's own JOIN to the game channel | Game resumes; request NAMES |
| NAMES reply (`353`, possibly several, ended by `366`) | Rebuild the present set |
| Player JOIN | Mark present; log in only if a session applies |
| Player PART, QUIT, KICK | Mark absent, end session, penalise |
| Player NICK | Follow the rename, penalise |
| Bot's own PART or KICK from the game channel | Pause the game, all players absent |
| Connection lost | Pause the game, all players absent, freeze accrual |

Accrual happens only while the game is running and the player is present. The
status command reports why the game is paused when it is.

The game channel is a single RFC-casefolded name. Commands from any other
channel are answered with a redirect to a private message and change no state.

### Identity and sessions

A hostmask is weak identity, and destructive operator commands depend on it, so
the session model is explicit rather than implied:

- A session is created by an explicit successful `login` and bound to the
  account, the current nick, and the observed hostmask.
- A session ends on QUIT, PART, KICK, nick change to a nick with no session,
  loss of the bot's connection, module unload, or an idle expiry.
- Where the network provides an authenticated account (IRCv3 `account-notify`,
  which this bot already requests), that account is preferred over the hostmask.
- Auto-login is off by default. When enabled it requires a network-authenticated
  account, never a bare hostmask, because cloaks, gateways, and recycled DHCP
  addresses make hostmask identity unsafe.
- Game-admin status lives in the player record and confers nothing outside the
  game. A game admin is not a bot admin.
- Every operator command that deletes or grants writes an entry to the game's
  own audit trail with the acting account, the target, and the time.

### Persistence

One flat text file of player records. **The format is normative and must be
written out in full before implementation**, including column order, delimiter
and escaping, encoding, integer bounds, duplicate-account policy, malformed-line
policy, and the treatment of unknown trailing fields. Compatibility with an
existing game is claimed only when fixture-based import and export tests pass
against a real file; until then the spec says "intended to interoperate", not
"compatible".

Writes are atomic: temporary file in the same directory, 0600 before content,
`os.replace`, one-deep backup retained. A read that fails validation is
quarantined under a timestamped name and the game refuses to start on that
dataset rather than overwriting it. Note that no writer in this repository calls
`fsync`, recorded as known issue 12; this module states its durability limit
rather than implying more.

Passwords are hashed and salted using the project's existing helper. Records
imported with a legacy hash are verified against that scheme and upgraded on the
next successful login.

State is flushed on a timer and on unload, not per command. The status command
reports last successful flush and whether state is dirty.

### Tick supervision

A tick computes its transition and commits only after validation, so a malformed
record cannot leave the game half-advanced. A record that fails validation is
isolated and reported, not retried forever. The task carries a done callback
recording clock health: last successful tick, consecutive failures, and whether
the game is degraded. Status surfaces all of it, because a game that has
silently stopped ticking while claiming to run is the worst failure mode here.

### Concurrency

Command handlers and the tick both mutate player state. All mutation goes
through a single lock held for the duration of a state transition, and the flush
takes a snapshot under that lock before writing outside it. This is the defect
class already recorded three times in this repository: `notes.py`, `steam.py`,
and `internets.py - _save_shadow_bans()` all serialize a structure in a worker
thread while the loop mutates it.

## Phases 3 to 6: game systems, scoped

Detail comes with each phase's own spec. Scope:

- **Levelling.** Time to next level grows geometrically on a configurable base
  and factor. Only idle presence accrues.
- **Penalties.** Speech, nick change, part, quit, kick, voluntary logout, and
  quest failure each add time scaled by level, with an optional per-event
  ceiling, recorded per type for display.
- **Equipment.** Ten slots, each an integer level; the sum decides battles. Idle
  players find items; a find replaces a slot only if it beats what is there.
- **Battles.** Compare effective totals with alignment modifiers and a random
  component. Winner gains time.
- **Alignment.** Good, evil, neutral, changeable at a cost, with opposing battle
  modifiers and an associated periodic event each.
- **Quests.** A timed form and a map-movement form on a toroidal grid. A penalty
  against any member fails the quest and sets the party back. Quest state
  optionally exported for an external display.
- **Operator commands.** Account and record management, game control, and
  backup. All audited.

## Security

The module stores credentials and accepts untrusted input from any channel user.

- **Passwords never reach a log**, via `SECRET_ARGS` on the four commands that
  take one. This mechanism exists because `.pwn` wrote passwords to disk, known
  issue 22.
- **All output is sanitised** through `strip_ctrl`. Player names, classes, and
  item names are attacker-controlled and must not be able to inject formatting
  or forge a line's shape.
- **Authentication limits are purpose-built.** The core flood gate gives generic
  spam protection only; registration, login, and password change get their own
  limits keyed by account and identity, because the core gate is per-nick and a
  nick is free to change.
- **`peval` is removed.** Revision 1 included an owner-only, disabled-by-default
  command evaluating arbitrary expressions in the bot's process. It is gone,
  along with its `owner` and `allow_peval` configuration. Reachable code
  execution in a process holding IRC credentials, roughly forty API keys, the
  audit HMAC key, and every player's password hash is not justifiable for
  operator convenience in a module that ships publicly, and an operator with
  shell access already has `.reload` and a Python prompt. This is the single
  largest deliberate divergence from the traditional game.

## Testing

Nothing here makes an outbound request, so the module is fully testable offline
with an injected clock, random source, and outbound sink.

Phase 1: claims reach the module while loaded and return to core on unload; two
claimants is a load error; a claimed name confers no privilege. Task helper:
exactly one task after reload, cancellation awaited on unload, restart with
backoff on a task that raises.

Phase 2: presence rebuilt from a multi-line NAMES reply; accrual frozen while
disconnected and not resumed from stale flags; bot kicked from the channel
pauses the game; session ends on each documented signal; auto-login refuses a
bare hostmask; monotonic progression correct across a simulated clock jump and a
long delayed tick; elapsed clamp applied and logged; flush snapshot taken under
the lock; a failed write leaves state dirty; a truncated database is quarantined
and refuses to start; a legacy hash verifies and upgrades; concurrent command
and tick mutation is serialized; tick failure isolates the bad record and marks
the game degraded.

Phases 3 to 6: per-phase, including commands invoked in a channel, password
commands redacted in the log, ordering of speech penalty against command
execution, quest members disconnecting or being deleted mid-quest, and output
volume when one event generates many messages.

Throughout: **self-sufficiency** - with `irpg` the only loaded module,
registration, login, and levelling work. That is what makes "unload everything
else and it is a game bot" testable rather than aspirational.

## Open questions

Revision 1 said there were none. There are, and each blocks the phase named.

1. **The persistence format.** Blocks phase 2. Needs a normative appendix and at
   least one real database file as a fixture. Without a real file, "interoperable"
   is an assertion.
2. **Which network account source to trust.** Blocks phase 2 sessions. The bot
   requests `account-notify`; whether the target network supplies it decides
   whether auto-login can be offered at all.
3. **Whether the claim mechanism should be general or specific.** Blocks phase 1.
   General claims fix known issue 9 for every module; a narrower mechanism is
   less code and less risk. Recommendation: general, because the narrow version
   leaves `health.py` still broken.
4. **Single channel or several.** Assumed single throughout. A multi-channel game
   changes presence, penalties, and the data model, and should be decided before
   phase 2 rather than retrofitted.
5. **Elapsed-time clamp value.** A default is needed for the resume-from-suspend
   case; it is a judgement call about how much progress a delayed tick may grant.
