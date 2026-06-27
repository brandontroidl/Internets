"""Weather command module — multi-provider with automatic fallback.

Commands:
    .weather / .w     — current conditions
    .forecast / .f    — multi-day daily forecast
    .hourly / .h      — hourly forecast (next 12 hours)
    .alerts / .al     — active weather alerts and warnings
    .aqi / .air       — air quality index and pollutants
    .astro / .sun     — sunrise, sunset, moon phase
    .history / .hist  — weather on a past date (YYYY-MM-DD)
    .marine / .sea    — ocean conditions (wave height, swell, water temp)
"""

from __future__ import annotations

import re
import logging
from typing import Any

from .base    import BotModule
from .geocode import geocode
from .units   import cf, kph, km_mi, mb, aqi_fmt, wave_fmt, swell_fmt

log = logging.getLogger("internets.weather")

_IRC_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(s: str, max_len: int = 200) -> str:
    """Strip IRC control chars and truncate untrusted API strings."""
    return _IRC_CTRL_RE.sub("", s)[:max_len]


# ── Formatters ────────────────────────────────────────────────────────

def _format_current(r: object) -> str:
    from weather_providers import WeatherResult
    if not isinstance(r, WeatherResult):
        raise TypeError(f"expected WeatherResult, got {type(r).__name__}")

    if r.wind_kph is not None and r.wind_kph < 1:
        wind_str = "Calm"
    elif r.wind_kph is not None:
        wd = _sanitize(r.wind_dir, 4)
        wind_str = f"from {wd} at {kph(r.wind_kph)}" if wd else kph(r.wind_kph)
    else:
        wind_str = "N/A"

    desc = _sanitize(r.description)
    source = _sanitize(r.source, 30)

    parts: list[str] = [f"Conditions {desc}", f"Temperature {cf(r.temperature)}"]

    if (r.feels_like_c is not None and r.temperature is not None
            and abs(r.feels_like_c - r.temperature) >= 2):
        parts.append(f"Feels like {cf(r.feels_like_c)}")

    parts += [
        f"Dew point {cf(r.dewpoint_c)}",
        f"Pressure {mb(r.pressure_mb)}",
        f"Humidity {f'{r.humidity:.0f}%' if r.humidity is not None else 'N/A'}",
        f"Visibility {km_mi(r.visibility_m)}",
        f"Wind {wind_str}",
    ]
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_forecast(r: object) -> str:
    from weather_providers import WeatherResult
    if not isinstance(r, WeatherResult):
        raise TypeError(f"expected WeatherResult, got {type(r).__name__}")
    if not r.forecast:
        return ""
    source = _sanitize(r.source, 30)
    chunks: list[str] = []
    for day in r.forecast:
        hi = cf(day.high_c)
        lo = cf(day.low_c) if day.low_c is not None else "N/A"
        name = _sanitize(day.day_name, 20)
        desc = _sanitize(day.description)
        chunks.append(f"{name} {desc} {hi} / {lo}")
    chunks.append(f"[{source}]")
    return " :: ".join(chunks)


def _format_hourly(r: object) -> str:
    from weather_providers import HourlyResult
    if not isinstance(r, HourlyResult):
        raise TypeError(f"expected HourlyResult, got {type(r).__name__}")
    if not r.hours:
        return ""
    source = _sanitize(r.source, 30)
    chunks: list[str] = []
    for h in r.hours[:12]:
        t = _sanitize(h.time, 10)
        temp = cf(h.temp_c) if h.temp_c is not None else "N/A"
        desc = _sanitize(h.description, 20)
        parts = [f"{t} {temp}"]
        if desc:
            parts[0] += f" {desc}"
        if h.precip_chance is not None and h.precip_chance > 0:
            parts[0] += f" ({h.precip_chance:.0f}% precip)"
        chunks.append(parts[0])
    chunks.append(f"[{source}]")
    return " :: ".join(chunks)


def _format_alerts(r: object) -> list[str]:
    from weather_providers import AlertsResult
    if not isinstance(r, AlertsResult):
        raise TypeError(f"expected AlertsResult, got {type(r).__name__}")
    if not r.alerts:
        return [f"No active alerts. [{_sanitize(r.source, 30)}]"]
    lines: list[str] = []
    for a in r.alerts[:5]:
        sev = _sanitize(a.severity, 10).upper()
        event = _sanitize(a.event, 60)
        headline = _sanitize(a.headline, 200)
        line = f"[{sev}] {event}"
        if headline and headline != event:
            line += f" — {headline}"
        lines.append(line)
    lines.append(f"[{_sanitize(r.source, 30)}]")
    return lines


