# IRC Protocol and Connection Lifecycle

What the bot actually puts on the wire, what it accepts back, and what state each
inbound line changes. Written at protocol level: every claim below was checked
against `internets.py`, `protocol.py`, and `sender.py` rather than against intent.

Scope: the client half of the IRC session only. Process startup, the module loader,
and the command dispatch pipeline are covered in
[Runtime Architecture](architecture.md) and
[Command Reference](command-reference.md). Line-level notes live in
[internals/internets.md](internals/internets.md),
[internals/protocol.md](internals/protocol.md), and
[internals/sender.md](internals/sender.md).

Owning symbols:

| Concern | Symbol |
|---|---|
| Connect, registration, read loop | `internets.py - IRCBot.run()` / `._connect()` |
| CAP + SASL state machine | `internets.py - IRCBot._handle_cap()` |
| Numerics, ISUPPORT, NAMES, MODE | `internets.py - IRCBot._handle_numeric()` |
| Membership and identity events | `internets.py - IRCBot._handle_membership()` |
| Stateless line parsers | `protocol.py` |
| Outbound queue and wire writes | `sender.py - Sender` |

---

## 1. Connection lifecycle

```{graphviz}
digraph irc_lifecycle {
    rankdir=TB;
    node [shape=box, fontname="Helvetica", fontsize=10];
    edge [fontname="Helvetica", fontsize=9];

    start     [label="run()\nautoload modules", shape=ellipse];
    connect   [label="_connect()\nTCP + TLS handshake\nper-connection state reset"];
    register  [label="registration burst (priority 0)\nPASS? / CAP LS 302 / NICK / USER"];
    capneg    [label="CAP negotiation\nLS -> REQ -> ACK|NAK"];
    sasl      [label="SASL PLAIN\nAUTHENTICATE"];
    capend    [label="CAP END\n_cap_busy = False"];
    motd      [label="376 / 422 end of MOTD\nMODE +user_modes\nNickServ IDENTIFY / OPER\nstart keepalive + rejoin"];
    steady    [label="steady state\nreadline -> _process()"];
    lost      [label="link lost\nread timeout, PONG timeout,\nreset, TLS or OS error"];
    backoff   [label="wait _backoff_jittered(attempt)\n15s doubling to 300s, +-25%"];
    abort     [label="reconnect_aborted\n(SASL hard-fail, no NickServ fallback)", shape=box, style=dashed];
    shutdown  [label="graceful_shutdown()\nQUIT, drain, close", shape=ellipse];

    start    -> connect;
    connect  -> register;
    register -> capneg;
    capneg   -> sasl    [label="sasl ACKed\n+ NS_PW + TLS"];
    capneg   -> capend  [label="no sasl"];
    sasl     -> capend  [label="903 / 902 / 904 / 905"];
    capend   -> motd;
    motd     -> steady;
    steady   -> lost;
    lost     -> backoff;
    backoff  -> connect [label="retry"];
    backoff  -> abort   [label="permanent"];
    steady   -> shutdown [label="_stop set"];
    connect  -> backoff [label="connect failed"];
}
```

The read loop is a race between `reader.readline()` and `self._stop.wait()`
(`internets.py - IRCBot.run()`), so a `.shutdown`, SIGINT, or SIGTERM is acted on
immediately instead of after the next server PING.

---

## 2. TCP connect and TLS

`internets.py - IRCBot._connect()` opens the socket with
`asyncio.open_connection(SERVER, PORT, ssl=ssl_ctx, limit=_READ_LIMIT)`.
`_READ_LIMIT` is 8192 bytes; a longer line raises and is discarded (section 12).

### What is actually verified

TLS is on unless `[irc] ssl = false`. The context is
`ssl.create_default_context()`, which for the client purpose means:

| Property | Value in this build | Source |
|---|---|---|
| `verify_mode` | `CERT_REQUIRED` (chain to system CA store) | `ssl.create_default_context()` default |
| `check_hostname` | `True`, hostname from `SERVER` | default; asyncio passes `server_hostname` |
| `minimum_version` | TLS 1.3 | `_connect()` sets `TLSVersion.TLSv1_3` |
| Client certificate | none loaded | no `load_cert_chain()` call anywhere |

Two documented downgrades, both explicit and both logged:

