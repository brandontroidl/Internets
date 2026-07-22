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

from .base import strip_ctrl

log = logging.getLogger("internets.geocode")

# ---------------------------------------------------------------------------
# TTL cache (per Nominatim ToS)
# ---------------------------------------------------------------------------
# Nominatim's usage policy explicitly requires clients to cache results.
# We cache by ``(lowercased-query, user_agent)`` because the country-pin
# logic above is fully determined by the query string - same query → same
# upstream call, every time.  The UA is part of the key so two operators
# sharing a single process (unlikely, but possible during reloads) don't
# cross-contaminate each other's cache.
#
# Implementation: an LRU-ish ``OrderedDict`` keyed by the cache key.
# ``move_to_end`` on hit; evict from the front when we hit the cap.
# A ``threading.Lock`` serialises mutations - geocode() is awaited via
# ``asyncio.to_thread`` so cache access happens from a worker thread.
#
# The cache stores a 4-tuple result (lat, lon, name, cc) or the sentinel
# ``None`` (negative result) - both are valid and worth caching.

_GEOCODE_CACHE_TTL = 24 * 60 * 60   # 24h, per Nominatim ToS
_GEOCODE_CACHE_MAX = 1000           # bounded - memory cap
_geocode_cache: "OrderedDict[tuple[str, str, str], tuple[float, Optional[tuple[float, float, str, str]]]]" = OrderedDict()
_geocode_cache_lock = threading.Lock()
_geocode_cache_stats = {"hits": 0, "misses": 0, "evictions": 0}


def _cache_key(query: str, user_agent: str,
               default_country: str) -> tuple[str, str, str]:
    # default_country is part of the key: the same bare numeric code can
    # resolve to a different place depending on the operator's home country.
    return (query.strip().lower(), user_agent, default_country)


def _cache_get(key: tuple[str, str, str]) -> tuple[bool, Optional[tuple[float, float, str, str]]]:
    """Return ``(found, value)``.  ``found`` is False on miss or expiry."""
    now = time.time()
    with _geocode_cache_lock:
        entry = _geocode_cache.get(key)
        if entry is None:
            _geocode_cache_stats["misses"] += 1
            return (False, None)
        ts, value = entry
        if now - ts > _GEOCODE_CACHE_TTL:
            # Expired - drop and treat as miss.
            del _geocode_cache[key]
            _geocode_cache_stats["misses"] += 1
            return (False, None)
        # LRU touch.
        _geocode_cache.move_to_end(key)
        _geocode_cache_stats["hits"] += 1
        return (True, value)


def _cache_put(key: tuple[str, str, str], value: Optional[tuple[float, float, str, str]]) -> None:
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
      * ``size``       - current number of cached entries
      * ``hits``       - successful lookups since process start
      * ``misses``     - lookups that fell through to the network
      * ``evictions``  - entries dropped due to the size cap

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
# placeholder - sending generic UAs gets the bot's IP banned (and is also
# a poor neighbour move).  The check below is intentionally permissive:
# any "@" or "http" inside the UA is treated as a contact identifier.
def _ua_has_contact(ua: str) -> bool:
    """Return True if the UA appears to embed an email or URL."""
    if not ua:
        return False
    lower = ua.lower()
    return ("@" in ua and "." in ua.split("@", 1)[1]) or "http://" in lower or "https://" in lower


# Cap the JSON we read from Nominatim - normal responses are <10 KB; this
# bounds memory if upstream misbehaves or a TLS-stripping proxy injects HTML.
_MAX_BODY_BYTES = 128 * 1024

# Cap display name length we emit downstream so a long upstream string
# (Nominatim sometimes returns >300 char ``display_name`` fields) can't
# blow past the 510-byte IRC line limit when combined with other context.
_MAX_NAME_CHARS = 160

# Cap the user's raw query length.  Anything longer is junk and also
# inflates the cost of the upstream request.
_MAX_QUERY_CHARS = 200

def _strip_ctrl(s: object, max_len: int = _MAX_NAME_CHARS) -> str:
    """Drop IRC control bytes from upstream strings and cap length.

    Nominatim ``display_name`` and ``address.*`` values are user-editable
    OSM data: anyone with an OSM account can put ``\r\nQUIT :pwned`` in a
    place name.  We must never splice raw OSM strings into an IRC line.
    """
    return strip_ctrl(s, max_len)

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

_USZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

# Coordinate parsing - decimal (comma/space), hemisphere (39°N 98°W), and DMS.
# Free-text Nominatim mangles the non-decimal forms ("39°N 98°W" resolves to a
# random Missouri suburb), so _parse_coords normalises to signed decimal and the
# exact point is reverse-geocoded instead.
_COORD_DECIMAL_RE = re.compile(
    r"^\s*([-+]?\d{1,3}(?:\.\d+)?)\s*[,\s]\s*([-+]?\d{1,3}(?:\.\d+)?)\s*$")
# Two degree[/minute/second] components, each carrying a hemisphere letter on
# either side - required so the axis (N/S vs E/W) and sign are unambiguous.
_COORD_DMS_RE = re.compile(
    r"^\s*"
    r"(?P<ah1>[NSEWnsew])?\s*(?P<ad>\d{1,3}(?:\.\d+)?)\s*[°ºd]?\s*"
    r"(?:(?P<am>\d{1,2}(?:\.\d+)?)\s*['′m]\s*)?"
    r"(?:(?P<asec>\d{1,2}(?:\.\d+)?)\s*[\"″]\s*)?(?P<ah2>[NSEWnsew])?"
    r"[\s,]+"
    r"(?P<bh1>[NSEWnsew])?\s*(?P<bd>\d{1,3}(?:\.\d+)?)\s*[°ºd]?\s*"
    r"(?:(?P<bm>\d{1,2}(?:\.\d+)?)\s*['′m]\s*)?"
    r"(?:(?P<bsec>\d{1,2}(?:\.\d+)?)\s*[\"″]\s*)?(?P<bh2>[NSEWnsew])?"
    r"\s*$")

# Postal-code classification (see _postal_kind).  The Canadian alphanumeric
# and UK formats are globally unique, so they pin a country with zero
# ambiguity.  A bare numeric code (5-digit ZIP, 4-digit CH, etc.) is shared
# across countries and is resolved home-country-first, then globally.
# CA and UK are disjoint: a CA code always ends in a digit, a UK one always
# ends in two letters.
_ZIP4_RE       = re.compile(r"^\d{5}-\d{4}$")               # ZIP+4 → US only
_CA_POSTAL_RE  = re.compile(r"^[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d$")
_UK_POSTAL_RE  = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}$")
# Distinctive dashed/alphanumeric international formats whose shape alone
# identifies the country (determinism / provider-independence, not accuracy -
# the free-text path resolves these too, but luck-of-ranking, not by contract).
# Only the country-UNIQUE forms are pinned; bare numeric equivalents (7-digit
# JP, 8-digit BR) stay generic-numeric since the digit count isn't unique.
# IE Eircode ends in a 4-char group; CA (3-char inward) and UK (digit+2 letters)
# do not, so the three stay disjoint.
_JP_POSTAL_RE  = re.compile(r"^\d{3}-\d{4}$")               # Japan (dashed)
_BR_POSTAL_RE  = re.compile(r"^\d{5}-\d{3}$")               # Brazil CEP (dashed)
_IE_POSTAL_RE  = re.compile(r"^[A-Za-z]\d[A-Za-z\d]\s?[A-Za-z\d]{4}$")  # Ireland Eircode
_NUM_POSTAL_RE = re.compile(r"^\d{4,10}$")                  # bare numeric
_CC_RE         = re.compile(r"^[a-z]{2}$")                  # ISO-3166-1 alpha-2

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
    # No IGNORECASE - uppercase only
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
# • "georgia" is absent - it clashes with the US state; word-drop handles it.
# • Lowercase 2-letter codes (fr, de, gb …) are excluded - too many clash
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

