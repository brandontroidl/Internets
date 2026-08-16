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

The three floors sets can drift, and did. Before the fix recorded in the
Unreleased CHANGELOG section, the `weatherkit` and `all` extras pinned
`PyJWT>=2.10.1` and `cryptography>=44.0.0` while `requirements.txt` required
`>=2.13.0` and `>=50.0.0`, so `pip install internets-irc[weatherkit]` could
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
| `requests>=2.32.3` | none (core) | Yes | Nothing works. Every module HTTP call goes through `modules/base.py - fetch_json()`, which is built on it |
| `aiohttp>=3.14.3` | `async` | No | Weather HTTP falls back to `requests` in a thread |
| `argon2-cffi>=23.1.0` | `argon2` | Conditional | An `argon2$` password hash cannot be verified; admin auth fails closed |
| `bcrypt>=4.2.0` | `bcrypt` | Conditional | A `bcrypt$` password hash cannot be verified; admin auth fails closed |
| `PyJWT>=2.13.0` | `weatherkit` | No | The Apple WeatherKit provider cannot sign its ES256 token |
| `cryptography>=50.0.0` | `weatherkit` | No | Same; PyJWT needs it for ES256 |
| `defusedxml>=0.7.1` | `xml` | Conditional | Three command modules fail to load at all |

The `all` extra is the union of the six optional packages. The `dev` extra
(`pytest`, `pytest-asyncio`, `pytest-cov`, `coverage`, `bandit[sarif]`,
`pip-audit`, `build`) is tooling only and never reaches a deployment.

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
resolution dropped `typing_extensions>=4.4`, which `aiohttp` requires below
Python 3.13, so every Python < 3.13 leg of the Tests workflow fails at `pip
install -r requirements.lock --require-hashes` with "all requirements must have
their versions pinned". CI has been red on `main` since 2026-08-13.

A second defect hides the first on Windows: the workflow's install step runs
three `pip` commands in one `run:` block, and `pwsh` does not fail fast, so the
failing install reports success and the job dies much later in pytest with a
confusing `ModuleNotFoundError`.

Until the lock is regenerated on 3.10, install from `requirements.txt` rather
than the lock on Python 3.10 through 3.12. Item 6 in
[known-issues.md](known-issues.md); findings detail in
[internals/ci-and-packaging.md](internals/ci-and-packaging.md#findings).
:::

The 3.0.0 CHANGELOG entry records the same class of failure from the other
direction: the lock was re-generated on 3.10 precisely so a hash-pinned install
stays valid across the whole matrix, "3.14 alone drops aiohttp's conditional
`typing-extensions` / `async-timeout`". The constraint is documented, was
honored once, and was then violated. It is a procedure with no enforcement: no
CI job checks the lockfile header against the supported floor.

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
   comment beside it (this is the established convention; every existing floor
   has one).
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
query pack; `bandit` runs twice per push, informationally at MEDIUM and above,
then as a gate at HIGH severity with HIGH confidence, with SARIF uploaded to the
Security tab either way.

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

That UA value is `[secrets] weather_user_agent` and it is shared by every module
HTTP call, so the operator's real email or URL is attached to every outbound
request the bot makes, not only the geocoding ones. That is the intended design
and it is recorded in [integrations.md](integrations.md#privacy-what-leaves-the-machine).

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

1. **Confirm it is the provider, not the bot.** Weather commands fail over on
   their own, so a user-visible weather failure means the whole chain is
   exhausted. A single-source module replies with a fixed string and logs the
   failure; check the log for the real status before assuming.
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
