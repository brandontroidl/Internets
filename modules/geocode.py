from __future__ import annotations

import re
import logging
import requests

log = logging.getLogger("internets.geocode")

# Full name → USPS abbreviation for display formatting
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

_COORD_RE = re.compile(r"^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$")


def _format_name(addr: dict[str, str], fallback: str) -> tuple[str, str]:
    cc   = addr.get("country_code", "").lower()
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county") or ""
    if cc == "us":
        state = _STATE_ABBR.get(addr.get("state", ""), addr.get("state", ""))
        return f"{city}, {state}".strip(", ") or fallback, cc
    country = addr.get("country", "")
    return f"{city}, {country}".strip(", ") or fallback, cc


def geocode(query: str, user_agent: str) -> tuple[float, float, str, str] | None:
    """
    Resolve a location string to (lat, lon, display_name, country_code).
    Returns None on failure.  Accepts place names, zip codes, or 'lat,lon'.
    """
    query = query.strip().strip("'\"")
    hdrs: dict[str, str] = {"User-Agent": user_agent}

    m = _COORD_RE.match(query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        try:
            r    = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
                headers=hdrs, timeout=10,
            )
            d    = r.json()
            name, cc = _format_name(d.get("address", {}), f"{lat:.4f},{lon:.4f}")
            return lat, lon, name, cc
        except Exception:
            return lat, lon, f"{lat:.4f},{lon:.4f}", ""

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 1},
            headers=hdrs, timeout=10,
        )
        hits = r.json()
        if not hits:
            return None
        hit      = hits[0]
        lat, lon = float(hit["lat"]), float(hit["lon"])
        name, cc = _format_name(hit.get("address", {}), hit.get("display_name", query))
        return lat, lon, name, cc
    except Exception as e:
        log.warning(f"Geocode '{query}': {e}")
    return None
