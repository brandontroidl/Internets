# Documentation governance

How this documentation set stays true after the day it was written.

The 2026-08 reconstruction found the same failure repeatedly: a hand-maintained
list and a line-number citation both go wrong silently. A list of eleven
commands stays a list of eleven commands after a twelfth is added, and reads as
authoritative. A citation to `internets.py:412` still resolves after an edit
above it; the line exists, it just says something else now. Neither fails a
build, a test, or a review that is not specifically looking for it.

The rules below exist to make that class of drift either impossible or loud.
They are not aspirational: three of them are already enforced by a script, and
this page says plainly which are not.

---

## 1. Rules in force

**Source is authoritative.** Where this documentation and the code disagree, the
code is right and the documentation is a defect. Documentation describes what the
code does, not what it was meant to do. Where the behavior is a defect, the doc
says so and links to [known-issues](known-issues.md) rather than describing the
intent as if it worked.

**Citations are symbol-based.** The form is `` `internets.py - IRCBot._dispatch()` ``,
not `` `internets.py:618` ``. A symbol citation survives every edit that does not
rename the symbol, and when it does break, `scripts/verify-doc-citations.py`
resolves it against the file's AST and fails. A line citation survives nothing
and can only be checked for range sanity. As of 2026-08-16 the corpus holds 1097
citations: 1093 symbol-verified, 4 line citations remaining, 0 failures.

**Any list that can be generated is generated.** The command inventory in
[command-reference](command-reference.md) comes from
`scripts/gen-command-reference.py`, which walks `modules/` and reads
`AdminCommandsMixin._CORE` rather than trusting prose. Hand-editing it is a
regression even when the edit is correct, because the next generated run will
not agree with it.

**A hand-maintained enumeration that cannot be generated gets a completeness
gate.** `tests/run_tests.py` carries one: it enumerates the security-relevant
modules and asserts each references `strip_ctrl`. That is the pattern to copy
when a list must stay in sync with code and no generator is practical.

---

## 2. Tooling

Four scripts. None of them runs automatically (section 7).

| Script | What it does | Run it |
|---|---|---|
| `gen-command-reference.py` | regenerates the command inventory | after any command change |
| `verify-doc-citations.py` | AST-resolves every symbol citation | before every commit touching docs or cited source |
| `remap-doc-citations.py` | renumbers surviving line citations | only after a source edit, with `--apply` second |
| `build-docs.sh` | builds HTML and PDF | before a release, and after structural doc changes |

### `scripts/gen-command-reference.py`

Instantiates every `BotModule` subclass without running `__init__`, folds
aliases into their primary command, and emits the Markdown table. Two modes:

```bash
scripts/gen-command-reference.py                              # emit Markdown
scripts/gen-command-reference.py --check docs/command-reference.md
```

`--check` is the drift gate: it exits 1 listing every registered command whose
name does not appear anywhere in the file. It proves presence, not correctness
of the surrounding description, so a command whose behavior changed passes the
gate while its prose is wrong. Run it from the repository root; it inserts the
repo root on `sys.path` and sets `sys.argv` because `config.py` parses argv at
import time.

### `scripts/verify-doc-citations.py`