def _format_aqi(r: object) -> str:
    from weather_providers import AirQualityResult
    if not isinstance(r, AirQualityResult):
        raise TypeError(f"expected AirQualityResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)
    parts: list[str] = [aqi_fmt(r.aqi, r.category)]
    if r.pm25 is not None:
        parts.append(f"PM2.5 {r.pm25:.1f}μg/m³")
    if r.pm10 is not None:
        parts.append(f"PM10 {r.pm10:.1f}μg/m³")
    if r.o3 is not None:
        parts.append(f"O₃ {r.o3:.1f}μg/m³")
    if r.no2 is not None:
        parts.append(f"NO₂ {r.no2:.1f}μg/m³")
    if r.co is not None:
        parts.append(f"CO {r.co:.1f}μg/m³")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_astronomy(r: object) -> str:
    from weather_providers import AstronomyResult
    if not isinstance(r, AstronomyResult):
        raise TypeError(f"expected AstronomyResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)
    parts: list[str] = []
    if r.sunrise:
        parts.append(f"Sunrise {_sanitize(r.sunrise, 20)}")
    if r.sunset:
        parts.append(f"Sunset {_sanitize(r.sunset, 20)}")
    if r.day_length:
        parts.append(f"Day length {_sanitize(r.day_length, 20)}")
    if r.moonrise:
        parts.append(f"Moonrise {_sanitize(r.moonrise, 20)}")
    if r.moonset:
        parts.append(f"Moonset {_sanitize(r.moonset, 20)}")
    if r.moon_phase:
        parts.append(f"Moon {_sanitize(r.moon_phase, 30)}")
    if r.moon_illumination is not None:
        parts.append(f"Illumination {r.moon_illumination:.0f}%")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_historical(r: object) -> str:
    from weather_providers import HistoricalResult
    if not isinstance(r, HistoricalResult):
        raise TypeError(f"expected HistoricalResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)
    parts: list[str] = [f"Date {_sanitize(r.date, 20)}"]
    if r.description:
        parts.append(f"Conditions {_sanitize(r.description)}")
    if r.high_c is not None:
        parts.append(f"High {cf(r.high_c)}")
    if r.low_c is not None:
        parts.append(f"Low {cf(r.low_c)}")
    if r.avg_c is not None:
        parts.append(f"Avg {cf(r.avg_c)}")
    if r.precip_mm is not None:
        parts.append(f"Precip {r.precip_mm:.1f}mm")
    if r.max_wind_kph is not None:
        parts.append(f"Wind {kph(r.max_wind_kph)}")
    if r.avg_humidity is not None:
        parts.append(f"Humidity {r.avg_humidity:.0f}%")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_marine(r: object) -> str:
    from weather_providers import MarineResult
    if not isinstance(r, MarineResult):
        raise TypeError(f"expected MarineResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)
    parts: list[str] = []
    if r.wave_height_m is not None:
        parts.append(wave_fmt(r.wave_height_m, r.wave_period_s, r.wave_direction))
    if r.swell_height_m is not None:
        parts.append(swell_fmt(r.swell_height_m, r.swell_period_s, r.swell_direction))
    if r.wind_wave_height_m is not None:
        parts.append(f"Wind waves {r.wind_wave_height_m:.1f}m / {r.wind_wave_height_m * 3.281:.1f}ft")
    if r.water_temp_c is not None:
        parts.append(f"Water {cf(r.water_temp_c)}")
    if not parts:
        parts.append("No marine data available")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_uv(r: object) -> str:
    from weather_providers import UVResult
    if not isinstance(r, UVResult):
        raise TypeError(f"expected UVResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)
    parts: list[str] = []
    if r.uv_index is not None:
        cat = f" ({_sanitize(r.category, 12)})" if r.category else ""
        parts.append(f"UV index {r.uv_index:.1f}{cat}")
    else:
        parts.append("UV index N/A")
    if r.uv_max is not None:
        parts.append(f"Peak today {r.uv_max:.1f}")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_pollen(r: object) -> str:
    from weather_providers import PollenResult
    from weather_providers.base import pollen_cat_5
    if not isinstance(r, PollenResult):
        raise TypeError(f"expected PollenResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)

    # Google Pollen — tree/grass/weed Universal Pollen Index (0-5).
    if any(v is not None for v in (r.tree_index, r.grass_index, r.weed_index)):
        parts = [
            f"{label} {pollen_cat_5(val)} ({val:.0f}/5)"
            for label, val in (("Tree", r.tree_index), ("Grass", r.grass_index),
                               ("Weed", r.weed_index)) if val is not None
        ]
        if r.triggers:
            parts.append("Top: " + ", ".join(_sanitize(t, 20) for t in r.triggers))
        parts.append(f"[{source}]")
        return " :: ".join(parts)

    # Pollen.com / IQVIA — overall index (0-12) + dominant allergens.
    if r.overall_index is not None:
        head = f"Pollen index {r.overall_index:.1f}/12"
        if r.category:
            head += f" ({_sanitize(r.category, 16)})"
        parts = [head]
        if r.triggers:
            parts.append("Top: " + ", ".join(_sanitize(t, 20) for t in r.triggers))
        parts.append(f"[{source}]")
        return " :: ".join(parts)

    # Open-Meteo — CAMS per-species concentrations (grains/m³).
    taxa = [("Alder", r.alder), ("Birch", r.birch), ("Grass", r.grass),
            ("Mugwort", r.mugwort), ("Olive", r.olive), ("Ragweed", r.ragweed)]
    parts = [f"{name} {val:.0f}" for name, val in taxa if val is not None]
    if not parts:
        return f"No pollen data for this location. [{source}]"
    parts.append("grains/m³")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_wildfire(r: object) -> str:
    from weather_providers import WildfireResult
    if not isinstance(r, WildfireResult):
        raise TypeError(f"expected WildfireResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)
    if not r.fire_count:
        return f"No active fires detected nearby. [{source}]"
    parts: list[str] = [f"{r.fire_count} active fire(s) nearby"]
    if r.nearest_km is not None:
        nm = f" {_sanitize(r.nearest_name, 40)}" if r.nearest_name else ""
        parts.append(f"Nearest{nm} {r.nearest_km:.0f}km")
    if r.max_acres is not None:
        parts.append(f"Largest {r.max_acres:,.0f} acres")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_space(r: object) -> str:
    from weather_providers import SpaceWeatherResult
    if not isinstance(r, SpaceWeatherResult):
        raise TypeError(f"expected SpaceWeatherResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)
    parts: list[str] = []
    if r.kp_index is not None:
        cat = f" ({_sanitize(r.kp_category, 20)})" if r.kp_category else ""
        parts.append(f"Kp {r.kp_index:.1f}{cat}")
    if r.aurora_pct is not None:
        parts.append(f"Aurora chance {r.aurora_pct:.0f}%")
    if not parts:
        parts.append("Space weather data unavailable")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_tides(r: object) -> str:
    from weather_providers import TideResult
    if not isinstance(r, TideResult):
        raise TypeError(f"expected TideResult, got {type(r).__name__}")
    source = _sanitize(r.source, 30)
    parts: list[str] = []
    if r.station:
        parts.append(f"Station {_sanitize(r.station, 40)}")
    if r.next_high_time:
        h = f" ({r.next_high_m:.1f}m)" if r.next_high_m is not None else ""
        parts.append(f"Next high {_sanitize(r.next_high_time, 30)}{h}")
    if r.next_low_time:
        lo = f" ({r.next_low_m:.1f}m)" if r.next_low_m is not None else ""
        parts.append(f"Next low {_sanitize(r.next_low_time, 30)}{lo}")
    if r.water_temp_c is not None:
        parts.append(f"Water {cf(r.water_temp_c)}")
    if len(parts) <= (1 if r.station else 0):
        parts.append("No tide data available")
    parts.append(f"[{source}]")
    return " :: ".join(parts)


# ── Module ────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# Per-provider flag aliases — each provider gets its own ``-<alias>``
# flag.  All aliases for one provider map to the same canonical id used
# by the dispatcher.  Keep aliases globally unique; if you add a new
# provider, pick a fresh short form (``-aw`` is Apple WeatherKit, not
# AccuWeather; use ``-acc`` for AccuWeather).
_PROVIDER_FLAGS: dict[str, str] = {
    # NWS (US gov)
    "nws":                "nws",
    # Meteomatics
    "meteomatics":        "meteomatics", "mm":            "meteomatics",
    # Apple WeatherKit
    "weatherkit":         "weatherkit",  "wk":            "weatherkit",
    "apple":              "weatherkit",  "appleweather":  "weatherkit",
    "aw":                 "weatherkit",
    # Open-Meteo
    "openmeteo":          "openmeteo",   "om":            "openmeteo",
    # Visual Crossing
    "visualcrossing":     "visualcrossing", "vc":         "visualcrossing",
    # AccuWeather
    "accuweather":        "accuweather", "acc":           "accuweather",
    # OpenWeatherMap
    "openweathermap":     "openweathermap", "owm":        "openweathermap",
    # WeatherBit
    "weatherbit":         "weatherbit",  "wb":            "weatherbit",
    # WeatherAPI.com
    "weatherapi":         "weatherapi",  "wapi":          "weatherapi",
    # Pirate Weather (Dark Sky compat)
    "pirateweather":      "pirateweather", "pirate":      "pirateweather",
    "pw":                 "pirateweather",
    # Stormglass (marine)
    "stormglass":         "stormglass",  "sg":            "stormglass",
    # Tomorrow.io
    "tomorrowio":         "tomorrowio",  "tio":           "tomorrowio",
    "tomorrow":           "tomorrowio",
    # World Weather Online
    "worldweatheronline": "worldweatheronline", "wwo":    "worldweatheronline",
    # Weatherstack
    "weatherstack":       "weatherstack", "ws":           "weatherstack",
    # AirNow (US EPA air quality — air_quality only)
    "airnow":             "airnow",      "an":            "airnow",
    # PurpleAir (crowdsourced PM2.5 — air_quality only)
    "purpleair":          "purpleair",   "pa":            "purpleair",
    # WAQI / OpenAQ / IQAir (air_quality)
    "waqi":               "waqi",
    "openaq":             "openaq",      "oaq":           "openaq",
    "iqair":              "iqair",       "iq":            "iqair",
    # MET Norway / Yr (current/forecast/hourly/alerts/nowcast)
    "metno":              "metno",       "yr":            "metno",
    # SunriseSunset.io (astronomy)
    "sunrisesunset":      "sunrisesunset", "ss":          "sunrisesunset",
    # currentuvindex.com (uv)
    "currentuvindex":     "currentuvindex", "cuv":        "currentuvindex",
    # GDACS / ECCC (alerts)
    "gdacs":              "gdacs",
    "eccc":               "eccc",
    # NASA POWER (historical)
    "nasapower":          "nasapower",   "power":         "nasapower",
    # NIFC / NASA FIRMS (wildfire)
    "nifc":               "nifc",
    "firms":              "firms",
    # NOAA SWPC (space_weather)
    "swpc":               "swpc",
    # TideCheck / NOAA CO-OPS (tides)
    "tidecheck":          "tidecheck",   "tc":            "tidecheck",
    "noaa_coops":         "noaa_coops",  "coops":         "noaa_coops",
    # Pollen.com / IQVIA (US pollen) + Google Pollen (global pollen)
    "pollendotcom":       "pollendotcom", "pollencom":    "pollendotcom",
    "pc":                 "pollendotcom",
    "googlepollen":       "google_pollen", "google_pollen": "google_pollen",
    "gp":                 "google_pollen",
}


def _parse_weather_flags(arg: str | None
                         ) -> tuple[str | None, bool, str | None, str | None]:
    """Pull provider flags and ``-l`` out of a weather command arg.

    Recognized anywhere in the line:
        -l              list active providers for the capability
        -<provider>     force a specific provider (e.g. -wk, -appleweather,
                        -visualcrossing, -nws, -om, -vc, -pw, -sg, -wb)
        -p <name>       backwards-compatible explicit form
    Unrecognized tokens (including ``-n <nick>`` and bare ``YYYY-MM-DD``)
    are passed through unchanged in the returned ``rest``.

    Returns (force_provider, list_mode, rest_arg_or_None, unknown_flag_or_None).
    ``unknown_flag`` is the first ``-foo`` token that wasn't a recognized
    provider, ``-l``, ``-p``, or ``-n`` — caller can warn the user.
    """
    if not arg:
        return None, False, None, None
    tokens = arg.strip().split()
    force_provider: str | None = None
    list_mode = False
    unknown_flag: str | None = None
    keep: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        low = t.lower()
        if low == "-l":
            list_mode = True
            i += 1
            continue
        if low == "-p" and i + 1 < len(tokens):
            cand = tokens[i + 1].lower()
            force_provider = _PROVIDER_FLAGS.get(cand, cand)
            i += 2
            continue
        if low == "-n":
            # _resolve() handles `-n <nick>` itself.  Keep both tokens
            # together so the existing regex still matches.
            keep.append(t)
            if i + 1 < len(tokens):
                keep.append(tokens[i + 1])
                i += 2
            else:
                i += 1
            continue
        if low.startswith("-") and len(low) > 1 and not low[1].isdigit():
            # Looks like a flag — check provider alias table.
            alias = low[1:]
            canonical = _PROVIDER_FLAGS.get(alias)
            if canonical:
                force_provider = canonical
                i += 1
                continue
            # Unknown flag: remember the first one and drop the token so
            # it doesn't pollute the geocoder query.
            if unknown_flag is None:
                unknown_flag = t
            i += 1
            continue
        keep.append(t)
        i += 1
    rest = " ".join(keep) if keep else None
    return force_provider, list_mode, rest, unknown_flag


def _flag_examples_for(canonical: str) -> str:
    """Return ``"-foo/-bar"`` style alias list for a canonical provider id."""
    aliases = sorted({a for a, c in _PROVIDER_FLAGS.items() if c == canonical},
                     key=len)
    return "/".join(f"-{a}" for a in aliases)


class WeatherModule(BotModule):
    """Multi-provider weather commands — current, forecast, hourly, alerts,
    air quality, astronomy, historical, and marine conditions.

    All commands accept two leading flags:
        -p <provider>   force a specific active provider (no fallback)
        -l              list active providers + capabilities, then exit
    """

    COMMANDS: dict[str, str] = {
        "weather":   "cmd_weather",   "w":    "cmd_weather",
        "forecast":  "cmd_forecast",  "f":    "cmd_forecast",
        "hourly":    "cmd_hourly",    "h":    "cmd_hourly",
        "alerts":    "cmd_alerts",    "al":   "cmd_alerts",
        "aqi":       "cmd_aqi",       "air":  "cmd_aqi",
        "astro":     "cmd_astro",     "sun":  "cmd_astro",
        "history":   "cmd_history",   "hist": "cmd_history",
        "marine":    "cmd_marine",    "sea":  "cmd_marine",
        "nowcast":   "cmd_nowcast",   "nc":   "cmd_nowcast",
        "uv":        "cmd_uv",        "uvi":  "cmd_uv",
        "pollen":    "cmd_pollen",    "allergy": "cmd_pollen",
        "wildfire":  "cmd_wildfire",  "fire": "cmd_wildfire",
        "space":     "cmd_space",     "aurora": "cmd_space",
        "tides":     "cmd_tides",     "tide": "cmd_tides",
        "providers": "cmd_providers",
    }

    def on_load(self) -> None:
        from weather_providers import configure
        import secret_store
        configure(self.bot.cfg)
        self._ua = (secret_store.get("weather_user_agent")
                    or self.bot.cfg["weather"]["user_agent"])
        self._cooldown = int(self.bot.cfg["bot"]["api_cooldown"])
        # Home country for resolving bare, cross-country postal codes
        # (geocode() validates/normalizes; a bad value falls back to "us").
        self._default_country = self.bot.cfg["weather"].get("default_country", "us")

    def _resolve(self, nick: str, arg: str | None) -> tuple[str | None, str | None]:
        if arg:
            m = re.match(r"^-n\s+(\S+)$", arg.strip(), re.IGNORECASE)
            if m:
                target = m.group(1)
                # Privacy: refuse cross-user lookups for opted-out nicks.
                # The invoker can still look up themselves regardless of
                # their own opt-out state.  We probe the store via the
                # public is_opted_out() helper; if it isn't available
                # (older Store) the check defaults to allow.
                if target.lower() != nick.lower():
                    is_opted_out = getattr(
                        getattr(self.bot, "_store", None), "is_opted_out", None,
                    )
                    if callable(is_opted_out) and is_opted_out(target):
                        return None, f"{target} has opted out of location sharing."
                saved = self.bot.loc_get(target)
                return (saved, None) if saved else (None, f"{target} has no saved location.")
            return arg.strip(), None
        saved = self.bot.loc_get(nick)
        if saved:
            return saved, None
        # Fall back to the operator-configured default_location if set, so the
        # bot answers .weather out of the box before anyone runs regloc.
        default = self.bot.cfg["bot"].get("default_location", "").strip()
        if default:
            return default, None
        p = self.bot.cfg["bot"]["command_prefix"]
        return None, f"{nick}: no location saved — use {p}regloc <city or zip> first."

    async def _geo(self, nick: str, reply_to: str,
                   arg: str | None) -> tuple[float, float, str, str] | None:
        raw, err = self._resolve(nick, arg)
        if raw is None:
            self.bot.privmsg(reply_to, err)
            return None
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down ({self._cooldown}s cooldown)")
            return None
        geo = await geocode(raw, self._ua, default_country=self._default_country)
        if geo is None:
            self.bot.privmsg(reply_to, f"{nick}: location not found: '{raw}'")
        return geo

    # State badge for ``provider_status()`` — only "active"/"cold"/"failing"
    # actually appear in `-l` output since unconfigured providers are hidden.
    _STATE_BADGE: dict[str, str] = {
        "active":  "[OK]",   # registered + auth working + recent calls succeeded
        "cold":    "[?]",    # registered, no calls yet
        "failing": "[X]",    # registered but recent calls failed
    }

    def _send_provider_list(self, nick: str, reply_to: str, capability: str) -> None:
        """Public ``-l`` listing: only active + configured providers, ranked by
        accuracy for this capability, each tagged with auth state.  Providers
        without keys are hidden — only what's usable shows up."""
        from weather_providers import dispatcher, provider_status
        from weather_providers._dispatch import CAPABILITY_METHODS
        if capability not in CAPABILITY_METHODS:
            self.bot.preply(nick, reply_to, f"{nick}: unknown capability '{capability}'")
            return
        status = {s["id"]: s for s in provider_status()}
        chain = dispatcher._sorted_for_capability(capability)
        if not chain:
            self.bot.preply(nick, reply_to,
                f"{nick}: no active providers support '{capability}'. "
                "Configure an API key via `python -m secret_store set <name>`.")
            return
        parts: list[str] = []
        for i, pid in enumerate(chain):
            s = status.get(pid, {})
            badge = self._STATE_BADGE.get(s.get("state", ""), "[?]")
            parts.append(f"{i+1}.{pid} {badge} ({_flag_examples_for(pid)})")
        self.bot.preply(nick, reply_to,
            f"{nick}: {capability} providers (most → least accurate): "
            + ", ".join(parts))
        self.bot.preply(nick, reply_to,
            f"{nick}: legend  [OK]=auth ok, calls succeeding  "
            "[?]=loaded, untested  [X]=loaded but failing")

    def _validate_provider(self, nick: str, reply_to: str,
                           provider: str, capability: str) -> bool:
        """Returns True if provider is active and supports capability."""
        from weather_providers import dispatcher, provider_capabilities
        if provider not in dispatcher.provider_ids:
            active = ", ".join(sorted(dispatcher.provider_ids)) or "(none)"
            self.bot.preply(nick, reply_to,
                f"{nick}: provider '{provider}' is not active. Active: {active}")
            return False
        if capability not in provider_capabilities(provider):
            self.bot.preply(nick, reply_to,
                f"{nick}: provider '{provider}' doesn't support '{capability}'.")
            return False
        return True

    def _warn_unknown_flag(self, nick: str, reply_to: str, flag: str) -> None:
        """Tell the user an unrecognized flag was ignored."""
        self.bot.preply(nick, reply_to,
            f"{nick}: unknown flag {flag!r} — try -l to list providers.")

    # ── Commands ─────────────────────────────────────────────────────

    async def _weather_cmd(self, name: str, capability: str,
                           nick: str, reply_to: str, arg: str | None,
                           fetch_fn: Any, format_fn: Any,
                           fail_msg: str, **kw: Any) -> None:
        """Generic weather command: flags → geo → fetch → format → send."""
        provider, list_mode, rest, bad_flag = _parse_weather_flags(arg)
        if bad_flag:
            self._warn_unknown_flag(nick, reply_to, bad_flag)
            return
        if list_mode:
            self._send_provider_list(nick, reply_to, capability)
            return
        if provider and not self._validate_provider(nick, reply_to, provider, capability):
            return
        geo = await self._geo(nick, reply_to, rest)
        if geo is None:
            return
        lat, lon, display, cc = geo
        log.info("%s%s %r (%s) [%.4f,%.4f]",
                 name, f" [{provider}]" if provider else "",
                 display, cc or "?", lat, lon)
        if provider:
            kw["force_provider"] = provider
        result = await fetch_fn(lat, lon, display, **kw)
        if result:
            self.bot.privmsg(reply_to, f":: {display} :: {format_fn(result)} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: {fail_msg}")

    async def cmd_weather(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_weather
        await self._weather_cmd("weather", "current", nick, reply_to, arg,
            get_weather, _format_current, "weather data unavailable right now.")

    async def cmd_forecast(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_forecast
        await self._weather_cmd("forecast", "forecast", nick, reply_to, arg,
            get_forecast, _format_forecast, "forecast unavailable right now.", days=4)

    async def cmd_hourly(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_hourly
        await self._weather_cmd("hourly", "hourly", nick, reply_to, arg,
            get_hourly, _format_hourly, "hourly forecast unavailable right now.", hours=12)

    async def cmd_alerts(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_alerts
        provider, list_mode, rest, bad_flag = _parse_weather_flags(arg)
        if bad_flag:
            self._warn_unknown_flag(nick, reply_to, bad_flag)
            return
        if list_mode:
            self._send_provider_list(nick, reply_to, "alerts")
            return
        if provider and not self._validate_provider(nick, reply_to, provider, "alerts"):
            return
        geo = await self._geo(nick, reply_to, rest)
        if geo is None:
            return
        lat, lon, display, cc = geo
        log.info("alerts%s %r (%s) [%.4f,%.4f]",
                 f" [{provider}]" if provider else "",
                 display, cc or "?", lat, lon)
        kwargs = {"force_provider": provider} if provider else {}
        result = await get_alerts(lat, lon, display, **kwargs)
        if result:
            self.bot.privmsg(reply_to, f":: {display} Alerts ::")
            for line in _format_alerts(result):
                self.bot.privmsg(reply_to, f"  {line}")
        else:
            self.bot.privmsg(reply_to, f"{nick}: alert data unavailable.")

    async def cmd_aqi(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_air_quality
        await self._weather_cmd("aqi", "air_quality", nick, reply_to, arg,
            get_air_quality, _format_aqi, "air quality data unavailable right now.")

    async def cmd_astro(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_astronomy
        await self._weather_cmd("astro", "astronomy", nick, reply_to, arg,
            get_astronomy, _format_astronomy, "astronomy data unavailable right now.")

    async def cmd_marine(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_marine
        await self._weather_cmd("marine", "marine", nick, reply_to, arg,
            get_marine, _format_marine, "marine data unavailable — location may be inland.")

    async def cmd_history(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_historical
        provider, list_mode, rest, bad_flag = _parse_weather_flags(arg)
        if bad_flag:
            self._warn_unknown_flag(nick, reply_to, bad_flag)
            return
        if list_mode:
            self._send_provider_list(nick, reply_to, "historical")
            return
        if provider and not self._validate_provider(nick, reply_to, provider, "historical"):
            return
        target_date = ""
        loc_arg = rest
        if rest:
            parts = rest.split(None, 1)
            if parts and _DATE_RE.match(parts[0]):
                target_date = parts[0]
                loc_arg = parts[1] if len(parts) > 1 else None
        kw: dict[str, Any] = {"target_date": target_date}
        if provider:
            kw["force_provider"] = provider
        geo = await self._geo(nick, reply_to, loc_arg)
        if geo is None:
            return
        lat, lon, display, cc = geo
        log.info("history%s%s %r (%s) [%.4f,%.4f]",
                 f" {target_date}" if target_date else "",
                 f" [{provider}]" if provider else "",
                 display, cc or "?", lat, lon)
        result = await get_historical(lat, lon, display, **kw)
        if result:
            self.bot.privmsg(reply_to, f":: {display} :: {_format_historical(result)} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: historical data unavailable right now.")

    async def cmd_nowcast(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_nowcast
        provider, list_mode, rest, bad_flag = _parse_weather_flags(arg)
        if bad_flag:
            self._warn_unknown_flag(nick, reply_to, bad_flag)
            return
        if list_mode:
            self._send_provider_list(nick, reply_to, "nowcast")
            return
        if provider and not self._validate_provider(nick, reply_to, provider, "nowcast"):
            return
        geo = await self._geo(nick, reply_to, rest)
        if geo is None:
            return
        lat, lon, display, cc = geo
        log.info("nowcast%s %r (%s) [%.4f,%.4f]",
                 f" [{provider}]" if provider else "",
                 display, cc or "?", lat, lon)
        kwargs = {"force_provider": provider} if provider else {}
        result = await get_nowcast(lat, lon, display, **kwargs)
        if result:
            parts: list[str] = []
            if result.summary:
                parts.append(_sanitize(result.summary))
            for e in result.entries[:8]:
                t = _sanitize(e.time, 10)
                intensity = _sanitize(e.intensity, 10) if e.intensity else ""
                ptype = _sanitize(e.precip_type, 10) if e.precip_type else ""
                label = f"{ptype} {intensity}".strip() or "none"
                mm = f"{e.precip_mm:.1f}mm" if e.precip_mm is not None else ""
                parts.append(f"{t} {label} {mm}".strip())
            parts.append(f"[{_sanitize(result.source, 30)}]")
            self.bot.privmsg(reply_to, f":: {display} :: {' :: '.join(parts)} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: nowcast unavailable — no provider supports precipitation nowcasting.")

    async def cmd_uv(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_uv
        await self._weather_cmd("uv", "uv", nick, reply_to, arg,
            get_uv, _format_uv, "UV data unavailable right now.")

    async def cmd_pollen(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_pollen
        await self._weather_cmd("pollen", "pollen", nick, reply_to, arg,
            get_pollen, _format_pollen, "pollen data unavailable — CAMS covers Europe only.")

    async def cmd_wildfire(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_wildfire
        await self._weather_cmd("wildfire", "wildfire", nick, reply_to, arg,
            get_wildfire, _format_wildfire, "wildfire data unavailable right now.")

    async def cmd_space(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_space_weather
        await self._weather_cmd("space", "space_weather", nick, reply_to, arg,
            get_space_weather, _format_space, "space-weather data unavailable right now.")

    async def cmd_tides(self, nick: str, reply_to: str, arg: str | None) -> None:
        from weather_providers import get_tides
        await self._weather_cmd("tides", "tides", nick, reply_to, arg,
            get_tides, _format_tides, "tide data unavailable — no station near this location.")

    async def cmd_providers(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Show provider health and capability status.  Admin only."""
        if not self.bot.is_admin(nick):
            self.bot.preply(nick, reply_to, f"{nick}: admin only — authenticate first.")
            return
        from weather_providers import dispatcher
        self.bot.preply(nick, reply_to, "Provider health:")
        for line in dispatcher.health_summary().split("\n"):
            self.bot.preply(nick, reply_to, f"  {line}")
        self.bot.preply(nick, reply_to, "Capability chains:")
        for line in dispatcher.capability_matrix().split("\n"):
            self.bot.preply(nick, reply_to, line)

    def help_lines(self, prefix: str) -> list[str]:
        """Compact, grouped help.

        Seven lines + the ``[weather]`` header — small enough to clear the
        send queue's 5-message burst near-instantly and stay well within a
        10-msg/3-sec network flood limit (output is token-bucketed in
        sender.py regardless).  Commands are grouped by theme rather than
        listed one-per-line, and provider flags are summarised with a
        pointer to ``-l`` instead of dumping all of them — so this stays
        compact no matter how many providers are loaded.
        """
        p = prefix
        from weather_providers import dispatcher
        n = len(dispatcher.provider_ids)

        def row(label: str, body: str) -> str:
            # \x02 = IRC bold; label padded inside the codes so the visible
            # columns line up.  Two leading spaces match the other modules.
            return f"  \x02{label:<8}\x02 {body}"

        lines = [
            row("Weather",  f"{p}weather/.w  {p}forecast/.f  {p}hourly/.h  {p}nowcast/.nc"),
            row("Air/Sky",  f"{p}aqi/.air  {p}uv/.uvi  {p}pollen/.allergy  {p}astro/.sun"),
            row("Hazards",  f"{p}alerts/.al  {p}wildfire/.fire  {p}space/.aurora"),
            row("Sea/Past", f"{p}marine/.sea  {p}tides/.tide  {p}history/.hist"),
            row("Where",    f"city / ZIP / 'lat,lon';  {p}regloc saves yours;  add -n <nick> for someone else's"),
            row("Flags",    f"{n} providers active — force one with -<flag> (e.g. -nws -vc -aw);  {p}<cmd> -l lists them"),
            row("Try",      f"{p}w 90210   {p}aqi -an 67127   {p}f -vc Tokyo   {p}uv London   {p}tides -coops"),
            row("Admin",    f"{p}providers  provider health + capability chains  [admin]"),
        ]
        return lines


def setup(bot: object) -> WeatherModule:
    return WeatherModule(bot)  # type: ignore[arg-type]
