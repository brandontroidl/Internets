# claude.py - Claude chat over the Anthropic Messages API

## Purpose

`ClaudeModule` lets people talk to the bot in natural language. A channel line
that starts with the bot's nick (`Internets: what's a quasar?`) gets an answer
from Claude; `.claude` / `.ask` does the same as a command and works in PM. On
top of that sit short per-conversation memory, saved facts, personas, a
channel backlog used for recaps, optional unprompted chime-ins, replies to
`/me` emotes, web search, and a moderation pair for channel ops. Base contract:
[base](base.md).

Three files:

| File | Holds |
|---|---|
| `modules/claude.py` | the IRC surface, the prompts, and the API call |
| `modules/_claude_state.py` | `ClaudeState` (persisted) and `Conversations` (memory only) |
| `modules/_claude_intent.py` | pure parsing: addressing, emotes, locally answered requests |

The two helpers have a leading underscore, so `.load` refuses them and the
`.help` gate skips them.

## Commands

| Command | Handler | Who |
|---|---|---|
| `.claude <message>` (`.ask`) | `modules/claude.py - ClaudeModule.cmd_claude()` | anyone |
| `.claudereset [channel]` | `modules/claude.py - ClaudeModule.cmd_reset()` | anyone; `channel` needs op or admin |
| `.ai [on\|off]` | `modules/claude.py - ClaudeModule.cmd_ai()` | op or admin, in the channel |
| `.talkback [on\|off]` | `modules/claude.py - ClaudeModule.cmd_talkback()` | op or admin, in the channel |
| `.scheck [#chan] [nick]` | `modules/claude.py - ClaudeModule.cmd_scheck()` | op or admin; naming a channel needs admin |
| `.skick <nick> <#chan>` | `modules/claude.py - ClaudeModule.cmd_skick()` | op in that channel, or admin |
| `.skban <nick> <#chan>` | `modules/claude.py - ClaudeModule.cmd_skban()` | op in that channel, or admin |
| `.aitell <#chan> <directive>` | `modules/claude.py - ClaudeModule.cmd_aitell()` | admin |

"Op" is `bot.is_chanop()` for the channel in question; "admin" is
`bot.is_admin()`.

## The raw-line path

Most traffic never reaches a command. `modules/claude.py - ClaudeModule.on_raw()`
parses each `PRIVMSG` and, in order:

1. Drops the bot's own lines and `[claude] banned_nicks` entirely. Shadow-banned
   senders never arrive: the core skips the module fanout for them.
2. Records the sender's host (bounded LRU of 2000) for `.skban`.
3. Stops for PMs, for `[claude] blocked_channels`, for channels where `.ai off`
   was set, and for lines starting with the command prefix.
4. Appends the line to the channel backlog unless the sender has opted out.
5. Stops for `[claude] ignored_nicks` and when no client is configured.
6. Routes a CTCP `ACTION` or a `* nick hugs Bot` line aimed at the bot to an
   emote reply, an addressed line to a conversation, and anything else to the
   chime-in check.

Emote and addressed lines pass `bot.flood_limited()` first, because they never
go through the dispatcher and would otherwise have no flood gate. Each reply
runs as a module task (`BotModule.spawn`) under a 55 s `wait_for`, so it is
cancelled on unload and cannot outlive the 60 s budget that commands get.

Addressing is decided by `modules/_claude_intent.py - addressed_text()`.
`[claude] intent_check = heuristic` (the default) needs the nick at the start
of the line, so talk *about* the bot is ignored; `off` answers any mention.

## Conversations

`modules/claude.py - ClaudeModule._converse()` handles an addressed line or a
`.claude` argument.

**Answered locally** (no API call, no API cooldown), from
`modules/_claude_intent.py - local_intent()`:

| Say | Effect |
|---|---|
| `remember <fact>` | saves a fact, 5 to 300 characters, at most 50 per nick; "remember when/how/that..." is conversation |
| `forget <text>` / `forget everything` | removes the best fuzzy match, or all your facts |
| `what do you remember about me` / `... about <nick>` | lists saved facts |
| `reply as a pirate`, `from now on be ...` | persona: channel-wide from an op or admin, otherwise yours only |
| `reply to me as ...` | your persona in this channel |
| `speak to <nick> like ...` | someone else's persona; op or admin only |
| `... for everyone` / `... in this channel` | forces channel scope; refused for non-ops |
| `reset personality`, `stop acting`, `be yourself` | clears yours; an op also clears the channel's |
| `I'm in EST`, `set my timezone to Europe/Paris` | saves a zone, resolved by `devtools._resolve_zone()` plus eastern/central/mountain/pacific |
| `I prefer 12hr` / `use 24 hour` | clock format |
| `what time is it` (8 words or fewer) | the time in your zone, or UTC with a hint |

A timezone phrase that does not resolve ("I'm in trouble") falls through to
Claude. An opted-out user cannot save facts.

**Sent to Claude** otherwise, after `bot.rate_limited()`. The request carries:

- a system prompt: IRC style (plain text, short, says it is a bot if sincerely
  asked), the current UTC time, an operator line (below), `[claude]
  system_prompt`, the channel and per-user personas, and the speaker's saved
  facts;
- the speaker's recent turns in this channel from `Conversations` (12 messages,
  expiring after 30 idle minutes, 500 conversations at most);
