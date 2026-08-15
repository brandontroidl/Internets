# protocol.py - pure IRC parsing and serialization primitives

## Purpose

Stateless IRC protocol helpers extracted from `internets.py` so the bot class stays
focused on orchestration and state. Every function is a pure transform: no bot state, no
I/O, no logging, no imports beyond `base64` and `re`. The nontrivial value of the file is
in its *failure contracts*: the ISUPPORT parsers return `None` for structurally invalid
tokens specifically so the caller can keep its current table instead of replacing it with
a partial one that silently corrupts MODE parameter alignment.

## Responsibilities / boundaries

Belongs here:

- IRCv3 tag stripping (`strip_tags()`).
- ISUPPORT (005) token parsing: `CHANMODES` (`parse_isupport_chanmodes()`) and
  `PREFIX` (`parse_isupport_prefix()`).
- Channel MODE string parsing with correct parameter consumption
  (`parse_mode_changes()`).
- NAMES (353) entry parsing (`parse_names_entry()`).
- SASL PLAIN payload construction (`sasl_plain_payload()`).

Deliberately not here:

- Which regex extracts the token from the raw 005/353/MODE line - that lives in
  `internets.py` (`_RE_CHANMODES`, `_RE_PREFIX`, `_RE_353`, `_RE_MODE`); these functions
  receive the already-extracted token/argument strings.
- What to do with a `None` result (keep-current-table policy) - caller's decision,
  implemented in `internets.py - IRCBot._handle_numeric()`.
- Default CHANMODES/PREFIX tables used before a 005 arrives
  (`internets.py - _DEFAULT_CHANMODE_TYPES` / `_DEFAULT_PREFIX_MODES`).
- Outbound line formatting and byte limits (`sender.py - Sender._write_line()`).

## Dependencies and dependents

- Dependencies: stdlib `base64`, `re` only. No project imports, so the module is safely
  importable from anywhere (including tests) with zero side effects.
- Dependents: `internets.py` (all six functions; the only production caller) and
  `tests/test_protocol.py`.

## Lifecycle

Imported once by `internets.py` at startup. Functions are called per inbound line during
`IRCBot._process()` / `IRCBot._handle_numeric()`; `sasl_plain_payload()` is called once
per SASL negotiation (`internets.py:921`). No initialization, no teardown.

## State

None. Every function is pure; all state produced (mode tables, chanop sets) is owned by
the caller.

## Concurrency

No shared state, so the functions are trivially thread-safe. All production calls happen
on the event-loop thread inside the inbound line handler.

## Failure behavior

- `parse_isupport_chanmodes()` and `parse_isupport_prefix()` return `None` on malformed
  input rather than raising or returning a partial result; `IRCBot._handle_numeric()`
  logs `event=isupport_malformed` and keeps the current table
  (`internets.py:961-976`).
- `parse_mode_changes()` never raises on short argument lists: `take_param()` returns
  `None` once `args` is exhausted (a server sending fewer parameters than the mode
  string implies yields `param=None` for the tail).
- `parse_names_entry()` on an all-prefix entry (e.g. `"@@"`) returns the original entry
  with `is_op=False` rather than an empty nick.
- `sasl_plain_payload()` raises only on non-UTF-8-encodable input, which `str` cannot
  produce; effectively infallible.

## Security

- `sasl_plain_payload()` handles the NickServ password. It is a pure transform with no
  logging - consistent with the repo rule that secret helpers have no side effects. The
  caller gates the send on TLS (`internets.py - IRCBot._tls_or_refuse()`) and the
  resulting `AUTHENTICATE` line is redacted from logs by `sender.py - redact_secrets()`.
- The `None`-on-malformed contract of the ISUPPORT parsers is itself a security-adjacent
  control: the CHANMODES docstring documents the concrete attack surface - a truncated
  `beI` token would leave `k` untyped, so `MODE #c +ko sekrit nick` consumes no
  parameter for `k` and the channel key lands where the operator nick belongs,
  corrupting chanop tracking (which gates admin-visible state). Pinned by
  `tests/test_protocol.py - TestParseIsupportChanmodes.test_truncated_token_rejected`.
- No input reaches the filesystem, network, or shell.

## Classes

None.

## Functions and methods

### `strip_tags(line) -> str`

Removes an IRCv3 message-tags block: if the line starts with `@`, everything up to and
including the first space is dropped. Edge cases: an empty line passes through; a line
that is *only* a tag block (no space) partitions to `""`. Does not parse or preserve tag
contents - the bot negotiates `server-time` but only needs the tags gone so `PING`/`PONG`
and command matching still work (`internets.py - IRCBot._process()`, which cites tagged
PING going unanswered as the motivating failure). Called first on every inbound line.

### `parse_isupport_chanmodes(token) -> dict[str, str] | None`

Parses `CHANMODES=A,B,C,D` into `{mode_char: "A"|"B"|"C"|"D"}`.

- Structural validity = at least four comma-separated groups (`token.count(",") < 3`
  rejects), NOT non-emptiness; the docstring explains why a non-empty parse of a
  truncated token is the dangerous case.
- More than four groups: extras beyond index 3 are silently ignored (the loop iterates
  labels A-D only). This matches the ISUPPORT spec's "future extensions may add groups"
  posture.
- Individual empty groups are legal (`",k,,imnpst"`), pinned by
  `TestParseIsupportChanmodes.test_empty_groups_are_legal`.
- Duplicate mode chars across groups: last group wins (dict overwrite); servers do not
  send this.

