# pkginfo.py - package registry lookups (PyPI / npm / crates.io)

Keyless registry metadata (271 lines) for the three big language ecosystems,
one command each, with an explicit package-name validator guarding the URL
path. Base contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.pypi` | `.pypi <package>` | `**name** version :: summary :: license L released YYYY-MM-DD :: url` |
| `.npm` | `.npm <package>` | `**name** version :: description :: license L published YYYY-MM-DD` |
| `.crates` | `.crates <name>` | `**name** version :: description :: N downloads license L :: docs/homepage` |

All three handlers share the skeleton: rate-limit gate first, usage line on
empty arg, clip to `_MAX_INPUT` (120), reject via `_valid_pkg()` with
`invalid package name`, then the sync worker via `asyncio.to_thread`.

## Input validation

`_valid_pkg()` = `_PKG_RE` (`^[A-Za-z0-9._@/-]{1,100}$`) AND no `..`
substring. The in-code comment states why both are needed: the charset regex
alone admits `..` (every character is individually allowed), so the explicit
substring check is what blocks path-traversal segments inside the trusted
registry URL. Names are additionally percent-encoded with
`quote(name, safe="")` at the call site, so `@scope/pkg` becomes
`@scope%2Fpkg` (the encoded form the npm registry expects for scoped
packages). The pattern mirrors ipinfo.py / ipintel.py.

Validation companion `tests/test_pkginfo_validate.py` pins exactly this
contract: rejects `../etc`, `a/../b`, an embedded NUL (`req\x00uests`), and
the empty string; accepts `requests` and `@scope/pkg`.

## Integration

All three via `base.fetch_json`, 10 s timeout, `allow_404=True` (404 = miss),
shared UA (the `_crates_sync()` docstring notes crates.io requires a
descriptive User-Agent):

| Worker | Endpoint | Cap | Parsing notes |
|---|---|---|---|
| `_pypi_sync()` | `GET https://pypi.org/pypi/<pkg>/json` | default 256 KB | version/summary/license from `info`; URL from `project_url` > `home_page` > `package_url`; release date = `upload_time_iso_8601` (or `upload_time`) of the current version's FIRST file, date part only - an approximation of "release date" |
| `_npm_sync()` | `GET https://registry.npmjs.org/<pkg>` | 1 MB (`_NPM_MAX_BYTES` - the full-history registry document is large) | latest from `dist-tags.latest`; `license` may be a string or `{type: ...}` dict, both handled; publish date from `time[latest]` |
| `_crates_sync()` | `GET https://crates.io/api/v1/crates/<name>` | 2 MB (popular crates carry many versions) | version from `crate.max_version` > `newest_version`; license lives on `versions[0]`, not the crate root; downloads formatted with thousands separators inside its own try/except; link = `documentation` > `homepage` > `repository` |

Output fields are sanitized per-field with `strip_ctrl` and clipped by
`_clip()` (strip_ctrl + hard cap with a `…` ellipsis on truncation).

## Configuration

None. Keyless; `is_configured()` returns `True`. `on_load()` resolves the
shared `weather_user_agent` credential.

## Failure behavior

Misses (`None` from `allow_404`, or a non-dict payload) return
`<registry>: '<name>' not found`. The except clause is a specific tuple
(`requests.RequestException, ResponseTooLarge, KeyError, ValueError,
TypeError`) - unlike the bare-`Exception` modules in this batch - logging a
warning and returning `<registry>: lookup failed`. Behavioral evidence:
`tests/test_pkginfo.py` covers happy, not-found, malformed-payload,
exception, and control-character-stripping paths for all three workers plus
`_clip()` truncation.

## Security notes

- The only user input reaching a URL path is triple-guarded: length clip,
  `_valid_pkg()`, and `quote(safe="")`. Everything else about the request is
  constant.
- Hardcoded registry hosts; no SSRF surface.
- Privacy: package name and UA only; no nick/channel.
- Upstream description/summary/license text is third-party-author controlled
  (registry metadata); `strip_ctrl`/`_clip` sanitize before IRC.

## Findings

- questionable | pkginfo.py - `_pypi_sync()` | The "released" date is the
  upload time of the first file listed for the current version; file order in
  the PyPI `releases` map is not a documented contract, so the date can be the
  wheel or the sdist arbitrarily (same day in practice, but the field is an
  approximation presented as fact).
- questionable | pkginfo.py - `import requests` (module top) | requests is
  imported eagerly only to name `requests.RequestException` in except clauses,
  while `base.py` deliberately lazy-imports it to stay importable in minimal
  test environments; harmless here but inconsistent with the base pattern.