# Canadian province abbreviations - none collide with US state abbreviations.
_CA_PROVINCE_ABBRS: frozenset[str] = frozenset(
    ["ON","QC","BC","AB","MB","SK","NS","NB","NL","PE","NT","YT","NU"]
)
_CA_PROVINCE_ABBR_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(sorted(_CA_PROVINCE_ABBRS)) + r")(?!\w)"
    # No IGNORECASE - uppercase only
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
# Postal-code classification + resolution
# ---------------------------------------------------------------------------
# Free-text Nominatim ``q=`` searches fuzzy-match a postal code against the
# nearest house-number / building, so "08000" pinned to the US returns a
# random Ohio motel and "A1A 1A1" with no pin returns a Swiss street.  We
# fix that by classifying the input and resolving it with structured
# postal-code lookups (Nominatim ``postalcode=`` / Zippopotam) that match the
# code AS a postal code - a bogus code returns nothing instead of garbage.


def _normalize_cc(cc: str) -> str:
    """Coerce an operator-supplied home country to a safe ISO2 code.

    Anything that isn't two ASCII letters falls back to ``us`` so a typo'd
    ``[weather] default_country`` can't disable the home-country bias or
    inject junk into the ``countrycodes`` parameter.
    """
    cc = (cc or "").strip().lower()
    return cc if _CC_RE.match(cc) else "us"


def _postal_kind(s: str) -> str | None:
    """Classify a string as a postal code, or None if it isn't one.

    Returns ``"us"`` (ZIP+4 - unambiguously US), ``"ca"`` (Canadian
    alphanumeric), ``"uk"`` (UK postcode), ``"ie"`` / ``"jp"`` / ``"br"``
    (distinctive Eircode / dashed-Japan / dashed-Brazil formats, each
    country-unique), ``"num"`` (bare numeric, shared across countries →
    home-first), or None (not a postal code → free-text).  The ``ie``/``jp``/
    ``br`` kinds are also their ISO2 country code, so _resolve_postal pins them
    directly.
    """
    s = s.strip()
    if _ZIP4_RE.match(s):
        return "us"
    if _CA_POSTAL_RE.match(s):
        return "ca"
    if _UK_POSTAL_RE.match(s):
        return "uk"
    if _IE_POSTAL_RE.match(s):
        return "ie"
    if _JP_POSTAL_RE.match(s):
        return "jp"
    if _BR_POSTAL_RE.match(s):
        return "br"
    if _NUM_POSTAL_RE.match(s):
        return "num"
    return None


# Bare 2-letter country codes accepted as a postal override.  A real ISO2,
# MINUS those that collide with a US state or Canadian province abbreviation:
# in a US/CA-centric bot a trailing "ca"/"il"/"oh" means the subdivision
# (California / Illinois / Ohio), not Canada/Israel/-, so "90210 ca" must
# resolve the US ZIP, not pin to Canada.  Forcing those countries still works
# via the full name ("90210 canada", "08000 israel").
_ISO2_OVERRIDES: frozenset[str] = (
    frozenset(_COUNTRY_NAME_MAP.values())
    - frozenset(a.lower() for a in _US_STATE_ABBRS)
    - frozenset(a.lower() for a in _CA_PROVINCE_ABBRS)
)


def _split_postal_country(query: str) -> tuple[str, str | None]:
    """Split ``"<postal-code> <country>"`` into ``(core, iso2)``.

    Honours an explicit override like ``"08000 spain"`` / ``"08000 es"`` so a
    user can force a country for an otherwise-ambiguous bare code.  Only
    splits when the leading part is itself a postal code, so city+province /
    city+country queries ("london ontario", "paris france") are returned
    unchanged as ``(query, None)`` and fall through to the free-text loop.
    A bare 2-letter tail is accepted only when it is a real ISO2 that is NOT
    a US-state / CA-province abbreviation, so "90210 ca" (ZIP + state) stays
    on the working free-text path instead of mis-pinning to Canada.
    """
    toks = query.split()
    if len(toks) >= 2:
        for n in (3, 2, 1):
            if len(toks) <= n:
                continue
            tail = " ".join(toks[-n:]).lower()
            cc = _COUNTRY_NAME_MAP.get(tail)
            if cc is None and n == 1 and tail in _ISO2_OVERRIDES:
                cc = tail
            if cc and _postal_kind(" ".join(toks[:-n])):
                return " ".join(toks[:-n]), cc
    return query, None


def _fsa(code: str) -> str:
    """First 3 alphanumerics of a Canadian postal code (the FSA), uppercased.

    Zippopotam keys Canadian data by Forward Sortation Area (the outward
    half), which is the granularity OSM/Canada-Post-free data actually has.
    """
    return re.sub(r"[^A-Za-z0-9]", "", code)[:3].upper()


