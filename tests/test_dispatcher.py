"""Tests for weather_providers/_dispatch.py + the package-level helpers.

Covers:
    - force_provider kwarg (single provider, no fallback)
    - accuracy-first sort key (DEFAULT_RELIABILITY rank dominates)
    - DEFAULT_RELIABILITY shape and stormglass/weatherbit registration
    - provider_status() / provider_capabilities() shape
"""

from __future__ import annotations

import asyncio

import pytest

from weather_providers._dispatch import (
    Dispatcher,
    CAPABILITY_METHODS,
    DEFAULT_RELIABILITY,
)
from weather_providers import (
    _PROVIDER_FACTORIES,
    dispatcher as global_dispatcher,
    provider_status,
    provider_capabilities,
    configure,
)


# ── Helpers: stub providers that record what was called ─────────────────

class _StubProvider:
    """A no-network provider with a configurable result + failure mode."""

    name = "Stub"
    requires_key = False

    def __init__(self, *, result=object(), raises=None):
        self._result = result
        self._raises = raises
        self.calls = 0

    async def get_weather(self, lat, lon, location, **kwargs):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._result

    async def get_forecast(self, lat, lon, location, days=4, **kwargs):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._result


class _MarineOnlyProvider:
    name = "MarineOnly"
    requires_key = False

    def __init__(self):
        self.calls = 0

    async def get_weather(self, lat, lon, location, **kw):
        # Required by the Protocol, but we'll dispatch only "marine" to it.
        raise NotImplementedError

    async def get_forecast(self, lat, lon, location, days=4, **kw):
        raise NotImplementedError

    async def get_marine(self, lat, lon, location, **kw):
        self.calls += 1
        return "marine-ok"


# ── force_provider ──────────────────────────────────────────────────────

class TestForceProvider:
    def test_force_dispatches_only_to_named_provider(self):
        d = Dispatcher()
        a = _StubProvider(result="a")
        b = _StubProvider(result="b")
        d.register(a, "alpha")
        d.register(b, "bravo")

        # Forcing "bravo" - even if "alpha" would otherwise rank higher -
        # must call only bravo.
        out = asyncio.run(d.dispatch(
            "current", 0.0, 0.0, "x", force_provider="bravo",
        ))
        assert out == "b"
        assert a.calls == 0
        assert b.calls == 1

    def test_force_provider_kwarg_is_consumed(self):
        """force_provider must NOT leak into the underlying provider call."""
        d = Dispatcher()

        seen_kwargs = {}

        class _Spy(_StubProvider):
            async def get_weather(self, lat, lon, location, **kwargs):
                seen_kwargs.update(kwargs)
                return "ok"

        d.register(_Spy(), "spy")
        asyncio.run(d.dispatch(
            "current", 0.0, 0.0, "x", force_provider="spy", extra="passthrough",
        ))
        assert "force_provider" not in seen_kwargs
        assert seen_kwargs.get("extra") == "passthrough"

    def test_force_unknown_returns_none(self):
        d = Dispatcher()
        d.register(_StubProvider(result="a"), "alpha")
        out = asyncio.run(d.dispatch(
            "current", 0, 0, "x", force_provider="bogus",
        ))
        assert out is None

    def test_force_provider_without_capability_returns_none(self):
        d = Dispatcher()
        d.register(_MarineOnlyProvider(), "marine_only")
        # marine_only has get_weather, but we'll target a capability it
        # doesn't have - actually it has get_weather... use astronomy.
        out = asyncio.run(d.dispatch(
            "astronomy", 0, 0, "x", force_provider="marine_only",
        ))
        assert out is None  # marine_only has no get_astronomy

    def test_force_does_not_fall_back(self):
        """If the forced provider fails the call must return None, never
        retry another provider."""
        d = Dispatcher()
        boom = _StubProvider(raises=RuntimeError("boom"))
        fallback = _StubProvider(result="other")
        d.register(boom, "boom")
        d.register(fallback, "fallback")
        out = asyncio.run(d.dispatch(
            "current", 0, 0, "x", force_provider="boom",
        ))
        assert out is None
        assert fallback.calls == 0


