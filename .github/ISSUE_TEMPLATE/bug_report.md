---
name: Bug report
about: Report a defect or unexpected behaviour in the bot
title: "[Bug] "
labels: ["bug"]
assignees: ""
---

**Do NOT paste the contents of `secrets.ini` or `config.local.ini` into
this issue.** If you are not sure whether a value is sensitive, leave it
out — a maintainer will ask if it is needed.

If you believe the bug has security impact, stop here and follow
[`SECURITY.md`](../../SECURITY.md) instead of filing a public issue.

## Summary

A clear, one- or two-sentence description of the problem.

## Steps to reproduce

1.
2.
3.

## Expected behaviour

What you thought would happen.

## Actual behaviour

What actually happened, including any error messages.

## Environment

- Bot version (`internets --version` or `config.__version__`):
- Python version (`python --version`):
- Operating system and version:
- IRC network (e.g. Libera.Chat):
- ircd type, if known (e.g. solanum, InspIRCd):

## Logs

The bot scrubs credentials from its **outbound IRC** logging automatically,
but please double-check before pasting — local logs, tracebacks, and
manual copy/paste can still contain tokens, passwords, hostmasks, or
private channel content. Trim what you do not need.

<details>
<summary>Relevant log output (click to expand)</summary>

```
paste scrubbed log lines here
```

</details>

## Additional context

Anything else you think a maintainer should know — recent config
changes, modules enabled, related issues, etc.
