"""Meteomatics - hourly forecast."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ..base import deg_to_card
_B = "https://api.meteomatics.com"
async def fetch(headers, lat, lon, location, hours=12):
    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT%H:00:00Z")
    end = (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:00:00Z")
    data = await get_json(f"{_B}/{start}--{end}:PT1H/t_2m:C,wind_speed_10m:kmh,wind_dir_10m:d,precip_1h:mm,prob_precip_1h:p/{lat},{lon}/json", headers=headers)
    by_time = {}
    for item in data.get("data",[]):
        param = item.get("parameter","")
        for coord in item.get("coordinates",[]):
            for d in coord.get("dates",[]):
                dt = d.get("date",""); by_time.setdefault(dt, {})[param] = d.get("value")
    entries = []
    for dt in sorted(by_time.keys())[:hours]:
        v = by_time[dt]
        try: tm = datetime.fromisoformat(dt.replace("Z","+00:00")).strftime("%I %p").lstrip("0")
        except Exception: tm = dt
        entries.append(HourlyEntry(time=tm, temp_c=v.get("t_2m:C"), precip_mm=v.get("precip_1h:mm"), precip_chance=v.get("prob_precip_1h:p"), wind_kph=v.get("wind_speed_10m:kmh"), wind_dir=deg_to_card(v.get("wind_dir_10m:d"))))
    return HourlyResult(source="Meteomatics", location=location, hours=entries)