Walks `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, and every `.md` under
`docs/` except `_build` and `autoapi`. For a symbol citation it parses the cited
file's AST and confirms the symbol exists as a module-level function, a class, a
method, a class attribute, or a `self.x` assignment inside a method. Basenames
are deliberately ambiguous in this repo (`base.py` exists twice, `__init__.py`
many times), so it collects every candidate path and accepts a symbol found in
any of them, ranked by path affinity to the citing document.

```bash
scripts/verify-doc-citations.py             # full report, exit 1 on failure
scripts/verify-doc-citations.py --summary   # counts only
```

Line citations are reported as REVIEW, never PASS, and only checked for
existence and range. That asymmetry is the point: the tool cannot content-verify
a line citation, so it refuses to bless one.

### `scripts/remap-doc-citations.py`

A mechanical helper for the four line citations that remain, and for any that a
future edit strands. It diffs a source file at a git ref against the working
tree with `difflib`, builds an exact old-to-new line map, and rewrites the
citations. Report first, apply second:

```bash
scripts/remap-doc-citations.py HEAD internets.py admin_cmds.py
scripts/remap-doc-citations.py HEAD internets.py admin_cmds.py --apply
```

Lines deleted outright are reported UNMAPPABLE and left for a human, which
usually means the surrounding prose needs rewriting rather than renumbering.
This tool makes a citation arithmetically correct and nothing more. `difflib`
can mis-anchor when a block moves and is edited in the same commit, and an
off-by-one still resolves to a real line, so spot-check by content afterwards.
It is not a substitute for `verify-doc-citations.py`, and neither is a
substitute for converting the citation to symbol form.

### `scripts/build-docs.sh`

```bash
scripts/build-docs.sh          # HTML and PDF
scripts/build-docs.sh html     # HTML only
scripts/build-docs.sh pdf      # PDF only
```

Both outputs are required. A change that renders in HTML can overflow the
printed page, most often a table wider than four columns or a code line over
about 90 characters.

:::{admonition} The PDF build hides its own layout warnings
:class: warning
`build_pdf()` runs three `xelatex` passes as
`xelatex -interaction=nonstopmode "$TEXNAME" >/dev/null 2>&1 || true`. Every
warning goes to `/dev/null` and the non-zero exit is swallowed. The script's
only failure check is whether a PDF file exists afterwards, so a PDF full of
text running off the right margin is reported as a clean build.

Overfull boxes must be measured from the log directly:

```bash
grep -c 'Overfull \\hbox' docs/_build/latex/internets.log
grep 'Overfull \\hbox' docs/_build/latex/internets.log | head -20
```

As of 2026-08-16 the log reports 323 overfull horizontal boxes, the worst about
96pt (roughly 1.3 inches) past the margin. Treat the count as a budget: record
it, and if it rises after your change, find what you added that is too wide.
:::

---

## 3. What to update when you change X

This is the section that saves the most time, and the one most worth reading
before an edit rather than after. Each row is a change whose obvious edit is
incomplete and whose incompleteness does not fail loudly. It extends the
coordinated-update points in [handoff](handoff.md#changes-that-require-coordinated-updates)
with the documentation side of each.

| Change | Code artifacts | Doc artifacts |
|---|---|---|
| Add a core command | `_CORE`, `_CORE_PUBLIC` | command-reference (generated), administration |
| Add a module | `COMMANDS`, `_MODULE_GROUPS`, autoload, `forget()` | command-reference (generated), modules |
| Add a provider | 5 code sites, see below | providers, writing-providers, configuration |
| Add a config key | `config.ini.example` | configuration, deployment |
| Add a secret | `KNOWN_SECRETS`, `CONFIG_LOCATIONS`, `config.ini.example` | configuration, security-model, handoff |
| Change a state schema | `store._SCHEMA_VERSION` | state-and-persistence, operations |
| Add a metric | `MetricsRegistry._register_defaults()` | metrics-and-observability |

The detail behind each row follows.

### Add a core command

Code: `admin_cmds.py - AdminCommandsMixin._CORE` maps the word to the method
name. `AdminCommandsMixin._CORE_PUBLIC` decides whether it works
unauthenticated; anything absent from that frozenset is derived as admin-only by
`_core_admin_cmds()`, so the help output and the admin gate both follow
automatically once the set is right.

Docs: regenerate [command-reference](command-reference.md) and run
`gen-command-reference.py --check` against it. The counts printed at the bottom
of that file are generated, so do not hand-edit them. If the command is
operator-facing, [administration](administration.md) needs a prose entry too;
that one has no gate.

### Add a command module

Code: `COMMANDS` values must name `async def` methods on the class.
`modules.base.BotModule.__init_subclass__` validates this at class-definition
time, so a typo or a synchronous handler raises `TypeError` at import rather
than at first use. Add the module to `[bot] autoload` if it should load at
startup. Implement `is_configured()` returning `False` when a required key is
absent, so the module hides from `.help` cleanly. Override `forget(nick)` if it
persists anything per-nick, because `.forgetme` reaches modules only through
that hook.

The easily-missed one: `admin_cmds._MODULE_GROUPS` is a hand-maintained mapping
of module name to `.help` category. A module absent from it still appears,
collected into the ungrouped leftover line at the end, so this degrades visibly
rather than silently. It is still wrong.

Docs: regenerate [command-reference](command-reference.md); add the module to
[modules](modules.md); follow [output-conventions](output-conventions.md) for
the reply shape.

### Add a weather provider

Five code sites, per [handoff](handoff.md#adding-a-weather-provider): the
package under `weather_providers/<id>/` with `get_*` capability methods, the
factory registration in `weather_providers/__init__.py`,
`secret_store.KNOWN_SECRETS` and `secret_store.CONFIG_LOCATIONS`,
`weather_providers/_dispatch.py - DEFAULT_RELIABILITY` for every capability the
provider answers, and `config.ini.example`. Four of the five fail quietly: a
misspelled method name means the capability simply does not exist, and a
capability missing from the reliability table gets rank 99 and sorts last.

Docs: [providers](providers.md) for the per-provider entry,
[writing-providers](writing-providers.md) if the contract changed, and
[configuration](configuration.md) for the new keys.

### Add a config key

Code: add it to `config.ini.example` in the right section. Anything read through
`cfg[...].get(...)` with a default works without a schema change, which is what
makes this one easy to under-document.

Docs: [configuration](configuration.md) is the key-by-key reference and is the
one artifact that must change. [deployment](deployment.md) needs it too if the
key affects a deployment decision. `config.ini.example` is a shipped artifact
and part of the documentation surface, not just a template. Note the standing
caveat recorded in [handoff](handoff.md): the example file already cannot
configure much of what the code reads, so the gap is pre-existing and growing.

### Add a secret

Code: `secret_store.KNOWN_SECRETS` (a name absent from it is invisible to
`secret_store list`), `secret_store.CONFIG_LOCATIONS` (a name absent from it is
invisible to `migrate`), and the `[secrets]` block of `config.ini.example`.

Docs: [configuration](configuration.md) for the key,
[security-model](security-model.md) if it changes the trust surface, and the
secrets inventory in [handoff](handoff.md). Never record a value, only a name
and a location.

### Change a state file schema

Code: `store.py - _SCHEMA_VERSION`, currently 2. `store._unwrap()` rejects an
unknown version with `_StoreRejected` rather than best-effort parsing it, so a
bump without a migration path makes existing state unreadable and quarantined.
Additive fields do not need a bump; a bump is for a change that requires
transforming stored data.

Docs: [state-and-persistence](state-and-persistence.md) for the shape,
[operations](operations.md) for the backup and recovery consequences.

### Add a metric

Code: `metrics.py - MetricsRegistry._register_defaults()`, so the attribute
exists on the shared registry and call sites can do
`registry.<name>.inc(labels={...})` without a lookup.

Docs: [metrics-and-observability](metrics-and-observability.md) carries two
hand-maintained tables, one of metric names and one of emission sites. Both need
the new metric. Neither is generated and neither is gated, which makes this the
most drift-prone row in this section.

### Edit a source file that documentation cites

Symbol citations survive. For the four remaining line citations, editing the
file above one invalidates it silently. Run `verify-doc-citations.py` first to
see whether any point into the file you are about to change, and use
`remap-doc-citations.py` only if they do. Better: convert them to symbol form
and delete the problem.

---

## 4. ADR lifecycle

[design-decisions](design-decisions.md) holds numbered architectural decision
records, ADR-001 through ADR-016. An ADR captures a decision that a future
engineer would otherwise re-litigate or accidentally revert: one that is
non-obvious, that cost something, or whose obvious cleanup reintroduces a
failure the project already had.

Existing entries carry six fields: Decision, Context, Rationale, Alternatives,
Tradeoffs, Constraints. Constraints is the load-bearing one, because it states
what a future change must not break.

### Status

The existing sixteen entries carry no explicit status line; all of them are
accepted and current. Going forward, a new or changed entry declares one:

| Status | Meaning |
|---|---|
| proposed | written, not yet implemented; the code does not match it yet |
| accepted | implemented and current; the code matches |
| superseded by ADR-NNN | no longer governs; kept for the reasoning it records |

### Supersession

An ADR is never deleted and never edited into its own replacement. The reason a
decision was made is exactly what a future engineer needs when the replacement
looks wrong. Supersession is two edits:

1. The old entry's status becomes `superseded by ADR-NNN`. Its body is left
   alone.
2. The new entry's Context explains what changed since the old one, and its
   Alternatives section names the superseded approach explicitly, so the
   question does not get re-opened from scratch a third time.

Numbers are never reused.

### Machine-checkable decisions

A decision that lives only in prose gets violated and then distrusted. Where a
decision can be checked mechanically, it gets an enforcing test in the same
change that adds the ADR, and the ADR names the test.

The pattern already exists in-repo. `tests/run_tests.py` enumerates the
security-relevant modules and asserts each references `strip_ctrl`; that is the
sanitization decision made executable. The gate proves only that a call site
exists in the file, not that the string you emitted passed through it, and the
test comment says so. An honest partial gate that states its own limit is
correct governance; a gate that overstates what it proves is worse than none.

Where a decision genuinely cannot be checked (ADR-001's rule that no accessor
reachable from a worker may become a coroutine, for instance), the ADR says so
in Constraints, and that absence is itself information: it tells a reviewer this
one depends on them.

---

## 5. Known-issues lifecycle

[known-issues](known-issues.md) is the defect register. It is not a backlog and
not a wish list.

### What enters it

A defect enters when it changes runtime behavior and has been **verified against
source**, meaning reproduced or confirmed by reading the code, not inferred from
a comment. Each entry carries:

- the symbol, in citation form
- what actually happens, in the present tense
- how it was verified
- the shape a fix would take, naming an in-repo model where one exists
- severity, judged on user impact rather than on how hard the fix is

Style matters here in a specific way: an entry describes behavior, never blame,
and never asserts a cause it did not verify. If the mechanism is unclear, the
entry says the mechanism is unclear.

### How an entry leaves

An entry is removed only when a regression test exists that **fails without the
fix and passes with it**, exercising the real production path. A fix without
that test does not close the entry; it changes the entry to record that the code
was changed and the test is still owed.

The reason is the register's own history. Its closing section records that no
end-to-end dispatch test exists, that 44 of the 75 files under `modules/` have
no behavioral test at all, and that
`tests/test_physcalc.py - TestRc.test_five_band` asserts the output of a
miscalculated resistor decode, locking a defect in as expected behavior. A test
that was never seen failing has unknown power, and one written to match current
output has negative power. A test written after the fix, which is the common
case here, recovers its proof value by mutation: break the invariant in the
implementation, watch exactly that test go red, restore, re-run green.

### Risk acceptance

Deciding not to fix something is the maintainer's call, and it is a legitimate
outcome. It is recorded **in the entry**, not by deleting the entry:

```markdown
**Accepted risk (2026-08-16):** not fixed. The exposure requires an
authenticated admin session, and the fix would change the audit record
format. Revisit if the audit format changes for another reason.
```

Deleting an accepted-risk entry destroys the only record that the risk was
considered, and guarantees the next reader rediscovers it and re-does the
analysis. The register is allowed to contain things that will never be fixed.

---

## 6. Periodic review

None of this survives without someone looking. The intervals below are what the
artifacts actually decay at, not a ritual.

| Frequency | Check |
|---|---|
| every commit touching docs | `verify-doc-citations.py` exits 0 |
| every commit adding a command | `gen-command-reference.py --check` exits 0 |
| every release | full `build-docs.sh`, plus the overfull-box count |
| quarterly | the reconciliation pass below |

The quarterly pass is the one that catches what the per-commit checks cannot,
because a curated document cannot reveal its own omissions:

- **Known-issues against reality.** Walk each open entry and re-verify it still
  reproduces. A fixed-but-not-closed entry sends the next reader chasing a
  defect that no longer exists, which costs as much trust as a missed one.
- **ADR constraints against the code.** For each ADR's Constraints section, check
  the constraint still holds. This is where a silent revert shows up.
- **Generated artifacts against their generators.** Re-run
  `gen-command-reference.py` and diff against the committed file, rather than
  trusting that `--check` passing means the file is current. `--check` only
  proves presence.
- **The overfull-box budget.** Compare the current count against the last
  recorded one.
- **Hand-maintained enumerations.** The two tables in
  [metrics-and-observability](metrics-and-observability.md),
  `admin_cmds._MODULE_GROUPS`, `weather_providers/_dispatch.py - DEFAULT_RELIABILITY`,
  and the secrets inventory in [handoff](handoff.md). None is generated and none
  is gated.
- **`config.ini.example` against what the code reads.** The known gap here is
  recorded in [handoff](handoff.md) and grows by default.

---

## 7. What is not enforced

Stated plainly, because a governance page that implies more enforcement than
exists is itself a drift hazard.

`.github/workflows/` contains `tests.yml`, `security.yml`, and `codeql.yml`.
None of them runs `gen-command-reference.py --check`, `verify-doc-citations.py`,
or `build-docs.sh`. There is no `Makefile` and no `.pre-commit-config.yaml`. All
four scripts in section 2 are manual, and every rule in section 1 is currently
maintained by whoever remembers to run them.

The lowest-effort change that would move the most: add
`gen-command-reference.py --check docs/command-reference.md` and
`verify-doc-citations.py` as two steps in the existing `tests.yml` `lint` job.
Both are fast, both exit non-zero on failure, and both are currently green, so
neither would land red. Until that happens, treat section 6 as the real
mechanism.

---

## Related reading

- [handoff](handoff.md) - the coordinated-update points this page extends, and
  the open-defect list.
- [knowledge-recovery](knowledge-recovery.md) - how the corpus was reconstructed
  and why content checking beats renumbering.
- [testing](testing.md) - the two suites, and the conventions a regression test
  must follow.
- [design-decisions](design-decisions.md) - the ADR set itself.
- [known-issues](known-issues.md) - the defect register itself.
- [output-conventions](output-conventions.md) - the sibling rules for what the
  bot emits.
