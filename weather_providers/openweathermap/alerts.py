"""OpenWeatherMap - alerts (requires OneCall 3.0 API)."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
_B = "https://api.openweathermap.org/data/3.0"

# OWM doesn't expose a structured severity on the OneCall alert payload -
# the only signal is the ``tags`` array (e.g. ["Extreme temperature value"])
# and the event name itself. Map common keywords to our severity vocab.
_SEVERITY_KEYWORDS = (
    ("extreme",   ("extreme",)),
    ("severe",    ("severe", "warning", "tornado", "hurricane", "tsunami",
                   "blizzard", "ice storm")),
    ("moderate",  ("moderate", "watch", "storm", "thunderstorm", "flood",
                   "high wind")),
    ("minor",     ("minor", "advisory", "statement", "outlook")),
)


def _classify(event: str, tags) -> str:
    # fix: previously hard-coded "moderate" for every OWM alert,
    # ignoring the real semantics on the payload. Best-effort: classify
    # via the tags array plus the event-name keywords.
    haystack = (event or "").lower()
    if isinstance(tags, list):
        haystack = " ".join([haystack] + [str(t).lower() for t in tags])
    for sev, kws in _SEVERITY_KEYWORDS:
        if any(kw in haystack for kw in kws):
            return sev
    return "unknown"


async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/onecall", params={"lat": lat, "lon": lon, "appid": key, "exclude": "minutely,hourly,daily"})
    alerts = []
    for a in data.get("alerts",[]):
        alerts.append(AlertEntry(
            event=a.get("event","Unknown"),
            severity=_classify(a.get("event", ""), a.get("tags")),
            headline=a.get("sender_name",""),
            start=a.get("start",""),
            end=a.get("end",""),
            description=(a.get("description") or "")[:300],
        ))
    return AlertsResult(source="OpenWeatherMap", location=location, alerts=alerts)