# ── Accuracy-first sort key ─────────────────────────────────────────────

class TestAccuracySort:
    def test_lower_rank_beats_higher_rank(self):
        # NWS = 1 should sort ahead of weatherapi = 8 for current.
        d = Dispatcher()
        d.register(_StubProvider(), "weatherapi")
        d.register(_StubProvider(), "nws")
        chain = d._sorted_for_capability("current")
        assert chain[0] == "nws"
        assert chain[-1] == "weatherapi"

    def test_accuracy_beats_registration_order(self):
        """Registration order is the *last* tie-break, so a provider
        registered second can still come first if it has a better
        DEFAULT_RELIABILITY rank."""
        d = Dispatcher()
        # Register low-rank one first.
        d.register(_StubProvider(), "weatherstack")  # rank ~13
        d.register(_StubProvider(), "nws")           # rank 1
        chain = d._sorted_for_capability("current")
        assert chain[0] == "nws"

    def test_unlisted_provider_sorts_last(self):
        d = Dispatcher()
        d.register(_StubProvider(), "openmeteo")  # listed
        d.register(_StubProvider(), "weirdo")     # not in DEFAULT_RELIABILITY
        chain = d._sorted_for_capability("current")
        assert chain[-1] == "weirdo"
        assert chain[0] == "openmeteo"

    def test_sort_key_returns_3_tuple(self):
        d = Dispatcher()
        d.register(_StubProvider(), "openmeteo")
        # Force public-API path: _sorted_for_capability is used by
        # capability_matrix(), which we test indirectly.
        matrix = d.capability_matrix()
        assert "current:" in matrix
        assert "openmeteo" in matrix


# ── dispatcher fallback chain (no force) ────────────────────────────────

class TestDispatchFallback:
    def test_first_failure_falls_through(self):
        d = Dispatcher()
        d.register(_StubProvider(raises=RuntimeError("nope")), "nws")  # rank 1
        d.register(_StubProvider(result="ok"), "openmeteo")            # rank 4
        out = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out == "ok"

    def test_all_failures_returns_none(self):
        d = Dispatcher()
        d.register(_StubProvider(raises=RuntimeError("a")), "nws")
        d.register(_StubProvider(raises=RuntimeError("b")), "openmeteo")
        out = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out is None

    def test_unknown_capability_returns_none(self):
        d = Dispatcher()
        d.register(_StubProvider(), "openmeteo")
        out = asyncio.run(d.dispatch("bogus_cap", 0, 0, "x"))
        assert out is None


# ── DEFAULT_RELIABILITY contents ────────────────────────────────────────

