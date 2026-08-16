# cocktail.py - cocktail recipe lookup via TheCocktailDB

Keyless wrapper around TheCocktailDB's public test tier. One class
`CocktailModule` on the shared [base](base.md) contract; blocking helper
`_fetch_sync()` via `asyncio.to_thread`. Structurally a twin of
[recipe.py](recipe.md).

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.cocktail <name>` (alias `.drink`) | `cocktail.py - CocktailModule.cmd_cocktail()` | `.cocktail margarita` | `<Drink> (<Category>, <Glass>) | <all "qty ingredient" pairs> | <instructions, first 200 chars>` |

## Integration

- Endpoint: `GET https://www.thecocktaildb.com/api/json/v1/1/search.php?s=<name>`
  (`cocktail.py - _fetch_sync()`) - `/1/` is the public test key (module
  docstring: unlimited for low volume). Timeout 10 s.
- Inline `requests.get(stream=True)` with 256 KB read cap (sanctioned form).
  Miss is a 200 with `"drinks": null` -> `no cocktail matched '<name>'`; first
  result wins.
- Ingredients are 15 parallel field pairs `strIngredient1..15` /
  `strMeasure1..15`, all joined (no 12-item cut like recipe.py; empty list
  renders `(no ingredient list)`). Instructions have newlines flattened and are
  truncated at 200 chars with `...`.
- Privacy: sends only the drink name.

## Configuration

No key; `is_configured()` True. Shared UA credential.

## Failure behavior

`requests.RequestException` -> `TheCocktailDB unavailable`; oversize ->
`TheCocktailDB response too large`; other exceptions ->
`TheCocktailDB response parse error`. Rate limit check before the thread spawn.

## Security notes

Size cap before parse; reply passed once through `_strip_ctrl()` (400-char
cap).

## Findings

None beyond the batch-wide `_strip_ctrl` alias duplication (see mtg.md).
