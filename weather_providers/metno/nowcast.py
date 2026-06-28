"""MET Norway — precipitation nowcast (nowcast/2.0, Nordic radar only)."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json, HTTPError
from ..base import NowcastResult, NowcastEntry

_BASE = "https://api.met.no/weatherapi/nowcast/2.0/complete"
_HEADERS = {"User-Agent": "Internets-IRC-Bot/2.x github.com/brandontroidl/Internets"}


def _intensity(mm):
    if mm is None or mm <= 0:
        return "none"
    if mm < 0.5:
        return "light"
    if mm < 2.0:
        return "moderate"
    return "heavy"


async def fetch(lat: float, lon: float, location: str, steps: int = 8) -> NowcastResult:
    try:
        data = await get_json(_BASE, params={"lat": round(lat, 4), "lon": round(lon, 4)},
                              headers=_HEADERS)
    except HTTPError as e:
        # 422 = outside Nordic radar coverage -> let dispatcher fall back.
        if e.status == 422:
            raise HTTPError("MET Norway: no radar coverage for this location",
                            status=None, provider_hint="metno") from e
        raise

    props = data.get("properties", {})
    cov = props.get("meta", {}).get("radar_coverage", "ok")
    if cov and str(cov).lower() not in ("ok", ""):
        raise HTTPError(f"MET Norway: radar coverage {cov}",
                        status=None, provider_hint="metno")

    ts = props.get("timeseries", [])
    entries: list[NowcastEntry] = []
    for item in ts[: max(steps, 0)]:
        n1 = item.get("data", {}).get("next_1_hours", {})
        mm = n1.get("details", {}).get("precipitation_amount")
        try:
            tm = datetime.fromisoformat(
                item.get("time", "").replace("Z", "+00:00")
            ).strftime("%I:%M %p").lstrip("0")
        except Exception:  # noqa: BLE001
            tm = item.get("time", "")
        entries.append(NowcastEntry(
            time=tm, precip_mm=mm, precip_type="rain" if (mm or 0) > 0 else "",
            intensity=_intensity(mm)))

    summary = "No precipitation expected shortly"
    for e in entries:
        if e.precip_mm and e.precip_mm > 0:
            summary = f"Precipitation around {e.time}"
            break
    return NowcastResult(source="MET Norway", location=location,
                         summary=summary, entries=entries)