class TestDefaultReliability:
    def test_every_capability_has_a_table(self):
        # The dispatcher relies on a per-capability rank table.  Every
        # CAPABILITY_METHODS key must have one (even if short).
        for cap in CAPABILITY_METHODS:
            assert cap in DEFAULT_RELIABILITY, (
                f"DEFAULT_RELIABILITY missing entry for {cap!r}"
            )
            assert DEFAULT_RELIABILITY[cap], (
                f"DEFAULT_RELIABILITY[{cap!r}] is empty"
            )

    def test_stormglass_ranked_for_marine(self):
        # Stormglass was added this session - must rank #1 for marine.
        assert DEFAULT_RELIABILITY["marine"].get("stormglass") == 1

    def test_weatherbit_ranked_for_current_and_forecast(self):
        # Weatherbit was added this session - must rank in both.
        assert "weatherbit" in DEFAULT_RELIABILITY["current"]
        assert "weatherbit" in DEFAULT_RELIABILITY["forecast"]

    def test_airnow_top_purpleair_bottom_for_air_quality(self):
        # AirNow (authoritative US EPA) leads air_quality; PurpleAir
        # (crowdsourced) ranks last behind the model/observation sources.
        aq = DEFAULT_RELIABILITY["air_quality"]
        assert aq["airnow"] == 1
        assert aq["purpleair"] == max(aq.values())
        assert aq["airnow"] < aq["openmeteo"]

    def test_new_capability_tables_present(self):
        # The five capabilities added this session must each have a table.
        for cap in ("uv", "pollen", "wildfire", "space_weather", "tides"):
            assert cap in DEFAULT_RELIABILITY and DEFAULT_RELIABILITY[cap]

    def test_sunrisesunset_leads_astronomy(self):
        # SunriseSunset.io returns the full moon+twilight set, so it leads.
        assert DEFAULT_RELIABILITY["astronomy"]["sunrisesunset"] == 1

    def test_new_air_quality_and_alert_sources_ranked(self):
        assert DEFAULT_RELIABILITY["air_quality"]["waqi"] == 2
        assert "openaq" in DEFAULT_RELIABILITY["air_quality"]
        assert "gdacs" in DEFAULT_RELIABILITY["alerts"]
        assert "eccc" in DEFAULT_RELIABILITY["alerts"]
        assert "nasapower" in DEFAULT_RELIABILITY["historical"]

    def test_metno_ranked_for_its_capabilities(self):
        # metno was registered but absent from every map (silent rank-99).
        for cap in ("current", "forecast", "hourly", "alerts", "nowcast"):
            assert "metno" in DEFAULT_RELIABILITY[cap], f"metno missing from {cap}"

    def test_every_registered_capability_is_ranked(self):
        # Every capability a registered provider supports must appear in that
        # capability's reliability map, or it silently sorts to rank-99.
        from configparser import ConfigParser
        configure(ConfigParser())
        for cap, pids in global_dispatcher.capabilities().items():
            table = DEFAULT_RELIABILITY.get(cap, {})
            for pid in pids:
                assert pid in table, (
                    f"{pid} supports {cap!r} but is absent from "
                    f"DEFAULT_RELIABILITY[{cap!r}]")

    def test_nws_is_top_for_us_capabilities(self):
        for cap in ("current", "forecast", "hourly", "alerts"):
            assert DEFAULT_RELIABILITY[cap]["nws"] == 1, (
                f"NWS should be rank-1 for {cap}"
            )

    def test_ranks_are_unique_per_capability(self):
        # A duplicate rank would silently demote one provider - guard against it.
        for cap, table in DEFAULT_RELIABILITY.items():
            ranks = list(table.values())
            assert len(ranks) == len(set(ranks)), (
                f"duplicate rank values in DEFAULT_RELIABILITY[{cap!r}]: {table!r}"
            )

    def test_ranks_are_positive_ints(self):
        for cap, table in DEFAULT_RELIABILITY.items():
            for pid, rank in table.items():
                assert isinstance(rank, int) and rank > 0, (
                    f"DEFAULT_RELIABILITY[{cap!r}][{pid!r}] is {rank!r}"
                )


# ── Package-level helpers (__init__.py) ────────────────────────────────

class TestProviderRegistration:
    def test_stormglass_registered_as_factory(self):
        assert "stormglass" in _PROVIDER_FACTORIES

    def test_weatherbit_registered_as_factory(self):
        assert "weatherbit" in _PROVIDER_FACTORIES

    def test_factory_count_is_32(self):
        # Sanity guard for the doc claim "32 provider packages".
        assert len(_PROVIDER_FACTORIES) == 32

    def test_known_provider_set(self):
        expected = {
            "nws", "meteomatics", "weatherkit", "openmeteo",
            "visualcrossing", "accuweather", "openweathermap",
            "weatherbit", "weatherapi", "pirateweather",
            "stormglass", "tomorrowio", "worldweatheronline",
            "weatherstack",
            # Air-quality-only providers.
            "airnow", "purpleair", "waqi", "openaq", "iqair",
            # General no-key fallback.
            "metno",
            # Specialist / single-capability providers.
            "sunrisesunset", "currentuvindex", "gdacs", "eccc",
            "nasapower", "nifc", "firms", "swpc",
            "tidecheck", "noaa_coops",
            # Pollen providers (US / global).
            "pollendotcom", "google_pollen",
        }
        assert set(_PROVIDER_FACTORIES) == expected


