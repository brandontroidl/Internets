import re
import logging
import requests
from .units import cf, kph_from_ms, km_mi, mb_from_pa, deg_to_card, fmt_dt, fmt_short

log = logging.getLogger("internets.nws")

_NWS_BASE  = "https://api.weather.gov"
_ALERT_ICON = {"Extreme": "‼", "Severe": "!", "Moderate": "~", "Minor": "-"}


def get_grid(lat, lon, headers):
    try:
        r = requests.get(f"{_NWS_BASE}/points/{lat:.4f},{lon:.4f}", headers=headers, timeout=10)
        r.raise_for_status()
        return r.json().get("properties")
    except Exception as e:
        log.warning(f"NWS grid {lat:.4f},{lon:.4f}: {e}")
    return None


def current(lat, lon, grid, headers):
    try:
        r   = requests.get(grid["observationStations"], headers=headers, timeout=10)
        sid = r.json()["features"][0]["properties"]["stationIdentifier"]
        obs = requests.get(
            f"{_NWS_BASE}/stations/{sid}/observations/latest",
            headers=headers, timeout=10,
        ).json()["properties"]

        wind_ms  = obs.get("windSpeed",          {}).get("value")
        wind_deg = obs.get("windDirection",      {}).get("value")
        hi_c     = obs.get("heatIndex",          {}).get("value")
        wc_c     = obs.get("windChill",          {}).get("value")
        humidity = obs.get("relativeHumidity",   {}).get("value")

        if wind_ms is not None and wind_ms < 0.5:
            wind_str = "Calm"
        elif wind_ms is not None:
            card     = deg_to_card(wind_deg)
            wind_str = f"from {card} at {kph_from_ms(wind_ms)}" if card else kph_from_ms(wind_ms)
        else:
            wind_str = "N/A"

        parts = [
            f"Conditions {obs.get('textDescription', 'N/A') or 'N/A'}",
            f"Temperature {cf(obs.get('temperature', {}).get('value'))}",
        ]
        if hi_c is not None: parts.append(f"Heat index {cf(hi_c)}")
        if wc_c is not None: parts.append(f"Wind chill {cf(wc_c)}")
        parts += [
            f"Dew point {cf(obs.get('dewpoint', {}).get('value'))}",
            f"Pressure {mb_from_pa(obs.get('barometricPressure', {}).get('value'))}",
            f"Humidity {f'{humidity:.0f}%' if humidity is not None else 'N/A'}",
            f"Visibility {km_mi(obs.get('visibility', {}).get('value'))}",
            f"Wind {wind_str}",
            f"Updated {fmt_dt(obs.get('timestamp', ''))}",
        ]
        return " :: ".join(parts)
    except Exception as e:
        log.warning(f"NWS current: {e}")
    return None


def forecast(grid, headers):
    try:
        periods = requests.get(
            grid["forecast"], headers=headers, timeout=10,
        ).json()["properties"]["periods"]

        days, i = [], 0
        while i < len(periods) and len(days) < 4:
            p = periods[i]
            if not p["isDaytime"]:
                i += 1
                continue
            hi_c = (p["temperature"] - 32) * 5/9 if p["temperatureUnit"] == "F" else p["temperature"]
            lo_c = None
            if i + 1 < len(periods) and not periods[i + 1]["isDaytime"]:
                nt   = periods[i + 1]
                lo_c = (nt["temperature"] - 32) * 5/9 if nt["temperatureUnit"] == "F" else nt["temperature"]
                i   += 2
            else:
                i += 1
            days.append((p["name"], p.get("shortForecast", ""), hi_c, lo_c))

        return " :: ".join(
            f"{name} {cond} {cf(hi)} / {cf(lo) if lo is not None else 'N/A'}"
            for name, cond, hi, lo in days
        ) or None
    except Exception as e:
        log.warning(f"NWS forecast: {e}")
    return None


