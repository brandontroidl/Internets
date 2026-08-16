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

Four caps are in play. They are not the same cap, they do not compose the way
they look like they do, three of them are the number 400, and - the part that
gets missed - **only two of them apply to every reply.**

| Limit | Symbol | Unit | Direction | Applies |
|---|---|---|---|---|
| 512 | `sender.Sender._MAX_IRC_LINE` | bytes | outbound | always, on every line |
| 400 | `internets.IRCBot._MAX_BODY` | bytes | outbound | always, on every body |
| 400 | `modules.base.strip_ctrl` default | characters | outbound | **only when a module calls it** |
| 400 | `internets.IRCBot._MAX_ARG_LEN` | characters | **inbound** | on the command argument, never on a reply |

The last two are the ones to be careful with. `_MAX_ARG_LEN` bounds what a user
may send *in*; it constrains reply length only indirectly, by limiting one
possible source of it. And `strip_ctrl()` is a function a module chooses to
call, not a filter the send path applies: 66 of the 75 files in `modules/`
reference it, and the rest emit whatever they assembled.

Most of the nine are harmless - `units.py` emits nothing, `dice.py` and
`bofh.py` emit only self-generated text, `privacy.py`, `channels.py` and
`health.py` emit values the bot itself owns. **`modules/twitch.py` is not.** It
imports `BotModule`, `fetch_json` and `help_row` from `.base` and no sanitizer
at all, then interpolates upstream-controlled `display_name`, `game_name` and
`title` fields straight into bolded reply lines
(`modules/twitch.py - TwitchModule.cmd_twitch()` and its siblings). A hostile
or compromised upstream response is emitted with its control bytes intact. Do
not read "four caps apply" as meaning the send path will catch that for you.

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

`internets.IRCBot._dispatch()` refuses an inbound `arg` over `_MAX_ARG_LEN`
characters with `input too long (max 400 chars).` before the handler runs, so
any module advertising a larger input is advertising something unreachable.
That message is emitted by the core dispatcher through its own
`self.notice(...)`, not by any module; `modules/translate.py` has a
similar-looking line of its own against a different, module-local cap.

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
   42   ~55.5 s
   44   ~58.5 s
```

The rule is `(lines - CAPACITY) x REFILL`: the first 5 are free, the rest are
metered at 1.5 s each.

That range is not hypothetical. `.help all` renders every **loaded** command
through `admin_cmds._help_grid()` at four columns. On the shipped
`config.ini.example` autoload that is 162 names for a non-admin, 41 grid rows
plus a header, so 42 lines and about 56 seconds; counting every file in
`modules/` instead gives 169 names, 43 rows and 44 lines. The full derivation,
including the admin figures, is in
[performance](performance.md#23-what-a-large-reply-does-to-the-budget). Either
way a user who types it gets five lines instantly and waits about a minute for
the rest, with no indication that more is coming. See
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

The convention the modules follow, counted by walking the AST of every file in
`modules/` for calls on the bot object:

- **Results go to the channel.** 395 `bot.privmsg(...)` call sites, 376 of them
  targeting `reply_to`.
- **Refusals go privately as a NOTICE.** 96 `bot.notice(...)` call sites across
  65 files, and **every one of them targets `nick`** rather than `reply_to`,
  which is the convention holding without exception. A refusal is noise to
  everyone except the person who typed it, and a `NOTICE` does not trigger most
  clients' highlight or logging behavior the way a `PRIVMSG` does.
- **Long administrative output uses `preply`.** `.help`, `.stats`, and the audit
  views in `admin_cmds.py` all use it, so a 42-line grid lands in the invoker's
  query window rather than the channel.

The notice population is far less varied than its size suggests: **79 of the 96
are the single rate-limit line** `f"{nick}: slow down - try again in a few
seconds"`, leaving 17 that say anything else (PM-only refusals, `admin only`,
and the privacy opt-in/opt-out confirmations). Read that as one convention
applied 79 times, not as 96 distinct refusal messages.