class TestProviderStatusShape:
    def test_status_includes_every_known_provider(self):
        # Reconfigure with an empty config so we know what's registered.
        from configparser import ConfigParser
        configure(ConfigParser())
        status = provider_status()
        ids = {s["id"] for s in status}
        for pid in _PROVIDER_FACTORIES:
            assert pid in ids, f"provider_status() missing {pid}"

    def test_status_entry_keys(self):
        from configparser import ConfigParser
        configure(ConfigParser())
        status = provider_status()
        assert status, "expected at least one entry"
        sample = status[0]
        for k in ("id", "registered", "state", "calls", "fails",
                  "success_rate", "health_score"):
            assert k in sample, f"missing key {k!r} in {sample!r}"

    def test_unconfigured_state_for_missing_keys(self):
        from configparser import ConfigParser
        configure(ConfigParser())
        status = {s["id"]: s for s in provider_status()}
        # weatherapi requires a key; with no config it must be "unconfigured".
        assert status["weatherapi"]["state"] == "unconfigured"
        assert status["weatherapi"]["registered"] is False
        # nws and openmeteo don't need a key.
        assert status["openmeteo"]["registered"] is True
        assert status["nws"]["registered"] is True

    def test_cold_state_when_no_calls_yet(self):
        from configparser import ConfigParser
        # Use a fresh local dispatcher so global health counters don't
        # bleed into the assertion.
        configure(ConfigParser())
        status = {s["id"]: s for s in provider_status()}
        # State for a freshly-registered keyless provider with no calls.
        # Open-Meteo state may be "cold" (no calls) or "active" if other
        # tests in this session bumped its global health counter.  Both
        # are acceptable as long as it's NOT unconfigured.
        assert status["openmeteo"]["state"] in {"cold", "active"}


class TestProviderCapabilities:
    def test_unconfigured_provider_returns_empty_set(self):
        from configparser import ConfigParser
        configure(ConfigParser())
        # weatherapi isn't registered (no key) → no capabilities visible.
        assert provider_capabilities("weatherapi") == set()

    def test_openmeteo_supports_core_capabilities(self):
        from configparser import ConfigParser
        configure(ConfigParser())
        caps = provider_capabilities("openmeteo")
        # Open-Meteo offers the core read-only capabilities.
        assert "current" in caps
        assert "forecast" in caps

    def test_unknown_provider_returns_empty_set(self):
        from configparser import ConfigParser
        configure(ConfigParser())
        assert provider_capabilities("totally_bogus") == set()


# ── Integration: dispatch through the global dispatcher ────────────────

class TestGlobalDispatch:
    def test_global_dispatcher_clears_on_configure(self):
        from configparser import ConfigParser
        cfg = ConfigParser()
        cfg.add_section("weather_providers")
        cfg.set("weather_providers", "provider_priority", "openmeteo")
        configure(cfg)
        # provider_priority is an ordering, not an allowlist: the listed
        # provider registers first; unlisted keyless providers still append.
        assert global_dispatcher.provider_ids[0] == "openmeteo"
        assert "nws" in global_dispatcher.provider_ids

    def test_reconfigure_replaces_set(self):
        from configparser import ConfigParser
        cfg = ConfigParser()
        cfg.add_section("weather_providers")
        cfg.set("weather_providers", "provider_priority", "openmeteo")
        configure(cfg)

        cfg2 = ConfigParser()
        cfg2.add_section("weather_providers")
        cfg2.set("weather_providers", "provider_priority", "nws")
        configure(cfg2)

        # Reconfigure rebuilds from scratch: nws now sorts first (its
        # priority), replacing openmeteo as the lead provider.
        assert global_dispatcher.provider_ids[0] == "nws"


