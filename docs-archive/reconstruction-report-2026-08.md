# Documentation reconstruction - completion report

Point-in-time record of the documentation reconstruction carried out on
2026-08-15 and 2026-08-16 against branch `docs-reconstruction`. This is a
snapshot of the work, not living documentation. The living artifacts are the
guides under `docs/`, the implementation reference under `docs/internals/`, and
the defect register at `docs/known-issues.md`.

## Files inspected

| Category | Count |
| --- | --- |
| Python source files read | 229 |
| Root modules | 13 |
| Command modules (`modules/`) | 75 files, 70 loadable, 69 registering commands |
| Weather provider core | 5 files |
| Weather provider sub-packages | 32 providers, 135 files |
| Test files | 42 (40 `test_*.py` plus `conftest.py` and `run_tests.py`) |
| CI workflows, packaging, scripts | 3 workflows, `pyproject.toml`, 2 requirements files, 6 scripts |

Documentation coverage was checked in the reverse direction rather than assumed:
every source file was traced to a page, and every enumerable population
(config keys, secrets, metrics, state files, environment variables, CLI flags,
registered commands) was enumerated from source and traced into the docs.

## Documentation created

Layer 2, the implementation reference (130 pages under `docs/internals/`):

- 13 root-module pages, one per source file
- 74 command-module pages plus an index
- 5 weather-aggregation core pages
- 32 provider pages plus two index pages
- `tests.md` and `ci-and-packaging.md`

Layer 1, new guides:

`irc-protocol`, `command-reference`, `operations`, `administration`,
`troubleshooting`, `state-and-persistence`, `logging-and-auditing`,
`metrics-and-observability`, `integrations`, `testing`, `writing-providers`,
`known-issues`, `data-retention`, `incident-response`, `disaster-recovery`,
`versioning-and-support`, `dependencies`, `release-process`,
`service-objectives`, `performance`, `output-conventions`,
`documentation-governance`.

Root: `PRIVACY.md`, which the bot has always told users to read and which had
never existed.

## Documentation rewritten

`README.md` (541 to 264 lines), `CONTRIBUTING.md`, `SECURITY.md`, and in
`docs/`: `architecture` (837 to 601), `security-model` (1323 to 727),
`configuration`, `deployment`, `modules`, `writing-modules`, `providers` (1139
to 686), `design-decisions`, `handoff` (1638 to 547), `knowledge-recovery`,
`getting-started` (an 11-line stub to a 330-line procedure), `executive`,
`index`.

Three large guides got shorter because per-file detail moved into the
implementation reference and the defect catalogue moved into the register;
duplication was removed rather than content.

## Consolidated or removed

Nothing was deleted. Prior versions of every rewritten file remain in git
history. `docs-archive/` holds this report and the working ledger the
reconstruction ran from; both are records, not references.

## Tooling added

| Script | Purpose |
| --- | --- |
| `scripts/gen-command-reference.py` | Generates the command inventory from registration; `--check` fails on drift |
| `scripts/verify-doc-citations.py` | Resolves every symbol citation through the cited file's AST; range-checks line and prose citations |

Both are wired into the CI lint job, because a documentation rule nothing
enforces is decoration.

## Technical claims corrected

Documentation that contradicted the source, found and fixed:

- The `.help` system was documented as working for core commands; it was not,
  and the fix for that is the first commit on this branch.
- `README.md` claimed 72 command modules; the verified figure is 70 loadable
  and 69 registering.
- `docs/configuration.md` claimed `[seen] max_age_days` was hardcoded; it is
  configurable, and the internals page already had it right.
- `docs/internals/tests.md` had the test-file count, module denominator, and
  `run_tests.py` line count all slightly wrong.
- `docs/internals/modules/scinews.md` said roughly 130 feeds; there are 173.
- `docs/internals/modules/health.md` documented `.uptime` as a working public
  command; it is shadowed by the core command and never runs.
- `docs/state-and-persistence.md` said `[seen]` appears in
  `config.ini.example`; it does not.
- `docs/deployment.md` attributed store quarantine to 5.0.0; CHANGELOG and tag
  reachability put it in 4.0.0.
- A prose claim cited `bot.check_flood`, which exists nowhere in the source.

Corrections to findings recorded earlier in this same reconstruction:

- The `stocks.py` key leak was recorded as triggering on a network outage. It
  triggers on any 401, which makes a botched key rotation publish the
  replacement key.
- The pollen user-agent path was recorded as sending an empty agent; it fails
  open with a hardcoded one.
- The module count was recorded as 70 command-registering; 70 are loadable and
  69 register commands.
- An entry claimed two source comments wrongly assert `fsync`; there is one,
  plus a separate wrong claim about append-binary mode.

## Citation verification

Citation style moved from line numbers to symbols, because line numbers rot on
the next edit with no signal.

