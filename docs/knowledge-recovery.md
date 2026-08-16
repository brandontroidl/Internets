# Knowledge recovery

A method, not a fact sheet. It answers: the original context is gone, so how do
you rebuild a trustworthy picture of this project without guessing, and how do
you tell a claim you can rely on from one you cannot.

Use it when a document and the code disagree, when a number in prose looks
stale, when you need to know why something is the way it is, or when you are
about to assert something about this system that somebody else will act on.

The whole method reduces to one habit: derive from source, verify
programmatically where a script can decide it, and treat every prose document
including this one as evidence one step removed.

## The authority order

When two sources disagree, the higher one wins. This ordering is not a
preference; it reflects how each source can go wrong.

| Rank | Source | How it goes wrong |
| --- | --- | --- |
| 1 | The source code | Cannot be stale relative to itself. Can still be wrong about its own intent. |
| 2 | The test suite | Encodes intended behavior and past incidents. Can pin a defect (see below). |
| 3 | Config parsing code | Authoritative for what a key does. `config.ini.example` is a template and drifts from it. |
| 4 | `docs/internals/` | Written from full source reads, symbol-cited, with findings sections. One step removed. |
| 5 | The guide-level docs | Correct at the abstraction they describe. Furthest from the code. |

Two qualifications that matter more than the ordering itself.

**A test can be wrong in a specific, dangerous way.** A test written after the
implementation asserts what the code does, not what it should do. This repo has a
confirmed instance: `tests/test_physcalc.py - test_five_band` asserts the value
produced by a defective resistor-code calculation in
`modules/physcalc.py - _rc_from_bands()`. The test is green and the behavior is
wrong. So a passing test proves the code and the test agree, not that either is
correct. When a test looks like it merely restates the implementation, treat it
as a change detector and go to the source.

**A docstring is prose and gets the prose ranking, not the source ranking.**
Several docstrings in this repo describe behavior the code does not have: the
`secret_store` module docstring claims encryption at rest where the
implementation is plaintext plus 0600 permissions, `audit_log`'s claims
append-binary mode where `record()` opens text mode, and `hashpw`'s
`_FAST_HASH_THRESHOLD_S` comment describes an automatic cost backoff that was
never implemented. Read the body, not the docstring.

## Regenerating the command inventory

Never hand-count commands. `scripts/gen-command-reference.py` walks `modules/`,
imports each file, instantiates each `BotModule` subclass without running
`__init__` (the same technique `tests/test_help.py` uses, so no network, keys, or
config are needed), folds aliases into their primary command, and reads the core
set from `AdminCommandsMixin._CORE` and `_CORE_PUBLIC`.

```
scripts/gen-command-reference.py
```

The last line is the count:

```
Primary module commands: 165. Core public: 4. Core admin: 23.
```

It has a drift gate. This is the check to wire into any process that touches
commands:

```
scripts/gen-command-reference.py --check docs/command-reference.md
```

It exits 1 and names every registered command absent from the document. Note
what it does and does not prove: it proves no command is missing from the doc. It
does not prove the doc has no invented commands, and it does not check
descriptions.

Two subtleties in the script that explain apparent count mismatches. Its `_SKIP`
set excludes `__init__`, `base`, `geocode`, and `units` by name; `_netsafe` is
excluded implicitly because it defines no `BotModule` subclass. And the count of
modules yielded is not the count of modules with commands, because `linktitle`
defines a loadable module that registers zero commands and runs entirely from the
raw-line fanout.

## Verifying counts programmatically

Every count in this documentation set should be reproducible by a command. These
are the ones the current numbers came from, with the answers as of 2026-08-15.

Module files, and the module and command counts:

```
ls modules/*.py | wc -l                      # 75
scripts/gen-command-reference.py | tail -1   # 165 / 4 / 23
```

Weather providers, three independent ways, which is why the number is trusted:

```
ls -d weather_providers/*/ | grep -v __pycache__ | wc -l          # 32
grep -c "_reg(" weather_providers/__init__.py                     # 33 (32 + the def)
grep -h "requires_key" weather_providers/*/__init__.py | sort | uniq -c
```

The last gives 12 keyless and 20 keyed. Verify the key gating by AST-walking the
factories for a return-None path rather than trusting the attribute alone;
`weatherkit` additionally needs PyJWT present, and `pollendotcom` is
User-Agent-gated but registers unconditionally.

