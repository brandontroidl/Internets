"""WeatherBit.io - historical weather (daily summary for past dates)."""
from __future__ import annotations
from datetime import date, timedelta
from .._http import get_json
from ..base import HistoricalResult, ms_to_kph

_B = "https://api.weatherbit.io/v2.0"

async def fetch(key, lat, lon, location, target_date=""):
    if not target_date:
        target_date = (date.today() - timedelta(days=1)).isoformat()
    # WeatherBit history needs start_date and end_date
    end = (date.fromisoformat(target_date) + timedelta(days=1)).isoformat()
    data = await get_json(f"{_B}/history/daily", params={
        "key": key, "lat": lat, "lon": lon, "units": "M",
        "start_date": target_date, "end_date": end,
    })
    items = data.get("data", [])
    if not items:
        raise ValueError("WeatherBit history returned no data")
    d = items[0]
    return HistoricalResult(
        source="WeatherBit",
        location=location,
        date=target_date,
        high_c=d.get("max_temp"),
        low_c=d.get("min_temp"),
        avg_c=d.get("temp"),
        description="",
        precip_mm=d.get("precip"),
        # units=M -> max_wind_spd is m/s; convert to km/h like the siblings.
        max_wind_kph=ms_to_kph(d.get("max_wind_spd")),
        avg_humidity=d.get("rh"),
    )
