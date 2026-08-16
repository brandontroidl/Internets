# geocode.py - location-string classifier and Nominatim/Zippopotam resolver

## Purpose

`modules/geocode.py` turns a free-form user location string ("92253",
"A1A 1A1", "39N 98W", "la quinta california", "08000 spain") into
`(lat, lon, display_name, country_code)` or `None`. It is a library, not a
command module: it defines no `BotModule` subclass and registers no commands
(`tests/test_help.py` lists it in `_SKIP` for exactly that reason). Its
callers are `modules/weather.py - WeatherModule._geo()` and
`modules/location.py - LocationModule` (regloc/myloc).

The design center is determinism over fuzziness: every input is CLASSIFIED
first (coordinates, postal code, or free text) and routed to the resolver that
matches the input as what it is, because Nominatim's free-text `q=` search
fuzzy-matches everything against everything (a US-pinned "08000" returned a
random Ohio motel, an unpinned "A1A 1A1" a Swiss street, an unparsed
"39N 98W" a Missouri suburb - each recorded in the code comments and now
locked in by tests).

## Responsibilities and boundaries

Belongs here: input classification, country inference, structured postal
resolution, coordinate parsing/reverse-geocoding, the free-text
settlement-vs-prominence search, display-name formatting, result caching, and
Nominatim usage-policy compliance (UA gate, bounded request counts, caching).

Deliberately not here: rate limiting per user (callers do that), saved
locations (store.py), weather providers, and any IRC I/O. The one weather
concern that leaks in - `us_state_code()` for alert-area widening - is here
because the state tables already live here.

## External integration

Three endpoints, all HTTPS to fixed hosts (no user-controlled URLs, so no
SSRF surface and no `_netsafe` involvement):

| Endpoint | Used by | Purpose |
|---|---|---|
| `nominatim.openstreetmap.org/search` | `_search_place()`, `_nominatim_postal()` | free-text and `postalcode=` structured search |
| `nominatim.openstreetmap.org/reverse` | `geocode()` coordinate branch | name a parsed coordinate pair |
| `api.zippopotam.us/<cc>/<code>` | `_zippo()` | keyless postal geocoder; the only source for Canadian codes (Canada Post data is proprietary to OSM) |

Auth: none. The Nominatim usage policy is the governing contract instead:

- Identifiable User-Agent with contact info - enforced by
  `geocode.py - _ua_has_contact()`; a UA without an `@domain` or `http(s)://`
  makes `geocode()` refuse to call out at all (fail closed, log warning),
  because generic UAs get the bot's IP banned for the whole channel. The UA
  comes from the caller (secret `weather_user_agent`, env override
  `INTERNETS_WEATHER_USER_AGENT`, fallback `[weather] user_agent`).
- Result caching required - the 24h TTL cache below.
- 1 req/s - approached by bounding per-query request counts (`_MAX_DROPS`
  caps the word-drop loop; the `stop` flag aborts retries on transport
  errors) plus the cache plus the callers' per-nick cooldowns. There is no
  process-wide throttle (see Findings).

Size caps: Nominatim bodies are read through
`geocode.py - _read_json_capped()` (streamed, capped at
`_MAX_BODY_BYTES` = 128 KB, invalid JSON -> None); Zippopotam goes through
`base.py - fetch_json()` with the same `max_bytes`. No bare `r.json()` or
unbounded reads anywhere in the file. Timeout 10s per request
(`geocode.py - _get()`).

What is sent upstream: only the (truncated, quoted-stripped) query string,
derived country pins, and the operator UA. Never the invoking nick or any
IRC context.

## The resolution pipeline (`geocode()`)

`geocode.py - geocode(query, user_agent, *, default_country="us")`, async.
Order matters; each stage returns (through the cache) without falling into
the next unless stated:

1. **Input hygiene** - reject None/empty; strip surrounding quotes; truncate
   to `_MAX_QUERY_CHARS` (200). `_normalize_cc()` coerces the
   operator-supplied `default_country` to a real two-letter code, falling
   back to `us` so a typo cannot disable the home bias or inject junk into
   `countrycodes`.
2. **UA gate** - `_ua_has_contact()` as above; fail closed.
3. **Cache lookup** - key `(query.lower(), user_agent, default_country)`
   (`_cache_key()`); `default_country` is in the key because the same bare
   numeric code legitimately resolves differently per home country.
4. **Coordinate passthrough** - `_parse_coords()` accepts decimal pairs
   ("34.5,-117.2"), hemisphere decimals ("39N 98W" in either order), and DMS
   ("39 50'15\"N 98 35'W"), normalizing to signed decimal with range
   validation. A bare "39 98" (no comma/sign/decimal point) is deliberately
   rejected as ambiguous. On a parse, the exact point is REVERSE-geocoded to
   name it; any reverse failure degrades to the coordinates themselves as the
   display name (never a lookup failure - the user gave exact coordinates).
