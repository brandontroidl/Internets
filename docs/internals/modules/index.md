# Command modules - implementation reference index

One page per module under `modules/`. Each page carries the module's command
table, upstream integration, configuration key, failure behavior, and findings.
Command inventories are not duplicated here: they are generated from actual
registration by `scripts/gen-command-reference.py` (see the command reference in
the main documentation), whose `--check` mode gates drift.

## Infrastructure (no commands)

- [base](base.md) - BotModule contract, `fetch_json` size-capped HTTP, `resolve_public`, `cred`
- [_netsafe](_netsafe.md) - SSRF guard: `safe_open`, DNS pinning, private-range blocking
- [units](units.md) - shared weather formatting helpers
- [geocode](geocode.md) - location classifier, Nominatim/Zippopotam resolution, cache
- [example](example.md) - annotated template module for new development

## Weather and space

- [weather](weather.md) - 15 command families over the provider aggregation layer
- [location](location.md) - saved locations (.regloc/.myloc/.delloc)
- [astro2](astro2.md) - solar activity, NEOs, launches, moon phase, sky
- [iss](iss.md) - ISS position and crew
- [apod](apod.md) - NASA picture of the day
- [spacex](spacex.md) - upcoming launches
- [satpass](satpass.md) - satellite passes over a location

## Science and math

- [mathx](mathx.md) - primality, factoring, bases, stats, big numbers
- [calc](calc.md) - sandboxed expression evaluator
- [physcalc](physcalc.md) - physics/electronics calculators
- [numberfact](numberfact.md) - number trivia and on-this-day facts
- [scinews](scinews.md) - curated science feed reader
- [hn](hn.md) - Hacker News front page
- [reddit](reddit.md) - subreddit headlines
- [xkcd](xkcd.md) - xkcd comics

## Developer, network, security

- [devtools](devtools.md) - jwt, semver, uuid5, tz, unix, color, cron
- [devutils](devutils.md) - b64, hex, morse, uuid, epoch
- [encode](encode.md) - encodings, hashes, generators
- [netcalc](netcalc.md) - CIDR/subnet/port math
- [dnsutils](dnsutils.md) - DoH lookups, rDNS, whois/RDAP, ASN
- [ipinfo](ipinfo.md) - IP geolocation
- [ipintel](ipintel.md) - IP reputation (.ip/.rep)
- [secinfo](secinfo.md) - CVE, HIBP, hash-id, CVSS, cipher info
- [probe](probe.md) - headers, TLS cert, TCP, reachability
- [httpcode](httpcode.md) - HTTP status reference

## Reference and lookup

- [reflookup](reflookup.md) - wiki, doi, isbn, so, rfc, rtfm, arxiv, element
- [dictionary](dictionary.md) - dictionary definitions
- [urbandictionary](urbandictionary.md) - Urban Dictionary
- [translate](translate.md) - translation (unofficial endpoint)
- [search](search.md) - web search (DDG scrape / Brave API)
- [scholar](scholar.md) - OpenAlex/ORCID scholarly search
- [pkginfo](pkginfo.md) - PyPI/npm/crates package info
- [ghinfo](ghinfo.md) - GitHub repo info

## Media and finance

- [imdb](imdb.md) - OMDb film/TV lookup
- [lastfm](lastfm.md) - Last.fm now-playing/profile
- [youtube](youtube.md) - YouTube search
- [mtg](mtg.md) - Magic cards (Scryfall)
- [poke](poke.md) - Pokemon (PokeAPI)
- [dnd](dnd.md) - D&D 5e SRD
- [recipe](recipe.md) - TheMealDB
- [cocktail](cocktail.md) - TheCocktailDB
- [steam](steam.md) - Steam profiles/games, nick registry
- [twitch](twitch.md) - Twitch streams (OAuth client-credentials)
- [idlerpg](idlerpg.md) - IdleRPG player lookup
- [crypto](crypto.md) - CoinGecko prices
- [fx](fx.md) - ECB exchange rates
- [stocks](stocks.md) - stock/crypto quotes with provider failover

## Social and utility

- [remind](remind.md) - scheduled reminders
- [tell](tell.md) - offline messages
- [seen](seen.md) - last-seen tracking
- [notes](notes.md) - per-user notes
- [channels](channels.md) - join/part/users, ChanServ ownership verification
- [urls](urls.md) - shorten/expand (is.gd)
- [privacy](privacy.md) - opt-out and .forgetme
- [linktitle](linktitle.md) - passive URL title announcement
- [qdb](qdb.md) - quote database lookup
- [health](health.md) - bot self-check

## Fun

- [bofh](bofh.md) - excuse generator
- [cowsay](cowsay.md) - cowsay
- [fact](fact.md), [catfact](catfact.md), [chuck](chuck.md), [dadjoke](dadjoke.md),
  [advice](advice.md), [bored](bored.md) - one-endpoint joke/fact APIs
- [games](games.md) - coin, 8ball, rps, choose
- [dice](dice.md) - dice roller
- [fml](fml.md) - FML quotes (HTML scrape)
- [qr](qr.md) - QR code link generator

```{toctree}
:hidden:
:glob:

*
```
