# Output conventions

How the bot talks to a channel, and how to keep that readable on clients the
project cannot test against.

This matters more here than in most bots for three reasons. Replies are dense
and delimiter-heavy: a single `.weather` or `.element` line packs six or more
labelled fields into one IRC message. Several commands are deliberately
multi-line, and the sender's token bucket spreads those lines out over real
seconds. And `.alerts` carries National Weather Service warnings, where a
truncated or reordered line is a safety problem rather than a cosmetic one.

The mechanics of sanitizing a string live in
[writing-modules](writing-modules.md#9-output-sanitization-and-irc-line-length).
This page is the layer above: what the assembled line should look like, and why.

---

## 1. The hard limits

Four independent caps apply to every reply. They are not the same cap, they do
not compose the way they look like they do, and three of them are the number
400.

| Limit | Symbol | Unit | Effect |
|---|---|---|---|
| 512 | `sender.Sender._MAX_IRC_LINE` | bytes | whole IRC line, truncated |
| 400 | `internets.IRCBot._MAX_BODY` | bytes | body split into chunks |
| 400 | `modules.base.strip_ctrl` default | characters | string truncated |
| 400 | `internets.IRCBot._MAX_ARG_LEN` | characters | inbound arg refused |

`sender.Sender._write_line()` is the last stop. It removes `\r`, `\n`, and
`\x00`, encodes to UTF-8, and if the result exceeds 510 bytes it truncates to
510 and then walks backwards off any UTF-8 continuation byte so a multi-byte
character is never cut in half. Nothing warns; the line simply arrives short.

`internets.IRCBot._split_msg()` runs earlier and splits the body into 400-**byte**
chunks with the same continuation-byte guard, so a long reply becomes several
IRC lines rather than one truncated one. Byte, not character: a line of
degree signs, box drawing, or CJK reaches the chunk boundary two to three times
sooner than its character count suggests.

`modules.base.strip_ctrl()` truncates at 400 **characters** by default, and it
runs before either of the above. It also strips the full C0 range `\x00-\x1f`
plus `\x7f`, which includes `\x02` (bold), `\x03` (color), `\x0f` (reset),
`\x16` (reverse), `\x1b` (ESC), and `\x07` (BEL). That is what makes it the real
defense against a hostile upstream, and also what makes assembling formatting
before sanitizing a mistake (section 5).

`internets.IRCBot._dispatch()` refuses an inbound `arg` over 400 characters with
`input too long (max 400 chars).` before the handler runs, so any module
advertising a larger input is advertising something unreachable.

### The token bucket makes multi-line output slow

`sender.Sender` is a priority queue plus a token bucket: `CAPACITY = 5`,
`REFILL = 1.5` seconds per token. Protocol traffic is priority 0 and bypasses
the bucket. Everything a module emits, `PRIVMSG` and `NOTICE` alike, is
priority 1 and pays a token.

The arithmetic that matters when you choose a reply shape:

```
lines   time until the last line is written
    1   immediate
    5   immediate (burst capacity)
   10   ~7.5 s
   20   ~22.5 s
   44   ~58.5 s
```

44 is not hypothetical. `.help all` renders every registered command through
`admin_cmds._help_grid()` at four columns; with 165 primary module commands and
4 public core commands currently registered, that is 43 grid rows plus a header.
A user who types it gets five lines instantly and waits about a minute for the
rest, with no indication that more is coming. See
[design-decisions](design-decisions.md) ADR-012 for why the bucket exists and
must not be flattened.

The practical rule: **prefer one dense line to a burst of thin ones.** Where
multiple lines are genuinely right (a header plus one row per item), cap the
item count in the module rather than letting an upstream response size decide
how long the channel is occupied.

---

## 2. Where a reply goes

`internets.IRCBot` exposes four entry points. Which one you pick is a
user-visible convention, not an implementation detail.

| Call | In a channel | In a PM |
|---|---|---|
| `privmsg(target, msg)` | to `target` | to `target` |
| `notice(target, msg)` | `NOTICE` to `target` | `NOTICE` to `target` |
| `reply(nick, reply_to, msg)` | `PRIVMSG` to the channel | `PRIVMSG` to the nick |
| `preply(nick, reply_to, msg)` | `NOTICE` to the nick | `PRIVMSG` to the nick |

The convention the modules follow, counted across `modules/`:

- **Results go to the channel.** 352 `bot.privmsg(reply_to, ...)` call sites.
- **Refusals go privately as a NOTICE.** 93 `bot.notice(nick, ...)` call sites
  across 65 modules, overwhelmingly rate-limit messages and "not configured"
  errors. A refusal is noise to everyone except the person who typed it, and a
  `NOTICE` does not trigger most clients' highlight or logging behavior the way
  a `PRIVMSG` does.
- **Long administrative output uses `preply`.** `.help`, `.stats`, and the audit
  views in `admin_cmds.py` all use it, so a 44-line grid lands in the invoker's
  query window rather than the channel.

---

## 3. The formatting vocabulary actually in use

There is no single project-wide separator rule. Two are in active use and both
are correct within a module; what is not acceptable is mixing them inside one
reply.

| Separator | Modules | Typical use |
|---|---|---|
| ` \| ` | 31 | field separator, general purpose |
| ` :: ` | 13 | field separator, reference and calculation output |
| ` - ` | most | label-to-value or a qualifier inside one field |

`modules/weather.py - _format_hourly()` uses ` :: `. `modules/imdb.py` and
`modules/crypto.py` use ` | `. `modules/encode.py - _codepoint()` is a good
model of the dense form:

```
U+00E9 'e' :: LATIN SMALL LETTER E WITH ACUTE :: cat Ll :: UTF-8 c3 a9 :: Latin-1 Supplement
```

**Bold** (`\x02`) appears in 39 module files and is used for exactly one thing:
the identity of the record being reported, at the head of the line. Compare
`modules/imdb.py` (`\x02Title\x02 [Year] ...`), `modules/crypto.py`
(`\x02BTC\x02 $43,210.50 ...`), and `modules/reflookup.py`
(`\x02Helium\x02 (He) :: Z=2 ...`). Bold is never used for emphasis inside a
sentence, and never for a whole line.

**Color** (`\x03NN`) appears in six modules. In every one of them the color is
redundant with a word or a shape that carries the same meaning:

```python
# modules/steam.py - the label carries the state, the color repeats it
1: ("ONLINE", "\x0303"), 2: ("BUSY", "\x0304"),
# modules/stocks.py - the triangle carries direction, the color repeats it
arrow = "\x0303▲\x03" if change >= 0 else "\x0304▼\x03"
```

Keep it that way (section 4).

**Errors and usage hints are prefixed with the invoker's nick**, `f"{nick}: ..."`,
321 times across `modules/`. In a busy channel this is the only thing that ties
a refusal to the person who caused it. The forms in use:

```python
self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
self.bot.privmsg(reply_to, f"{nick}: {p}qr <text>")
self.bot.notice(nick, f"{nick}: input too long (max 400 chars).")
```

Note the second: the command prefix is read from config
(`self.bot.cfg["bot"]["command_prefix"]`) rather than hardcoded as `.`, because
`internets.IRCBot._cmd_prefix()` lets a `.rehash` change it at runtime.

**Help lines go through one helper.** `modules.base.help_row()` prepends the
live prefix, pads the usage column to 24 characters, and emits the two leading
spaces the `.help` renderer expects. 70 of the 71 modules that define
`help_lines()` use it; `modules/weather.py` is the one exception and uses a
local `row()` helper because it renders a category grid rather than a flat list.

**Aliases are written into the usage string with a leading dot and no spaces:**

```python
help_row(prefix, "bofh/.excuse", "Random BOFH excuse")
help_row(prefix, "dict/.dictionary <word> [/N]", "Dictionary definition")
```

`.help <cmd>` matches on the leading token, so the primary name must come first.
Core commands render their aliases the same way, joined with `/`, derived from
`AdminCommandsMixin._CORE` rather than hand-written.

---

## 4. Accessibility and client compatibility

The bot has no way to know what is rendering its output. Assume the worst
plausible reader: a text-mode client piping to a screen reader, an 80-column
terminal, a bouncer replaying scrollback, and a mobile client that reflows
lines. Each rule below follows from a constraint in section 1 rather than from
taste.

### Put the value that matters first

`sender.Sender._write_line()` truncates from the right and says nothing. A line
whose first field is the answer still informs after truncation; a line that
builds up to its conclusion does not.

`modules/weather.py - _format_alerts()` is the model:

```
[EXTREME] Tornado Warning - Take shelter now in a basement or interior room
```

Severity first, event second, headline last. Truncation costs the advisory text
and keeps the warning. The same function sorts by severity before applying its
five-alert cap, so a routine statement issued more recently cannot bury a
Tropical Storm Warning, and appends `... and N more` rather than letting five
alerts read as all of them.

### Never let color be the only carrier of meaning

Not every client renders `\x03`, several strip it, and a screen reader ignores
it entirely. Every color site in the codebase already pairs the color with a
word or a glyph. When adding one, pair it too. The failure mode is a green
number and a red number that read identically once the color is gone.

### Keep the field order fixed

A reader who has learned that `.weather` puts the temperature third can find it
without parsing. Ordering by whatever the upstream API returned first, or
omitting a field silently when it is null and shifting everything left, defeats
that. Prefer a stable order with an explicit absent marker over a compacted
line, unless the omission is the point.

### Do not decorate

`admin_cmds.py` headers are the counter-example the project should not extend:

```python
f"── \x02stats\x02 ─────────..."
```

That line carries 43 U+2500 box-drawing characters. A screen reader may announce
each one. Each costs three bytes of the 400-byte chunk budget, so 129 bytes of a
`.stats` header carry no information at all. The `.help`, `.audit`,
`.fingerprint`, and `.shadow-list` headers use a milder two-and-two bracket
(`── label ──`), which is the tolerable end of the same habit. A short label
does the same job for free:

```
stats:
```

### Prefer ASCII in emitted strings

The codebase is inconsistent here and the inconsistency is worth knowing about
before you copy a neighbour. Non-ASCII characters currently reach IRC output
from at least these places:

| Character | Where | Note |
|---|---|---|
| U+2013 en dash | `dice.py`, `hn.py`, `netcalc.py`, `encode.py` | inside numeric ranges |
| U+2192 arrow | `netcalc.py`, `secinfo.py`, `seen.py`, `translate.py` | mapping notation |
| U+00D7 multiply | `netcalc.py`, `numberfact.py` | `4 × /26` |
| U+2500 box drawing | `admin_cmds.py` | header rules on five commands |
| U+03BC, U+00B3, U+2082 | `weather.py` | `PM2.5 12.0μg/m³`, `O₃` |
| U+25B2 / U+25BC | `stocks.py` | paired with color |

ASCII substitutes exist for all of the first three: `1-100`, `->`, `x`. The
scientific units in `weather.py` are the defensible case, since `ug/m3` is
meaningfully less precise, and the triangles in `stocks.py` are the redundant
encoding that keeps color from being load-bearing. Everything else is
decoration that costs bytes and renders unpredictably. New code should default
to ASCII and justify an exception.

### Do not assume the reader saw the command

Bouncer replay, a client that hides its own sent lines, and a channel scrolling
past all break the assumption that a bare result is self-explanatory. This is
what the `f"{nick}: "` prefix on refusals buys, and why `.alerts` prints a
`:: <location> Alerts ::` header before its rows.

---

## 5. Verified pitfalls

:::{admonition} Known defect: `modules/bored.py` sanitizes away its own bold
:class: warning
`modules/bored.py - _fetch_sync()` builds the line with `\x02bored?\x02`, then
passes the assembled string through `_strip_ctrl()`. `strip_ctrl` removes the
full C0 range, so it deletes the two `\x02` bytes the function just added. The
reply is emitted unbolded and nothing reports it.

The rule is directional: **sanitize each untrusted field, then assemble.** Never
sanitize after adding your own control bytes. `modules/imdb.py` and
`modules/reflookup.py` show the correct order.
:::

:::{admonition} Known defect: `modules/qr.py` advertises an unreachable cap and truncates its own URL
:class: warning
`modules/qr.py` sets `_MAX_INPUT = 1000` and prints `(max 1000 chars)` in both
its usage hint and its help row. `internets.IRCBot._dispatch()` refuses any
`arg` over 400 characters first, so the 1000-character path is unreachable and
the advertised limit is wrong by a factor of 2.5.

The second half is worse. The handler percent-encodes the input into a URL and
emits it through `_strip_ctrl(url)`, which is left at the 400-character default.
The fixed URL prefix is 62 characters, and percent-encoding expands most
non-alphanumeric input threefold, so an input well under the dispatcher's 400
still produces a URL over 400 and is silently cut mid-query-string. The user
gets a link that loads a broken QR code.

When the string being emitted is a URL, a hash, or any other atom that is
useless when partial, pass an explicit `max_len` rather than relying on the
default.
:::

:::{admonition} Known defect: an error reply can publish an API key
:class: danger
`modules/stocks.py - _try_providers()` interpolates `str(exception)` for each
failed provider into the channel reply. A `requests` transport error embeds the
full request URL, and these providers carry credentials in the query string
(`token=`, `apikey=`). `sender.redact_secrets()` does not help: it applies to
log output, not to `PRIVMSG` bodies.

**Never interpolate an exception into a reply.** Emit `type(e).__name__` plus the
provider name. `weather_providers/pirateweather/_codes.py - safe_get_json()` is
the in-repo model. Full entry in [known-issues](known-issues.md).
:::

The general rule behind that last one: **an error message must never echo back
something the user supplied in confidence.** `.pwn` takes a password as its
argument. If its error path ever grows a "could not look up `<arg>`" message,
the password lands in the channel, in the bot's log, and in every bouncer
replaying it.

---

## 6. Private versus channel

Some replies disclose data about a person. The rule is that the disclosure
target is the subject, not the channel.

| Command | Symbol | Routing |
|---|---|---|
| `.privacy` | `modules/privacy.py - PrivacyModule.cmd_privacy()` | PM-gated, replies to `nick` |
| `.pwn` | `modules/secinfo.py - SecinfoModule.cmd_pwn()` | PM-only, refuses in channel |
| `.seen` | `modules/seen.py - SeenModule.cmd_seen()` | replies to `reply_to` |

`.privacy` is gated by `_require_pm()` and then addresses every one of its lines
to `nick` directly rather than to `reply_to`, so it cannot leak even if the gate
is bypassed. It discloses the caller's saved location, current hostmask,
per-channel first-seen and last-seen timestamps, and opt-out status: only ever
about the invoker, never a third party.

`.pwn` refuses outright when `reply_to != nick`, with a message that explains
why rather than just declining:

```python
self.bot.notice(nick, f"{nick}: please PM me that command - "
                      "never type a password in a channel")
```

That is the right shape for a private-only command. The refusal is a `NOTICE` to
the nick, so the channel does not see that a password was typed either.

`.seen` is the deliberate exception: it answers in the channel, because "when
did X last speak" is a channel-scoped question. It is not unguarded. It checks
`store.is_opted_out(target)` first and answers `never seen <target>` for anyone
opted out, which is indistinguishable from a genuine miss.

The rule for a new command: **if the reply contains data about someone who did
not type the command, either it needs a channel-scoped justification like
`.seen` has, or it belongs in a PM.** Consult
[security-model](security-model.md) before deciding it is the former.

---

## 7. Checklist for a module author

Read alongside [writing-modules](writing-modules.md), which covers the
sanitization mechanics this list assumes.

- [ ] Every upstream or user-derived field passes through
      `modules.base.strip_ctrl()` **before** the line is assembled, not after.
- [ ] Any string that must survive intact (URL, hash, identifier) gets an
      explicit `max_len`, not the 400-character default.
- [ ] No exception object is interpolated into a reply. `type(e).__name__` only.
- [ ] The reply is one line unless multiple lines are genuinely the right shape,
      and if multiple, the item count is capped in the module.
- [ ] The most important value is first in the line, so a truncated line still
      answers the question.
- [ ] Field order is fixed and does not shift when a field is absent.
- [ ] One separator throughout the reply: ` | ` or ` :: `, not both.
- [ ] Bold, if used, marks the record identity at the head of the line only.
- [ ] Color, if used, repeats meaning that a word or glyph already carries.
- [ ] Emitted strings are ASCII, or the exception is justified.
- [ ] Refusals are `bot.notice(nick, f"{nick}: ...")`; results are
      `bot.privmsg(reply_to, ...)`.
- [ ] The command prefix comes from `cfg["bot"]["command_prefix"]`, never a
      literal `.`.
- [ ] `help_lines()` returns `modules.base.help_row()` output, with aliases
      written as `primary/.alias` and the primary name first.
- [ ] If the reply discloses data about anyone other than the invoker, the
      routing decision is deliberate and documented in the module docstring.
- [ ] Nothing the user typed in confidence can appear in an error path.

---

## Related reading

- [writing-modules](writing-modules.md) - the module contract, and section 9 for
  sanitization mechanics.
- [command-reference](command-reference.md) - the generated command inventory.
- [design-decisions](design-decisions.md) - ADR-012 on the priority queue and
  token bucket.
- [security-model](security-model.md) - what counts as disclosure.
- [known-issues](known-issues.md) - the defect register, including the three
  named above.
- [internals/sender](internals/sender.md) - line-level detail on the send path.