5. **Postal path** - `_split_postal_country()` peels an explicit trailing
   country ("08000 spain" / "08000 es"), then `_postal_kind()` classifies the
   core; if it is a postal code, `_resolve_postal()` answers and the function
   RETURNS - a postal code never falls through to fuzzy free text, because
   that fallback is precisely what produced the wrong-country matches
   (`run_tests.py - "geocode: non-postal input never touches the postal
   resolvers"` proves the inverse direction).
6. **Settlement pass** - one `featureType=settlement` search on the full
   query. Free-text `q=` returns the best-ranked OSM object of ANY kind, so a
   business can outrank the place it was named after ("new york new york" ->
   the Las Vegas casino). A transport/parse failure here aborts the whole
   lookup (`stop=True`) rather than burning more requests
   (`run_tests.py - "a transport error on the settlement pass does not
   trigger a second request"`).
7. **Free-text word-drop loop** - unconstrained search on the query; on no
   hit, drop the last word and retry (recovers trailing-token typos:
   "la quinta caifornia" -> "la quinta"), capped at `_MAX_DROPS` = 4 drops
   (at most 5 free-text requests + 1 settlement request per query;
   `run_tests.py - "free-text word-drop loop is capped"`). The countrycodes
   pin is re-derived fresh for each candidate, which is how
   "tbilisi georgia" self-heals: "georgia" pins the full query to the US
   (miss), the word-drop retries "tbilisi" unpinned, which resolves globally.
8. **Settlement-vs-prominence arbitration** - when both passes hit, keep the
   settlement if it matched the FULL query and its OSM `importance` is >= the
   free hit's; a settlement match on the full query always beats a free hit
   found only after dropping tokens (the truncated query answers a different
   question). The comments pin the calibration cases: the casino loses to the
   city, but Graceland (importance 0.5087) must beat a minor township, so
   `importance` is used ONLY to rank two candidates for the same query, never
   as an absolute bar. If upstream ever stops returning `importance`, both
   scores are 0.0 and the tie goes to the settlement - documented as a
   silent partial regression in the comment. Tests:
   "settlement pass beats a POI top-hit", "a minor settlement never preempts
   a far more prominent landmark", "a settlement hit on the full query beats
   a word-dropped free-text hit", "a settlement miss falls back to
   unconstrained search".
9. Every terminal outcome, including `None`, passes through the local
   `_store()` closure into the cache.

## Country and US-state inference

- `_looks_like_us()` - a 5-digit ZIP shape (`_USZIP_RE`), a full US state
  name anywhere in the query (case-insensitive `_US_STATE_NAME_RE`), or an
  UPPERCASE standalone state abbreviation (`_US_STATE_ABBR_RE` - lowercase is
  deliberately excluded: "ca"/"or"/"in"/"la" are common words). Pins
  `countrycodes=us`. The known false positive - "tbilisi georgia" - is
  documented and self-heals via word-drop.
- `_country_code_for()` - only consulted when `_looks_like_us()` said no.
  `_COUNTRY_NAME_MAP` maps ~200 country names/aliases, common territories,
  Canadian provinces, and Australian states to ISO2 (so "london ontario" ->
  ca, "brisbane queensland" -> au). Design exclusions are commented per
  entry class: "georgia" is absent (US-state clash), lowercase 2-letter codes
  are absent (word clash), Australian state abbreviations are absent (WA vs
  Washington). Longest-first alternation so multi-word names win.
  `_CA_PROVINCE_ABBR_RE` (uppercase-only) catches "toronto ON".
- `us_state_code()` - whole-query-only match against `_STATE_QUERY` (state
  names + abbreviations, both lowercased - safe here because the ENTIRE query
  being "ms" is unambiguous in a way that "ms" inside a query is not).
  Used by `weather.py - cmd_alerts()` for state-wide alert widening.
  `run_tests.py - "geocode: us_state_code matches a bare state
  name/abbreviation only"` covers positives and the "jackson mississippi"
  negatives.

## Postal classification and resolution

`_postal_kind()` classifies by shape:

| Kind | Pattern | Meaning |
|---|---|---|
| `us` | `_ZIP4_RE` (ZIP+4) | unambiguously US |
| `ca` | `_CA_POSTAL_RE` (A1A 1A1) | globally unique |
| `uk` | `_UK_POSTAL_RE` | globally unique |
| `ie` / `jp` / `br` | Eircode / dashed 3-4 / dashed 5-3 | format-unique, kind doubles as ISO2 |
| `num` | `_NUM_POSTAL_RE` (4-10 digits) | shared across countries -> home-first |
| None | anything else | free text |