**No notice says "not configured".** That refusal shape does not exist on the
notice path at all. An unconfigured module is instead hidden from `.help` by
`modules/base.py - BotModule.is_configured()`, and the few modules that report
the condition explicitly do it to the channel - `modules/lastfm.py` uses
`bot.privmsg(reply_to, "Last.fm API key not configured - see [lastfm] in
config.ini")`. Dispatch still works for an unconfigured module, so an admin can
`.load` it and add the key later.

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
`modules/crypto.py` use ` | `. `modules/encode.py - _unicode()` is a good
model of the dense form:

```
U+00E9 'é' :: LATIN SMALL LETTER E WITH ACUTE :: cat Ll :: UTF-8 C3 A9 :: Latin-1 Supplement
```

Note the two details that are easy to get wrong when quoting this: the glyph
column shows the **actual character** (`shown = ch` unless its category is `C`
or `Z`, in which case a middle dot stands in), and the UTF-8 bytes are
**uppercase**, from `ch.encode("utf-8").hex(" ").upper()`.

**Bold** (`\x02`) is referenced in 38 files under `modules/` and emitted from
36 of them (counted by walking each file's AST for a non-docstring string
literal containing `\x02`; a raw text search overcounts by two, which are
comments).

It is used for **two** things, not one, and both are established:

1. **The identity of the record**, at the head of the line. `modules/imdb.py`
   (`\x02Title\x02 [Year] ...`), `modules/crypto.py`
   (`\x02BTC\x02 $43,210.50 ...`), `modules/reflookup.py`
   (`\x02Helium\x02 (He) :: Z=2 ...`).
2. **Field labels, mid-line, throughout the reply.** This is at least as common
   as the first. `modules/imdb.py` - the same line quoted above - continues
   `\x02Rating\x02 8.8/10 | \x02Genre\x02 ... | \x02Director\x02 ...`, and the
   pattern repeats in `lastfm`, `ipinfo`, `twitch`, `steam`, `idlerpg`, `poke`
   and `youtube`.

There is also one clear use for emphasis inside a sentence:
`modules/idlerpg.py` emits `player 'x' not found (\x02note:\x02 names are case
sensitive)`. And `modules/youtube.py` combines bold with color in a single
token (`\x0303\x02[+]\x02\x03 {likes} likes`).

So treat what follows as **guidance for new code**, not a description of the
existing corpus: pick one of the two roles per reply and hold it, prefer the
record-identity form for a single-record lookup and the field-label form for a
dense multi-field line, and do not mix both in one message. Bolding a whole
line is the one thing nothing in the codebase does; keep it that way.

**Color** (`\x03NN`) is referenced in 9 files and emitted from **seven**:
`crypto`, `idlerpg`, `linktitle`, `steam`, `stocks`, `twitch`, `youtube`. In
every one of them the color is redundant with a word or a shape that carries
the same meaning:

```python
# modules/steam.py - the label carries the state, the color repeats it
1: ("ONLINE", "\x0303"), 2: ("BUSY", "\x0304"),
# modules/stocks.py - the triangle carries direction, the color repeats it
arrow = "\x0303▲\x03" if change >= 0 else "\x0304▼\x03"
```

Keep it that way (section 4).

**Errors and usage hints are prefixed with the invoker's nick**, `f"{nick}: ..."`,
320 times across `modules/`. In a busy channel this is the only thing that ties
a refusal to the person who caused it. The forms in use:

