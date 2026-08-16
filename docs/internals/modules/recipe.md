# recipe.py - recipe lookup via TheMealDB

Keyless wrapper around TheMealDB's public test tier. One class `RecipeModule`
on the shared [base](base.md) contract; blocking helper `_fetch_sync()` via
`asyncio.to_thread`. Structurally a twin of [cocktail.py](cocktail.md).

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.recipe <name>` (alias `.meal`) | `recipe.py - RecipeModule.cmd_recipe()` | `.recipe arrabiata` | `<Meal> (<Area> <Category>) | <up to 12 "qty ingredient" pairs, then ", + N more"> | <source or YouTube link>` |

## Integration

- Endpoint: `GET https://www.themealdb.com/api/json/v1/1/search.php?s=<name>`
  (`recipe.py - _fetch_sync()`) - the `/1/` path segment is the public test
  key. Timeout 10 s.
- Inline `requests.get(stream=True)` with 256 KB read cap (sanctioned form).
  A miss is a 200 with `"meals": null`, mapped to `no recipe matched '<name>'`;
  first result wins.
- Ingredients come as 20 parallel field pairs `strIngredient1..20` /
  `strMeasure1..20`; the loop joins non-empty pairs, shows the first 12, and
  counts the rest. Link prefers `strSource` over `strYoutube`, else empty
  (leaving a trailing `| ` in the reply).
- Privacy: sends only the dish name.

## Configuration

No key; `is_configured()` True. Shared UA credential.

## Failure behavior

`requests.RequestException` maps to `TheMealDB unavailable`; oversize to
`TheMealDB response too large`; other exceptions to
`TheMealDB response parse error`. Rate limit check before the thread spawn.

## Security notes

Size cap before parse; reply passed once through `_strip_ctrl()` (400-char
cap, which is the effective truncation for long ingredient lists).

## Findings

None beyond the batch-wide `_strip_ctrl` alias duplication (see mtg.md).
