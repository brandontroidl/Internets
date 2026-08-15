# translate.py - text translation via the unofficial Google Translate gtx endpoint

Keyless translation (121 lines) using Google's legacy web-client endpoint - not
an official API. Base contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.t` / `.translate` | `.t [src] <tgt> <text>` e.g. `.t en es Hello` | `[t] [detected→tgt] translated text` |

Argument parsing in `TranslateModule.cmd_translate()`: split into at most 3
words-plus-rest; if the first two words both match `_LANG_RE` (`^[a-z]{2}$`)
they are source and target, else if only the first matches it is the target
with auto-detected source. Anything else gets the usage line. Empty text and
text over `_MAX_QUERY_CHARS` (500) are rejected before any request.

## Integration

`_translate_sync()` (via `asyncio.to_thread`) does NOT use `fetch_json` - the
endpoint returns loose JSON arrays, and the module implements the sanctioned
inline stream + cap pattern instead:

- `GET https://translate.googleapis.com/translate_a/single` with
  `client=gtx, sl=<src|auto>, tl=<tgt>, dt=t, q=<text>` (requests encodes all
  params), hardcoded `User-Agent: Mozilla/5.0`, 10 s timeout, `stream=True`.
- Body read with `r.raw.read(_MAX_BODY_BYTES + 1)` and rejected over 256 KB
  (`_MAX_BODY_BYTES`) before `json.loads` - the in-code comment names the
  threat as a hostile MITM or a Google A/B interstitial.
- Response shape `[[ [chunk, ...], ... ], _, detected_lang, ...]` is walked
  defensively: every index and type is checked, translation chunks concatenated
  from `data[0][i][0]` strings only.
- The detected language from `data[2]` is re-validated against `_LANG_RE`
  before splicing (rendering `??` if it fails, e.g. region-tagged codes like
  `zh-CN`); the in-code comment names the reason - an upstream value like
  `"xx\r\nQUIT"` must not break PRIVMSG framing.
- Language codes are validated twice: at the handler (parse-time) and again at
  the top of `_translate_sync()` so a future caller bypassing the handler
  cannot smuggle path/query characters via `sl`/`tl`.

## Configuration

None. Keyless; base-default `is_configured()`. Note this is the one module in
the batch that does not use the shared `weather_user_agent` credential - it
sends a fixed browser-style `Mozilla/5.0` UA, presumably because the gtx
endpoint serves web clients.

## Failure behavior

Every validation failure and every exception path returns the flat string
`translation failed` (exceptions also log a warning); an empty translation
returns `empty result`. Nothing propagates to the handler.

## Security notes

The full user text is sent to Google - the largest privacy surface in this
batch (arbitrary channel-typed content, though never nick or channel name).
Output is `strip_ctrl`-capped at 400 chars. Hardcoded host, no SSRF surface.
The endpoint is unofficial and unauthenticated; heavy use risks IP throttling
or breakage without notice.

## Findings

- questionable | translate.py - `_translate_sync()` | The gtx endpoint is an
  unofficial, undocumented interface with no stability or ToS guarantee; the
  module is one upstream change away from silent breakage (mitigated by the
  defensive parse, which degrades to "translation failed").
- questionable | translate.py - `_LANG_RE` | Only bare 2-letter codes are
  accepted, so region-qualified targets (`zh-CN`, `pt-BR`) cannot be requested
  and a region-qualified detected source renders as `??`; the code comment
  documents this as a deliberate conservative choice.
- questionable | translate.py - `TranslateModule.cmd_translate()` | Rate-limit
  gate runs after usage/validation replies (same ordering issue as
  dictionary.py).
- test-gap | translate.py - `_translate_sync()` | No tests exist for this
  module; the defensive array-walk and the detected-language re-validation are
  exactly the shape-sensitive logic other modules cover with canned responses.