```python
# modules/*: the rate-limit refusal, 79 identical sites
self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
# modules/qr.py: a usage hint, to the channel
self.bot.privmsg(reply_to, f"{nick}: {p}qr <text>")
# internets.py - IRCBot._dispatch(): core, not a module
self.notice(nick, f"{nick}: input too long (max {self._MAX_ARG_LEN} chars).")
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
before you copy a neighbour. The inventory below was rebuilt by walking each
file's AST and looking only at **non-docstring string literals**, so it counts
characters that can reach output rather than every occurrence in the source:

| Character | Files | Note |
|---|---|---|
| U+2192 rightwards arrow | `admin_cmds.py`, `base.py`, `netcalc.py`, `privacy.py`, `secinfo.py`, `seen.py`, `translate.py`, `weather.py` | mapping notation; 8 files, the widest-spread of any |
| U+2013 en dash | `dice.py`, `dnsutils.py`, `encode.py`, `hn.py`, `netcalc.py` | inside numeric ranges |
| U+00B0 degree | `geocode.py`, `iss.py`, `satpass.py`, `weather.py` | coordinates and temperatures |
| U+2026 ellipsis | `numberfact.py`, `pkginfo.py`, `satpass.py` | truncation marker |
| U+00D7 multiply | `netcalc.py`, `numberfact.py` | `4 × /26` |
| U+00B3 superscript 3 | `numberfact.py`, `weather.py` | `m³` and cube notation |
| U+2500 box drawing | `admin_cmds.py` | header rules on five commands |
| U+03BC mu, U+2082, U+2083 | `weather.py` | `μg/m³`, `O₃`, `NO₂` |
| U+25B2 / U+25BC | `stocks.py` | paired with color |
| U+2190 / U+2191 / U+2193 | `seen.py`, `crypto.py` | direction markers, paired with color in `crypto` |
| U+00B7 middle dot | `encode.py` | substitute glyph for a control or space codepoint |
| U+00B5 micro sign | `physcalc.py` | `µs` |
| U+2605 black star | `ghinfo.py` | repository star count |
| U+23F0 alarm clock | `remind.py` | emoji, in the reminder-fired reply |

Three things in that table are worth calling out.

**U+03BC and U+00B5 are different codepoints for the same thing.**
`weather.py` uses GREEK SMALL LETTER MU and `physcalc.py` uses MICRO SIGN. They
render identically and compare unequal. If either is kept, the codebase should
pick one.

**U+23F0 is an emoji**, in `modules/remind.py - RemindModule` where a fired
reminder is announced. It is the only emoji in emitted output and it is the
clearest case for replacement.

**Two non-ASCII uses look like output and are not**, so a grep-based inventory
will wrongly include them: `modules/geocode.py` accepts `°`, `º`, `′` and `″`
inside `_COORD_DMS_RE` (an *input* pattern for degree-minute-second
coordinates), and `modules/calc.py` uses the noncharacter U+FDD0 as an internal
tokenizing sentinel that is substituted back out before the reply is built.
Neither reaches the wire, and neither should be "fixed".

ASCII substitutes exist for most of the list: `1-100`, `->`, `x`, `...`, `deg`,
`*`. The scientific units in `weather.py` are the defensible case, since `ug/m3`
is meaningfully less precise, and the triangles in `stocks.py` are the redundant
encoding that keeps color from being load-bearing. Everything else is
decoration that costs bytes (each of these is 2 to 4 UTF-8 bytes against the
400-byte chunk budget) and renders unpredictably. New code should default to
ASCII and justify an exception.

### Do not assume the reader saw the command

Bouncer replay, a client that hides its own sent lines, and a channel scrolling
past all break the assumption that a bare result is self-explanatory. This is
what the `f"{nick}: "` prefix on refusals buys, and why `.alerts` prints a
`:: <location> Alerts ::` header before its rows.

---

## 5. Verified pitfalls

:::{admonition} Defect (not in the register): `modules/bored.py` sanitizes away its own bold
:class: warning
Unlike the two below, this one has **no entry in
[known-issues](known-issues.md)**; it was found while writing this page. It is
cosmetic, which is presumably why, but the ordering mistake behind it is not.

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
- [ ] Bold, if used, plays one role for the whole reply: record identity at the
      head, or field labels throughout. Not both, and never a whole line.
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
- [known-issues](known-issues.md) - the defect register. It carries the
  `modules/qr.py` and `modules/stocks.py` entries named above; the
  `modules/bored.py` one is not in it.
- [internals/sender](internals/sender.md) - line-level detail on the send path.