### `parse_isupport_prefix(token) -> tuple[set[str], dict[str, str]] | None`

Parses `PREFIX=(modes)symbols` into `(mode_set, {symbol: mode})` via
`re.match(r"\(([^)]*)\)(.*)", token)`.

- Returns `None` for a token with no parenthesized group (`"garbage"`).
- `PREFIX=()` is *valid* and returns `(set(), {})` - a server advertising no membership
  prefixes; the docstring stresses that the caller must distinguish this from `None`
  (replace vs keep). Pinned by
  `TestParseIsupportPrefix.test_empty_advertisement_is_valid_not_malformed`.
- Mismatched lengths (more modes than symbols or vice versa) are tolerated: the symbol
  map zips only `min(len(modes), len(symbols))` pairs; the mode set still contains all
  modes.
- Note: the only production caller keeps just the mode set and discards the symbol map
  (`internets.py:976`, `self._prefix_modes, _ = parsed`) - see Findings.

### `parse_mode_changes(mode_str, args, prefix_modes, chanmode_types) -> list[tuple[bool, str, str | None]]`

Parses a channel MODE change into `(adding, mode_char, param_or_None)` tuples,
consuming parameters positionally from `args` according to type:

| Mode class | Consumes a parameter |
|---|---|
| in `prefix_modes` (o/v/h/a/q...) | always (set and unset) |
| type A (list: b, e, I) | always |
| type B (always-param: k, L) | always |
| type C (param-on-set: l, H) | only when adding |
| type D (never: i, m, n) | never |
| unknown (not in either table) | never |

`+`/`-` flip the `adding` state and consume nothing. Parameter alignment is the whole
point: `tests/test_protocol.py - TestParseModeChanges.test_loq_desync_fix` pins the real
incident (`+Loq` on a server where `L` is type B) where a missing `L->B` entry shifted
every following parameter and corrupted chanop tracking. Exhausted `args` yield
`param=None` (no exception). The caller applies only prefix-mode changes with a non-None
param to the chanop set (`internets.py - IRCBot._handle_numeric()`,
`internets.py:1008-1020`).

The "unknown -> no parameter" fallback means an unadvertised parameterized mode would
still shift subsequent parameters; the defense is the default tables plus the
keep-current-table policy on malformed 005, not this function.

### `parse_names_entry(entry) -> tuple[str, bool]`

Strips leading membership prefix symbols from a NAMES entry using the hard-coded set
`~&@%+` and reports `is_op` when the prefix contains `~` (owner), `&` (admin), or `@`
(op). Voice (`+`) and halfop (`%`) do not count as op
(`TestParseNamesEntry.test_voice_not_op`, `test_halfop_not_op`). `lstrip` handles
multi-prefix (`~&@nick`). All-prefix entry returns `(entry, False)` as a guard. Caller:
353 handling in `IRCBot._handle_numeric()` (`internets.py:999-1007`).

### `sasl_plain_payload(nick, password) -> str`

Builds the SASL PLAIN initial response: base64 of `\0<nick>\0<password>` - an empty
authzid, `nick` as authcid, per RFC 4616. UTF-8 encoded before base64
(`TestSaslPlainPayload.test_unicode`). Caller: `internets.py:921` inside the
`AUTHENTICATE +` handler.

## Implementation walk

- `protocol.py:1-11` (docstring + imports): states the no-state/no-I/O charter;
  compatibility (`from __future__ import annotations`).
- `protocol.py:14-18` (`strip_tags`): protocol processing; single partition.
- `protocol.py:21-49` (`parse_isupport_chanmodes`): validation (group count), then
  protocol processing (group-to-type mapping). The long docstring documents the
  incident-derived reason for the structural check.
- `protocol.py:52-68` (`parse_isupport_prefix`): validation (regex), protocol
  processing (set + zip map). Docstring documents the `None` vs `(set(), {})`
  distinction.
- `protocol.py:71-106` (`parse_mode_changes`): protocol processing; a small state
  machine (`adding` flag, `arg_idx` cursor) with the type table driving parameter
  consumption. `take_param()` centralizes the exhaustion guard.
- `protocol.py:109-119` (`parse_names_entry`): protocol processing; prefix strip +
  op-set intersection, with the empty-nick guard.
- `protocol.py:122-125` (`sasl_plain_payload`): security/protocol formatting; encode
  then base64.

Every line of the file is accounted for above; there is no dead code.

## Findings

- questionable | protocol.py - parse_isupport_prefix() | The symbol map it builds is
  discarded by the only production caller (`internets.py:976` unpacks it into `_`), and
  `parse_names_entry()` instead hard-codes `~&@%+` with a hard-coded op subset `~&@`;
  on a server whose PREFIX advertises non-standard symbols (or maps `&` to something
  other than admin), NAMES-based chanop tracking diverges from MODE-based tracking,
  which does honor the advertised modes. Half the parsed data is dead on arrival.
- questionable | protocol.py - parse_names_entry() | Hard-coded prefix and op symbol
  sets duplicate knowledge that PREFIX parsing already provides (same root cause as the
  finding above; fixing one fixes both).
- test-gap | protocol.py - parse_mode_changes() | No test covers argument exhaustion
  (mode string implying more parameters than `args` supplies); the `take_param()` guard
  returning `None` past the end is unexercised.
- test-gap | protocol.py - strip_tags() | The tag-only line (`"@tags"` with no space,
  which returns `""`) is untested.
