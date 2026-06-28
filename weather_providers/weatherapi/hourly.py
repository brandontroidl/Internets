"""WeatherAPI.com — hourly forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
_B = "https://api.weatherapi.com/v1"
async def fetch(key, lat, lon, location, hours=12):
    data = await get_json(f"{_B}/forecast.json", params={"key": key, "q": f"{lat},{lon}", "days": max(1,(hours+23)//24), "aqi": "no", "alerts": "no"})
    now = datetime.now(); entries = []
    for fd in data.get("forecast",{}).get("forecastday",[]):
        for h in fd.get("hour",[]):
            try:
                if datetime.fromisoformat(h.get("time","")) < now: continue
            except Exception: pass  # nosec B110: best-effort cleanup
            if len(entries) >= hours: break
            try: tm = datetime.fromisoformat(h.get("time","")).strftime("%I %p").lstrip("0")
            except Exception: tm = h.get("time","")
            entries.append(HourlyEntry(time=tm, temp_c=h.get("temp_c"), description=h.get("condition",{}).get("text",""), precip_mm=h.get("precip_mm"), precip_chance=h.get("chance_of_rain"), humidity=h.get("humidity"), wind_kph=h.get("wind_kph"), wind_dir=h.get("wind_dir","")))
        if len(entries) >= hours: break
    return HourlyResult(source="WeatherAPI", location=location, hours=entries)
