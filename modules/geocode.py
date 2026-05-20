from __future__ import annotations

import asyncio
import json
import re
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional

import requests

log = logging.getLogger("internets.geocode")

# ---------------------------------------------------------------------------
# TTL cache (per Nominatim ToS)
# ---------------------------------------------------------------------------
# Nominatim's usage policy explicitly requires clients to cache results.
# We cache by ``(lowercased-query, user_agent)`` because the country-pin
# logic above is fully determined by the query string — same query → same
# upstream call, every time.  The UA is part of the key so two operators
# sharing a single process (unlikely, but possible during reloads) don't
# cross-contaminate each other's cache.
#
# Implementation: an LRU-ish ``OrderedDict`` keyed by the cache key.
# ``move_to_end`` on hit; evict from the front when we hit the cap.
# A ``threading.Lock`` serialises mutations — geocode() is awaited via
# ``asyncio.to_thread`` so cache access happens from a worker thread.
#
# The cache stores a 4-tuple result (lat, lon, name, cc) or the sentinel
# ``None`` (negative result) — both are valid and worth caching.

_GEOCODE_CACHE_TTL = 24 * 60 * 60   # 24h, per Nominatim ToS
_GEOCODE_CACHE_MAX = 1000           # bounded — memory cap
_geocode_cache: "OrderedDict[tuple[str, str], tuple[float, Optional[tuple[float, float, str, str]]]]" = OrderedDict()
_geocode_cache_lock = threading.Lock()
_geocode_cache_stats = {"hits": 0, "misses": 0, "evictions": 0}


def _cache_key(query: str, user_agent: str) -> tuple[str, str]:
    return (query.strip().lower(), user_agent)


def _cache_get(key: tuple[str, str]) -> tuple[bool, Optional[tuple[float, float, str, str]]]:
    """Return ``(found, value)``.  ``found`` is False on miss or expiry."""
    now = time.time()
    with _geocode_cache_lock:
        entry = _geocode_cache.get(key)
        if entry is None:
            _geocode_cache_stats["misses"] += 1
            return (False, None)
        ts, value = entry
        if now - ts > _GEOCODE_CACHE_TTL:
            # Expired — drop and treat as miss.
            del _geocode_cache[key]
            _geocode_cache_stats["misses"] += 1
            return (False, None)
        # LRU touch.
        _geocode_cache.move_to_end(key)
        _geocode_cache_stats["hits"] += 1
        return (True, value)


def _cache_put(key: tuple[str, str], value: Optional[tuple[float, float, str, str]]) -> None:
    now = time.time()
    with _geocode_cache_lock:
        if key in _geocode_cache:
            _geocode_cache.move_to_end(key)
        _geocode_cache[key] = (now, value)
        # Evict oldest entries until we're under the cap.
        while len(_geocode_cache) > _GEOCODE_CACHE_MAX:
            _geocode_cache.popitem(last=False)
            _geocode_cache_stats["evictions"] += 1


def geocode_cache_stats() -> dict[str, int]:
    """Return a snapshot of cache statistics.

    Keys:
      * ``size``       — current number of cached entries
      * ``hits``       — successful lookups since process start
      * ``misses``     — lookups that fell through to the network
      * ``evictions``  — entries dropped due to the size cap

    Useful for status output / ops dashboards.
    """
    with _geocode_cache_lock:
        return {
            "size": len(_geocode_cache),
            "hits": _geocode_cache_stats["hits"],
            "misses": _geocode_cache_stats["misses"],
            "evictions": _geocode_cache_stats["evictions"],
        }

# ---------------------------------------------------------------------------
# Security / abuse-control constants
# ---------------------------------------------------------------------------
# Nominatim's usage policy requires a unique, contactable User-Agent.  We
# refuse to call out if the configured UA looks like the default template
# placeholder — sending generic UAs gets the bot's IP banned (and is also
# a poor neighbour move).  The check below is intentionally permissive:
# any "@" or "http" inside the UA is treated as a contact identifier.
def _ua_has_contact(ua: str) -> bool:
    """Return True if the UA appears to embed an email or URL."""
    if not ua:
        return False
    lower = ua.lower()
    return ("@" in ua and "." in ua.split("@", 1)[1]) or "http://" in lower or "https://" in lower