def _zippo_parse(data: object) -> tuple[float, float, str, str] | None:
    """Parse a Zippopotam.us response into ``(lat, lon, name, cc)`` or None.

    Fails closed (None) on any missing/oversize/unparseable field.  Place
    names from Zippopotam can carry a long parenthetical FSA list, so we trim
    at the first ``(`` and apply the same control-byte stripping as Nominatim
    output before it reaches an IRC line.
    """
    if not isinstance(data, dict):
        return None
    places = data.get("places")
    if not isinstance(places, list) or not places or not isinstance(places[0], dict):
        return None
    p = places[0]
    try:
        lat = float(p.get("latitude"))
        lon = float(p.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    cc = _strip_ctrl(data.get("country abbreviation", ""), 8).lower()
    city = _strip_ctrl(p.get("place name", "")).split("(", 1)[0].strip()
    if cc == "us":
        state = _strip_ctrl(p.get("state abbreviation", ""), 8)
        name = f"{city}, {state}".strip(", ")
    else:
        country = _strip_ctrl(data.get("country", ""), 64)
        name = f"{city}, {country}".strip(", ")
    return (lat, lon, (name or city)[:_MAX_NAME_CHARS], cc)


def _valid_latlon(lat: float, lon: float) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` if in range, else None (reject bad coordinates)."""
    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return (lat, lon)
    return None


def _dms_to_deg(d: str, m: str | None, s: str | None) -> float:
    """Degrees[+minutes/60+seconds/3600] as a positive decimal magnitude."""
    val = float(d)
    if m:
        val += float(m) / 60.0
    if s:
        val += float(s) / 3600.0
    return val


def _parse_coords(query: str) -> tuple[float, float] | None:
    """Parse a coordinate string to ``(lat, lon)`` signed decimal, or None.

    Accepts decimal pairs (comma- or space-separated), hemisphere decimals
    ("39°N 98°W", "N39 W98", either order), and DMS ("39°50'15\\"N 98°35'W").
    Returns None for anything that isn't an unambiguous coordinate pair, so
    place names and postal codes fall through to their own resolvers.  A bare
    "39 98" (no comma, sign, or decimal point) is intentionally rejected as too
    ambiguous to claim as coordinates.
    """
    query = query.strip()
    m = _COORD_DECIMAL_RE.match(query)
    if m and any(c in query for c in ",.+-"):
        return _valid_latlon(float(m.group(1)), float(m.group(2)))
    m = _COORD_DMS_RE.match(query)
    if not m:
        return None
    ah = (m.group("ah1") or m.group("ah2") or "").lower()
    bh = (m.group("bh1") or m.group("bh2") or "").lower()
    # Require exactly one N/S component and one E/W component.
    if not ah or not bh or (ah in "ns") == (bh in "ns"):
        return None
    av = _dms_to_deg(m.group("ad"), m.group("am"), m.group("asec"))
    bv = _dms_to_deg(m.group("bd"), m.group("bm"), m.group("bsec"))
    if ah in "ns":
        (lat, lath), (lon, lonh) = (av, ah), (bv, bh)
    else:
        (lat, lath), (lon, lonh) = (bv, bh), (av, ah)
    if lath == "s":
        lat = -lat
    if lonh == "w":
        lon = -lon
    return _valid_latlon(lat, lon)


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
# Structured postal-code resolvers (always called via asyncio.to_thread)
# ---------------------------------------------------------------------------

async def _nominatim_postal(code: str, cc: str | None,
                            hdrs: dict[str, str]) -> tuple[float, float, str, str] | None:
    """Structured Nominatim ``postalcode=`` search, optionally pinned to *cc*.

    Unlike free-text ``q=``, the ``postalcode`` parameter matches the value
    as a postal code, so a code that doesn't exist in the pinned country
    returns nothing instead of a fuzzy nearest-object match.  Returns None on
    miss / transport error / unparseable hit (fail closed).
    """
    params: dict = {"postalcode": code, "format": "json",
                    "limit": 1, "addressdetails": 1}
    if cc:
        params["countrycodes"] = cc
    try:
        with await asyncio.to_thread(
            _get, "https://nominatim.openstreetmap.org/search",
            params=params, headers=hdrs,
        ) as r:
            hits = _read_json_capped(r)
    except Exception as e:
        log.warning(f"Nominatim postal '{code}' ({cc or 'global'}): {e}")
        return None
    if not (isinstance(hits, list) and hits and isinstance(hits[0], dict)):
        return None
    hit = hits[0]
    try:
        lat = float(hit.get("lat"))
        lon = float(hit.get("lon"))
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    addr = hit.get("address", {})
    if not isinstance(addr, dict):
        addr = {}
    name, rcc = _format_name(addr, hit.get("display_name", code))
    return (lat, lon, name, rcc)


async def _zippo(cc: str, code: str,
                 user_agent: str) -> tuple[float, float, str, str] | None:
    """Postal lookup via Zippopotam.us - a free, keyless, purpose-built postal
    geocoder.  Used where OSM/Nominatim lacks postal coverage (notably
    Canada, whose Canada-Post data is proprietary).  A 404 (no such code in
    that country) is a clean miss, not an error; any other failure also
    yields None (fail closed).
    """
    from .base import fetch_json  # noqa: PLC0415 - lazy, keeps import graph light
    url = f"https://api.zippopotam.us/{cc.lower()}/{code}"
    try:
        data = await asyncio.to_thread(
            fetch_json, url, ua=user_agent, allow_404=True, max_bytes=_MAX_BODY_BYTES)
    except Exception as e:
        log.warning(f"Zippopotam {cc}/{code}: {e}")
        return None
    return _zippo_parse(data) if data is not None else None


async def _resolve_postal(kind: str, code: str, hint: str | None,
                          default_country: str, hdrs: dict[str, str],
                          user_agent: str) -> tuple[float, float, str, str] | None:
    """Resolve a classified postal code to a location.

    ``ca`` → Zippopotam by FSA (Nominatim can't); ``us`` (ZIP+4) → pinned
    Nominatim; ``uk`` → Nominatim pinned to the hint or gb; ``num`` (bare
    numeric) → explicit-hint pin if given, else home country first (Nominatim
    then Zippopotam backstop), then global best-match.  Returns None if the
    code resolves nowhere - we deliberately do NOT fall back to fuzzy
    free-text, which is what produced the wrong-country matches.
    """
    if kind == "ca":
        return await _zippo("ca", _fsa(code), user_agent)
    if kind == "us":
        # ZIP+4 - the +4 is sub-ZIP granularity neither Nominatim nor
        # Zippopotam carries, so resolve the 5-digit base, US-pinned.
        base = code.split("-", 1)[0]
        return (await _nominatim_postal(base, "us", hdrs)
                or await _zippo("us", base, user_agent))
    if kind == "uk":
        return await _nominatim_postal(code, hint or "gb", hdrs)
    if kind in ("ie", "jp", "br"):
        # Format-unique → the kind IS the ISO2; pin straight to it.
        return await _nominatim_postal(code, kind, hdrs)
    # kind == "num"
    if hint:
        return (await _nominatim_postal(code, hint, hdrs)
                or await _zippo(hint, code, user_agent))
    return (await _nominatim_postal(code, default_country, hdrs)
            or await _zippo(default_country, code, user_agent)
            or await _nominatim_postal(code, None, hdrs))


# ---------------------------------------------------------------------------
# Free-text place search
# ---------------------------------------------------------------------------

async def _search_place(candidate: str, hdrs: dict[str, str], *,
                        feature_type: str | None = None,
                        ) -> tuple[tuple[float, float, str, str] | None, float, bool]:
    """Run one Nominatim free-text search for *candidate*.

    Returns ``(hit, importance, stop)``.  ``hit`` is ``(lat, lon, name, cc)``
    on success, else None.  ``importance`` is the matched object's OSM
    prominence score (0.0 when upstream omits it), used ONLY to compare two
    candidate answers for the same query - see ``geocode``.  ``stop`` is True
    when the caller must NOT retry with a shorter query - either the transport
    failed, or upstream returned a row we could not parse.  Retrying either
    case just burns requests against Nominatim's 1 req/s policy for no gain.

    *feature_type* maps to Nominatim's ``featureType`` parameter; pass
    ``"settlement"`` to constrain the search to cities/towns/villages.
    """
    params: dict = {"q": candidate, "format": "json",
                    "limit": 1, "addressdetails": 1}
    if feature_type:
        params["featureType"] = feature_type

    if _USZIP_RE.match(candidate) or _looks_like_us(candidate):
        params["countrycodes"] = "us"
    else:
        cc = _country_code_for(candidate)
        if cc:
            params["countrycodes"] = cc

    try:
        with await asyncio.to_thread(
            _get, "https://nominatim.openstreetmap.org/search",
            params=params, headers=hdrs,
        ) as r:
            hits = _read_json_capped(r)
    except Exception as e:
        log.warning(f"Geocode '{candidate}': {e}")
        return (None, 0.0, True)

    if not (isinstance(hits, list) and hits and isinstance(hits[0], dict)):
        return (None, 0.0, False)

    hit = hits[0]
    # Defensive coercion: Nominatim returns lat/lon as strings.  If they're
    # missing or unparseable, stop rather than propagate an exception with
    # attacker-influenced data.
    try:
        lat = float(hit.get("lat"))
        lon = float(hit.get("lon"))
    except (TypeError, ValueError):
        return (None, 0.0, True)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return (None, 0.0, True)
    try:
        imp = float(hit.get("importance"))
    except (TypeError, ValueError):
        imp = 0.0
    addr = hit.get("address", {})
    if not isinstance(addr, dict):
        addr = {}
    # display_name and candidate are upstream/user strings - _format_name
    # will _strip_ctrl them before returning, never interpret IRC codes.
    name, cc = _format_name(addr, hit.get("display_name", candidate))
    return ((lat, lon, name, cc), imp, False)


# ---------------------------------------------------------------------------
# Public geocode function
# ---------------------------------------------------------------------------

async def geocode(query: str, user_agent: str, *,
                  default_country: str = "us") -> tuple[float, float, str, str] | None:
    """
    Resolve a location string to (lat, lon, display_name, country_code).
    Returns None on failure.

    Accepted input formats
    ----------------------
    • lat,lon          - "34.5,-117.2"
    • Postal code      - "92253" / "90210-1234" / "A1A 1A1" / "SW1A 1AA"
    • Postal + country - "08000 spain" / "08000 es"  (explicit override)
    • City + US state  - "la quinta california" / "portland OR"
    • City + country   - "paris france" / "london england"
    • City + province  - "london ontario" / "toronto ON"
    • City alone       - "london"  (Nominatim global prominence ranking)

    Postal codes
    ------------
    Postal codes are classified (_postal_kind) and resolved with structured
    lookups, NOT fuzzy free-text:
      • CA alphanumeric / UK formats are globally unique → pinned with no
        ambiguity (CA via Zippopotam, which has data OSM lacks).
      • A bare numeric code is genuinely shared across countries.  It is
        resolved home-country-first (``default_country``, default "us"): a
        real local ZIP stays local, a code that isn't valid there falls back
        to the global best match (so 43812→Ohio but 08000→Barcelona).  An
        explicit trailing country ("08000 spain") overrides the home bias.

    City / place names still use the free-text word-drop loop below, where the
    countrycodes constraint is derived fresh for each candidate so dropping an
    ambiguous trailing token (e.g. "georgia" from "tbilisi georgia") yields an
    unconstrained search that resolves via Nominatim's global ranking.
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

    # Bound the operator-supplied home country: a bad value must not disable
    # the home bias or inject junk into countrycodes - fall back to "us".
    default_country = _normalize_cc(default_country)

    # Nominatim usage policy: require an identifiable User-Agent that
    # includes a contact (email or URL).  If the operator hasn't
    # configured one we refuse to call out - better to fail the geocode
    # than get the bot's IP banned for the whole channel.
    if not _ua_has_contact(user_agent):
        log.warning(
            "Nominatim UA missing contact info - set [weather] user_agent to "
            "include an email or URL.  Geocode disabled."
        )
        return None
    hdrs: dict[str, str] = {"User-Agent": user_agent}

    # ---- TTL cache lookup ------------------------------------------------
    # We only reach this point with a non-empty, length-capped query and a
    # validated UA - both are part of the cache key.  ``found`` differentiates
    # a cached negative result (None) from a miss.
    cache_key = _cache_key(query, user_agent, default_country)
    found, cached = _cache_get(cache_key)
    if found:
        return cached

    def _store(result):
        """Cache *result* (including ``None`` negatives) and return it.

        We cache failures so a flood of identical bad queries can't
        keep hammering Nominatim - TTL covers the case where the
        upstream eventually starts returning useful data again.
        """
        _cache_put(cache_key, result)
        return result

    # ---- Coordinate passthrough ------------------------------------------
    # Decimal, hemisphere (39°N 98°W), and DMS forms, parsed deterministically
    # so free-text can't mangle them (un-parsed, "39°N 98°W" → Creve Coeur MO).
    # _parse_coords already range-validates, so an out-of-range pair returns
    # None here and falls through rather than reverse-geocoding a bad point.
    coords = _parse_coords(query)
    if coords is not None:
        lat, lon = coords
        try:
            with await asyncio.to_thread(
                _get, "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
                headers=hdrs,
            ) as r:
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

    # ---- Postal-code resolution (structured, country-aware) --------------
    # Classify the input; if it's a postal code, resolve it with structured
    # lookups and return - including a negative cache on miss.  We do NOT fall
    # through to the fuzzy free-text loop for a postal code: that fuzzy match
    # is exactly what returned the wrong country (a US motel for "08000", a
    # Swiss street for "A1A 1A1").
    core, hint = _split_postal_country(query)
    kind = _postal_kind(core)
    if kind:
        return _store(await _resolve_postal(
            kind, core, hint, default_country, hdrs, user_agent))

    # ---- Settlement pass -------------------------------------------------
    # Free-text ``q=`` returns the single best-ranked OSM object of ANY kind,
    # so a query that happens to name a business outranks the place it was
    # named after: "new york new york" resolves to the Las Vegas casino (the
    # US pin comes from the state name), "north shore new jersey" to a
    # residential street.  Asking for ``featureType=settlement`` constrains
    # the search to cities/towns/villages, which is usually what a weather
    # lookup wants.
    #
    # But preferring the settlement UNCONDITIONALLY is also wrong: a tiny
    # township named Graceland in South Africa would then preempt the famous
    # Memphis landmark, and a suburb called Newton Circus would preempt a
    # better answer for "circus circus".  So we run both searches and keep the
    # more prominent object.
    #
    # NOTE on ``importance``: it is meaningless as an ABSOLUTE quality bar -
    # Oxford Circus (a wrong answer) scores 0.5086 and Graceland (a right one)
    # 0.5087.  It is only used here to rank two candidate answers to the SAME
    # query against each other, which is what it actually measures.
    sett_hit, sett_imp, stop = await _search_place(
        query, hdrs, feature_type="settlement")
    if stop:
        # Transport or parse failure - Nominatim is unhappy, don't hammer it.
        return _store(None)

    # ---- Place-name search with word-drop fallback -----------------------
    # If the unconstrained search returns no hits for the full query, drop the
    # last token and retry.  This recovers from typos in trailing state/country
    # tokens (e.g. "la quinta caifornia" → "la quinta") and from overly-specific
    # queries that don't match any single OSM object.
    # Cap the sequential Nominatim hits per command: an adversarial many-token
    # query would otherwise drop one token at a time for up to ~100 requests,
    # breaching the 1 req/s usage policy and risking a channel-wide IP ban.
    _MAX_DROPS = 4
    candidate = query
    drops = 0
    while candidate:
        free_hit, free_imp, stop = await _search_place(candidate, hdrs)
        if free_hit is not None:
            # A settlement match on the FULL query always beats a match found
            # only after dropping tokens - the truncated query answers a
            # different question than the user asked.
            if sett_hit is not None and (candidate != query or sett_imp >= free_imp):
                return _store(sett_hit)
            if candidate != query:
                log.info(f"Geocode: '{query}' missed, resolved via truncated '{candidate}'")
            return _store(free_hit)
        if stop:
            break

        words = candidate.rsplit(None, 1)
        if len(words) < 2 or drops >= _MAX_DROPS:
            break
        candidate = words[0]
        drops += 1

    # Unconstrained search found nothing usable; the settlement hit (if any)
    # is the best answer we have.
    return _store(sett_hit)