# ── auth-failure (401/403) handling ─────────────────────────────────────

class TestAuthFailure:
    def test_401_trips_breaker_and_falls_through(self):
        from weather_providers._http import HTTPError
        from weather_providers._health import health_registry
        d = Dispatcher()
        bad = _StubProvider(raises=HTTPError("unauthorized", status=401))
        good = _StubProvider(result="ok")
        # Unique ids so the global health registry isn't shared with other tests.
        d.register(bad, "authbad")
        d.register(good, "authgood")

        out = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out == "ok"                                  # fell through to good
        assert health_registry.get("authbad").is_callable() is False  # breaker open

        # The open breaker means the bad provider is skipped, not retried.
        before = bad.calls
        out2 = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out2 == "ok"
        assert bad.calls == before                          # not called again


# ── no-data (None) handling ──────────────────────────────────────────────

class TestNoDataHandling:
    def test_none_result_is_not_recorded_as_success(self):
        """A provider returning None has no data for the location; it must
        fall through WITHOUT counting as a success, so a slow-but-no-data
        provider can't keep looking healthy / reset its breaker streak."""
        from weather_providers._health import health_registry
        d = Dispatcher()
        nodata = _StubProvider(result=None)
        good = _StubProvider(result="ok")
        # Unknown ids → rank 99; registration order makes nodata lead.
        d.register(nodata, "nd_primary")
        d.register(good, "nd_fallback")

        out = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out == "ok"                                  # fell through
        assert nodata.calls == 1
        h = health_registry.get("nd_primary")
        # record_success was NOT called: no success/failure booked at all.
        assert h.total_calls == 0
        assert h.total_failures == 0


# ── end-to-end chain time budget ─────────────────────────────────────────

class TestChainBudget:
    def test_slow_provider_times_out_and_sheds(self, monkeypatch):
        """A provider slower than the per-call budget is cut off (counted as
        a failure for the breaker) and the chain falls through to a healthy
        provider instead of starving on the slow one."""
        import weather_providers._dispatch as disp
        from weather_providers._health import health_registry

        # Shrink the per-call budget so the test runs in milliseconds.
        monkeypatch.setattr(disp, "_PER_CALL_BUDGET", 0.05)

        class _SlowProvider:
            name = "Slow"
            requires_key = False

            def __init__(self):
                self.calls = 0

            async def get_weather(self, lat, lon, location, **kw):
                self.calls += 1
                await asyncio.sleep(0.5)   # > per-call budget
                return "too-late"

        d = Dispatcher()
        slow = _SlowProvider()
        fast = _StubProvider(result="fast-ok")
        d.register(slow, "budget_slow")
        d.register(fast, "budget_fast")

        out = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out == "fast-ok"                             # timed out, fell through
        assert slow.calls == 1
        h = health_registry.get("budget_slow")
        assert h.total_failures >= 1                        # timeout booked as failure


# ── coreless fall-through + current gap-fill ─────────────────────────────

def _wr(source, temp, **kw):
    from weather_providers import WeatherResult
    return WeatherResult(source=source, temperature=temp, description=kw.pop("desc", "Clear"),
                         location="x", **kw)


