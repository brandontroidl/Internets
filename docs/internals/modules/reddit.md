# reddit.py - top subreddit post (`.reddit` / `.r`)

Keyless wrapper around old.reddit.com's public JSON listing. Base contract:
[base](base.md).

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.reddit`, `.r` | `<sub> [period]` | Top post of the subreddit for the period (hour/day/week/month/year/all, default day): title, score, comment count, author, permalink. |

Handler: `reddit.py - RedditModule.cmd_reddit()` (both names map to it). The sub
argument tolerates `r/` or `/r/` prefixes (`lstrip("/").removeprefix("r/")`), then
must match `_VALID_SUB` = `^[A-Za-z0-9_]{1,21}$`; invalid names and invalid periods
reply with an error before any network I/O.

## Integration

`GET https://old.reddit.com/r/<sub>/top.json?t=<period>&limit=1` via the
module-local inline stream+cap in `reddit.py - _fetch_sync()`: timeout 10 s, cap
`_MAX_BODY_BYTES` = 512 KiB, `Accept: application/json`, and critically
`allow_redirects=False` - reddit redirects private/banned subs, and following
redirects would leak requests to arbitrary reddit-chosen locations and confuse the
status mapping. Status handling before `raise_for_status`:

| Status | Reply |
|---|---|
| 404 | "no subreddit r/<sub>" |
| 403 | "r/<sub> is private or quarantined" |
| 301/302/303 | "r/<sub> redirected - likely private or banned" |

The UA is the configured weather UA; the module docstring notes reddit aggressively
403s default UAs, so the contact-email-bearing weather UA is deliberate. Sent
upstream: subreddit name and period (user-chosen, validated), the UA. No nick.

## Configuration

Keyless; `is_configured()` unconditionally True. `on_load()` reads only
`weather_user_agent` (env `INTERNETS_WEATHER_USER_AGENT`).

## Failure behavior

Transport errors -> "reddit unavailable"; over-cap -> "reddit response too large";
JSON/shape errors (broad except) -> "reddit response parse error"; empty listing ->
"r/<sub> returned no posts". Never raises to the dispatcher.

## Security notes

- The subreddit regex is the injection guard: it is applied after prefix stripping
  and before the name is interpolated into the URL path, so no traversal or
  query-smuggling characters can reach the request.
- `allow_redirects=False` doubles as an SSRF-adjacent hardening: the bot never
  follows a location header from this endpoint.
- Post title/author pass `strip_ctrl` (cap 400) before IRC; the permalink is
  re-rooted onto `https://old.reddit.com` rather than trusting `p["url"]` when a
  permalink exists.

## Findings

- questionable | `reddit.py - RedditModule.cmd_reddit()` | The per-nick rate-limit
  check runs after the usage/validation replies (unlike every other module in this
  batch, which gates first), so a flooding user can elicit unlimited usage/error
  privmsg lines that bypass the limiter; only the network fetch is throttled.
- test-gap | `reddit.py` | No test file exercises this module (sub validation,
  redirect/403/404 mapping, permalink re-rooting).
