# Security policy

How to report a vulnerability in the Internets IRC bot, what to expect after
you do, and what is in scope. This page is about the reporting process. It does
not describe how the bot defends itself; that is `docs/security-model.md`.

## Reporting a vulnerability

Report privately through the repository **Security** tab, "Report a
vulnerability". That opens a GitHub private security advisory. Private
vulnerability reporting is enabled on this repository and is the only accepted
channel; the advisory thread is also where follow-up happens, so keep the
conversation there.

Do not use a public issue, a pull request, a discussion thread, or an IRC
message. `.github/ISSUE_TEMPLATE/config.yml` disables blank issues and points
here for exactly this reason.

Please do not disclose publicly while a report is open.

## Before you report

`docs/known-issues.md` is the permanent findings register: the single
authoritative list of verified defects that are known and not yet fixed,
several with security impact, each with the symbol, the verification, and the
shape a fix would take. Read it first. A report matching an existing entry is a
duplicate, and you will be pointed back at the entry.

Those defects are deliberately not restated here. Two lists of the same
findings drift apart and the stale one gets believed.

## What to include

- The command, module, or file involved, and the commit or tag you tested.
- How to reproduce it, with the minimum steps that show the effect.
- The impact you can actually demonstrate, separated from what you suspect.
- Whether a configured API key, an admin password, or stored user data is
  reachable, since that changes the severity considerably.

Redact your own credentials before sending. A report containing a live key is a
second incident.

## What happens next

Expect an acknowledgement in about a week. This is a single-maintainer project
and that is an expectation the maintainer has set, not a guarantee; the wider
support picture is in `docs/versioning-and-support.md`, "Support expectations".

After acknowledgement:

- Whether a finding becomes a fix is the maintainer's decision, not the
  reporter's. A fix that changes runtime behaviour is weighed on its own terms.
- If a report turns out to be a design decision rather than a defect, you will
  be told which decision and where it is recorded.
- If it is valid but already known, you will be pointed at its entry in
  `docs/known-issues.md`.
- Fixes land on `main` and reach users in the next tagged release.

There is no bug bounty.

## Supported versions

| Version | Supported |
|---|---|
| `main` | yes |
| Latest tagged release | yes |
| Any earlier tag | no |

There are no backport branches, so an older tag receives nothing: upgrade
rather than requesting a patch for a superseded version. `internets --version`
prints the running version.

## Scope

**In scope** - the bot's own code in this repository:

- command dispatch, admin authentication, and the authorization gates;
- the secret store and how credentials are read, stored, and logged;
- outbound HTTP handling, including the SSRF guards and the response size caps;
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

A vulnerability in a dependency rather than in this code should go upstream
first. Report it here only when the bot's particular use of that dependency is
what makes it exploitable; otherwise an ordinary issue is the right place. The
dependency pinning, audit, and response policy is in `docs/dependencies.md`.

## Where the defenses are documented

`docs/security-model.md` is the full treatment: the threat model, every control
with the source symbol that implements it, and the verified limits of each.
Read it there rather than from a summary here, because a summary of a security
control is exactly the kind of text that survives the control changing.

Related: `PRIVACY.md` for what user data is recorded and what a user can erase,
`docs/logging-and-auditing.md` for the audit chain, and
`docs/incident-response.md` for what an operator does when something has
already happened.