# Cap the JSON we read from Nominatim — normal responses are <10 KB; this
# bounds memory if upstream misbehaves or a TLS-stripping proxy injects HTML.
_MAX_BODY_BYTES = 128 * 1024

# Cap display name length we emit downstream so a long upstream string
# (Nominatim sometimes returns >300 char ``display_name`` fields) can't
# blow past the 510-byte IRC line limit when combined with other context.
_MAX_NAME_CHARS = 160

# Cap the user's raw query length.  Anything longer is junk and also
# inflates the cost of the upstream request.
_MAX_QUERY_CHARS = 200

_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: object, max_len: int = _MAX_NAME_CHARS) -> str:
    """Drop IRC control bytes from upstream strings and cap length.

    Nominatim ``display_name`` and ``address.*`` values are user-editable
    OSM data: anyone with an OSM account can put ``\r\nQUIT :pwned`` in a
    place name.  We must never splice raw OSM strings into an IRC line.
    """
    text = "" if s is None else str(s)
    cleaned = "".join(ch for ch in text if ch not in _IRC_CTRL_BYTES)
    return cleaned[:max_len]

# ---------------------------------------------------------------------------
# US state display formatting (full name → USPS abbreviation)
# ---------------------------------------------------------------------------

_STATE_ABBR: dict[str, str] = {
    "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA",
    "Colorado":"CO","Connecticut":"CT","Delaware":"DE","Florida":"FL","Georgia":"GA",
    "Hawaii":"HI","Idaho":"ID","Illinois":"IL","Indiana":"IN","Iowa":"IA",
    "Kansas":"KS","Kentucky":"KY","Louisiana":"LA","Maine":"ME","Maryland":"MD",
    "Massachusetts":"MA","Michigan":"MI","Minnesota":"MN","Mississippi":"MS",
    "Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV","New Hampshire":"NH",
    "New Jersey":"NJ","New Mexico":"NM","New York":"NY","North Carolina":"NC",
    "North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA",
    "Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD","Tennessee":"TN",
    "Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA","Washington":"WA",
    "West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY","District of Columbia":"DC",
}

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_COORD_RE = re.compile(r"^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$")
_USZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

# ---------------------------------------------------------------------------
# US location detection
# ---------------------------------------------------------------------------
# Rules for pinning a Nominatim search to countrycodes=us:
#   1. 5-digit (or ZIP+4) zip code.
#   2. A full US state name appears anywhere in the query (case-insensitive).
#   3. A 2-letter UPPERCASE state abbreviation appears as a standalone word.
#      Lowercase is intentionally excluded: "ca"/"or"/"in"/"la" etc. are too
#      ambiguous (Spanish articles, conjunctions, prepositions).
#
# NOTE – "Georgia" matches as a US state, so "tbilisi georgia" is initially
# pinned to countrycodes=us and returns no hit.  The word-drop loop then
# retries "tbilisi" without any constraint, which resolves correctly.

_US_STATE_NAMES: frozenset[str] = frozenset(k.lower() for k in _STATE_ABBR)
_US_STATE_ABBRS: frozenset[str] = frozenset(_STATE_ABBR.values())

_US_STATE_NAME_RE = re.compile(
    r"(?<!\w)(?:" +
    "|".join(re.escape(n) for n in sorted(_US_STATE_NAMES, key=len, reverse=True)) +
    r")(?!\w)",
    re.IGNORECASE,
)
_US_STATE_ABBR_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(a) for a in sorted(_US_STATE_ABBRS)) + r")(?!\w)"
    # No IGNORECASE — uppercase only
)


