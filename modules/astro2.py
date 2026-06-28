"""Astronomy / space-weather commands — all KEYLESS.

  .solar          NOAA SWPC latest GOES X-ray flux class + recent flare
  .neo            NASA NeoWs near-earth-object feed (DEMO_KEY default)
  .launches [n]   The Space Devs upcoming launches (1-3)
  .moon [date]    Moon phase — pure compute, no network
  .sky <object>   Bundled Messier catalog lookup — pure data

Every outbound call goes through ``base.fetch_json`` (size-capped); no
API key is required (NASA ``DEMO_KEY`` is the documented public default,
overridable via ``nasa_api_key`` in the secret store like the apod module).
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

import requests
from .base import (
    BotModule,
    ResponseTooLarge,
    cred,
    fetch_json,
    help_row,
    strip_ctrl,
)

log = logging.getLogger("internets.astro2")

# ── endpoints ─────────────────────────────────────────────────────────
_XRAY_URL = "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-latest.json"
_SSN_URL = "https://services.swpc.noaa.gov/json/sunspot_report.json"
_NEO_URL = "https://api.nasa.gov/neo/rest/v1/feed"
_LAUNCH_URL = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/"

# SWPC / NASA payloads are small JSON arrays/objects; the launch feed with
# the full expand can run large, so it gets a roomier cap.
_LAUNCH_MAX_BYTES = 512 * 1024


# ── .solar — NOAA SWPC GOES X-ray + flare ─────────────────────────────
def _fetch_solar(ua: str) -> str:
    try:
        flare = fetch_json(_XRAY_URL, ua=ua, timeout=10)
        # The endpoint returns a list of recent flare records; take the last.
        rec = flare[-1] if isinstance(flare, list) and flare else flare
        if not isinstance(rec, dict):
            return "solar data unavailable"
        cls = strip_ctrl(rec.get("max_class") or rec.get("current_class")
                         or rec.get("flare_class") or "?", 12)
        when = strip_ctrl(rec.get("max_time") or rec.get("time_tag")
                          or rec.get("begin_time") or "", 32)
        parts = [f"GOES X-ray flare class \x02{cls}\x02"]
        if when:
            parts.append(f"peak {when}")
        # Best-effort sunspot number — non-fatal if the feed is unavailable.
        try:
            ssn = fetch_json(_SSN_URL, ua=ua, timeout=10)
            srec = ssn[-1] if isinstance(ssn, list) and ssn else ssn
            if isinstance(srec, dict):
                num = srec.get("ssn") or srec.get("sunspot_number")
                if num is not None:
                    parts.append(f"SSN {strip_ctrl(num, 8)}")
        except (requests.RequestException, ResponseTooLarge, ValueError,
                KeyError, TypeError):
            pass
        return strip_ctrl(" | ".join(parts))
    except (requests.RequestException, ResponseTooLarge) as e:
        log.warning("solar request: %s", e)
        return "solar lookup failed"
    except (ValueError, KeyError, TypeError, IndexError) as e:
        log.warning("solar parse: %r", e)
        return "solar data unavailable"


# ── .neo — NASA Near-Earth Object feed ────────────────────────────────
def _fetch_neo(key: str, ua: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        d = fetch_json(
            _NEO_URL,
            ua=ua,
            params={"start_date": today, "end_date": today, "api_key": key},
            timeout=12,
        )
        if not isinstance(d, dict):
            return "NEO data unavailable"
        objs = (d.get("near_earth_objects") or {}).get(today) or []
        if not objs:
            return f"NEO {today}: no near-earth objects in feed"
        count = len(objs)
        closest = None
        closest_km = None
        for o in objs:
            for ca in (o.get("close_approach_data") or []):
                try:
                    km = float(ca["miss_distance"]["kilometers"])
                except (KeyError, ValueError, TypeError):
                    continue
                if closest_km is None or km < closest_km:
                    closest_km = km
                    closest = o.get("name", "?")
        head = f"NEO {today}: \x02{count}\x02 near-earth object" + ("s" if count != 1 else "")
        if closest is not None and closest_km is not None:
            head += (f" | closest {strip_ctrl(closest, 60)} "
                     f"({closest_km:,.0f} km)")
        return strip_ctrl(head)
    except (requests.RequestException, ResponseTooLarge) as e:
        log.warning("neo request: %s", e)
        return "NEO lookup failed"
    except (ValueError, KeyError, TypeError) as e:
        log.warning("neo parse: %r", e)
        return "NEO data unavailable"


# ── .launches — The Space Devs upcoming ───────────────────────────────
def _fetch_launches(n: int, ua: str) -> str:
    n = max(1, min(n, 3))
    try:
        d = fetch_json(
            _LAUNCH_URL,
            ua=ua,
            params={"limit": n, "mode": "list"},
            timeout=12,
            max_bytes=_LAUNCH_MAX_BYTES,
        )
        if not isinstance(d, dict):
            return "launch data unavailable"
        results = d.get("results") or []
        if not results:
            return "no upcoming launches found"
        out: list[str] = []
        for r in results[:n]:
            if not isinstance(r, dict):
                continue
            name = strip_ctrl(r.get("name") or "?", 80)
            # In LL2 "list" mode these nested fields may be a dict, a bare
            # string, or absent — handle all shapes.
            prov = r.get("launch_service_provider")
            prov_name = strip_ctrl(
                (prov.get("name") if isinstance(prov, dict) else prov) or "?", 40)
            net = strip_ctrl(r.get("net") or "?", 32)
            pad = r.get("pad")
            pad_name = strip_ctrl(
                (pad.get("name") if isinstance(pad, dict) else pad) or "?", 50)
            out.append(f"{name} ({prov_name}) {net} @ {pad_name}")
        if not out:
            return "no upcoming launches found"
        return strip_ctrl("next launches: " + " | ".join(out))
    except (requests.RequestException, ResponseTooLarge) as e:
        log.warning("launches request: %s", e)
        return "launch lookup failed"
    except Exception as e:  # parsing — never raise to the caller  # noqa: BLE001
        log.warning("launches parse: %r", e)
        return "launch data unavailable"


# ── .moon — pure mean-phase compute ───────────────────────────────────
_PHASES = [
    "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
    "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent",
]
_SYNODIC = 29.53058867  # mean synodic month, days
# Known new moon: 2000-01-06 18:14 UTC -> Julian Day.
_KNOWN_NEW_JD = 2451550.26


def _julian_day(dt: datetime) -> float:
    """Julian Day for a UTC datetime (Fliegel-Van Flandern)."""
    y, m = dt.year, dt.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = (math.floor(365.25 * (y + 4716))
          + math.floor(30.6001 * (m + 1))
          + dt.day + b - 1524.5)
    jd += (dt.hour + dt.minute / 60 + dt.second / 3600) / 24.0
    return jd


def moon_phase(dt: datetime) -> str:
    """Mean moon-phase summary for ``dt`` (UTC): name, illumination %, age."""
    jd = _julian_day(dt)
    age = (jd - _KNOWN_NEW_JD) % _SYNODIC
    if age < 0:
        age += _SYNODIC
    # Illuminated fraction from phase angle (0 at new, 1 at full).
    frac = (1 - math.cos(2 * math.pi * age / _SYNODIC)) / 2
    idx = int((age / _SYNODIC) * 8 + 0.5) % 8
    name = _PHASES[idx]
    return (f"\x02{name}\x02 — {frac * 100:.0f}% illuminated, "
            f"{age:.1f} days old")


def _moon(arg: str | None) -> str:
    if arg and arg.strip():
        try:
            dt = datetime.strptime(arg.strip(), "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return "usage: .moon [YYYY-MM-DD]"
        label = dt.strftime("%Y-%m-%d")
    else:
        dt = datetime.now(timezone.utc)
        label = dt.strftime("%Y-%m-%d")
    return strip_ctrl(f"Moon {label}: {moon_phase(dt)}")


# ── .sky — bundled Messier catalog (pure data) ────────────────────────
# (number) -> (common name, type, constellation, magnitude)
_MESSIER: dict[int, tuple[str, str, str, str]] = {
    1: ("Crab Nebula", "Supernova remnant", "Taurus", "8.4"),
    2: ("", "Globular cluster", "Aquarius", "6.3"),
    3: ("", "Globular cluster", "Canes Venatici", "6.2"),
    4: ("", "Globular cluster", "Scorpius", "5.6"),
    5: ("", "Globular cluster", "Serpens", "5.6"),
    6: ("Butterfly Cluster", "Open cluster", "Scorpius", "4.2"),
    7: ("Ptolemy Cluster", "Open cluster", "Scorpius", "3.3"),
    8: ("Lagoon Nebula", "Nebula", "Sagittarius", "6.0"),
    9: ("", "Globular cluster", "Ophiuchus", "7.7"),
    10: ("", "Globular cluster", "Ophiuchus", "6.6"),
    11: ("Wild Duck Cluster", "Open cluster", "Scutum", "6.3"),
    12: ("", "Globular cluster", "Ophiuchus", "6.7"),
    13: ("Hercules Globular Cluster", "Globular cluster", "Hercules", "5.8"),
    14: ("", "Globular cluster", "Ophiuchus", "7.6"),
    15: ("", "Globular cluster", "Pegasus", "6.2"),
    16: ("Eagle Nebula", "Open cluster + nebula", "Serpens", "6.0"),
    17: ("Omega Nebula", "Nebula", "Sagittarius", "6.0"),
    18: ("", "Open cluster", "Sagittarius", "7.5"),
    19: ("", "Globular cluster", "Ophiuchus", "6.8"),
    20: ("Trifid Nebula", "Nebula", "Sagittarius", "6.3"),
    21: ("", "Open cluster", "Sagittarius", "6.5"),
    22: ("Sagittarius Cluster", "Globular cluster", "Sagittarius", "5.1"),
    23: ("", "Open cluster", "Sagittarius", "6.9"),
    24: ("Sagittarius Star Cloud", "Star cloud", "Sagittarius", "4.6"),
    25: ("", "Open cluster", "Sagittarius", "6.5"),
    26: ("", "Open cluster", "Scutum", "8.0"),
    27: ("Dumbbell Nebula", "Planetary nebula", "Vulpecula", "7.5"),
    28: ("", "Globular cluster", "Sagittarius", "6.8"),
    29: ("", "Open cluster", "Cygnus", "7.1"),
    30: ("", "Globular cluster", "Capricornus", "7.7"),
    31: ("Andromeda Galaxy", "Spiral galaxy", "Andromeda", "3.4"),
    32: ("", "Dwarf elliptical galaxy", "Andromeda", "8.1"),
    33: ("Triangulum Galaxy", "Spiral galaxy", "Triangulum", "5.7"),
    34: ("", "Open cluster", "Perseus", "5.5"),
    35: ("", "Open cluster", "Gemini", "5.3"),
    36: ("", "Open cluster", "Auriga", "6.3"),
    37: ("", "Open cluster", "Auriga", "6.2"),
    38: ("", "Open cluster", "Auriga", "7.4"),
    39: ("", "Open cluster", "Cygnus", "4.6"),
    40: ("Winnecke 4", "Double star", "Ursa Major", "8.4"),
    41: ("", "Open cluster", "Canis Major", "4.5"),
    42: ("Orion Nebula", "Nebula", "Orion", "4.0"),
    43: ("De Mairan's Nebula", "Nebula", "Orion", "9.0"),
    44: ("Beehive Cluster", "Open cluster", "Cancer", "3.7"),
    45: ("Pleiades", "Open cluster", "Taurus", "1.6"),
    46: ("", "Open cluster", "Puppis", "6.1"),
    47: ("", "Open cluster", "Puppis", "4.4"),
    48: ("", "Open cluster", "Hydra", "5.8"),
    49: ("", "Elliptical galaxy", "Virgo", "8.4"),
    50: ("", "Open cluster", "Monoceros", "5.9"),
    51: ("Whirlpool Galaxy", "Spiral galaxy", "Canes Venatici", "8.4"),
    52: ("", "Open cluster", "Cassiopeia", "5.0"),
    53: ("", "Globular cluster", "Coma Berenices", "7.6"),
    54: ("", "Globular cluster", "Sagittarius", "7.6"),
    55: ("", "Globular cluster", "Sagittarius", "6.3"),
    56: ("", "Globular cluster", "Lyra", "8.3"),
    57: ("Ring Nebula", "Planetary nebula", "Lyra", "8.8"),
    58: ("", "Barred spiral galaxy", "Virgo", "9.7"),
    59: ("", "Elliptical galaxy", "Virgo", "9.6"),
    60: ("", "Elliptical galaxy", "Virgo", "8.8"),
    61: ("", "Spiral galaxy", "Virgo", "9.7"),
    62: ("", "Globular cluster", "Ophiuchus", "6.5"),
    63: ("Sunflower Galaxy", "Spiral galaxy", "Canes Venatici", "8.6"),
    64: ("Black Eye Galaxy", "Spiral galaxy", "Coma Berenices", "8.5"),
    65: ("", "Spiral galaxy", "Leo", "9.3"),
    66: ("", "Spiral galaxy", "Leo", "8.9"),
    67: ("", "Open cluster", "Cancer", "6.1"),
    68: ("", "Globular cluster", "Hydra", "7.8"),
    69: ("", "Globular cluster", "Sagittarius", "7.6"),
    70: ("", "Globular cluster", "Sagittarius", "7.9"),
    71: ("", "Globular cluster", "Sagitta", "8.2"),
    72: ("", "Globular cluster", "Aquarius", "9.3"),
    73: ("", "Asterism", "Aquarius", "9.0"),
    74: ("Phantom Galaxy", "Spiral galaxy", "Pisces", "9.4"),
    75: ("", "Globular cluster", "Sagittarius", "8.5"),
    76: ("Little Dumbbell Nebula", "Planetary nebula", "Perseus", "10.1"),
    77: ("Cetus A", "Spiral galaxy", "Cetus", "8.9"),
    78: ("", "Nebula", "Orion", "8.3"),
    79: ("", "Globular cluster", "Lepus", "8.0"),
    80: ("", "Globular cluster", "Scorpius", "7.3"),
    81: ("Bode's Galaxy", "Spiral galaxy", "Ursa Major", "6.9"),
    82: ("Cigar Galaxy", "Starburst galaxy", "Ursa Major", "8.4"),
    83: ("Southern Pinwheel Galaxy", "Spiral galaxy", "Hydra", "7.5"),
    84: ("", "Lenticular galaxy", "Virgo", "9.1"),
    85: ("", "Lenticular galaxy", "Coma Berenices", "9.1"),
    86: ("", "Lenticular galaxy", "Virgo", "8.9"),
    87: ("Virgo A", "Elliptical galaxy", "Virgo", "8.6"),
    88: ("", "Spiral galaxy", "Coma Berenices", "9.6"),
    89: ("", "Elliptical galaxy", "Virgo", "9.8"),
    90: ("", "Spiral galaxy", "Virgo", "9.5"),
    91: ("", "Barred spiral galaxy", "Coma Berenices", "10.2"),
    92: ("", "Globular cluster", "Hercules", "6.3"),
    93: ("", "Open cluster", "Puppis", "6.2"),
    94: ("Cat's Eye Galaxy", "Spiral galaxy", "Canes Venatici", "8.2"),
    95: ("", "Barred spiral galaxy", "Leo", "9.7"),
    96: ("", "Spiral galaxy", "Leo", "9.2"),
    97: ("Owl Nebula", "Planetary nebula", "Ursa Major", "9.9"),
    98: ("", "Spiral galaxy", "Coma Berenices", "10.1"),
    99: ("Coma Pinwheel Galaxy", "Spiral galaxy", "Coma Berenices", "9.9"),
    100: ("", "Spiral galaxy", "Coma Berenices", "9.3"),
    101: ("Pinwheel Galaxy", "Spiral galaxy", "Ursa Major", "7.9"),
    102: ("Spindle Galaxy", "Lenticular galaxy", "Draco", "9.9"),
    103: ("", "Open cluster", "Cassiopeia", "7.4"),
    104: ("Sombrero Galaxy", "Spiral galaxy", "Virgo", "8.0"),
    105: ("", "Elliptical galaxy", "Leo", "9.3"),
    106: ("", "Spiral galaxy", "Canes Venatici", "8.4"),
    107: ("", "Globular cluster", "Ophiuchus", "7.9"),
    108: ("Surfboard Galaxy", "Barred spiral galaxy", "Ursa Major", "10.0"),
    109: ("", "Barred spiral galaxy", "Ursa Major", "9.8"),
    110: ("", "Dwarf elliptical galaxy", "Andromeda", "8.1"),
}

# common-name -> Messier number, for name lookups.
_MESSIER_BY_NAME: dict[str, int] = {
    name.lower(): num for num, (name, _t, _c, _m) in _MESSIER.items() if name
}


def _parse_messier_num(s: str) -> int | None:
    s = s.strip().lower().replace(" ", "")
    if s.startswith("m"):
        s = s[1:]
    if s.isdigit():
        return int(s)
    return None


def sky_lookup(query: str) -> str:
    """Look up a Messier object by number (M31 / 31) or common name."""
    q = (query or "").strip()
    if not q:
        return "usage: .sky <M-number|name>  e.g. .sky M31 or .sky Orion Nebula"
    num = _parse_messier_num(q)
    if num is None:
        num = _MESSIER_BY_NAME.get(q.lower())
    if num is None or num not in _MESSIER:
        return f"no Messier object matching '{strip_ctrl(q, 40)}'"
    name, typ, const, mag = _MESSIER[num]
    label = f"M{num}" + (f" ({strip_ctrl(name, 50)})" if name else "")
    return strip_ctrl(
        f"\x02{label}\x02 — {strip_ctrl(typ, 40)} in {strip_ctrl(const, 30)}, "
        f"mag {strip_ctrl(mag, 8)}")


class Astro2Module(BotModule):
    """`.solar` / `.neo` / `.launches` / `.moon` / `.sky` — space & astronomy."""

    COMMANDS: dict[str, str] = {
        "solar": "cmd_solar",
        "neo": "cmd_neo",
        "launches": "cmd_launches",
        "moon": "cmd_moon",
        "sky": "cmd_sky",
    }

    def on_load(self) -> None:
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        # NASA permits DEMO_KEY (stricter rate limits) like the apod module.
        self._key: str = cred(self.bot.cfg, "nasa_api_key",
                              "apod", "api_key", "DEMO_KEY") or "DEMO_KEY"

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_solar(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        result = await asyncio.to_thread(_fetch_solar, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_neo(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        result = await asyncio.to_thread(_fetch_neo, self._key, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_launches(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        n = 1
        if arg and arg.strip():
            s = arg.strip().split()[0]
            if s.isdigit():
                n = int(s)
            else:
                p = self.bot.cfg["bot"]["command_prefix"]
                self.bot.privmsg(reply_to, f"{nick}: {p}launches [n]  (n = 1-3)")
                return
        result = await asyncio.to_thread(_fetch_launches, n, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_moon(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        # Pure compute — no network, but keep the gate + to_thread shape.
        self.bot.privmsg(reply_to, _moon(arg))

    async def cmd_sky(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}sky <M-number|name>  e.g. {p}sky M31")
            return
        self.bot.privmsg(reply_to, sky_lookup(arg))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "solar", "NOAA space weather: X-ray flare class + SSN"),
            help_row(prefix, "neo", "NASA near-earth objects today + closest"),
            help_row(prefix, "launches [n]", "Next 1-3 rocket launches"),
            help_row(prefix, "moon [YYYY-MM-DD]", "Moon phase, illumination, age"),
            help_row(prefix, "sky <M#|name>", "Messier catalog lookup"),
        ]


def setup(bot: object) -> Astro2Module:
    return Astro2Module(bot)  # type: ignore[arg-type]
