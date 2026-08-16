# Dependencies and supply chain

What the bot depends on, how those dependencies are pinned, how the pins are
regenerated and why that procedure has a hard constraint, what audits them, and
what governs the several dozen third-party HTTP services the bot calls at
runtime.

Python packages and network services are both supply chain. The first half of
this page covers packages; the second covers services, which have no lockfile
and are the larger uncontrolled surface.

## The pin model: three files, three jobs

| File | Contains | Consumed by |
|---|---|---|
| `requirements.txt` | Human-maintained security **floors**, each annotated with the CVE or advisory that set it | Operators installing a deployment |
| `requirements.lock` | Machine-generated exact pins with `--hash=sha256:` for every package and transitive | CI, with `--require-hashes` |
| `pyproject.toml` extras | Per-feature floors that must match `requirements.txt` | `pip install internets-irc[...]` |

The stated version policy, identical in both `requirements.txt` and
`pyproject.toml`: a lower bound is the most recent stable release known to be
free of the annotated CVEs at the time it was set, and there are **no upper
bounds**, so pip always takes the newest compatible release. The
cross-platform, five-Python CI matrix is the declared tripwire for a breaking
upstream release.

### The extras have their own gate

The three floor sets can drift, and did - twice, in two separate events the
CHANGELOG records separately.

| Event | Extras pinned | `requirements.txt` required |
|---|---|---|
| First (Unreleased, `### Security`) | `PyJWT>=2.10.1`, `cryptography>=44.0.0` | `>=2.13.0`, `>=48.0.1` |
| Second (Unreleased, `### Fixed`) | `aiohttp`, `cryptography` left at the pre-bump floors | `>=3.14.3`, `>=50.0.0` |

The `>=50.0.0` cryptography floor (PYSEC-2026-3552) is the *second* event's; at
the time of the first, the floor was `>=48.0.1` (GHSA-537c-gmf6-5ccf). Either
way the consequence was the same: `pip install internets-irc[weatherkit]` could
resolve a `cryptography` this project's own requirements file calls unsafe.

CI cannot catch that, because `security.yml` audits `requirements.lock` only and
never the extras. The check therefore lives in the standalone suite:

> `tests/run_tests.py`, test name
> **"DEPS: pyproject extras never sit below the requirements.txt security floors"**

It parses every `name>=version` floor out of `requirements.txt`, then walks every
list-valued block in `pyproject.toml` except `dev` (which has its own policy) and
fails on any pin whose version tuple sorts below the corresponding floor. Run it
with `python tests/run_tests.py`.

## Direct dependencies

Only one package is mandatory. `pyproject.toml` `[project] dependencies` lists
`requests` alone; everything else is an extra. `requirements.txt` installs the
full set, which is what a real deployment wants.

| Package | Extra | Required? | Without it |
|---|---|---|---|
| `requests>=2.32.3` | none (core) | Yes | Nothing works. Both the shared `modules/base.py - fetch_json()` helper and the direct call sites are built on it |
| `aiohttp>=3.14.3` | `async` | No | Weather HTTP falls back to `requests` in a thread |
| `argon2-cffi>=23.1.0` | `argon2` | Conditional | An `argon2$` password hash cannot be verified; admin auth fails closed |
| `bcrypt>=4.2.0` | `bcrypt` | Conditional | A `bcrypt$` password hash cannot be verified; admin auth fails closed |
| `PyJWT>=2.13.0` | `weatherkit` | No | The Apple WeatherKit provider cannot sign its ES256 token |
| `cryptography>=50.0.0` | `weatherkit` | No | Same; PyJWT needs it for ES256 |
| `defusedxml>=0.7.1` | `xml` | Conditional | Three command modules fail to load at all |

The `all` extra is the union of the six optional packages. The `dev` extra
(`pytest`, `pytest-asyncio`, `pytest-cov`, `coverage`, `bandit[sarif]`,
`pip-audit`, `build`) is tooling only and never reaches a deployment.