def _looks_like_us(query: str) -> bool:
    """Return True if the query clearly references a US state."""
    return bool(_US_STATE_NAME_RE.search(query) or _US_STATE_ABBR_RE.search(query))


# ---------------------------------------------------------------------------
# International country / territory / province detection
# ---------------------------------------------------------------------------
# Maps common English country name variants (and subdivision names that imply
# a country) to ISO 3166-1 alpha-2 codes.  Keys are lowercase; matching is
# case-insensitive via regex.
#
# Design notes:
# • "georgia" is absent — it clashes with the US state; word-drop handles it.
# • Lowercase 2-letter codes (fr, de, gb …) are excluded — too many clash
#   with common words.  A few safe uppercase aliases (UAE, UK) are included.
# • Canadian provinces and Australian states are included so that
#   "london ontario" → ca and "brisbane queensland" → au.

_COUNTRY_NAME_MAP: dict[str, str] = {
    # ---- UN member states ------------------------------------------------
    "afghanistan":"af","albania":"al","algeria":"dz","andorra":"ad",
    "angola":"ao","antigua":"ag","argentina":"ar","armenia":"am",
    "australia":"au","austria":"at","azerbaijan":"az",
    "bahamas":"bs","bahrain":"bh","bangladesh":"bd","barbados":"bb",
    "belarus":"by","belgium":"be","belize":"bz","benin":"bj",
    "bhutan":"bt","bolivia":"bo","botswana":"bw",
    "brazil":"br","brasil":"br",
    "brunei":"bn","bulgaria":"bg",
    "burkina faso":"bf","burundi":"bi",
    "cambodia":"kh","cameroon":"cm","canada":"ca",
    "cape verde":"cv","cabo verde":"cv",
    "central african republic":"cf","chad":"td",
    "chile":"cl","china":"cn",
    "colombia":"co","comoros":"km",
    "congo":"cg","democratic republic of the congo":"cd","drc":"cd",
    "costa rica":"cr","croatia":"hr","cuba":"cu","cyprus":"cy",
    "czechia":"cz","czech republic":"cz",
    "denmark":"dk","djibouti":"dj","dominica":"dm","dominican republic":"do",
    "ecuador":"ec","egypt":"eg","el salvador":"sv",
    "equatorial guinea":"gq","eritrea":"er","estonia":"ee",
    "eswatini":"sz","swaziland":"sz","ethiopia":"et",
    "fiji":"fj","finland":"fi","france":"fr",
    "gabon":"ga","gambia":"gm","germany":"de","ghana":"gh","greece":"gr",
    "grenada":"gd","guatemala":"gt",
    "guinea":"gn","guinea-bissau":"gw","guyana":"gy",
    "haiti":"ht","honduras":"hn","hungary":"hu",
    "iceland":"is","india":"in","indonesia":"id",
    "iran":"ir","iraq":"iq","ireland":"ie","israel":"il","italy":"it",
    "jamaica":"jm","japan":"jp","jordan":"jo",
    "kazakhstan":"kz","kenya":"ke","kiribati":"ki",
    "north korea":"kp","south korea":"kr","korea":"kr",
    "kosovo":"xk","kuwait":"kw","kyrgyzstan":"kg",
    "laos":"la","latvia":"lv","lebanon":"lb","lesotho":"ls",
    "liberia":"lr","libya":"ly","liechtenstein":"li",
    "lithuania":"lt","luxembourg":"lu",
    "madagascar":"mg","malawi":"mw","malaysia":"my",
    "maldives":"mv","mali":"ml","malta":"mt",
    "marshall islands":"mh","mauritania":"mr","mauritius":"mu",
    "mexico":"mx","méxico":"mx",
    "micronesia":"fm","moldova":"md","monaco":"mc",
    "mongolia":"mn","montenegro":"me","morocco":"ma",
    "mozambique":"mz","myanmar":"mm","burma":"mm",
    "namibia":"na","nauru":"nr","nepal":"np",
    "netherlands":"nl","holland":"nl",
    "new zealand":"nz",
    "nicaragua":"ni","niger":"ne","nigeria":"ng",
    "north macedonia":"mk","macedonia":"mk",
    "norway":"no",
    "oman":"om",
    "pakistan":"pk","palau":"pw","palestine":"ps","panama":"pa",
    "papua new guinea":"pg","paraguay":"py","peru":"pe",
    "philippines":"ph","poland":"pl","portugal":"pt",
    "qatar":"qa",
    "romania":"ro","russia":"ru","rwanda":"rw",
    "saint lucia":"lc","saint kitts":"kn","saint vincent":"vc",
    "samoa":"ws","san marino":"sm",
    "saudi arabia":"sa","senegal":"sn","serbia":"rs",
    "seychelles":"sc","sierra leone":"sl","singapore":"sg",
    "slovakia":"sk","slovenia":"si","solomon islands":"sb",
    "somalia":"so","south africa":"za","south sudan":"ss",
    "spain":"es","sri lanka":"lk","sudan":"sd","suriname":"sr",
    "sweden":"se","switzerland":"ch","syria":"sy",
    "taiwan":"tw","tajikistan":"tj","tanzania":"tz",
    "thailand":"th","timor-leste":"tl","east timor":"tl",
    "togo":"tg","tonga":"to","trinidad":"tt","trinidad and tobago":"tt",
    "tunisia":"tn","turkey":"tr","türkiye":"tr","turkmenistan":"tm","tuvalu":"tv",
    "uganda":"ug","ukraine":"ua",
    "united arab emirates":"ae","uae":"ae",
    "united kingdom":"gb","uk":"gb","great britain":"gb",
    "britain":"gb","england":"gb","scotland":"gb",
    "wales":"gb","northern ireland":"gb",
    "united states":"us","usa":"us",   # caught by _looks_like_us first
    "uruguay":"uy","uzbekistan":"uz",
    "vanuatu":"vu","venezuela":"ve",
    "vietnam":"vn","viet nam":"vn",
    "yemen":"ye","zambia":"zm","zimbabwe":"zw",
    # ---- Common territories / special regions ----------------------------
    "hong kong":"hk","macau":"mo","macao":"mo",
    "puerto rico":"us","guam":"us",
    "us virgin islands":"us","american samoa":"us",
    # ---- Canadian provinces and territories → ca -------------------------
    "ontario":"ca","quebec":"ca","british columbia":"ca",
    "alberta":"ca","manitoba":"ca","saskatchewan":"ca",
    "nova scotia":"ca","new brunswick":"ca",
    "newfoundland":"ca","labrador":"ca",
    "prince edward island":"ca",
    "northwest territories":"ca","yukon":"ca","nunavut":"ca",
    # ---- Australian states and territories → au --------------------------
    # (Abbreviations excluded: WA clashes with Washington state)
    "new south wales":"au","victoria":"au","queensland":"au",
    "south australia":"au","western australia":"au",
    "tasmania":"au","australian capital territory":"au",
}

