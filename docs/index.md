# Internets

A modular IRC bot and multi-provider weather aggregator built on Python's
`asyncio` and RFC 2812. It provides worldwide weather (current, forecast,
hourly, nowcast, air quality, UV, pollen, astronomy, alerts, wildfire, space
weather, marine, tides, historical), stock/crypto/FX prices, movie and music
lookups, dictionary and reference tools, a large developer / encoding /
network / security toolkit, IP geolocation and reputation, curated news feeds,
and stateful IRC-native tools (seen, tell, remind, notes). Modules load,
unload, and reload without restarting the bot.

- **Platform support:** Linux, macOS, FreeBSD, Windows, WSL/WSL2, Cygwin, MinGW, MSYS2
- **Python:** 3.10+ (CI runs 3.10 through 3.14)
- **License:** ISC
- **Source:** <https://github.com/brandontroidl/Internets>

This site collects the project's narrative guides alongside a full API
reference generated from the source.

```{toctree}
:maxdepth: 2
:caption: Getting started

getting-started
```

```{toctree}
:maxdepth: 2
:caption: Guides

architecture
configuration
modules
providers
security-model
deployment
```

```{toctree}
:maxdepth: 2
:caption: API Reference

autoapi/index
```