- `INTERNETS_ALLOW_TLS12=1` in the environment lowers the floor to TLS 1.2 and logs
  `event=tls_minimum_downgraded` at WARNING.
- `[irc] ssl_verify = false` sets `check_hostname = False` and
  `verify_mode = CERT_NONE`, and logs `event=tls_unverified` at WARNING on
  **every** connect, deliberately, so the setting cannot regress quietly.

Not implemented: certificate pinning, CRL or OCSP checking (Python's `ssl` does
neither by default), SASL EXTERNAL / CertFP, and STARTTLS. Port 6697-style implicit
TLS is the only encrypted mode.

### Credential gate

Every outbound credential passes `internets.py - IRCBot._tls_or_refuse()` first.
On a plaintext link it logs `event=plaintext_cred_refused` at CRITICAL and the send
is suppressed. It reads `_tls_active` through `getattr(self, "_tls_active", False)`,
so a call before the first `_connect()` fails closed. The four gated credentials are
the server password, the SASL password, the NickServ password, and the oper password.

### Per-connection state reset

`_connect()` resets, in order: `_nick` back to the configured `NICKNAME`, the
CAP/SASL/identify flags, `_caps`, the `_last_pong` monotonic clock, `_chanops`, and
the ISUPPORT tables back to `_DEFAULT_CHANMODE_TYPES` / `_DEFAULT_PREFIX_MODES`. The
ISUPPORT reset is load-bearing: a reconnect can land on a different server through
DNS round-robin or failover, and a stale `CHANMODES` table silently misaligns MODE
parameters until the new 005 arrives (section 8). The `Sender` is then replaced, so
nothing queued for the dead socket survives.

---

## 3. Registration

Sent as one burst at priority 0 before the first read, from
`internets.py - IRCBot.run()`:

```text
PASS <server_password>        # only if configured AND _tls_or_refuse passes
CAP LS 302                    # sets _cap_busy = True
NICK <current nick>
USER <NICKNAME> 0 * :<REALNAME>
```

Order notes:

- `PASS` precedes `NICK`/`USER` as RFC 2812 section 3.1 requires. It is omitted
  entirely when `[irc] server_password` is unset or when the link is not TLS.
- `NICK` uses `self._nick`, not the config constant, so a nick bumped by a previous
  433 is re-used on the retry within the same connection.
- `USER` sends the configured `NICKNAME` as the username field and mode `0`.
- `registered` is a loop-local flag; the burst is re-sent once per connection after
  every reconnect.

### 433 nick collision

`_handle_numeric()` handles `ERR_NICKNAMEINUSE`: while the current nick is shorter
than the configured base plus three characters it appends `_`; past that it replaces
the tail with `base + secrets.randbelow(90) + 10`, giving a two-digit suffix from a
cryptographic RNG rather than a predictable counter. The new `NICK` goes out at
priority 0 immediately.

---

## 4. CAP negotiation

Requested capabilities (`config.py - DESIRED_CAPS`), all optional:

```text
multi-prefix  away-notify  account-notify  chghost
extended-join  server-time  message-tags  sasl
```

Handled subcommands in `internets.py - IRCBot._handle_cap()`:

| Subcommand | Behavior |
|---|---|
| `LS` | intersect offered with `DESIRED_CAPS`; `CAP REQ` the intersection, else `CAP END` |
| `ACK` | replace `_caps` with the ACKed set; start SASL if eligible, else `CAP END` |
| `NAK` | log only, then `CAP END` |
| `NEW` | intersect and `CAP REQ` again; no `CAP END` |

Escape hatches: a `421` naming `CAP` (server has no CAP support) and a `451`
(not registered) both clear `_cap_busy`, and reaching end-of-MOTD with `_cap_busy`
still set sends a late `CAP END` from the read loop. Negotiation can therefore never
wedge registration permanently.

Capability use in practice: `server-time` and `message-tags` are consumed by
`protocol.py - strip_tags()`, which drops the whole `@tag` block before any matching
(so a tagged PING is still answered and a tagged PONG still refreshes liveness).
`chghost` and `account-notify` drive `_handle_membership()` (section 9).
`multi-prefix` widens the NAMES prefixes parsed by `protocol.py - parse_names_entry()`.
`away-notify` is requested but no `AWAY` handler exists, so the extra traffic is
parsed and discarded.

