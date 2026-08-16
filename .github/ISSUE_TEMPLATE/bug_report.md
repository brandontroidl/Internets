---
name: Bug report
about: Report a defect or unexpected behaviour in the bot
title: "[Bug] "
labels: ["bug"]
assignees: ""
---

**Do NOT paste the contents of `config.ini` (especially the `[secrets]`
section) or `config.local.ini` into this issue.** If you are not sure
whether a value is sensitive, leave it out - a maintainer will ask if
it is needed.

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

The bot redacts credentials from its **logging** of IRC lines in both
directions, but that is all it covers: redaction matches credential
verbs (`PASS`, `IDENTIFY`, `AUTHENTICATE`, `OPER`) in log output, and it
never touches a reply the bot composed and sent to a channel. Tracebacks,
provider error text, and a pasted channel transcript can all still carry
an API key, a password, a hostmask, or private channel content. Read what
you are pasting and trim it.

<details>
<summary>Relevant log output (click to expand)</summary>

```
paste scrubbed log lines here
```

</details>

## Additional context

Anything else you think a maintainer should know - recent config
changes, modules enabled, related issues, etc.