Test files and secrets:

```
ls tests/test_*.py | wc -l                                        # 40
python -c "import sys; sys.argv=['x']; import secret_store as s; \
print(len(s.KNOWN_SECRETS), len(s.CONFIG_LOCATIONS))"             # 41 40
```

The `sys.argv` assignment is not cosmetic. `config.py` parses argv at import
time and will exit on an unrecognized argument, which is why every script in
this repo that imports it pins argv first.

Two traps this project has already hit with counting.

**A narrow glob is not the population.** The initial reconnaissance for this
documentation pass globbed `weather_providers/*.py`, found five files, and
concluded the provider layer was five files. The 32 provider implementations are
in sub-packages, 135 files and 4427 lines, entirely missed. Before asserting a
count, confirm you enumerated the right population, not the one your first
pattern happened to match.

**Validate the instrument before trusting its verdict.** When a check disagrees
with what you believe, the check is as likely to be wrong as the target. The
citation verifier described below produced 72 false failures on its first run
from arbitrary basename resolution, and three more from instance attributes.
Neither its passes nor its failures meant anything until it was trap-tested
against a deliberately broken citation.

## Finding out why a decision was made

Four sources, in the order to try them.

**`docs/design-decisions.md`** holds sixteen ADRs covering the choices whose
obvious cleanup reintroduces a failure the project already had. If the thing you
want to change is in there, read the entry before proposing anything: several
of these look like accidental complexity and are not. ADR-003 (thread-local DNS
pinning rather than an IP-literal adapter), ADR-010 (the single-source weather
rule), and ADR-011 (a fresh module object per load with no `sys.modules` entry)
are the three most often re-litigated.

**`CHANGELOG.md`** is Keep a Changelog format and is unusually detailed: entries
explain the failure that motivated a change, not just the change. It is the best
source for "when did this behavior appear and what was it replacing."

**`git log`** for the file, then the commit body. Commit messages in this repo
carry reasoning. `git log -p --follow <file>` survives renames; `git log -S
'<string>'` finds the commit that introduced or removed a specific line, which
is usually faster than reading history forward.

**Test names and docstrings** encode incidents. A test named for a bug number or
carrying a comment about what it prevents is a record of something that happened.
`tests/run_tests.py` in particular holds completeness gates, which exist because
an enumeration drifted at least once.

When none of these answers it, the honest form is to say the rationale is not
recorded. `docs/internals/` follows this convention explicitly: where behavior
looks questionable, the page says so in a findings section rather than inventing
a rationale. Do not close that gap with a guess. An invented rationale is worse
than an acknowledged unknown because the next reader cannot tell them apart.

## Verifying documentation citations

Two scripts, and they do different jobs. Understanding the difference is the
point of this section.

`scripts/remap-doc-citations.py` fixes line-number citations after an edit moved
the lines. It builds an exact old-to-new line map with `difflib` by comparing a
file at a git ref against the working tree, then rewrites `file.py:123`
citations across `docs/*.md`, `README.md`, `CONTRIBUTING.md`, and `SECURITY.md`.

```
scripts/remap-doc-citations.py HEAD internets.py admin_cmds.py
scripts/remap-doc-citations.py HEAD internets.py admin_cmds.py --apply
```

It reports without `--apply`, which is the intended first run. Lines deleted
outright are reported as UNMAPPABLE and left alone, because a citation pointing
at deleted code usually means the surrounding prose needs rewriting, not
renumbering.

**Renumbering is necessary and not sufficient.** A remapped citation is
arithmetically correct and can still be wrong: it may have been wrong before the
edit, or `difflib` may have mis-anchored when a block moved and was edited in
the same commit, and an off-by-one still resolves to a real line. Nothing about
a valid line number says the line means what the prose claims.

`scripts/verify-doc-citations.py` checks meaning instead. It parses each cited
file's AST and confirms the cited symbol actually exists as a module-level
function, a class, a method of the named class, or an instance attribute
assigned in a method.

```
scripts/verify-doc-citations.py            # full report, exit 1 on failure
scripts/verify-doc-citations.py --summary  # counts only
```

