# Security Policy

The Internets IRC bot is a network-facing service: it connects to IRC, makes
outbound HTTP requests to third-party APIs, runs hot-reloadable command
modules, holds an admin/auth boundary, and feeds a honeypot to DNSBL threat
pipeline. Security reports are taken seriously.

## Supported Versions

Only the latest release and the `main` branch receive security fixes. There is
no back-porting to older tags.

| Version          | Supported          |
| ---------------- | ------------------ |
| latest release   | :white_check_mark: |
| `main`           | :white_check_mark: |
| older tags       | :x:                |

## Reporting a Vulnerability

Report privately. Do **not** open a public issue or pull request, and do not
disclose it in an IRC channel.

Use GitHub's private vulnerability reporting:

> **Security tab -> Report a vulnerability** ("Privately report a security
> vulnerability")

That opens a private advisory thread with the maintainer.

Where possible, include:

- the affected module / command / file and the version or commit hash,
- a minimal reproduction (the exact `.command` input, request, or state),
- the impact you can actually demonstrate (RCE, auth bypass, SSRF, secret or
  data leak, persistent injection, denial of service),
- logs or a small proof of concept.

What to expect (best effort, solo maintainer):

- acknowledgement, typically within about a week,
- an initial assessment: accepted, needs more info, or declined with a reason,
- a fix landed on `main` and coordinated disclosure once a patch exists.

## Scope

In scope (this repository's own code):

- command dispatch and the admin / auth boundary,
- secret handling and the two-tier secret store,
- the SSRF / `netsafe` DNS-pinning layer and outbound HTTP size caps,
- the persistence / store layer and data-integrity handling,
- the weather/geo, network, and threat-intel command modules.

Out of scope:

- third-party APIs the bot queries (report those to their vendors),
- a deployer's own misconfiguration: an exposed metrics endpoint, a weak admin
  password, a leaked or world-readable `config.ini`,
- findings that require an attacker who is already host-root or already an
  authenticated bot admin.

## Please do not, while testing

- run automated scanners or message floods against a live/production bot
  instance or IRC network,
- test against channels or users you do not control,
- exfiltrate data, pivot, or degrade service beyond the minimum needed to
  prove the issue.

Good-faith research that stays within this scope will not be pursued.

## Automated baseline

This repository continuously runs CodeQL (SAST), gitleaks (secret scanning),
Bandit, `pip-audit` (dependency CVEs), and Dependabot. A report that only
restates one of these tools' already-open findings is welcome but may already
be tracked.