- the new line as `<nick> text`.

**Operator authority.** When `bot.is_admin()` holds for the speaker (logged in
with `AUTH` from the hostmask bound at login), the system prompt names them the
authenticated operator whose instructions override personas and other users.
Every other speaker gets a line saying that only a speaker the prompt names
has that authority, so "I am the operator" from an unauthenticated nick, or a
nick that merely matches the operator's, carries nothing.

**Doctrine.** With `[claude] doctrine_file` set,
`modules/claude.py - ClaudeModule._doctrine_text()` reads the file and
expands its imports the way Claude Code expands `CLAUDE.md`: a line that is
only `@path` is replaced by that file (`modules/claude.py -
_expand_doctrine()`), resolved against the importing file's directory with `~`
expanded, outside fenced code blocks, at most 5 levels deep. So
`doctrine_file = ~/.claude/CLAUDE.md` sends the files that one imports, not
the bare import lines. The expanded text is capped at 256 KB and rebuilt when
any file it drew on changes mtime; each rebuild logs the file count and size.
It `ClaudeModule._call()` sends it as
the first system block with `cache_control`, ahead of the per-request text,
so the time, personas, and facts never break its cache prefix. This applies to
every call the module makes, `.scheck` and `.aitell` included. A configured
file, or any file it imports, that is missing or unreadable, an import cycle
or nesting past 5 levels, or an empty or over-cap result makes every call
answer `doctrine file unreadable - check [claude] doctrine_file` without
reaching the API: the module fails closed rather than answering unbound.

