"""NASA POWER — historical daily weather (global, no key)."""
from __future__ import annotations
from datetime import datetime, timedelta
from .._http import get_json, HTTPError
from ..base import HistoricalResult

_BASE = "https://power.larc.nasa.gov/api/temporal/daily/point"
_FILL = (-999, -999.0)


def _clean(v):
    """NASA fill value -999 means missing -> None."""
    if v is None or v in _FILL:
        return None
    return v


async def fetch(lat: float, lon: float, location: str, target_date: str = "") -> HistoricalResult:
    if target_date:
        ymd = target_date.replace("-", "")
        used_date = target_date
    else:
        d = datetime.now() - timedelta(days=7)
        ymd = d.strftime("%Y%m%d")
        used_date = d.strftime("%Y-%m-%d")
    data = await get_json(_BASE, params={
        "latitude": lat,
        "longitude": lon,
        "start": ymd,
        "end": ymd,
        "community": "RE",
        "format": "JSON",
        "parameters": "T2M,T2M_MAX,T2M_MIN,PRCPTOTCORR,RH2M,WS10M_MAX",
    })
    param = ((data.get("properties") or {}).get("parameter") or {})
    if not param:
        raise HTTPError("NASA POWER: no data for this date",
                        status=None, provider_hint="nasapower")

    def pick(name):
        return _clean((param.get(name) or {}).get(ymd))

    avg_c = pick("T2M")
    high_c = pick("T2M_MAX")
    low_c = pick("T2M_MIN")
    precip_mm = pick("PRCPTOTCORR")
    avg_humidity = pick("RH2M")
    ws = pick("WS10M_MAX")
    max_wind_kph = ws * 3.6 if ws is not None else None

    # All requested values missing -> no usable record for this point/date.
    if all(v is None for v in (avg_c, high_c, low_c, precip_mm,
                               avg_humidity, max_wind_kph)):
        raise HTTPError("NASA POWER: no data for this date",
                        status=None, provider_hint="nasapower")

    return HistoricalResult(
        source="NASA POWER", location=location, date=used_date,
        high_c=high_c, low_c=low_c, avg_c=avg_c,
        precip_mm=precip_mm, avg_humidity=avg_humidity,
        max_wind_kph=max_wind_kph, description="",
    )
