"""Weatherstack - historical weather (paid plans only)."""
from __future__ import annotations
from datetime import date, timedelta
from .._http import get_json
from ..base import HistoricalResult
from .current import _check_envelope
# fix: was http:// - leaked access_key in plaintext query string.
_B = "https://api.weatherstack.com"
async def fetch(key, lat, lon, location, target_date=""):
    if not target_date: target_date = (date.today() - timedelta(days=1)).isoformat()
    data = await get_json(f"{_B}/historical", params={"access_key": key, "query": f"{lat},{lon}", "units": "m", "historical_date": target_date})
    # fix: detect {"success":false,"error":...} envelope (was silently swallowed).
    _check_envelope(data)
    hist = data.get("historical",{}).get(target_date,{})
    # fix: was reporting totalsnow (snowfall, cm) in precip_mm; now use
    # the actual precipitation totals. Weatherstack exposes them under
    # hourly entries (no daily total), so we average the hourly precip
    # values when present; otherwise fall back to None.
    hourly_entries = hist.get("hourly") or []
    precip_values = [h.get("precip") for h in hourly_entries if isinstance(h, dict) and h.get("precip") is not None]
    precip_mm = sum(precip_values) if precip_values else None
    return HistoricalResult(source="Weatherstack", location=location, date=target_date, high_c=hist.get("maxtemp"), low_c=hist.get("mintemp"), avg_c=hist.get("avgtemp"), description="", precip_mm=precip_mm, avg_humidity=None)