| Measure | Result |
| --- | --- |
| Citations checked | 1318 |
| Symbol citations verified against the AST | 1231 |
| Symbol failures | 0 |
| Line citations remaining | 2, both illustrative examples of the retiring style |
| Line citations out of range | 0 |
| Missing cited files | 0 |
| Bare prose line ranges range-checked | 105 |
| Prose ranges past end of file | 0 |

Ten citations were wrong when checked and are fixed: four naming
`internets.py - main` (it is `_main`), four naming `_handle_line` (it is
`_process`), one naming a nonexistent `_format_air_quality`, and one naming a
`Weather` class that is `WeatherModule`. Three more were invisible to the first
version of the checker and surfaced only after it was corrected.

The checker itself was wrong twice before its output was trusted. Its first run
reported 72 failures that were all its own fault: it resolved ambiguous
basenames arbitrarily in a repository with several `base.py` and many
`__init__.py`. It was then blind to instance attributes and to bare prose line
ranges. Each correction was followed by a trap test confirming it still fails on
a deliberately broken citation.

## Build verification

| Measure | Result |
| --- | --- |
| HTML build | succeeds; zero warnings from the hand-written corpus |
| PDF build (xelatex) | succeeds, 950 pages, zero LaTeX errors |
| Severe page overflows (>100pt) | 0, was 76 |
| Worst overflow | 95.7pt, was 694pt |
| Graphviz diagrams validated | 7 of 7 |
| Orphaned or dangling toctree entries | 0 |
| Em-dashes and en-dashes in the corpus | 0 |

The PDF was the least trustworthy part of this work until late. `build-docs.sh`
exits 0 while discarding xelatex's output, so a badly overflowing page reports a
successful build. Overflow must be measured from `docs/_build/latex/internets.log`.

## Test and gate status

| Gate | Result |
| --- | --- |
| `python -m pytest tests/ -q` | 1738 passed, 3 skipped |
| `python tests/run_tests.py` | 213 passed, 0 failed |
| `scripts/verify-doc-citations.py` | exit 0 |
| `scripts/gen-command-reference.py --check` | passes |

One gate caught a regression introduced by this work: a bare version literal in
`docs/security-model.md`. The document was reworded rather than the gate
loosened.

## New findings

Nineteen entries in `docs/known-issues.md`, none fixed, all verified. Ranked by
user impact:

1. Provider failure publishes finance API keys into the channel, triggered by a
   401 and therefore self-amplifying during a key rotation.
2. Weather fallback is disabled for eleven of fourteen capabilities, which
   silently suppresses severe-weather alerts.
3. `.isprime` runs unbounded factoring on the event loop; one message hangs the
   bot for every user.
4. The shipped autoload enables seven data-collecting modules and omits the
   privacy module, so a template install has no erasure command.
5. Audit-chain verification accepts self-declared legacy records, reproduced end
   to end: a tampered chain verifies as intact.
6. CI has been red on `main` since 2026-08-13 from a lockfile resolved on the
   wrong Python version.
7. The bot's own log advice (`chmod 640`) breaks its fail-closed secret store,
   which requires exactly 0600.
8. Tide times report the day's first extremes as the next ones.
9. `.uptime` from the health module is unreachable, and the fallback the refusal
   message suggests is also refused.
10. `.health` reads two store attributes that do not exist.
11. PurpleAir applies its EPA correction to the wrong measurement variant.
12. Concurrency and durability gaps; `os.fsync` appears nowhere in the codebase.
13. Lower-severity items, thirteen of them.
14. A packaged install cannot find `modules/` or the config template.
15. The bot log is unprotected and holds the data `.forgetme` cannot reach.
16. `.fingerprint`, the most privacy-invasive admin command, is the only
    unaudited one.
17. Three retention controls disagree about what zero means.
18. Opting out creates a prune-immune record with unbounded retention.
19. Outbound and observability blind spots, eight of them.

## Remaining uncertainty

Stated plainly rather than omitted:

- **No load testing was performed.** `docs/performance.md` gives reasoned
  estimates with their assumptions shown and says so; no benchmark number in it
  was measured.
- **105 bare prose line ranges are range-checked, not content-verified.** They
  lie inside their files, but nothing confirms each range still describes the
  code it claims to. Converting them is a prose rewrite per file.
- **The eleven documents written last were audited by a separate adversarial
  pass**, whose findings are recorded against them. Documents written earlier
  had per-claim verification by their author and spot-checks by the
  orchestrator, not a second independent audit.
- **Third-party behavior cannot be verified from this repository**: provider
  quotas, API terms, and rate limits are documented as the code and their own
  documentation state them, and may have changed.
- **The defect register records verification method per entry.** Entries marked
  as read-and-confirmed are weaker evidence than the several that were
  reproduced.

## Definition of done

Met: every source file documented, every enumerable population traced from
source into the docs, all citations content-verified, both builds green, style
consistent, contradictions resolved in favor of the source, and the findings
recorded in a register rather than silently fixed.

Not met, deliberately: no defect found during this work was fixed, because each
changes runtime behavior on a live system and that decision belongs to the
maintainer. The exceptions are documentation defects and one docstring that
described behavior the code does not have.
