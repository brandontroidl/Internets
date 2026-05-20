from __future__ import annotations

import asyncio
import re
import logging
import requests

log = logging.getLogger("internets.geocode")

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
    cc   = addr.get("country_code", "").lower()
    city = (addr.get("city") or addr.get("town") or
            addr.get("village") or addr.get("county") or "")
    if cc == "us":
        state = _STATE_ABBR.get(addr.get("state", ""), addr.get("state", ""))
        return f"{city}, {state}".strip(", ") or fallback, cc
    country = addr.get("country", "")
    return f"{city}, {country}".strip(", ") or fallback, cc


# ---------------------------------------------------------------------------
# HTTP helper (always called via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _get(url: str, *, params: dict | None = None,
         headers: dict | None = None, timeout: int = 10) -> requests.Response:
    """Blocking HTTP GET."""
    return requests.get(url, params=params, headers=headers, timeout=timeout)


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
    query = query.strip().strip("'\"")
    hdrs: dict[str, str] = {"User-Agent": user_agent}

    # ---- Coordinate passthrough ------------------------------------------
    m = _COORD_RE.match(query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        try:
            r = await asyncio.to_thread(
                _get, "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
                headers=hdrs,
            )
            d = r.json()
            name, cc = _format_name(d.get("address", {}), f"{lat:.4f},{lon:.4f}")
            return lat, lon, name, cc
        except Exception:
            return lat, lon, f"{lat:.4f},{lon:.4f}", ""

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
            hits = r.json()
        except Exception as e:
            log.warning(f"Geocode '{candidate}': {e}")
            return None

        if hits:
            hit      = hits[0]
            lat, lon = float(hit["lat"]), float(hit["lon"])
            name, cc = _format_name(hit.get("address", {}), hit.get("display_name", candidate))
            if candidate != query:
                log.info(f"Geocode: '{query}' missed, resolved via truncated '{candidate}'")
            return lat, lon, name, cc

        words = candidate.rsplit(None, 1)
        if len(words) < 2:
            break
        candidate = words[0]

    return None