The comments record the disjointness proofs (CA always ends in a digit, UK in
two letters; Eircode's 4-char inward group separates it from both) and the
deliberate non-pinning of bare 7-digit JP / 8-digit BR codes (digit count is
not country-unique). A bare 5-digit ZIP is NOT `us` kind - it is `num`, and
the home-country default (typically `us`) provides the pin; that keeps
"08000" resolvable for a Spanish home country.

`_split_postal_country()` accepts a trailing country name (up to 3 tokens) or
a bare ISO2 from `_ISO2_OVERRIDES` - the ISO2 set minus every US-state and
CA-province abbreviation. "ca" is excluded (it means California in a
US/CA-centric bot), so "90210 ca" returns unchanged, stays on the free-text
path, and resolves the US ZIP rather than mis-pinning to Canada. The split
also requires the leading part to itself be a postal code, so
"london ontario" / "paris france" pass through untouched to the free-text
loop. Forcing an excluded country still works by full name ("90210 canada").
`run_tests.py - "ZIP + US-state abbreviation is NOT mis-read as a country
override"` locks this.

`_resolve_postal()` routes by kind:

- `ca` -> `_zippo("ca", _fsa(code))` - Zippopotam keyed by the 3-character
  Forward Sortation Area, the granularity free Canadian data actually has;
  Nominatim cannot do Canadian codes at all.
- `us` (ZIP+4) -> resolve the 5-digit base US-pinned via Nominatim
  `postalcode=`, Zippopotam backstop (the +4 is sub-ZIP granularity no free
  source carries).
- `uk` -> Nominatim pinned to the explicit hint or `gb`.
- `ie`/`jp`/`br` -> Nominatim pinned to the kind itself.
- `num` with explicit hint -> Nominatim then Zippopotam, pinned to the hint.
- `num` without hint -> home country first (Nominatim then Zippopotam), then
  ONE global unpinned Nominatim `postalcode=` search. This is the
  home-country-first contract: a real local ZIP stays local (43812 -> Ohio,
  never Barcelona), an invalid-at-home code falls through to the global best
  match (08000 -> Barcelona). Tests cover both directions plus the explicit
  override and the JP/BR/IE pins.

Structured `postalcode=` search matches the value AS a postal code, so a
bogus code returns nothing instead of a fuzzy nearest-object guess - the
whole point of this path; misses are cached negatives, not free-text retries.

`_zippo_parse()` fail-closes on any missing/oversize/unparseable field,
range-checks coordinates, trims Zippopotam's parenthetical FSA lists from
place names, and control-strips everything before it can reach an IRC line.

## Display-name formatting

`_format_name(addr, fallback)` builds "City, ST" (US, via `_STATE_ABBR`) or
"City, Country" (elsewhere) from Nominatim's `address` object. Two hardened
edge cases, both test-covered in `run_tests.py`:

- No city/town/village/county at all (parks, landmarks): take the feature's
  own name from the first component of `display_name` - but only when the
  fallback contains `", "` (comma-space), because the reverse-geocode path
  passes a bare "lat,lon" (comma, no space) as fallback and splitting that
  would print half a coordinate pair ("never truncates a coordinate fallback
  into half a pair").
- Every value is OSM-user-editable data; `_strip_ctrl()` (alias of
  `base.py - strip_ctrl` with a 160-char default from `_MAX_NAME_CHARS`)
  strips C0/DEL so a vandalized place name (`\r\nQUIT :pwned`, bold/color
  codes) cannot inject IRC protocol or spoof bot output. The 160 cap exists
  because Nominatim `display_name` can exceed 300 chars and the final IRC
  line must stay under 510 bytes with other content around it.

## TTL cache

Module-level `_geocode_cache`, an `OrderedDict` LRU (move-to-end on hit,
evict-from-front over `_GEOCODE_CACHE_MAX` = 1000) with
`_GEOCODE_CACHE_TTL` = 24h, guarded by a `threading.Lock` because entries are
touched from `asyncio.to_thread` worker threads as well as the loop. Both
positive results and `None` negatives are cached - a flood of identical bad
queries must not hammer Nominatim - with the TTL as the recovery path.
`geocode_cache_stats()` exposes size/hits/misses/evictions for ops surfaces.
Nothing is persisted; a restart empties the cache.

## Concurrency

`geocode()` and the resolver helpers are async; blocking `requests` calls are
offloaded with `asyncio.to_thread`. Two patterns coexist:

- `_zippo()` runs the entire `fetch_json` (connect + body read + parse) inside
  `to_thread` - fully off-loop.
- The Nominatim paths (`_search_place()`, `_nominatim_postal()`, the reverse
  branch) offload only `_get()` (connect + headers, `stream=True`) and then
  call `_read_json_capped(r)` - which performs the blocking `r.raw.read()` -
  on the event loop thread. See Findings.

Cache mutations are lock-serialized; the stats dict is only mutated under the
same lock. There is no per-host request serialization (see Findings).

## Failure behavior

Fail closed everywhere: transport error, oversize body, non-JSON, missing or
out-of-range lat/lon, unparseable rows all yield `None` (or `stop=True` in
`_search_place()`, which additionally suppresses retries so a failing
Nominatim is not hammered). The one deliberate fail-open is the coordinate
branch: user-supplied exact coordinates survive a reverse-geocode failure as
`(lat, lon, "lat,lon", "")`. All failures are logged at warning with the
query/code but no secrets.

## Functions (reference)

| Function | Role |
|---|---|
| `geocode()` | public entry; pipeline above |
| `us_state_code()` | whole-query US state matcher (alert widening) |
| `geocode_cache_stats()` | cache metrics snapshot |
| `_cache_key()` / `_cache_get()` / `_cache_put()` | TTL-LRU plumbing |
| `_ua_has_contact()` | Nominatim UA policy gate |
| `_strip_ctrl()` | OSM-string sanitizer (160-char default) |
| `_looks_like_us()` / `_country_code_for()` | country pinning |
| `_normalize_cc()` | operator home-country validation |
| `_postal_kind()` / `_split_postal_country()` / `_fsa()` | postal classification |
| `_resolve_postal()` / `_nominatim_postal()` / `_zippo()` / `_zippo_parse()` | structured postal resolution |
| `_valid_latlon()` / `_dms_to_deg()` / `_parse_coords()` | coordinate parsing |
| `_format_name()` | display-name construction |
| `_get()` / `_read_json_capped()` | HTTP + capped JSON read |
| `_search_place()` | one free-text/settlement Nominatim search |

## Implementation walk

- Lines 1-16: imports, logger.
- Lines 18-99: TTL cache (state management; design comment explains the key
  choice and the thread-safety requirement).
- Lines 101-137: policy/abuse constants and `_ua_has_contact()`,
  `_strip_ctrl()` (security enforcement).
- Lines 139-258: US state tables, ZIP/coordinate/postal regexes,
  `_looks_like_us()`, `us_state_code()` (classification data + validation).
- Lines 260-401: country map, `_COUNTRY_NAME_RE`, CA province abbreviations,
  `_country_code_for()` (classification).
- Lines 404-500: `_normalize_cc()`, `_postal_kind()`, `_ISO2_OVERRIDES`,
  `_split_postal_country()`, `_fsa()` (validation / business logic).
- Lines 503-584: `_zippo_parse()`, `_valid_latlon()`, `_dms_to_deg()`,
  `_parse_coords()` (parsing + validation).
- Lines 587-618: `_format_name()` (formatting + security).
- Lines 621-652: `_get()`, `_read_json_capped()` (resource management; the
  size cap is the JSON-bomb defense).
- Lines 654-747: structured postal resolvers (protocol processing).
- Lines 750-816: `_search_place()` (protocol processing; the `stop` tri-state
  is the request-budget control).
- Lines 819-1003: `geocode()` (control flow; the pipeline above, with the
  `_store` closure ensuring every exit is cached).

## Findings

- questionable | geocode.py - _read_json_capped() call sites | The Nominatim
  paths (`_search_place()`, `_nominatim_postal()`, the reverse branch in
  `geocode()`) offload only `_get()` to a worker thread and then execute the
  blocking `r.raw.read()` body read on the event loop thread; a slow upstream
  can stall the whole bot loop for up to the read timeout per request.
  `_zippo()` shows the correct pattern (entire fetch inside `to_thread`).
- questionable | geocode.py - geocode() / _store | A transport failure
  (`stop=True` from the settlement pass, or an exception in the coordinate
  reverse) is cached as a negative result with the same 24h TTL as a genuine
  "no such place", so one transient Nominatim outage marks a valid query
  "location not found" for a day.
- questionable | geocode.py - module level | Nominatim's 1 req/s policy is
  met by bounding per-query requests, caching, and caller-side per-nick
  cooldowns, but there is no process-wide throttle: several distinct users
  issuing uncached queries in the same second exceed the policy rate.
- test-gap | geocode.py - _cache_get() / _cache_put() | The TTL/LRU cache has
  no direct tests (expiry, eviction at the 1000 cap, negative-result
  round-trip); the geocode tests clear it as fixture hygiene but never
  exercise it.
