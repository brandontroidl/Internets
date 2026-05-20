# Security policy

Thank you for helping keep **Internets** and its users safe. This document
explains how to report a vulnerability, what response you can expect, and
what is in and out of scope.

## Supported versions

Only the **current minor release line** receives security fixes. At time
of writing that is the **2.5.x** line. Older minors are not patched —
please upgrade to the current minor before reporting.

| Version | Supported |
| ------- | --------- |
| 2.5.x   | Yes       |
| < 2.5   | No        |

The running version is recorded in [`pyproject.toml`](./pyproject.toml)
and exposed at runtime as `config.__version__`.

## Reporting a vulnerability

**Please do not open a public GitHub issue, Discussion, or pull request
for security problems.**

### Preferred channel

Use **GitHub Private Vulnerability Reporting** via the repository's
**Security tab**:
<https://github.com/brandontroidl/Internets/security/advisories/new>.
This keeps the report private until a fix is ready and lets us coordinate
a CVE if appropriate.

### Backup channel

If Private Vulnerability Reporting is unavailable for any reason, reach
the maintainer through the GitHub profile linked from the repository
Homepage in [`pyproject.toml`](./pyproject.toml) (currently
<https://github.com/brandontroidl/Internets>). No personal email is
published here on purpose; please use the GitHub-based channels above.

When you report, please include:

- The bot version (`internets --version` or `config.__version__`)
- Your Python version
- Your operating system and version
- Steps to reproduce
- Expected vs. actual behaviour
- A relevant log snippet, with any credentials or tokens scrubbed

## Response timeline

The maintainers will make a best effort to:

- **Acknowledge** your report within **7 days** of receipt.
- Provide a **mitigation plan** within **30 days** of acknowledgement.

Complex issues may take longer; we will keep you informed if so.

## Scope

**In scope**

- Code in this repository — the bot core, modules under `modules/`,
  helper scripts under `scripts/`, and packaging metadata.
- Configuration templates shipped in this repo (`config.ini`,
  `secrets.ini.example`).
- Deployment guidance in `README.md` and related docs.

**Out of scope**

- Third-party APIs the bot calls (weather providers, external services).
  Please report those to their respective vendors.
- The IRC network the bot connects to, its services, or other clients.
- The operator's host operating system, Python interpreter, container
  runtime, or local network configuration.

If you are unsure whether something is in scope, report it and we will
triage.

## Coordinated disclosure

Please hold public disclosure until **a fix has shipped** or **90 days
have elapsed from your initial report**, whichever comes first. If you
would like credit, we will name you in the release notes that accompany
the fix; if you prefer to remain anonymous, just say so in the report.

## Related documents

- [`THREAT_MODEL.md`](./THREAT_MODEL.md) — assets, trust boundaries, and
  threats considered during design.
- [`PRIVACY.md`](./PRIVACY.md) — what data the bot stores, logs, and
  transmits, and how it is protected.