### Known defect: multiline `CAP LS 302` is mishandled

Referenced in [known-issues.md](known-issues.md) and
[internals/internets.md](internals/internets.md#findings). Verified by direct regex
probe against `IRCBot._RE_CAP`, not inferred.

The bot requests `CAP LS 302`, which licenses the server to split the capability
list across several lines, each continuation marked by a bare `*` before the
trailing parameter. `_RE_CAP` does not model that marker:

```text
:srv CAP * LS :multi-prefix sasl      -> params = "multi-prefix sasl"      (correct)
:srv CAP * LS * :multi-prefix sasl    -> params = "* :multi-prefix sasl"   (wrong)
```

Actual behavior on a multiline reply:

1. The `*` continuation marker is tokenised as a capability name and never matches.
2. The first real capability on each continuation line keeps its leading `:`, so
   `:sasl` can never match `DESIRED_CAPS`. Capabilities in any later position on the
   same line still match.
3. The `LS` branch answers **each line independently**. A line carrying at least one
   desired capability triggers its own `CAP REQ`, so several REQs go out while the
   server is still listing.
4. A continuation line carrying no desired capability sends `CAP END` and clears
   `_cap_busy` mid-list. Capabilities listed on later lines - including `sasl` - are
   then never requested, and SASL silently does not happen.

A server whose capability list fits one line is unaffected, which is why this is not
visible on every network. There is no test covering `_handle_cap()` at all.

Second, smaller defect in the same handler: the `ACK` branch does
`self._caps = set(params.split())`, replacing rather than unioning. With several
REQs in flight (the case above) or a post-registration `CAP NEW` cycle, previously
granted capabilities disappear from `_caps`, and any ACK that does not start SASL
emits a `CAP END` even mid-session. Impact today is bounded because `_caps` is only
read to test for `sasl` during registration, but `_caps` is not trustworthy as
session state.

---

## 5. SASL

One mechanism: `PLAIN`. It is attempted only when all four hold - `sasl` is in
`_caps`, `[irc] nickserv_password` (or the secret store equivalent) is set, SASL is
not already in progress, and `_tls_or_refuse("sasl_password")` passes.

```text
->  AUTHENTICATE PLAIN
<-  AUTHENTICATE +
->  AUTHENTICATE <base64>
```

The payload is built by `protocol.py - sasl_plain_payload()`:

```text
base64( "\0" + nick + "\0" + password )
```

That is the RFC 4616 message `authzid NUL authcid NUL passwd` with an **empty
authzid**, `authcid` set to the bot's current runtime nick, and `passwd` set to the
NickServ password. Using `self._nick` rather than the startup `NICKNAME` constant is
deliberate: after a 433 bump the session identity is the bumped nick, and
authenticating the configured one fails.

Outcome numerics:

| Numeric | Meaning | Effect |
|---|---|---|
| 903 | RPL_SASLSUCCESS | `_ns_identified = True`, `CAP END` |
| 902 | ERR_NICKLOCKED | counted, `CAP END`; treated as transient |
| 904 | ERR_SASLFAIL | counted, `_sasl_failed_permanently = True`, `CAP END` |
| 905 | ERR_SASLTOOLONG | counted, `_sasl_failed_permanently = True`, `CAP END` |

Every outcome continues to `CAP END` so registration completes. The bot does not
retry SASL within a connection and does not send `AUTHENTICATE *` to abort.

### NickServ fallback

If SASL did not run or did not succeed, `_ns_identified` is still false when
end-of-MOTD arrives, and the read loop sends
`PRIVMSG NickServ :IDENTIFY <password>` (again gated on TLS). The target is the
literal string `NickServ`; the configurable `[bot] services_nick` (default
`ChanServ`) is used only for the invite request in section 8, not for identification.

Identification is then confirmed from either of two inbound signals, checked only
while `_ns_identified` is false:

- numeric `900` (RPL_LOGGEDIN), or
- a `NOTICE` whose source nick is `nickserv` and whose text contains `identified`
  or `recognized`.

`_deferred_rejoin()` polls `_ns_identified` for up to 10 s (40 ticks of 0.25 s)
before joining, so channels are joined after services have applied any vhost or
account grant. On timeout it logs `event=rejoin nickserv=timeout` and joins anyway.

### Permanent-failure short circuit

The reconnect path aborts instead of looping when all three hold:
`_sasl_failed_permanently` is set, the SASL failure count has reached 3, and no
NickServ password is configured as fallback. It logs
`event=reconnect_aborted reason=auth_failed` at CRITICAL and the bot exits its loop
rather than hammering the server with a credential that will not work.

---

## 6. Post-registration actions

Triggered once per connection by `376` (RPL_ENDOFMOTD) or `422` (ERR_NOMOTD), in
this order:

1. Late `CAP END` if `_cap_busy` is still set.
2. `MODE <nick> <[irc] user_modes>` if configured.
3. `PRIVMSG NickServ :IDENTIFY ...` if needed (section 5).
4. `OPER <oper_name> <oper_password>` if both are configured and the link is TLS.
5. Spawn the `keepalive` and `rejoin` background tasks.

Oper result handling: `381` (RPL_YOUREOPER) applies `[irc] oper_modes` and, if set,
`MODE <nick> +s <oper_snomask>`; `491` (ERR_NOOPERHOST) logs a warning and nothing
else. Neither triggers a retry.

---

## 7. Liveness: PING, PONG, and the pong timeout

Three independent timers, all constants on `IRCBot`:

| Constant | Value | Role |
|---|---|---|
| `_PING_INTERVAL` | 90 s | client PING period |
| `_PONG_TIMEOUT` | 240 s | silence after which the link is declared dead |
| `_READ_TIMEOUT` | 300 s | inactivity on `readline()` |

Inbound `PING` is answered from `_process()` before any other matching:

```text
PONG :<payload truncated to 400 characters>    # priority 0
```

The 400-character cap bounds an oversized-payload amplification; the literal slice
is asserted by the BUG-050 source-inspection test.

Inbound `PONG` sets `_last_pong = time.monotonic()`. Detection accepts both the bare
form (`PONG ...`) and the prefixed form (`:server PONG ...`) by checking tokens 0
and 1.

`IRCBot._keepalive()` sleeps 90 s, then compares `monotonic() - _last_pong` against
240 s. Past the threshold it logs `event=pong_timeout`, closes the writer, and
returns; the read loop then fails into the reconnect path. Otherwise it sends
`PING :<SERVER>` at priority 0. This detects a half-open TCP link roughly a minute
earlier than `_READ_TIMEOUT` would, and closing the writer is what converts a silent
half-open socket into an actionable error.

Note the asymmetry: the inbound-PING branch matches only `line.startswith("PING")`,
so a server that prefixes its PING (`:server PING ...`) would not be answered, while
a prefixed PONG is recognised. No known ircd sends a prefixed PING to a client;
recorded here as an actual-behavior limit, not a theoretical one.

---

## 8. ISUPPORT (005)

Only two tokens are consumed, both by `_handle_numeric()`: `CHANMODES` and `PREFIX`.
Everything else in the 005 line (`NETWORK`, `CHANTYPES`, `NICKLEN`, `TARGMAX`, ...)
is ignored. Channel-name shape is therefore validated against the hard-coded
`_CHAN_RE` (`^[#&+!][^\s,\x07]{1,49}$`) rather than against the advertised
`CHANTYPES`.

`protocol.py - parse_isupport_chanmodes()` splits `CHANMODES=A,B,C,D` into
`{mode_char: type}`:

| Type | Parameter rule | Typical modes |
|---|---|---|
| A | always takes one (list mode) | `b`, `e`, `I` |
| B | always takes one | `k`, `L` |
| C | takes one only when being set | `l`, `H` |
| D | never takes one | `i`, `m`, `n` |

`protocol.py - parse_isupport_prefix()` parses `PREFIX=(modes)symbols` into a mode
set plus a symbol-to-mode map.

### The None-on-malformed design

Both parsers return `None` on a structurally invalid token, and both callers keep
the existing table and log `event=isupport_malformed` rather than installing a
partial one. The distinction matters because an empty parse result is not the same
as a parse failure:

- `CHANMODES` validity is "at least four comma-separated groups", not "parsed to
  something non-empty". A truncated `CHANMODES=beI` yields a perfectly non-empty
  `{b:A, e:A, I:A}` while silently dropping `k -> B` and `l -> C`. With `k`
  untyped, `parse_mode_changes()` consumes no parameter for it, so
  `MODE #chan +ko sekrit nick` shifts every following parameter by one and **the
  channel key lands in the slot the operator nick should occupy** - the bot then
  records the key as a chanop. That is the incident the guard exists for. Individual
  empty groups stay legal, so `CHANMODES=,k,,imnpst` still parses.
- `PREFIX=()` is well-formed and means "this server advertises no membership
  prefixes". `(set(), {})` is the correct answer there, and `None` is the answer for
  a malformed token, so the caller can tell "replace with empty" from "keep what I
  have". An emptied `_prefix_modes` would make the op-mode set empty and silently end
  all MODE-driven chanop tracking.

`tests/run_tests.py` pins this: "a malformed ISUPPORT token never wipes the mode
tables".

Consistency note: the 005 branch is the only consumed line in `_handle_numeric()`
that does not `return True`, so a 005 falls through to `_handle_membership()` and
`_handle_privmsg()`. Harmless today because no later pattern can match a 005 line,
but it breaks the otherwise strict matched-means-consumed convention.

---

## 9. State tracking

All state is in-memory on the `IRCBot` instance. `active_channels` is mirrored to
`channels.json` on every change; per-channel user records go to the `Store`.

| Inbound | Effect on bot state |
|---|---|
| `353` NAMES | seed `_chanops[channel]` from prefixed entries |
| `JOIN` (self) | add to `active_channels`, persist |
| `JOIN` (other) | `Store.user_join(channel, nick, hostmask)` |
| `PART`/`KICK` (self) | drop channel, drop its `_chanops` set, persist |
| `PART`/`KICK` (other) | `Store.user_part`, discard from `_chanops` |
| `QUIT` | `Store.user_quit`, discard from every `_chanops`, drop cached hostmask, revoke admin session |
| `NICK` | rename in store, move `_chanops` entry, drop admin session |
| `CHGHOST` | refresh store record and `_nick_hosts` entry |
| `ACCOUNT` | audit log line, refresh store record via cached hostmask |
| `INVITE` | `JOIN` the channel, subject to a 5 s global cooldown |
| `473` | ask `[bot] services_nick` for an `INVITE` to that channel |
| `403`/`405`/`471`/`474`/`475`/`476` | drop the channel from saved state |

Identity rules worth stating plainly, because they are security behavior rather than
bookkeeping:

- A `NICK` change **drops** any admin session bound to the old nick; it is never
  migrated. Migrating would let a nick takeover launder an authenticated session
  onto an attacker-chosen nick.
- A `QUIT` drops both the cached hostmask and the admin session, so a later
  reconnector reusing the nick must re-authenticate.
- `is_admin()` re-checks that the nick's current hostmask still equals the one bound
  at auth time on **every** call, and revokes on divergence or on the `"unknown"`
  sentinel. A bare-nick `NICK` prefix stores `""` as the hostmask, which is
  fail-closed for this check.

The `NICK` regex treats the `user@host` half of the prefix as optional, per RFC 2812
(`nick [ [ "!" user ] "@" host ]`). Requiring the `!` previously dropped the
services-driven form `:Guest43341 NICK :Internets`, so the bot never learned its own
new nick, stopped recognising PMs addressed to it, and silently ignored every
prefix-less PM command. Both behaviors are now pinned by regression tests.

Account names from `ACCOUNT` (IRCv3 `account-notify`) are logged
(`event=account_change`) but **not persisted**; the handler's real work is refreshing
the store record and keeping the cached hostmask accurate, since admin auth is keyed
on hostmask. `extended-join` account data on a `JOIN` line is likewise parsed past
and discarded.

---

## 10. MODE parameter consumption

`protocol.py - parse_mode_changes(mode_str, args, prefix_modes, chanmode_types)`
walks the mode string once and returns `(adding, mode_char, param_or_None)` tuples.
A parameter is consumed when the character is a membership-prefix mode, or its
`CHANMODES` type is A or B, or its type is C **and** the change is additive.
Unknown characters consume nothing, which is exactly why a truncated `CHANMODES`
token is dangerous (section 8). Running past the end of `args` yields `None` rather
than raising.

Only channel MODE lines are processed (`_handle_numeric()` requires the target to
start with `#`, `&`, `+`, or `!`), and only op-granting modes update state:

```text
op_modes = {"o", "a", "q"} & self._prefix_modes
```

So owner (`q`), admin (`a`), and op (`o`) mark a nick as chanop; halfop (`h`) and
voice (`v`) do not. User MODE lines on the bot itself are not tracked.

### Known defect: NAMES and MODE disagree on what a chanop is

From [internals/protocol.md](internals/protocol.md#findings). The symbol map
returned by `parse_isupport_prefix()` is discarded by its only caller
(`self._prefix_modes, _ = parsed`), and `parse_names_entry()` instead hard-codes the
prefix set `~&@%+` with the op subset `~&@`. On a server whose `PREFIX` advertises
non-standard symbols, or maps `&` to something other than admin, NAMES-seeded chanop
state diverges from MODE-driven chanop state, which does honour the advertised
modes. Half the parsed ISUPPORT data is dead on arrival.

---

## 11. PRIVMSG, NOTICE, and CTCP

Commands are dispatched from `PRIVMSG` only. `NOTICE` is inspected for exactly one
thing - the NickServ identification signal in section 5 - and is otherwise ignored,
which is the correct posture: replying to a NOTICE is how bots get into loops.

`internets.py - IRCBot._handle_privmsg()` in order:

1. Match `:nick!user@host PRIVMSG <target> :<text>`; a non-match returns.
2. Count the message and record `_nick_hosts[nick] = hostmask` under `_auth_lock`.
3. Return immediately if the text begins with `\x01`. **No CTCP is answered at all** -
   not VERSION, not PING, not TIME, not FINGER. The bot sends CTCP ACTION via `.act`
   but never replies to one.
4. Decide PM versus channel by comparing the target to the current nick
   case-insensitively; `reply_to` becomes the nick for a PM, the channel otherwise.
5. Record the speaker in the channel's user set if the channel is tracked.
6. Extract the command word: in a channel the text must start with the live prefix;
   in a PM the prefix is optional when the bare first word is a known command.
7. Log the command with the argument credential-redacted (`.auth` / `.deauth`
   arguments are masked wholesale), then call `_dispatch()`.

Inbound logging is redacted before it is written: `internets.py - _redact_inbound()`
applies the shared verb list from `sender.py - redact_secrets()` to the trailing text
of a PRIVMSG or NOTICE only. Scoping to the trailing text stops a sender host like
`ident@host` from matching the `IDENT` verb, and the redaction is deliberately not
gated on the target matching the bot's nick, because the leak it closes happened
while nick tracking was broken.

The dispatch gates that run after this point - shadow-ban, PM-only auth, flood, the
per-channel burst gate, the 400-character argument cap, and the task cap - are
documented in [Command Reference](command-reference.md#dispatch-rules).

---

## 12. Inbound line handling and limits

`_process()` order of operations, per line:

1. `strip_tags()` removes any IRCv3 `@tag` block **first**, so every later match
   sees a clean line.
2. `PING` check, then `PONG` check; both return immediately.
3. Shadow-ban check on the prefix nick, which suppresses the module `on_raw` fanout
   only. Bot-internal handlers still run.
4. Module `on_raw()` fanout over a snapshot taken under `_mod_lock`; an exception in
   any module is logged at debug and cannot break line processing.
5. `_handle_cap()`, then `_handle_numeric()`, then `_handle_membership()`, then
   `_handle_privmsg()`. The first three return `True` to consume the line.

Limits:

| Limit | Value | Behavior on breach |
|---|---|---|
| Read buffer | 8192 bytes | count `oversized_lines`, drain to next newline, continue |
| Read inactivity | 300 s | raise `ConnectionResetError` into the reconnect path |
| PONG payload echo | 400 characters | truncated |
| Command argument | 400 characters | notice to the user, no dispatch |
| Concurrent command tasks | 50 | "bot is busy" notice, no dispatch |
| Command handler runtime | 60 s | timeout, counted, user notified |

Decoding is `utf-8` with `errors="replace"`, so malformed bytes cannot raise out of
the read loop. An empty line after stripping CR/LF is skipped.

---

## 13. Outbound path

All writes go through `sender.py - Sender`, never directly to the transport.

| Priority | Traffic | Rate limited? | Droppable? |
|---|---|---|---|
| 0 | PONG, CAP, NICK, PASS, AUTHENTICATE, QUIT, keepalive PING | no | no (evicts a priority-1 item to make room) |
| 1 | PRIVMSG, NOTICE, JOIN, MODE, everything user-visible | yes | yes, on a full queue |

Token bucket: capacity 5, refill one token per 1.5 s, so roughly 40 messages per
minute sustained with a burst of 5. Queue bound is 200 items; overflow drops
priority-1 traffic and bumps the drop counters that the shutdown summary reports.

`Sender._write_line()` applies two hard protections before the bytes leave:

- CR, LF, and NUL are stripped from every line, which is the transport-level defense
  against protocol injection through a command argument.
- The line is truncated to 510 bytes plus CRLF (RFC 2812 section 2.3), backing up
  over UTF-8 continuation bytes so a multi-byte character is never split.

Message bodies are additionally chunked at 400 bytes by
`internets.py - IRCBot._split_msg()` before they reach the queue, again on UTF-8
code-point boundaries.

Log redaction runs on the outbound side too (`redact_secrets()` inside
`_write_line()`), so a credential sent by the bot or injected through `.raw` is
masked in the debug log in both directions. Redaction is log-only; the wire always
gets the real bytes.

---

## 14. Reconnect and backoff

Any `ConnectionResetError`, `ConnectionAbortedError`, `BrokenPipeError`,
`ssl.SSLError`, or `OSError` in the read loop enters the reconnect arm, which:

1. Bumps `reconnects` and the Prometheus counter.
2. Evaluates the permanent-failure condition (section 5) and breaks out if it holds.
3. Cancels every background and command task, clears `_tasks`, stops the `Sender`.
4. Clears `_authed` and `_nick_hosts` under `_auth_lock`. Admin sessions never
   survive a disconnect.
5. Resets `identified` and `registered` so the full registration burst is re-sent.
6. Loops: wait, then `_connect()`; on failure increment and wait again.

Delay is `_backoff_jittered(attempt)`, wrapping the deterministic
`_backoff(attempt, base=15, cap=300) = min(15 * 2**attempt, 300)` with equal jitter
of plus or minus 25 percent, floored at zero:

| Attempt | Base delay | Actual range |
|---|---|---|
| 0 | 15 s | 11.25 - 18.75 s |
| 1 | 30 s | 22.5 - 37.5 s |
| 2 | 60 s | 45 - 75 s |
| 4 | 240 s | 180 - 300 s |
| 5+ | 300 s (cap) | 225 - 375 s |

`_backoff()` is kept deterministic so the schedule can be asserted exactly in tests;
jitter lives only in the wrapper. Decorrelated jitter was considered and rejected
because it compounds state across attempts. `attempt` resets to 0 at the start of
each reconnect episode, and the loop waits **before** its first attempt, so there is
always a delay of at least about 11 s after a drop.

Every wait is `asyncio.wait_for(self._stop.wait(), timeout=delay)`, so a shutdown
request during backoff is honoured immediately instead of after the delay.

---

## 15. Numerics and commands the bot acts on

Anything not listed here reaches the module `on_raw()` fanout and is otherwise
ignored. There is no generic numeric table and no unknown-numeric logging.

### Numerics

| Numeric | Name | Handling |
|---|---|---|
| 005 | RPL_ISUPPORT | parse `CHANMODES` and `PREFIX`; keep table on malformed token |
| 353 | RPL_NAMREPLY | seed `_chanops` for the channel |
| 376 | RPL_ENDOFMOTD | post-registration actions (section 6) |
| 381 | RPL_YOUREOPER | apply oper modes and snomask |
| 403 | ERR_NOSUCHCHANNEL | drop channel from saved state |
| 405 | ERR_TOOMANYCHANNELS | drop channel from saved state |
| 421 | ERR_UNKNOWNCOMMAND (for `CAP`) | clear `_cap_busy`; server has no CAP |
| 422 | ERR_NOMOTD | same as 376 |
| 433 | ERR_NICKNAMEINUSE | bump nick and resend `NICK` |
| 451 | ERR_NOTREGISTERED | send `CAP END`, clear `_cap_busy` |
| 471 | ERR_CHANNELISFULL | drop channel from saved state |
| 473 | ERR_INVITEONLYCHAN | ask services for an `INVITE` |
| 474 | ERR_BANNEDFROMCHAN | drop channel from saved state |
| 475 | ERR_BADCHANNELKEY | drop channel from saved state |
| 476 | ERR_BADCHANMASK | drop channel from saved state |
| 491 | ERR_NOOPERHOST | log oper failure |
| 900 | RPL_LOGGEDIN | mark identified |
| 902 | ERR_NICKLOCKED | SASL failure, transient |
| 903 | RPL_SASLSUCCESS | mark identified, `CAP END` |
| 904 | ERR_SASLFAIL | SASL failure, permanent |
| 905 | ERR_SASLTOOLONG | SASL failure, permanent |

Notably absent: 366 (end of NAMES), 401, 404, 432, 437, 465, and every WHOIS
numeric. `366` in particular is not used, so `_chanops` is seeded incrementally from
353 lines with no completion signal.

### Commands and non-numeric replies

| Command | Handling |
|---|---|
| `PING` | `PONG` with truncated payload, priority 0 |
| `PONG` | refresh `_last_pong` |
| `CAP` | `LS` / `ACK` / `NAK` / `NEW` (section 4) |
| `AUTHENTICATE +` | send the SASL PLAIN payload |
| `PRIVMSG` | command dispatch; CTCP ignored |
| `NOTICE` | NickServ identification detection only |
| `JOIN` | self versus other; channel or user tracking |
| `PART` | self versus other; chanop cleanup |
| `KICK` | same as `PART` |
| `QUIT` | store, chanops, hostmask, admin session |
| `NICK` | self-nick tracking, rename, session revocation |
| `MODE` | channel modes only; chanop updates |
| `INVITE` | auto-join with 5 s cooldown |
| `CHGHOST` | refresh cached hostmask and store record |
| `ACCOUNT` | log, refresh store record |

---

## 16. Known defects and gaps

Stated plainly rather than described as working. See
[known-issues.md](known-issues.md) for the full findings list.

- **Multiline `CAP LS 302` is mishandled** (section 4). The bot requests 302 and then
  cannot parse the continuation form; capability negotiation can terminate mid-list
  and lose SASL. Only single-line capability replies behave correctly.
- **`CAP ACK` replaces rather than unions `_caps`** (section 4), so `_caps` is not
  reliable session state and a second ACK can emit a stray `CAP END`.
- **NAMES-derived and MODE-derived chanop state can diverge** (section 10) because
  the ISUPPORT `PREFIX` symbol map is parsed and then discarded.
- **The 005 branch does not return `True`** (section 8), the one break in the
  matched-means-consumed convention.
- **No test coverage** for `_handle_cap()`, the shadow-ban filter, the
  keepalive/pong-timeout path, or the reconnect loop; no end-to-end test drives a
  line from `_process()` into a dispatched handler. Existing protocol coverage is
  real but partial: `_backoff`, `_redact_inbound`, `_split_msg`,
  `_handle_membership` and `_handle_numeric` units, ISUPPORT malformed-token
  behavior, and source-inspection checks. Note that despite its name,
  `tests/test_dispatcher.py` tests the weather-provider dispatcher, not bot command
  dispatch.
- **`request_shutdown()` before `run()`** sets `_shutdown_initiated` without an event
  to set, after which `_on_signal` ignores every subsequent signal as
  "shutdown_already_in_flight" while no shutdown is actually in flight. Nothing
  reaches this today; the console starts concurrently, which is the near miss.

---

## See also

- [Runtime Architecture](architecture.md) - process structure, dispatch pipeline,
  module loader.
- [Command Reference](command-reference.md) - the dispatch gates and every command.
- [Security Model](security-model.md) - trust boundaries, credential handling.
- [Configuration](configuration.md) - the `[irc]` and `[bot]` keys referenced above.
- [internals/internets.md](internals/internets.md),
  [internals/protocol.md](internals/protocol.md),
  [internals/sender.md](internals/sender.md) - line-level detail.