# Longest-first so "united arab emirates" matches before "emirates" etc.
_COUNTRY_NAME_RE = re.compile(
    r"(?<!\w)(?:" +
    "|".join(re.escape(k) for k in sorted(_COUNTRY_NAME_MAP, key=len, reverse=True)) +
    r")(?!\w)",
    re.IGNORECASE,
)

# Canadian province abbreviations — none collide with US state abbreviations.
_CA_PROVINCE_ABBRS: frozenset[str] = frozenset(
    ["ON","QC","BC","AB","MB","SK","NS","NB","NL","PE","NT","YT","NU"]
)
_CA_PROVINCE_ABBR_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(sorted(_CA_PROVINCE_ABBRS)) + r")(?!\w)"
    # No IGNORECASE — uppercase only
)


def _country_code_for(query: str) -> str | None:
    """
    Return ISO2 country code if the query contains a recognisable country
    name, common alias, or subdivision name.  Returns None if no match.

    Only called when _looks_like_us() has already returned False, so US
    state names that coincidentally appear here are never reached.
    """
    m = _COUNTRY_NAME_RE.search(query)
    if m:
        return _COUNTRY_NAME_MAP[m.group(0).lower()]
    if _CA_PROVINCE_ABBR_RE.search(query):
        return "ca"
    return None