Recap phrasing ("catch me up", "tldr", `^^`, "what do you think", see
`modules/_claude_intent.py - is_review()`) adds the last 60 backlog lines to the
system prompt, with a 30 s per-nick cooldown. The reply goes to the channel as
`nick: text`, after a 1.5 to 4 s delay, and is appended to the backlog. When
`nick: text` would take more than `[claude] max_channel_lines` IRC lines
(default 2, counted at the core's 400-byte split, `_LINE_BYTES`), the reply goes
to the asker by PM instead, nothing is posted in the channel, and the reply
stays out of the backlog; it still enters the asker's conversation history.
`.aitell`, chime-ins (200 characters), and emote replies (300) are not routed
this way.

History is stored as text only. Thinking blocks are never replayed: the system
prompt changes between requests (facts, personas, time), and the API rejects a
replayed thinking block whose prompt prefix changed, while a text-only history
is never checked.

## Chime-ins, emotes, recaps

`modules/claude.py - ClaudeModule._should_chime()` fires only where
`.talkback on` was set (off by default), never on a short greeting aimed at
someone, at most once per 200 s per channel, only once the backlog has five
lines, and then with a 5% chance per line (15% on laughter). The model gets the
last 20 backlog lines and may answer `SKIP`, in which case nothing is posted.

An emote aimed at the bot (`pets`, `hugs`, `pokes`, `slaps`, `waves`, and the
rest of `_EMOTE_VERBS`) is answered with a CTCP `ACTION`, subject to the API
cooldown.

## Moderation

`.scheck` sends the last 100 backlog lines (or one nick's) to Claude with a
moderation prompt that forbids medical or psychiatric diagnosis, and PMs the
result to the requester. When a nick was named, the result ends with ready
`.skick` / `.skban` commands. `.skban` sets `+b *!*@host` from the host last
seen for that nick, or `nick!*@*` if none was seen, then kicks. Both write an
audit record (`claude_skick` / `claude_skban`, with channel and target) through
the core's `IRCBot._audit()`, for ops as well as admins, as `.aitell` does
(`claude_aitell`, with the directive and the posted text). Nick and channel arguments are validated against IRC grammar before
anything is sent, so neither can carry a second command.

## The API call

`modules/claude.py - _complete_sync()` runs in a worker thread:

- `client.beta.messages.create` with `model` from `[claude] model` (default
  `claude-opus-5-5`), `max_tokens` 4096, `output_config.effort = "low"`, and
  server-side refusal fallback (`fallbacks: "default"`, beta
  `server-side-fallback-2026-07-01`).
- `web_search_20260209` with `max_uses: 3` when `[claude] web_search` is true.
  Cited URLs are collected from the text blocks' citations; only `http(s)` URLs
  survive, and they are appended as `sources:` only when the user asked for
  links.
- Replies are folded to one line, stripped with `strip_ctrl`, and capped at
  1200 characters (three IRC lines); a truncated reply ends with `[cut off]`.

Budget: one attempt of 40 s (`max_retries=0`) plus at most 4 s of typing delay
stays under the 55 s task bound and the 60 s command timeout; a test asserts
the sum. At most 3 calls run at once and 6 wait; the next one is told the bot
is busy rather than queued.

| Failure | Reply |
|---|---|
| 401 | `API key rejected - check anthropic_api_key` |
| 404 | `model not found - check [claude] model` |
| 429 | `rate limited upstream - try again later` |
| timeout | `request timed out` |
| connection | `cannot reach the API` |
| other status or exception | `request failed` |
| `stop_reason: refusal` | `declined to answer that` |

Only the exception class and status code are logged; exception text never
reaches the log or the channel.

## State

`modules/_claude_state.py - ClaudeState` persists to `[claude] file`
(`claude.json`), written by the module-store pattern (snapshot under the lock,
`mkstemp`, mode 0600, `os.replace`) every 60 s and on unload:

| Key | Contents | Bound |
|---|---|---|
| `facts` | nick to saved facts | 50 per nick, 300 characters each |
| `prefs` | nick to `tz` and `clock` | - |
| `personas` | channel to channel persona and per-nick personas | 200 characters each |
| `switches` | channel to `ai` / `talkback` | - |
| `backlog` | channel to `[ts, nick, text]` rows | 100 lines, 48 hours, 400 characters per line |

A corrupt or wrong-shaped file loads as empty and is overwritten by the next
save, the same as every module store (no integrity envelope).

`modules/claude.py - ClaudeModule.forget()` erases a nick's facts, preferences,
personas, backlog lines, conversations, and recorded host, then saves at once.

## Configuration

`[claude]` in `config.ini.example`: `model`, `system_prompt`,
`doctrine_file`, `max_channel_lines`, `blocked_channels`, `ignored_nicks`, `banned_nicks`, `intent_check`,
`web_search`, `file`. The key is `cred(cfg, "anthropic_api_key", "claude",
"api_key")`, so `INTERNETS_ANTHROPIC_API_KEY` or `[secrets] anthropic_api_key`.
Without the key or without the `anthropic` package the module stays silent and
`is_configured()` is False; `.claude` then says what is missing.

## Security notes

- Everything sent to IRC passes `strip_ctrl`; emote replies re-add only the
  CTCP wrapper.
- Personas and facts are user text placed in the system prompt, labelled as
  user requests. Anyone can steer the bot's style for themselves; only ops can
  steer it for the channel or for another nick. The model has no tools besides
  web search, so a steered persona can change what it says but not what the
  bot does. Operator authority is the same: it changes what the model is told
  to prioritise, not what the bot can do.
- The doctrine file goes to the API on every request, and channel users can
  ask the bot to repeat its instructions. The prompt tells the model not to
  recite it, which is a deterrent, not a control: put nothing in that file you
  would not want posted to the channel.
- The bot's own nick is read from the private `bot._nick` at use time, because
  there is no public accessor and it changes on NICK.

## Findings

- questionable | `modules/claude.py - ClaudeModule._converse()` | the dispatcher
  logs every `.claude` argument, so questions asked by command reach
  `internets.log`; addressed lines do not pass the dispatcher and are not logged.
- questionable | `modules/claude.py - ClaudeModule.on_raw()` | PM free text is
  not answered; in PM the bot answers `.claude` only, because the dispatcher
  already claims PM lines whose first word is a command.