def hourly(grid, headers):
    try:
        periods = requests.get(
            grid["forecastHourly"], headers=headers, timeout=10,
        ).json()["properties"]["periods"][:8]

        chunks = []
        for p in periods:
            t_c   = (p["temperature"] - 32) * 5/9 if p["temperatureUnit"] == "F" else p["temperature"]
            pop   = p.get("probabilityOfPrecipitation", {})
            pop   = pop.get("value") if isinstance(pop, dict) else None
            pop_s = f" {pop:.0f}%🌧" if pop and pop >= 20 else ""
            chunks.append(f"{fmt_short(p['startTime'])} {p.get('shortForecast', '')} {cf(t_c)}{pop_s}")
        return " :: ".join(chunks)
    except Exception as e:
        log.warning(f"NWS hourly: {e}")
    return None


def alerts(lat, lon, headers):
    """Returns a list of formatted alert lines, [] if none active, None on API error."""
    try:
        r = requests.get(
            f"{_NWS_BASE}/alerts/active",
            params={"point": f"{lat:.4f},{lon:.4f}", "status": "actual"},
            headers=headers, timeout=10,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
        if not features:
            return []

        lines = []
        for feat in features[:5]:
            p        = feat.get("properties", {})
            event    = p.get("event", "Unknown Alert")
            severity = p.get("severity", "Unknown")
            urgency  = p.get("urgency", "")
            icon     = _ALERT_ICON.get(severity, "?")
            headline = (p.get("headline") or p.get("description", "")[:120] or "").replace("\n", " ").strip()
            if len(headline) > 200:
                headline = headline[:197] + "..."
            onset   = p.get("onset") or p.get("effective", "")
            expires = p.get("expires") or p.get("ends", "")
            onset_s, exp_s = fmt_short(onset) if onset else "", fmt_short(expires) if expires else ""
            if onset_s and exp_s:   time_s = f" | {onset_s} → {exp_s}"
            elif exp_s:             time_s = f" | expires {exp_s}"
            else:                   time_s = ""
            lines.append(f"{icon} {event} [{severity}/{urgency}]{time_s} :: {headline}")
        return lines
    except Exception as e:
        log.warning(f"NWS alerts: {e}")
    return None


def discussion(grid, headers):
    """
    Fetch and parse the Area Forecast Discussion (AFD) for the grid's CWA.

    NWS AFD format: sections separated by && (inline or on its own line),
    each beginning with a .LABEL... line followed by prose wrapped at ~66 chars.
    Returns up to 4 formatted [LABEL] summary lines, or None on any failure.
    """
    try:
        office = grid.get("cwa", "")
        if not office:
            return None

        products = requests.get(
            f"{_NWS_BASE}/products/types/AFD/locations/{office}",
            headers=headers, timeout=10,
        )
        products.raise_for_status()
        items = products.json().get("@graph", [])
        if not items:
            return None

        product = requests.get(
            f"{_NWS_BASE}/products/{items[0]['id']}",
            headers=headers, timeout=10,
        )
        product.raise_for_status()
        text = product.json().get("productText", "")
        if not text:
            return None

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        out  = []

        for section in re.split(r"\s*&&\s*", text):
            section = section.strip()
            if not section:
                continue
            lm = re.search(r"^\.(\w[A-Z0-9 /()\-]+?)\s*\.\.\.", section, re.MULTILINE)
            if not lm:
                continue

            prose_raw   = re.sub(r"^\S[^\n]*\n", "\n", section[lm.end():])
            prose_lines = [
                ln.strip() for ln in prose_raw.splitlines()
                if ln.strip() and not re.match(r"^\*+[^*]+\*+$", ln.strip())
            ]
            if not prose_lines:
                continue

            prose = re.sub(r"\s+", " ", " ".join(prose_lines)).strip()
            if len(prose) > 350:
                prose = prose[:350].rsplit(" ", 1)[0] + " ..."

            out.append(f"[{lm.group(1).strip()}] {prose}")
            if len(out) >= 4:
                break

        return out or None
    except Exception as e:
        log.warning(f"NWS discussion: {e}")
    return None