# ---------------------------------------------------------------------------
# Display name formatting
# ---------------------------------------------------------------------------

def _format_name(addr: dict[str, str], fallback: str) -> tuple[str, str]:
    # Every value below ultimately comes from OSM, which is user-editable.
    # _strip_ctrl removes CR/LF/IRC formatting bytes so a vandalised place
    # name cannot be used to inject a second IRC command or spoof bot
    # output via reverse/bold/colour codes.
    cc   = _strip_ctrl(addr.get("country_code", ""), 8).lower()
    city = _strip_ctrl(addr.get("city") or addr.get("town") or
                       addr.get("village") or addr.get("county") or "")
    fallback = _strip_ctrl(fallback)
    if cc == "us":
        raw_state = addr.get("state", "")
        state = _STATE_ABBR.get(raw_state, raw_state)
        state = _strip_ctrl(state, 64)
        return (f"{city}, {state}".strip(", ") or fallback)[:_MAX_NAME_CHARS], cc
    country = _strip_ctrl(addr.get("country", ""), 64)
    return (f"{city}, {country}".strip(", ") or fallback)[:_MAX_NAME_CHARS], cc


# ---------------------------------------------------------------------------
# HTTP helper (always called via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _get(url: str, *, params: dict | None = None,
         headers: dict | None = None, timeout: int = 10) -> requests.Response:
    """Blocking HTTP GET (stream=True so callers can cap response size)."""
    return requests.get(url, params=params, headers=headers,
                        timeout=timeout, stream=True)


def _read_json_capped(r: requests.Response) -> object | None:
    """Read up to _MAX_BODY_BYTES from *r* and parse as JSON.

    Returns None if the response is oversize or not valid JSON.  Capping
    the body size protects against a misbehaving / hostile upstream
    streaming an unbounded payload at us.
    """
    try:
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
    except Exception as e:
        log.warning(f"Nominatim read: {e}")
        return None
    if len(body) > _MAX_BODY_BYTES:
        log.warning("Nominatim response exceeded size cap")
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as e:
        log.warning(f"Nominatim parse: {e}")
        return None


# ---------------------------------------------------------------------------
# Public geocode function
# ---------------------------------------------------------------------------