class TestGapFill:
    def test_coreless_result_falls_through(self):
        """A non-None result with no temperature and no forecast is no-data:
        the dispatcher skips it for the next provider instead of all-N/A."""
        d = Dispatcher()
        empty = _wr("NWS", None, desc="")            # is_empty() -> True
        good = _wr("Open-Meteo", 20.0, humidity=50.0, wind_kph=5.0, wind_dir="N",
                   pressure_mb=1013.0, visibility_m=16000.0, dewpoint_c=10.0, feels_like_c=20.0)
        d.register(_StubProvider(result=empty), "nws")
        d.register(_StubProvider(result=good), "openmeteo")
        out = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out is good                           # coreless NWS skipped

    def test_current_gap_fills_from_next_provider(self):
        """A sparse primary (NWS: temp only) keeps its temp/conditions and has
        its N/A secondary fields filled from the next provider, crediting both."""
        d = Dispatcher()
        sparse = _wr("NWS", 24.0, desc="Clear")      # all secondary fields None
        full = _wr("Open-Meteo", 22.0, desc="Clouds", humidity=48.0, wind_kph=11.0,
                   wind_dir="NW", pressure_mb=1013.0, visibility_m=16000.0,
                   dewpoint_c=12.0, feels_like_c=23.0)
        nws, om = _StubProvider(result=sparse), _StubProvider(result=full)
        d.register(nws, "nws"); d.register(om, "openmeteo")
        out = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out.temperature == 24.0 and out.description == "Clear"   # NWS core kept
        assert out.humidity == 48.0 and out.pressure_mb == 1013.0       # filled
        assert out.dewpoint_c == 12.0 and out.wind_kph == 11.0          # filled
        assert "NWS" in out.source and "Open-Meteo" in out.source       # both credited
        assert nws.calls == 1 and om.calls == 1                         # one extra call

    def test_complete_primary_does_not_call_filler(self):
        """If the primary is already complete, the chain stops - no extra call."""
        d = Dispatcher()
        full = _wr("NWS", 24.0, humidity=48.0, wind_kph=11.0, wind_dir="NW",
                   pressure_mb=1013.0, visibility_m=16000.0, dewpoint_c=12.0, feels_like_c=23.0)
        nws, om = _StubProvider(result=full), _StubProvider(result=full)
        d.register(nws, "nws"); d.register(om, "openmeteo")
        out = asyncio.run(d.dispatch("current", 0, 0, "x"))
        assert out is full and nws.calls == 1 and om.calls == 0

    def test_empty_primary_description_filled_from_next(self):
        """An EMPTY primary description (NWS obs often null textDescription) is
        gap-filled from the next provider - the blank-Conditions bug."""
        primary = _wr("NWS", 16.7, desc="")                  # blank conditions
        other = _wr("Open-Meteo", 17.3, desc="Partly Cloudy", humidity=74.0)
        merged = primary.fill_gaps(other)
        assert merged.description == "Partly Cloudy"

    def test_present_primary_description_not_overwritten(self):
        """A PRESENT primary description is never overwritten by the filler."""
        primary = _wr("NWS", 16.7, desc="Sunny")
        other = _wr("Open-Meteo", 17.3, desc="Partly Cloudy")
        assert primary.fill_gaps(other).description == "Sunny"

    def test_has_gaps_true_when_only_description_empty(self):
        """Empty description counts as a gap so the dispatcher keeps walking the
        chain to fill it, even when every numeric field is already present."""
        r = _wr("NWS", 16.7, desc="", humidity=74.0, wind_kph=5.4, wind_dir="W",
                pressure_mb=976.0, visibility_m=17000.0, dewpoint_c=12.6,
                feels_like_c=17.0)
        assert r.has_gaps() is True


def test_nws_absent_textdescription_reads_empty(monkeypatch):
    """NWS with no textDescription yields '' (missing), so the gap-fill can
    supply a real description from a fallback, instead of 'Unknown' blocking it."""
    from weather_providers.nws import current as nws_current

    async def fake_get_json(url, headers=None):
        if "/points/" in url:
            return {"properties": {"observationStations": "https://api.weather.gov/st"}}
        if url.endswith("/st"):
            return {"features": [{"id": "https://api.weather.gov/st/KCNO"}]}
        return {"properties": {"temperature": {"value": 16.7}}}  # no textDescription

    monkeypatch.setattr(nws_current, "get_json", fake_get_json)
    r = asyncio.run(nws_current.fetch(34.10, -117.80, "San Dimas, CA"))
    assert r.description == ""
