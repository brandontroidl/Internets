# numberfact.py - number trivia via local math + Wikipedia REST (.numberfact / .nf)

## Purpose

Number facts in four flavors: `math` computed locally, `trivia` / `date` /
`year` fetched from Wikipedia's REST API. The module docstring records the
history: it replaces numbersapi.com, which was sold off in 2025 (now a 301 to a
404ing publisher domain). Wikipedia results that are missing or boilerplate
degrade to the local `math_fact()` so the user always gets something. Base
contract: [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `numberfact` (alias `nf`) | `.numberfact <n\|random\|MM/DD> [trivia\|math\|date\|year]` | `42: <Wikipedia extract...>` / `On May 20: in 1927, ...` / `120 is a factorial: 5! = 120.` |

Default query is `random`, default type `trivia`; an `MM/DD` argument forces
type `date`. `is_configured()` returns `True` - the module is always available
(Wikipedia needs no key).

## External integration

- Endpoints (fixed hosts, HTTPS, no user-controlled URL parts):
  - `https://en.wikipedia.org/api/rest_v1/page/summary/{slug}` where slug is
    `f"{n}_(number)"` (trivia) or `str(year)` (year) - `n`/`year` are already
    parsed `int`s, so no injection or SSRF surface.
  - `https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{mm}/{dd}` with
    zero-padded validated ints.
- Auth: none. The User-Agent is the shared `weather_user_agent` credential
  (`cred(cfg, "weather_user_agent", "weather", "user_agent", "Internets/1.0")`,
  resolved in `NumberfactModule.on_load()`; `INTERNETS_WEATHER_USER_AGENT` env
  override per the [base](base.md) secret model). Optional; defaults to
  `Internets/1.0`.
- Timeout 10 s, `stream=True`, and the body is read through
  `numberfact.py - _read_capped()` with `_MAX_BODY_BYTES = 4 MiB` - the
  inline stream+cap pattern the repo's HTTP-size policy requires (the comment
  notes the onthisday feed alone can be ~1.5 MB). The `with requests.get(...)`
  form releases the streamed socket on every exit path (in-code comment).
- Privacy: only the number/date is sent; no nick, no location.

## Fetch/parse pipeline

All three fetchers are blocking `requests` functions run via
`asyncio.to_thread` from `cmd_numberfact()`:

- `numberfact.py - _fetch_trivia_sync()`: 404 -> `math_fact(n)`; over-cap ->
  "Wikipedia response too large"; empty extract or the "N is the natural number
  following..." boilerplate (`_BOILERPLATE_RE`) -> `math_fact(n)`; else the
  extract truncated to 300 chars (`_truncate()`, whitespace-collapsed,
  ellipsis).
- `numberfact.py - _fetch_year_sync()`: same shape for `page/summary/<year>`.
- `numberfact.py - _fetch_date_sync()`: picks one random event from the
  `events` array (`_rng.choice`) and formats `On <Month> <d>: in <year>, ...`
  truncated to 280.

Randomness uses `random.SystemRandom` throughout - the comment is explicit that
this is not security-motivated but avoids per-line `# nosec B311` for Bandit's
codebase-wide query.

## Local math facts

`numberfact.py - math_fact()` is deterministic: it computes every applicable
predicate then returns the single highest-priority fact. Priority (documented
in-code with rationale): factorial (k>=3) > perfect cube > small prime (<13) >
Fibonacci > prime > palindrome > high power of two (2^6+) > perfect square >
low power of two > triangular > power of ten > factorization + divisor-count
fallback. Special-cases 0, 1, -1; negatives get a fact rooted in |n|.
Predicates use integer-exact methods: `math.isqrt` for squares/triangulars
(`8n+1` square test), binary search for cubes, the `5n^2 +/- 4` identity for
Fibonacci, O(sqrt n) trial division for primality/factorization/divisors.

## Input validation and DoS bounds

- `_MAX_ABS_N = 10**12` caps user numbers for math, trivia and year paths; the
  in-code comment records the reason - `math_fact()` is O(sqrt n) and an
  uncapped 19-digit input burned ~90 s of CPU; the cap keeps every path at
  <= 1e6 loop iterations.
- Dates validated by `_parse_date()` (MM 1-12, DD within `_DAYS_IN_MONTH`,
  Feb allows 29).
- `random` picks: n in 1..2000, year in 1500..2026, date any valid MM/DD.
- Non-numeric first tokens (other than `random` or `MM/DD`) and unknown type
  tokens get usage replies.

## Failure behavior

`requests.RequestException` -> "Wikipedia unavailable"; any other exception in
fetch/parse -> "Wikipedia response parse error"; both log a warning and
neither raises to the dispatcher. 404 and boilerplate degrade to local math
facts rather than an error. All replies pass through `strip_ctrl()` (the
module's `_strip_ctrl` alias) - the Wikipedia extract is third-party text.

## Concurrency and state

No persistent or cached state; `_ua` is resolved once at `on_load()`. All
network work is off-loop via `to_thread`. The local-math path
(`t == "math"`) calls `math_fact()` synchronously on the event loop - up to
~1e6-iteration loops, tens of milliseconds at the 1e12 cap - a noted, bounded
trade (the trivia fallback path runs the same function inside the worker
thread instead).

## Findings

- doc-drift | tests/test_numberfact.py | The test file's docstring and its
  `ATTEMPTS = 40` retry harness assume `math_fact()` "may randomly choose among
  applicable facts"; the implementation is fully deterministic
  (priority-ordered), so the retry loop is dead weight - tests still pass but
  document behavior that does not exist.
- questionable | numberfact.py - cmd_numberfact() | The math path runs
  `math_fact()` on the event loop while the trivia path runs the identical
  function in a worker thread; a `.nf 999999999999 math` costs tens of
  milliseconds of loop stall that the trivia spelling avoids. Bounded by
  `_MAX_ABS_N`, so cosmetic rather than a DoS.
- test-gap | tests/test_numberfact.py | Only `math_fact()` is tested; the three
  fetchers, the boilerplate-fallback regex, `_parse_date()`, `_read_capped()`
  and the argument-parsing branches of `cmd_numberfact()` have no coverage.