async def geocode(query: str, user_agent: str) -> tuple[float, float, str, str] | None:
    """
    Resolve a location string to (lat, lon, display_name, country_code).
    Returns None on failure.

    Accepted input formats
    ----------------------
    • lat,lon          — "34.5,-117.2"
    • US zip code      — "92253" or "90210-1234"
    • City + US state  — "la quinta california" / "portland OR"
    • City + country   — "paris france" / "london england"
    • City + province  — "london ontario" / "toronto ON"
    • City alone       — "london"  (Nominatim global prominence ranking)
    • Coordinates      — "34.5,-117.2"

    Country pinning
    ---------------
    The countrycodes constraint is derived fresh for each candidate in the
    word-drop loop (not locked to the original query).  This ensures that
    dropping an ambiguous trailing token (e.g. "georgia" from "tbilisi
    georgia") eventually produces an unconstrained search that resolves
    correctly via Nominatim's global ranking.

    Priority: US zip > US state name/abbr > country/province name > none.
    """
    # Reject empty / whitespace-only queries cleanly so we never send an
    # empty ``q=`` to Nominatim (which their policy frowns on).
    if query is None:
        return None
    query = query.strip().strip("'\"")
    if not query:
        return None
    # Cap query length: anything longer is junk and inflates upstream cost.
    if len(query) > _MAX_QUERY_CHARS:
        query = query[:_MAX_QUERY_CHARS]

    # Nominatim usage policy: require an identifiable User-Agent that
    # includes a contact (email or URL).  If the operator hasn't
    # configured one we refuse to call out — better to fail the geocode
    # than get the bot's IP banned for the whole channel.
    if not _ua_has_contact(user_agent):
        log.warning(
            "Nominatim UA missing contact info — set [weather] user_agent to "
            "include an email or URL.  Geocode disabled."
        )
        return None
    hdrs: dict[str, str] = {"User-Agent": user_agent}

    # ---- TTL cache lookup ------------------------------------------------
    # We only reach this point with a non-empty, length-capped query and a
    # validated UA — both are part of the cache key.  ``found`` differentiates
    # a cached negative result (None) from a miss.
    cache_key = _cache_key(query, user_agent)
    found, cached = _cache_get(cache_key)
    if found:
        return cached

    def _store(result):
        """Cache *result* (including ``None`` negatives) and return it.

        We cache failures so a flood of identical bad queries can't
        keep hammering Nominatim — TTL covers the case where the
        upstream eventually starts returning useful data again.
        """
        _cache_put(cache_key, result)
        return result

    # ---- Coordinate passthrough ------------------------------------------
    m = _COORD_RE.match(query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        # Sanity-bound the coordinates before sending them upstream.
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return _store(None)
        try:
            r = await asyncio.to_thread(
                _get, "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
                headers=hdrs,
            )
            d = _read_json_capped(r)
            if not isinstance(d, dict):
                return _store((lat, lon, f"{lat:.4f},{lon:.4f}", ""))
            addr = d.get("address", {})
            if not isinstance(addr, dict):
                addr = {}
            name, cc = _format_name(addr, f"{lat:.4f},{lon:.4f}")
            return _store((lat, lon, name, cc))
        except Exception:
            return _store((lat, lon, f"{lat:.4f},{lon:.4f}", ""))

    # ---- Place-name search with word-drop fallback -----------------------
    # If Nominatim returns no hits for the full query, drop the last token
    # and retry.  This recovers from typos in trailing state/country tokens
    # (e.g. "la quinta caifornia" → "la quinta") and from overly-specific
    # queries that don't match any single OSM object.
    candidate = query
    while candidate:
        params: dict = {"q": candidate, "format": "json", "limit": 1, "addressdetails": 1}

        if _USZIP_RE.match(candidate) or _looks_like_us(candidate):
            params["countrycodes"] = "us"
        else:
            cc = _country_code_for(candidate)
            if cc:
                params["countrycodes"] = cc

        try:
            r = await asyncio.to_thread(
                _get, "https://nominatim.openstreetmap.org/search",
                params=params, headers=hdrs,
            )
            hits = _read_json_capped(r)
        except Exception as e:
            log.warning(f"Geocode '{candidate}': {e}")
            return _store(None)

        if isinstance(hits, list) and hits and isinstance(hits[0], dict):
            hit = hits[0]
            # Defensive coercion: Nominatim returns lat/lon as strings.
            # If they're missing or unparseable, skip this hit rather than
            # propagate an exception with attacker-influenced data.
            try:
                lat = float(hit.get("lat"))
                lon = float(hit.get("lon"))
            except (TypeError, ValueError):
                break
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                break
            addr = hit.get("address", {})
            if not isinstance(addr, dict):
                addr = {}
            # display_name and candidate are upstream/user strings — _format_name
                # will _strip_ctrl them before returning, never interpret IRC codes.
            name, cc = _format_name(addr, hit.get("display_name", candidate))
            if candidate != query:
                log.info(f"Geocode: '{query}' missed, resolved via truncated '{candidate}'")
            return _store((lat, lon, name, cc))

        words = candidate.rsplit(None, 1)
        if len(words) < 2:
            break
        candidate = words[0]

    return _store(None)
