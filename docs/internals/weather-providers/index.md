# Weather provider layer - implementation reference

The weather system is two layers. `modules/weather.py` owns the IRC surface
(command parsing, flags, formatting); everything below it is this package,
which selects providers, calls them, tracks their health, and normalizes what
they return into shared dataclasses.

Thirty-two providers are registered. The count is verified three ways: sub-package
directories on disk, `_reg()` factory calls in `weather_providers/__init__.py`,
and a pinned test (`tests/test_dispatcher.py - test_factory_count_is_32`).

## Core

- [init](init.md) - registry, factories, configuration wiring, quota accounting, and the end-to-end request trace
- [base](base.md) - normalized result dataclasses, derived fields, gap-fill boundary
- [dispatch](dispatch.md) - provider selection, reliability ranking, fallback, source attribution
- [health](health.md) - success/latency accounting and the circuit breaker
- [http](http.md) - size-capped HTTP with async and sync paths

## Providers

One page per registered provider, in [providers/](providers/index.md): general
forecast sources, specialists (air quality, pollen, UV), and government feeds
(tides, space weather, hazards).

```{toctree}
:hidden:
:maxdepth: 1

init
base
dispatch
health
http
providers/index
```