Symbol citations get a PASS or a FAIL. Line citations get REVIEW, never PASS,
even when the range is valid, because a line citation cannot be
content-verified mechanically. That asymmetry is deliberate: it is what makes
the line style visibly the one being retired.

This is why every citation in this corpus is now symbol-only
(`internets.py - IRCBot._dispatch()`). A symbol citation survives edits above it
and is mechanically checkable; a line citation is neither. Where a location has
no enclosing symbol - a module docstring, an import block - the prose names the
file and describes the location in words rather than carrying a line number.

Run `--summary` for the current numbers rather than trusting a figure written
here; the totals move with every doc edit. What a healthy result looks like:
zero symbol failures, zero missing files, and zero line citations of any kind.
The first full pass on 2026-08-15 verified 864 symbol citations clean against
162 legacy line citations, 47 of them in `deployment.md`; the conversion of the
remaining line citations finished on 2026-08-16, and `remap-doc-citations.py` is
kept only for the case where a line citation is reintroduced.

The errors that pass found are a useful illustration of what only a content
check catches. Four citations named a function that does not exist: one cited
"main" where the real entry point is the private `_main`, one cited a line
handler under an old name it was renamed away from, one cited an air-quality
formatter under the name of the capability rather than the method, and one cited
a constructor where the work actually happens in the module's `on_load` hook.
Every one of those cited a real file at a plausible name, and a line-based check
would have passed all four.

## Re-deriving the module inventory

Read the source, not the documentation, and note the four distinct populations
that get confused with each other.

| Population | How to get it | Count |
| --- | --- | --- |
| Files under `modules/` | `ls modules/*.py` | 75 |
| Loadable modules | instantiate `BotModule` subclasses | 70 |
| Modules registering commands | non-empty `COMMANDS` | 69 |
| Modules in the shipped autoload | `[bot] autoload` in the template | 67 |

The gaps between them are all meaningful. `base.py`, `geocode.py`, `units.py`,
`_netsafe.py`, and `__init__.py` are infrastructure and define no module.
`linktitle` is loadable but registers nothing, working entirely through the
`on_raw` fanout. And the autoload list is a deployment choice, not a property of
the code, which is how `privacy` came to be absent from it.

For a specific module, `docs/internals/modules/<name>.md` is the per-file page.
Its findings section is where anything questionable was recorded rather than
explained away.

## Re-deriving the provider inventory

Providers are discovered by structure, not by a list, so the source of truth
depends on the question.

- **Which providers exist**: directories under `weather_providers/`, one package
  each.
- **Which are registered**: the factory calls in `weather_providers/__init__.py`.
  A factory returning `None` when its key is absent is how key gating works.
- **What each can answer**: `hasattr` on capability method names. There is no
  declaration list. A misspelled `get_*` method is a capability that silently
  does not exist, which is exactly the failure mode this discovery mechanism
  buys convenience with.
- **How they rank**: `weather_providers/_dispatch.py - DEFAULT_RELIABILITY`.
  A capability absent from a provider's entry gets rank 99, silently last.

A `hasattr` sweep over all 32 provider classes is the check that catches the two
directions of reliability-table drift, and it found both: entries ranking a
provider for a capability it does not implement (`meteomatics` for nowcast,
`accuweather` for air quality) and a provider omitted from a capability it does
implement (`stormglass` for current). Re-run that sweep after any provider
change rather than reading the table.

Per-provider pages are under `docs/internals/weather-providers/providers/`.

## Recording what you learn

Two rules that keep this method from having to be repeated.

**Record the lapse, not only the plan.** A tracker naturally accumulates forward
work and silently omits backward facts: a defect found and not fixed, work
descoped, a gate skipped, a rename that stranded state. Those feel like history
rather than items, so they never get written down, and because they are absent
the next reader re-derives the same wrong picture.
[known-issues.md](known-issues.md) is where that goes for
this project; the prioritized version with impact statements is in
[handoff](handoff.md).

**Prefer a mechanical check to a written rule.** A constraint that lives only in
prose gets violated and then distrusted. Where a decision can be machine-checked,
pair it with the check: `gen-command-reference.py --check` for the command
inventory, `verify-doc-citations.py` for citations, `verify_install.sh` for the
packaged module list, and the completeness gates in `tests/run_tests.py` for
enumerations that must stay in sync. Each of those exists because the prose
version of the same rule failed at least once.