:::{note}
**`fetch_json()` is the preferred helper, not a chokepoint.** An AST walk over
`modules/*.py` counting `requests.<verb>(...)` calls finds 35 call sites in 32
files. One of those is `fetch_json()`'s own; the other **34, spread over 31
module files, call `requests` directly and bypass the helper**. Twenty-four
module files import `fetch_json`, and seven of those do both - the helper for
the JSON paths, a direct call for a streamed or non-JSON one.

That matters because `fetch_json()` is where the response size cap lives
(`max_bytes`, read via `r.raw.read(max_bytes + 1)` before any decode or parse).
A direct call site gets that bound only if it implements the stream-and-cap
pattern itself. As of this writing all of them do: every one of the 34 passes
`stream=`, there are 35 `.raw.read(` sites across `modules/` (the 34 plus
`fetch_json()`'s own), and no module reads a response body via a bare `.json()`
or `.text`. The invariant holds - but it holds by convention re-implemented at
each site, not by a property the architecture enforces, and nothing in CI checks
it. Treat a new direct call site as needing its own review.
:::

### The conditional cases, precisely

**`aiohttp` is a performance choice, not a functional one.**
`weather_providers/_http.py` sets `_HAS_AIOHTTP` from a guarded import at module
scope and picks a transport per call; without aiohttp every provider request
runs `requests` inside `asyncio.to_thread`. Commands still work, they just
occupy a thread-pool slot for the duration. `weather_providers/_http.py -
_get_session()` raises if it is somehow reached without aiohttp, which is a
caller-contract assertion rather than an operator-facing failure.

**`bcrypt` and `argon2-cffi` are alternative hash backends, and which one you
need is decided by your stored hash, not by the code.** `hashpw.py` supports
three formats distinguished by prefix: `scrypt$` (stdlib `hashlib`, no package,
and the CLI default), `bcrypt$`, and `argon2$`. The import is lazy and inside
the verify function, so a missing backend is not a startup failure:
`hashpw.py - _verify_bcrypt()` and `hashpw.py - _verify_argon2()` raise
`ValueError` naming the missing package rather than returning `False`. That
fails closed - admin authentication cannot succeed - but it does so at first
`.auth`, not at boot. A deployment on `scrypt$` needs neither package.

**`defusedxml` is a hard import in three modules.** `modules/idlerpg.py`,
`modules/scinews.py`, and `modules/reflookup.py` all import it at module scope,
so without the package `IRCBot.load_module()` fails for all three and the
commands vanish: `.irpg`, `.sci`, and the whole `reflookup` set (`.wiki`,
`.doi`, `.isbn`, `.so`, `.rfc`, `.arxiv`, `.element`, `.rtfm`). Note that
`reflookup` needs the parser only for the `.arxiv` ATOM path but imports it at
the top, so a missing package costs seven commands to protect one.

:::{note}
`requirements.txt` annotates `defusedxml` as "used by `modules/qdb.py`". That is
stale: `modules/qdb.py`'s own docstring records that it HTML-scrapes and does no
XML parsing at all. The real consumers are the three modules above. The comment
dates from 2.5.0, when `qdb` did parse XML.
:::

## The lockfile

`requirements.lock` currently pins 21 distributions: the 7 direct packages above
plus 14 transitives (`aiohappyeyeballs`, `aiosignal`, `argon2-cffi-bindings`,
`attrs`, `certifi`, `cffi`, `charset-normalizer`, `frozenlist`, `idna`,
`multidict`, `propcache`, `pycparser`, `urllib3`, `yarl`). Every one carries its
full set of `--hash=sha256:` lines, so `pip install --require-hashes` refuses a
wheel with the right name and the wrong hash. That is the defense against a PyPI
account takeover and against dependency confusion.

### Regeneration, and the constraint that makes it fragile

```bash
scripts/regen-lockfile.sh
```

The script creates an ephemeral venv, installs `pip-tools`, and runs
`pip-compile --generate-hashes --resolver=backtracking --strip-extras
--no-emit-options`. Run it whenever `requirements.txt` changes, from a
Dependabot bump or a hand edit, and commit both files in the same commit.

**It must run on Python 3.10 specifically.** The script probes for a 3.10
interpreter and exits with an explanation if it cannot find one. The reason is
that `pip-compile` resolves against the interpreter it runs on, so a dependency
gated by an environment marker such as `python_version < "3.11"` is simply
absent from a lock resolved on a newer interpreter. The lock must be resolved on
the **lowest supported** Python or it is not valid across the supported range.

:::{warning}
**This has already failed in production, and is failing now.** The committed
`requirements.lock` header records that it was generated with Python 3.14. That
resolution dropped the marker-gated transitives, so every Python < 3.13 leg of
the Tests workflow fails at `pip install -r requirements.lock --require-hashes`
with "all requirements must have their versions pinned". CI has been red on
`main` since 2026-08-13.

Two distributions are missing, and three separate declarations demand them.
Taken from the metadata of the versions this lockfile actually pins:

| Missing | Declared by | Marker |
|---|---|---|
| `typing_extensions>=4.4` | `aiohttp 3.14.3` | `python_version < "3.13"` |
| `typing-extensions>=4.2` | `aiosignal 1.4.0` | `python_version < "3.13"` |
| `typing-extensions>=4.1.0` | `multidict 6.7.1` | `python_version < "3.11"` |
| `async-timeout<6.0,>=4.0` | `aiohttp 3.14.3` | `python_version < "3.11"` |

`>=4.4` is the binding floor below 3.13. Note that it is version-sensitive:
`aiohttp` gained the direct `typing_extensions` requirement in **3.14.0** and
had none in 3.13.x, so checking this against an older installed `aiohttp`
rather than against the locked 3.14.3 gives the wrong answer and makes the
constraint look like it has no source. `async-timeout` is the transitive
`scripts/regen-lockfile.sh` names by hand in its own error message.

A second defect hides the first on Windows: the workflow's install step runs
three `pip` commands in one `run:` block, and `pwsh` does not fail fast, so the
failing install reports success and the job dies much later in pytest with a
confusing `ModuleNotFoundError`.

Until the lock is regenerated on 3.10, install from `requirements.txt` rather
than the lock on Python 3.10 through 3.12. Item 6 in
[known-issues.md](known-issues.md); findings detail in
[internals/ci-and-packaging.md](internals/ci-and-packaging.md#findings).
:::

Two CHANGELOG entries record the same class of failure from the other
direction, and they are distinct entries in distinct releases:

- **4.0.0**, under `### Security`, on the 20-CVE dependency bump: the lock was
  re-generated on 3.10 precisely so a hash-pinned install stays valid across the
  whole matrix, "3.14 alone drops aiohttp's conditional `typing-extensions` /
  `async-timeout`".
- **3.0.0** is where the constraint was first written into the tooling:
  `scripts/regen-lockfile.sh` "now requires Python 3.10 specifically and fails
  loudly otherwise", because "a lock built on 3.14 silently omitted" the
  `python_version < "3.11"` conditional transitives.

The constraint is documented, was honored once, and was then violated. It is a
procedure with no enforcement: no CI job checks the lockfile header against the
supported floor.

### The lockfile pins nothing until someone installs it

A lockfile constrains a resolution. It does not constrain a machine. The
installed environment can sit arbitrarily far from `requirements.lock` and
nothing in this repository notices, because `security.yml`'s `pip-audit` job
reads `requirements.lock` from the working tree and never inspects an
environment.

Measure the gap directly:

```bash
python -m pip list --format=json > /tmp/installed.json
python - <<'PY'
import json, re
lock = {}
for line in open("requirements.lock"):
    m = re.match(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", line)
    if m:
        lock[m.group(1).lower().replace("_", "-")] = m.group(2)
inst = {p["name"].lower().replace("_", "-"): p["version"]
        for p in json.load(open("/tmp/installed.json"))}
for name, want in sorted(lock.items()):
    have = inst.get(name)
    if have != want:
        print(f"{name}: locked {want}, installed {have or 'ABSENT'}")
PY
```

Run on the maintainer's working host on 2026-08-15, that reported **15 of the
21 locked distributions at a version other than the pin**: 13 behind
(`aiohttp`, `argon2-cffi`, `argon2-cffi-bindings`, `attrs`, `bcrypt`,
`certifi`, `charset-normalizer`, `cryptography`, `multidict`, `propcache`,
`pycparser`, `requests`, `yarl`) and 2 ahead (`aiohappyeyeballs`, `idna`).

Two of those are not merely stale, they sit **below the security floors
`requirements.txt` itself declares**, which is the condition the floors exist to
prevent:

| Package | `requirements.txt` floor | Installed | Advisories the floor cites |
|---|---|---|---|
| `aiohttp` | `>=3.14.3` | 3.13.5 | PYSEC-2026-3545 / 3546 / 3547 |
| `cryptography` | `>=50.0.0` | 46.0.7 | PYSEC-2026-3552 |

`bcrypt` (4.3.0 installed, 5.0.0 locked) and `requests` (2.33.1 installed,
2.34.2 locked) are behind the lock but still above their floors, so they are a
reproducibility gap rather than an exposure.

Three things follow, and they are the reason this belongs on a page about
pinning:

- **The drift is a predictable consequence of the advice two sections up.**
  While the lock is unusable on 3.10 through 3.12, the documented workaround is
  to install from `requirements.txt` - which has floors and no upper bounds, so
  it resolves to whatever PyPI offers that day and never to the pinned set. A
  broken lock does not just stop being enforced; it actively pushes deployments
  off the pins.
- **`scripts/sbom.sh` is the one tool here that sees the truth**, because it
  deliberately reads the installed environment rather than the manifests. An
  SBOM taken on a drifted host correctly reports the drifted versions - which is
  the intended behavior, and also why an SBOM generated from an un-reconciled
  environment is not a description of what the lockfile says ships.
- **Reconciling is one command, and it should precede any release build or
  SBOM:** `pip install -r requirements.lock --require-hashes` in the target
  venv, once the lock is regenerated on 3.10.

## Vulnerability response

### What is automated

| Mechanism | Scope | Cadence |
|---|---|---|
| `security.yml` `pip-audit` job | `requirements.lock` only | Every push and PR to `main`, plus Mondays 06:00 UTC |
| Dependabot (pip) | Repository manifests | Daily, security updates grouped separately |
| Dependabot (github-actions) | Workflow action pins | Daily |
| `security.yml` `bandit` job | The bot's own code | Same triggers |
| `security.yml` `gitleaks` job | Full git history | Same triggers |

The pip-audit invocation is:

```
pip-audit -r requirements.lock --strict --progress-spinner off \
          --ignore-vuln PYSEC-2025-183
```

`--strict` fails the job on any finding, which forces triage rather than
allowing an advisory to accumulate. The `-r requirements.lock` scope is
deliberate: a bare `pip-audit` would also inspect the local editable
`internets-irc` install, which has no PyPI entry and makes the run fail with
"Dependency not found on PyPI". The single ignored advisory, PYSEC-2025-183
against pyjwt, is disputed by the pyjwt maintainers - the alleged weak
encryption concerns the key length chosen by the *calling* application, and here
Apple issues the ES256 `.p8` key for WeatherKit, so the project does not pick it.
The workflow comment says to re-evaluate if a fixed release ever appears.

### The gap

**Nothing audits the extras.** `pip-audit` reads `requirements.lock`, which is
compiled from `requirements.txt`. A user who installs
`pip install internets-irc[weatherkit]` resolves against the `pyproject.toml`
floors instead, and no CI job ever sees that resolution. This is acknowledged in
the `pyproject.toml` comment itself and is the reason the floor-parity check was
added to `tests/run_tests.py` (above). Note what that check does and does not
buy: it proves the extras are not *below* the project's own floors. It cannot
prove the floors themselves are still CVE-free, because nothing re-audits a
floor once it is set. The floors are a point-in-time judgment that ages.

The practical response procedure, for a CVE against a runtime dependency:

1. Raise the floor in `requirements.txt`, with the advisory ID in the inline
   comment beside it. This is the convention for a floor that a CVE set, and
   four of the seven follow it: `requests` (CVE-2024-35195, CVE-2023-32681),
   `aiohttp` (PYSEC-2026-3545 / 3546 / 3547), `PyJWT` (the 2026 PYSEC fixes,
   subsuming CVE-2024-53861) and `cryptography` (PYSEC-2026-3552). The other
   three - `argon2-cffi`, `bcrypt`, `defusedxml` - carry functional rationale
   instead, because no advisory set them: bcrypt's `>=4.2.0` is "the Rust-backed
   implementation with current wheels", and `defusedxml`'s is billion-laughs
   hardening. A floor without an ID is not necessarily a documentation gap; it
   may simply not have been CVE-driven.
2. Raise the matching floor in every `pyproject.toml` extra that names the same
   package, including `all`. The floor-parity test fails if you miss one.
3. `scripts/regen-lockfile.sh` on Python 3.10, and commit `requirements.txt`,
   `requirements.lock`, and `pyproject.toml` together.
4. Record it under `### Security` in the CHANGELOG with the advisory IDs. The
   aiohttp 3.14.1 to 3.14.3 and cryptography 49.0.0 to 50.0.0 entry in the
   Unreleased section is the model.
5. Run `python tests/run_tests.py` and `pytest tests/`.

## Supply-chain controls in place

**Every GitHub Action is pinned to a commit SHA, in all three workflow files.**
Verified across `tests.yml`, `security.yml`, and `codeql.yml`: every `uses:` line
is a 40-character SHA with the human-readable tag in a trailing comment. The
distinct actions in use are `actions/checkout` (v5),
`actions/setup-python` (v6), `actions/upload-artifact` (v4),
`github/codeql-action/upload-sarif`, `github/codeql-action/init`,
`github/codeql-action/analyze` (v4), and `gitleaks/gitleaks-action` (v2.3.9). A
tag can be moved; a SHA cannot, so this closes the "compromised action retags"
path.

**CI installs runtime dependencies hash-checked.** `pip install -r
requirements.lock --require-hashes`. The dev extras are installed unhashed, with
the stated rationale that they run only on CI runners, never reach production,
and are tracked by Dependabot anyway.

**A built artifact is verified against its own manifest.**
`scripts/verify_install.sh` builds the wheel and sdist, installs the wheel into a
throwaway venv, and re-hashes every installed file against the SHA-256 recorded
in the wheel's `RECORD` metadata before smoke-testing imports. See
[release-process.md](release-process.md).

**An SBOM can be produced on demand.** `scripts/sbom.sh` emits CycloneDX 1.x
JSON (`sbom.cdx.json` by default, `OUT=` to redirect) via `pip-audit --format
cyclonedx-json`. It deliberately reads the **currently installed environment**
rather than the manifests, so the SBOM describes what actually ships rather than
what `pyproject.toml` claims. Run it inside the same venv used for `python -m
build`. It is not wired into any workflow, so an SBOM exists only when someone
runs the script.

**Static analysis on the bot's own code.** CodeQL runs the `security-extended`
query pack. `bandit` runs **three** times per push, over the same tree with the
same exclusions (`./tests`, `./.venv`, `./build`, `./dist`, `./.git`,
`./__pycache__`) and only the thresholds differing:

| Step | Flags | Effect |
|---|---|---|
| Informational | `-ll --exit-zero` | MEDIUM+ severity, any confidence; never fails |
| Gate | `-iii -ll` | HIGH confidence and MEDIUM+ severity; fails the job |
| SARIF | `-f sarif -o bandit.sarif \|\| true` | No thresholds, so LOW+ at any confidence; never fails |

The SARIF step is `if: always()` and the upload follows it, so findings reach
the Security tab whatever the gate does.

:::{warning}
**The workflow's own comment misdescribes its gate.** The step is named "Fail on
HIGH-severity bandit findings" and its preceding comment says "HIGH severity AND
HIGH confidence", but the flags are `-iii -ll`. In bandit, `-i` repeated sets
*confidence* and `-l` repeated sets *severity*, so `-iii` is HIGH confidence
while `-ll` is MEDIUM-and-above severity, not HIGH. The gate is therefore
stricter than its own documentation claims: a MEDIUM-severity, HIGH-confidence
finding blocks the merge, and a reader trusting the comment would not expect
that. This is a defect in `.github/workflows/security.yml`, not in this page;
raising it to `-lll` or correcting the comment are both one-line changes, and
which is right is a maintainer decision.
:::

## Third-party service governance

The bot calls several dozen external HTTP APIs. They are a supply chain with no
lockfile, no hashes, no advisory feed, and no version numbers - and they can
change or disappear without notice.

The inventory itself is [integrations.md](integrations.md): every service, which
command reaches it, which credential it needs, what leaves the machine, and how
each failure mode degrades. Do not duplicate that list. What follows is what the
inventory does not cover.

### Attribution and contact requirements

**Exactly one provider's usage policy is enforced in code.** Nominatim requires
a unique, contactable User-Agent, and `modules/geocode.py - _ua_has_contact()`
refuses to make the call at all when the configured UA carries neither an `@`
with a domain after it nor an `http://` / `https://` prefix. The failure is a
logged warning and a `None` result, chosen deliberately: a banned source IP would
break geocoding for the whole channel, so failing the one lookup is cheaper. Its
sibling control is the 24-hour TTL cache in `modules/geocode.py`, whose comment
cites the same policy - Nominatim's terms require clients to cache.

That UA value is `[secrets] weather_user_agent`, and it is threaded through the
module HTTP layer as the `ua` argument, so the operator's real email or URL is
attached to nearly every outbound request the bot makes, not only the geocoding
ones. That is the intended design and it is recorded in
[integrations.md](integrations.md#privacy-what-leaves-the-machine).

**One module opts out.** `modules/translate.py` hard-codes
`headers={"User-Agent": "Mozilla/5.0"}` and never reads the configured value.
It is the sole exception - every other `User-Agent` header in `modules/` passes
the `ua` parameter through. The privacy direction of that exception is
favorable (the operator's contact address does not reach the translation
endpoint) but it is undocumented in the source, unexplained, and it means the
statement "the configured UA goes out with every request" is not literally
true. An operator auditing what identifies their bot upstream should know the
one endpoint where it does not.

**No other provider's terms are represented in code or in this repository.** No
attribution string is emitted with any provider's data; no caching obligation
other than Nominatim's is implemented; no redistribution restriction is recorded
anywhere. That is a statement about the repository, not about the providers:
several of the services in use are known to publish attribution, caching, or
non-commercial clauses in their own terms, and none of them have been reviewed
here. An operator running the bot publicly is the party bound by those terms.

:::{note}
**Proposal for the maintainer.** The gap above is the one piece of dependency
governance with no artifact at all. A per-service register - one row per service
with the credential name, the licence or terms URL, whether attribution is
required, whether caching or redistribution is restricted, and the date the
terms were last read - would be the smallest thing that closes it, and it would
live naturally beside the existing inventory in
[integrations.md](integrations.md). Nothing here should be read as legal advice
or as a claim about any specific provider's current terms; the register is a
place to record what someone actually checked.
:::

### Quota exhaustion

Quota tracking exists and is explicitly **visibility, not enforcement**. The
counters live in `weather_providers/__init__.py`: `record_call()` increments a
per-provider daily count, `quota_status()` reports used, limit, remaining, and
percentage, and the limits themselves are the free-tier figures in
`_DEFAULT_QUOTA_LIMITS` (AccuWeather 50/day, WeatherBit 50/day, Stormglass
10/day, Tomorrow.io 500/day, and so on, with `None` for the providers that
publish no cap). `weather_providers/_dispatch.py - Dispatcher.dispatch()` calls
`record_call()` for every attempted upstream call, including ones that fail, so
the counter reflects requests made rather than requests that succeeded.

Three properties worth knowing before relying on it:

- **The counter never blocks a call.** Nothing consults `quota_status()` before
  dispatching. A provider at 100% of its limit is still called.
- **The state is in memory and resets on restart**, and the day rolls at
  midnight UTC. A bot that restarts often under-counts continuously.
- **The actual response to exhaustion is the health system, not the counter.**
  `weather_providers/_dispatch.py - _is_rate_limit_error()` classifies a 429 or
  an explicit quota message, `record_failure(rate_limited=True)` decays the
  provider's health score, and repeated failures open its circuit breaker for a
  cooldown. A 401 or 403 - the shape an expired or revoked key takes - trips the
  breaker immediately rather than burning a request per dispatch. The rate-limit
  counter itself decays with a 300-second half-life so a transient burst does
  not lock a provider out permanently.

Outside the weather subsystem there is no quota handling at all. No module
retries a failed HTTP call and nothing implements backoff against a 429;
`bot.rate_limited(nick)` is a per-nick channel-abuse throttle, not an upstream
quota mechanism. `.apod` and `.neo` fall back to NASA's shared `DEMO_KEY` and its
stricter quota when no key is configured, which is a degradation, not a control.

### When a provider disappears

This is routine, not hypothetical. The repository records four instances:
numbersapi.com went defunct and `modules/numberfact.py` was rewritten as a
Wikipedia and local-math hybrid (3.0.0); `api.spacexdata.com` began returning
HTTP 525 and `.spacex` moved to Launch Library 2 (4.0.0); Packet Storm was
dropped from the `scinews` feed set on a TLS error; and the APS Physics feed
moved hosts after the original path began returning 403 behind Cloudflare.

The observed pattern, which is a reasonable default procedure:

1. **Confirm it is the provider, not the bot - and do not read a weather
   failure as an exhausted chain.** Fallover works for only 3 of the 14
   capabilities. `weather_providers/_dispatch.py - Dispatcher.dispatch()` moves
   to the next provider on `result is None or (hasattr(result, "is_empty") and
   result.is_empty())`, and only `WeatherResult` and `HourlyResult` implement
   `is_empty()`. For the other eleven capabilities an empty result counts as
   success and **ends the chain at the first provider**, so a user-visible
   `.alerts`, `.marine`, `.aqi`, `.pollen` or `.astro` failure most likely
   means the top-ranked provider returned empty, not that every provider was
   tried. Check the log for which providers were actually dispatched before
   concluding anything about the others. This is item 2 in
   [known-issues.md](known-issues.md); until it is fixed, treat the provider
   chain as live only for `current`, `forecast` and `hourly`. A single-source
   module replies with a fixed string and logs the failure; check the log for
   the real status before assuming.
2. **Prefer replacing the source over removing the command.** Both `numberfact`
   and `spacex` kept their command surface unchanged across the swap. A command
   that vanishes is a visible regression for users; a command that changes its
   backing source is not.
3. **Prefer a keyless replacement.** Every replacement above was keyless, which
   avoids adding a credential to `secret_store` for a feature that just lost its
   original one.
4. **For a weather provider, removal is cheap by design.** A keyed provider whose
   factory returns `None` simply drops out of the chain with a `skipped (no
   <key>)` log line, so an unusable provider can be neutralized by removing its
   key without a code change.
5. **Record it in the CHANGELOG with the observed symptom**, in the shape the
   existing entries use: the HTTP status or error, not just "switched providers".
   That is what makes the next occurrence diagnosable.

A caveat that applies to the scraped sources specifically: `modules/fml.py` is
coupled to an upstream CSS class string and `modules/qdb.py` to an HTML layout.
Both report honestly when their parse finds nothing, but a silent upstream
redesign disables the command until a human notices. There is no monitoring for
this.

## Related reading

- [integrations.md](integrations.md) - the service inventory, credentials, and
  degradation behavior.
- [versioning-and-support.md](versioning-and-support.md) - supported Python, and
  why the lockfile is coupled to the floor.
- [release-process.md](release-process.md) - `verify_install.sh`, the packaging
  gate, and the release checklist.
- [internals/ci-and-packaging.md](internals/ci-and-packaging.md) - the workflows
  and scripts, file by file.
- [security-model.md](security-model.md) - the trust boundaries these
  dependencies sit on.
- [known-issues.md](known-issues.md) - the verified-defect register.
