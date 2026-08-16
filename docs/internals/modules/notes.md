# notes.py - per-nick personal sticky notes

## Purpose

`NotesModule` gives each nick a private list of short text notes with add /
list / show / delete / clear subcommands under a single `.notes` command. Base
contract: [base](base.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.notes` | `modules/notes.py - NotesModule.cmd_notes()` | `.notes <list\|add\|del\|show\|clear> [args]` |

Subcommands (dispatched inside `cmd_notes`; `del` aliases `delete`/`rm`):

- `list` - `_do_list()`: one header line + one line per note (`#N (3d ago) text`).
- `add <text>` - `_do_add()`: appends; reports truncation when input exceeded
  200 chars.
- `del <N>` - `_do_del()`: 1-based delete; echoes the deleted text; note
  numbers shift down after a delete.
- `show <N>` - `_do_show()`: full text of one note.
- `clear` - `_do_clear()`: two-step confirm; the second `.notes clear` must
  arrive within 60 s (`_CLEAR_WINDOW`, tracked in `self._clear_pending`).

Limits: 20 notes per nick (`_MAX_NOTES`), 200 chars per note (`_MAX_LEN`).

## Integration

None. No external HTTP. `is_configured()` returns `True`.

## State and persistence

- Store file: `notes.json` (override: `[notes] file`), shape
  `{"<nick_lower>": [{"text": str, "ts": epoch}, ...]}`.
- `NotesModule._save_notes()` takes `self._lock`, then writes atomically
  (`mkstemp` + `chmod 0o600` + `os.replace`, unlink-on-error). Mutating
  subcommands trigger a save via `asyncio.to_thread` from `cmd_notes`.
- Retention: none - notes live until deleted by the owner (documented in the
  module docstring).
- Privacy: free-text user content keyed by nick, 0600, local only.
  `NotesModule.forget()` (the `.forgetme` hook, see [privacy](privacy.md))
  pops the nick's whole list, then saves (deliberately after releasing the
  lock, since `_save_notes` re-acquires it - the comment says so).

## Failure behavior

Unreadable/malformed store: warn, start empty (load keeps only list-valued
keys and dict entries with non-empty `text`). Save failure: warning only;
memory stays authoritative.

## Security notes

- Note text is `base.strip_ctrl`-sanitized at add time (`_do_add`), and
  non-numeric arguments echoed in error messages are stripped too
  (`_do_del` / `_do_show`).
- `.notes` output goes to `reply_to`; invoking `.notes list` in a channel
  prints your notes into that channel - the module does not force PM. That is
  the caller's choice, but worth knowing.
- Rate-limited via `bot.rate_limited()`.

## Findings

- questionable | notes.py - NotesModule._do_add() / _do_del() / _do_clear() |
  the subcommand handlers mutate `self._notes` WITHOUT holding `self._lock`,
  while `_save_notes()` iterates the dict under the lock in a worker thread
  (`asyncio.to_thread`); a second user's command mutating during an in-flight
  save can make `json.dump` fail mid-iteration (RuntimeError), losing that
  save until the next change. Contrast tell.py, which mutates under its lock.
- questionable | notes.py - NotesModule._clear_pending | pending-clear
  timestamps are only removed on a successful clear or an empty-notes check;
  an abandoned confirm stays in the dict forever (unbounded only in theory -
  one float per nick that ever half-cleared).
- questionable | notes.py - module-level `_strip_ctrl()` | a one-line wrapper
  around `base.strip_ctrl` that only changes the default max length; callers
  could pass `_MAX_LEN` explicitly.
- test-gap | notes.py - NotesModule | no `tests/test_notes*` exists; the
  two-step clear window and index-shift semantics of `del` are untested.
