# Security policy

## Supported versions

| Version | Supported |
|---|---|
| `main` | yes |
| latest tagged release | yes |
| any earlier tag | no |

Fixes land on `main` and reach users in the next tagged release. There are no
backport branches, so an older tag receives nothing: upgrade rather than
requesting a patch for a superseded version. `internets --version` prints the
running version.

## Reporting a vulnerability

Report privately through the repository **Security** tab, "Report a
vulnerability". That opens a GitHub private security advisory, which is
enabled on this repository and is the only accepted channel.

Do not use a public issue, a pull request, a discussion thread, or an IRC
message. `.github/ISSUE_TEMPLATE/config.yml` disables blank issues and points
here for exactly this reason.

Include:

- the command, module, or file involved, and the commit or tag you tested;
- how to reproduce it, with the minimum steps that show the effect;
- the impact you can actually demonstrate, separated from what you suspect;
- whether a configured API key, an admin password, or stored user data is
  reachable, since that changes the severity considerably.

Redact your own credentials before sending. A report containing a live key is
a second incident.

Expect an acknowledgement in about a week. What happens after that depends on
severity: a fix that changes runtime behaviour is decided by the maintainer,
not by the finding. If a report turns out to be a design decision rather than
a defect, you will be told which decision and where it is recorded. Reports
that are valid but already known are pointed at their entry in the register
below.

There is no bug bounty.

## Scope

**In scope** - the bot's own code in this repository:

- command dispatch, admin authentication, and the authorization gates;
- the secret store and how credentials are read, stored, and logged;
- outbound HTTP handling, including the SSRF guards and the response size
  caps;
- inbound IRC parsing and anything echoed back into a channel;
- the audit log and its tamper evidence;
- on-disk state, its permissions, and the privacy and erasure commands;
- packaging and the CI supply-chain gates.

**Out of scope:**

- third-party APIs the bot calls, and any data they return. A weather or
  finance provider returning wrong or hostile data is handled by the bot's
  sanitization and size caps; a defect in *those* is in scope, a defect in the
  upstream service is not;
- the IRC server and network the bot connects to, including the ircd, its
  services, and channel operator behaviour;
- the host operating system, its Python installation, and the deployer's own
  configuration: an exposed metrics port, a weak admin password, a
  world-readable `config.ini`, an unprivileged account that is not
  unprivileged;
- denial of service by an admin against their own bot;
- findings from an automated scanner with no demonstrated impact on this
  codebase.

## Security controls in place

Summarized here; the full treatment, with the source symbols that implement
each control, is `docs/security-model.md`.

- **Authentication.** Admin access is password-gated with argon2id or bcrypt,
  with brute-force lockout and session binding. Hashes are verify-only;
  credentials that must go back on the wire are never hashed.
- **Authorization.** Admin commands check `_require_admin()` as their first
  statement, before argument parsing, and `.auth` and `.deauth` are refused in
  a channel by the dispatcher itself. Being a channel operator confers no
  authority.
- **Secrets.** Two-tier resolution, environment variable over a `[secrets]`
  section in a `config.ini` that is fail-closed at mode 0600. Outbound
  credential verbs are redacted from log output in both directions.
- **Network.** TLS to the IRC server with a version floor, and SSRF defenses
  on outbound HTTP: public-address resolution, blocked private and reserved
  ranges, and hard response size caps before a body is parsed.
- **Input handling.** Inbound IRC lines are length-bounded and validated;
  third-party and user text is stripped of control characters, CR, LF, and NUL
  before it reaches a bot-attributed line.
- **Audit.** Administrative actions are recorded to an HMAC-chained audit log
  with a key sidecar and a `verify()` path.
- **Privacy.** Modules that persist user data implement an erasure hook, so
  `.forgetme` reaches them. What is recorded, what leaves the machine, and
  what a user can erase is documented in `PRIVACY.md`.
- **Supply chain.** A hash-pinned lockfile installed with `--require-hashes`,
  `pip-audit --strict` on every push and weekly, bandit with a gating pass,
  CodeQL in `security-extended` mode, full-history gitleaks, GitHub Actions
  pinned to commit SHAs, and a wheel install-and-import gate.

Several of these controls have verified gaps. They are named in the register
below rather than glossed over here.

## Dependency policy

Runtime dependencies are declared in `requirements.txt` with **security
floors**: a lower bound at a release known free of the CVEs annotated beside
each entry, and no upper bound, so the newest compatible release is always
installed and upstream breakage surfaces on the next CI run across Python 3.10
through 3.14 on three operating systems.

`requirements.lock` is the hash-pinned resolution of that file, and CI installs
it with `--require-hashes`. It must be regenerated with
`scripts/regen-lockfile.sh`, which requires Python 3.10 so that transitive
dependencies gated below 3.11 are captured.

`pip-audit --strict` runs against the lockfile on push, on pull request, and
weekly. It carries one documented suppression, `PYSEC-2025-183`, a pyjwt
finding disputed by its maintainers that concerns the key size chosen by the
calling application; this project signs Apple WeatherKit JWTs with
Apple-issued keys, so the finding does not apply. Note the audit covers the
lockfile only and never the optional extras in `pyproject.toml`; the extras
floors are held to the `requirements.txt` policy by a check in
`tests/run_tests.py` instead.

Dependabot watches pip and GitHub Actions daily, with security updates grouped
separately from routine bumps so they can merge on their own. Secret scanning
and push protection are enabled on the repository.

Reporting a vulnerability in a dependency rather than in this code is welcome,
but report it upstream first; here, open an ordinary issue unless the bot's
particular use of that dependency is what makes it exploitable.

## Known unfixed issues

`docs/known-issues.md` is the permanent findings register. It is the single
authoritative list of verified defects that are known and not yet fixed,
including several with security impact, each with the symbol, the
verification, and the shape a fix would take.

Read it before reporting: a report matching an existing entry is a duplicate.
It is deliberately not duplicated here, because two lists of the same defects
drift apart and the stale one gets believed.
