# IRPG module design

An idle-RPG game module for Internets: players register, stay connected and
quiet, and gain levels for idle time. Activity costs progress. The module owns
the whole game and depends on no other module, so a deployment can unload
everything else and be a dedicated game bot.

Revision 3. Revision 1 claimed no open questions and was wrong. Revision 2
phased the work but front-loaded a core change nothing early needs, and
specified a claim mechanism with a security hole in it. Revision 3 reorders the
phases around a narrow playable path, closes that hole, and fixes a persistence
race. Each correction below was verified against source before being applied.

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

## Corrections carried forward

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
| 1 | **Done.** Cancellable module-owned task registry, per-module command-task draining, and connect/disconnect notifications | Every later phase depends on it; nothing game-specific, testable alone |
| 2 | Persistence: the normative player format, serialized versioned writes, quarantine, load-paused-on-corrupt | Expensive to change once real databases exist; no game logic needed to test it |
| 3 | Narrow playable path: explicit login, single-channel presence, levelling, speech and membership penalties | First point anyone can play; proves phases 1 and 2 under real use before more is built on them |
| 4 | Equipment and single-player events | Additive on a proven core |
| 5 | Challenges, team battles, alignment | Depends on equipment totals |
| 6 | Quests, then operator commands and the command-claim mechanism | Claims exist for `help`, `die`, `restart` and `rehash`, which are all operator surface; nothing before this needs them |

Revision 2 put command claims in phase 1. That was wrong: the four claimed names
are operator commands, and the narrow playable path in phase 3 needs none of
them. Front-loading a change to core command resolution bought nothing and
risked every module.

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

- **`auth` and `deauth` are permanently unclaimable**, refused at load. The
  PM-only guard for those two is keyed on the command word before resolution
  (`internets.py - IRCBot._handle_privmsg()`), so a module claiming `auth` would
  receive the real bot-admin password in its handler and make core
  authentication unreachable. A reserved set is a security boundary; logging a
  claim is not. Any future core command handling a credential joins that set.
- **Claims are the shared source of truth, not a dispatch-only override.**
  `admin_cmds.py - AdminCommandsMixin.cmd_help()` builds its listings from
  `_CORE` independently of dispatch, so a claim that only changed dispatch would
  leave core help describing a command the module now answers. Help, dispatch,
  log ownership, and the command metric all read the same registry.
- **A claim denies access to the core handler**, which is a privilege in itself
  even though it cannot invoke that handler. Revision 2's "claiming does not
  confer privilege" understated it. Interception is the risk; the reserved set
  is the mitigation.
- A claim is honoured only while the declaring module is loaded; unloading
  returns the name to core with no further action.
- **Unload does not currently drain running command tasks.**
  `IRCBot.unload_module()` removes the registry entries but a dispatched handler
  is already a scheduled task holding a bound method, so it can run on and mutate
  state after the module's final flush. Phase 1 either drains module-owned
  command tasks on unload or the module guards every mutation against a
  post-unload flag. Draining is preferred; the guard is the fallback if draining
  proves invasive.
- Two modules claiming one name is a load error, refused the same way a
  duplicate command registration is refused today.
- A claimed name is logged at load, because silently taking `die` from core is
  exactly the kind of thing an operator must be able to see.
- A claimed handler runs with the module's own authorization and cannot invoke
  the core handler. A claimed `restart` restarts nothing; it reaches the game's
  own restart. This bounds the privilege a claim grants but does not eliminate
  it, per the interception point above.

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
quarantined under a timestamped name and the module **loads paused and degraded
rather than refusing to load**. Revision 2 said "refuses to start", which
contradicted its own promise that status would explain a degraded game: if
`on_load()` raises, the module and its status command are never registered and
the operator is left reading logs. Loading paused keeps the diagnosis reachable
from IRC, and no dataset is ever overwritten. Note that no writer in this repository calls
`fsync`, recorded as known issue 12; this module states its durability limit
rather than implying more.

Passwords are hashed and salted using the project's existing helper. Records
imported with a legacy hash are verified against that scheme and upgraded on the
next successful login.

State is flushed on a timer and on unload, not per command, and the flush is
**versioned and serialized**. A naive snapshot-and-write loses data: flush A
snapshots version 1 and releases the lock, a command commits version 2, flush A
finishes and clears the dirty flag, and version 2 never reaches disk. So each
snapshot carries the mutation generation it was taken at; the dirty flag clears
only if the generation that reached disk is still current; and exactly one
writer runs at a time so an older snapshot cannot land after a newer one. Unload
stops new mutations, drains the writer, and verifies the final generation
persisted.

The status command reports last successful flush, the persisted generation, and
whether state is dirty.

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

Blocking, in phase order:

1. ~~The disconnect and reconnect notification contract.~~ **RESOLVED,
   phase 1 shipped.** `BotModule.on_connect()` and `on_disconnect()` are fanned
   out by `IRCBot._notify_modules()` from the connect-ok path and the
   connection-error branch. One module raising does not stop the others.
2. ~~Module-owned command task draining on unload.~~ **RESOLVED, phase 1
   shipped.** `IRCBot.create_module_task()` and `drain_module_tasks()` own
   background tasks per module; `_dispatch()` registers a module's command
   tasks the same way; `.unload`, `.reload` and `.reloadall` await the drain
   before unloading. The guard fallback was not needed.
3. **The persistence format.** Blocks phase 2. Needs a normative appendix and at
   least one real database file as a fixture. Without a real file,
   "interoperable" is an assertion, not a property.
4. **Reserved command set.** Blocks phase 6. `auth` and `deauth` are decided;
   whether anything else joins them needs a pass over `_CORE` when claims are
   built.

Deliberately not blocking, recorded so they are not mistaken for gaps:

- **Network account source.** Only matters if auto-login is offered, and it is
  off by default. Decide when someone wants it.
- **Single channel.** The design is single-channel by choice. Multi-channel is a
  later feature, not a prerequisite.
- **Elapsed-time clamp value.** Tuning. Pick a conservative default, make it
  bounded configuration, adjust from observation.
