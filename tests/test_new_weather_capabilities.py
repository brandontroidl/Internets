"""Tests for the capabilities/providers added this session:

new capabilities - uv, pollen, wildfire, space_weather, tides;
new providers - sunrisesunset, currentuvindex, gdacs, eccc, metno, waqi,
openaq, iqair, nasapower, nifc, firms, swpc, tidecheck, noaa_coops.

Covers the base helpers, capability auto-discovery for every new provider,
and mocked fetch() behaviour for the non-trivial parsers.  HTTP is mocked.
"""

from __future__ import annotations

import asyncio

import pytest

from weather_providers._http import HTTPError
from weather_providers._dispatch import Dispatcher
from weather_providers.base import (
    uv_category, kp_category,
    AstronomyResult, AirQualityResult, UVResult, HistoricalResult,
    SpaceWeatherResult, AlertsResult, WildfireResult,
)


# ── base helpers ─────────────────────────────────────────────────────────

class TestHelpers:
    @pytest.mark.parametrize("uv,cat", [
        (0, "Low"), (2.9, "Low"), (3, "Moderate"), (5.9, "Moderate"),
        (6, "High"), (7.9, "High"), (8, "Very High"), (10.9, "Very High"),
        (11, "Extreme"), (None, ""),
    ])
    def test_uv_category(self, uv, cat):
        assert uv_category(uv) == cat

    @pytest.mark.parametrize("kp,cat", [
        (0, "Quiet"), (4.9, "Quiet"), (5, "Minor storm (G1)"),
        (6, "Moderate storm (G2)"), (7, "Strong storm (G3)"),
        (8, "Severe storm (G4)"), (9, "Extreme storm (G5)"), (None, ""),
    ])
    def test_kp_category(self, kp, cat):
        assert kp_category(kp) == cat


# ── capability auto-discovery for every new provider ─────────────────────

class TestCapabilityDiscovery:
    @pytest.mark.parametrize("modpath,cls,key,caps", [
        ("weather_providers.sunrisesunset", "SunriseSunsetProvider", None, {"astronomy"}),
        ("weather_providers.currentuvindex", "CurrentUVIndexProvider", None, {"uv"}),
        ("weather_providers.gdacs", "GdacsProvider", None, {"alerts"}),
        ("weather_providers.eccc", "ECCCProvider", None, {"alerts"}),
        ("weather_providers.metno", "MetNoProvider", None,
         {"current", "forecast", "hourly", "alerts", "nowcast"}),
        ("weather_providers.waqi", "WAQIProvider", "k", {"air_quality"}),
        ("weather_providers.openaq", "OpenAQProvider", "k", {"air_quality"}),
        ("weather_providers.iqair", "IQAirProvider", "k", {"air_quality"}),
        ("weather_providers.nasapower", "NasaPowerProvider", None, {"historical"}),
        ("weather_providers.nifc", "NIFCProvider", None, {"wildfire"}),
        ("weather_providers.firms", "FirmsProvider", "k", {"wildfire"}),
        ("weather_providers.swpc", "SWPCProvider", None, {"space_weather"}),
        ("weather_providers.tidecheck", "TideCheckProvider", "k", {"tides"}),
        ("weather_providers.noaa_coops", "NoaaCoopsProvider", None, {"tides"}),
    ])
    def test_capabilities(self, modpath, cls, key, caps):
        import importlib
        klass = getattr(importlib.import_module(modpath), cls)
        provider = klass(key) if key is not None else klass()
        discovered = Dispatcher().register(provider, cls.lower())
        assert discovered == caps


# ── mocked fetch() behaviour ─────────────────────────────────────────────

def _patch(monkeypatch, module, fn):
    monkeypatch.setattr(module, "get_json", fn)


class TestSunriseSunset:
    def test_full_astronomy(self, monkeypatch):
        from weather_providers.sunrisesunset import astronomy
        async def stub(url, **kw):
            return {"status": "OK", "results": {
                "sunrise": "6:00:00 AM", "sunset": "8:00:00 PM",
                "day_length": "14:00:00", "moonrise": "3:00:00 PM",
                "moonset": "2:00:00 AM", "moon_phase": "Waxing Gibbous",
                "moon_illumination": "60.5"}}
        _patch(monkeypatch, astronomy, stub)
        r = asyncio.run(astronomy.fetch(40.0, -80.0, "Pittsburgh"))
        assert isinstance(r, AstronomyResult)
        assert r.moon_phase == "Waxing Gibbous"
        assert r.moon_illumination == pytest.approx(60.5)
        assert r.sunrise == "6:00:00 AM"

    def test_bad_status_raises(self, monkeypatch):
        from weather_providers.sunrisesunset import astronomy
        async def stub(url, **kw):
            return {"status": "ERROR"}
        _patch(monkeypatch, astronomy, stub)
        with pytest.raises(HTTPError):
            asyncio.run(astronomy.fetch(0.0, 0.0, "x"))


class TestWAQI:
    def test_ok(self, monkeypatch):
        from weather_providers.waqi import air_quality
        async def stub(url, **kw):
            return {"status": "ok", "data": {"aqi": 42, "city": {"name": "Beijing"}}}
        _patch(monkeypatch, air_quality, stub)
        r = asyncio.run(air_quality.fetch("k", 39.9, 116.4, "Beijing"))
        assert isinstance(r, AirQualityResult)
        assert r.aqi == 42 and r.category == "Good"
        assert "WAQI" in r.source

    def test_error_status_raises(self, monkeypatch):
        from weather_providers.waqi import air_quality
        async def stub(url, **kw):
            return {"status": "error", "data": "Unknown station"}
        _patch(monkeypatch, air_quality, stub)
        with pytest.raises(HTTPError):
            asyncio.run(air_quality.fetch("k", 0.0, 0.0, "x"))

    def test_dash_aqi_raises(self, monkeypatch):
        from weather_providers.waqi import air_quality
        async def stub(url, **kw):
            return {"status": "ok", "data": {"aqi": "-"}}
        _patch(monkeypatch, air_quality, stub)
        with pytest.raises(HTTPError):
            asyncio.run(air_quality.fetch("k", 0.0, 0.0, "x"))


class TestCurrentUVIndex:
    def test_uv_now_and_peak(self, monkeypatch):
        from weather_providers.currentuvindex import uv
        async def stub(url, **kw):
            return {"ok": True, "now": {"time": "2026-06-22T18:00:00Z", "uvi": 5.2},
                    "forecast": [{"time": "2026-06-22T20:00:00Z", "uvi": 7.1},
                                 {"time": "2026-06-23T12:00:00Z", "uvi": 9.0}]}
        _patch(monkeypatch, uv, stub)
        r = asyncio.run(uv.fetch(40.0, -80.0, "x"))
        assert isinstance(r, UVResult)
        assert r.uv_index == pytest.approx(5.2)
        assert r.uv_max == pytest.approx(7.1)   # same-day peak, not 9.0 (tomorrow)
        assert r.category == "Moderate"

    def test_not_ok_raises(self, monkeypatch):
        from weather_providers.currentuvindex import uv
        async def stub(url, **kw):
            return {"ok": False}
        _patch(monkeypatch, uv, stub)
        with pytest.raises(HTTPError):
            asyncio.run(uv.fetch(0.0, 0.0, "x"))


class TestNasaPower:
    def test_parses_date_and_fill(self, monkeypatch):
        from weather_providers.nasapower import historical
        async def stub(url, **kw):
            return {"properties": {"parameter": {
                "T2M": {"20200715": 25.0}, "T2M_MAX": {"20200715": 30.0},
                "T2M_MIN": {"20200715": 20.0}, "PRCPTOTCORR": {"20200715": -999},
                "RH2M": {"20200715": 55.0}, "WS10M_MAX": {"20200715": 5.0}}}}
        _patch(monkeypatch, historical, stub)
        r = asyncio.run(historical.fetch(40.0, -80.0, "x", "2020-07-15"))
        assert isinstance(r, HistoricalResult)
        assert r.avg_c == 25.0 and r.high_c == 30.0 and r.low_c == 20.0
        assert r.precip_mm is None            # -999 fill -> None
        assert r.max_wind_kph == pytest.approx(18.0)   # 5 m/s * 3.6
        assert r.date == "2020-07-15"

    def test_all_missing_raises(self, monkeypatch):
        from weather_providers.nasapower import historical
        async def stub(url, **kw):
            return {"properties": {"parameter": {
                "T2M": {"20200715": -999}, "T2M_MAX": {"20200715": -999},
                "T2M_MIN": {"20200715": -999}, "PRCPTOTCORR": {"20200715": -999},
                "RH2M": {"20200715": -999}, "WS10M_MAX": {"20200715": -999}}}}
        _patch(monkeypatch, historical, stub)
        with pytest.raises(HTTPError):
            asyncio.run(historical.fetch(0.0, 0.0, "x", "2020-07-15"))


class TestSWPC:
    def test_kp_and_nearest_aurora(self, monkeypatch):
        from weather_providers.swpc import space_weather
        async def stub(url, **kw):
            if "planetary_k_index" in url:
                return [{"time_tag": "t0", "kp_index": 2.0, "estimated_kp": 3.33}]
            if "ovation" in url:
                return {"coordinates": [[0, 0, 1], [280, 40, 42], [281, 40, 5]]}
            return {}
        _patch(monkeypatch, space_weather, stub)
        # lon -80 -> 280 in 0..359 grid; lat 40 -> grid (280,40) == 42%
        r = asyncio.run(space_weather.fetch(40.0, -80.0, "x"))
        assert isinstance(r, SpaceWeatherResult)
        assert r.kp_index == pytest.approx(3.33)   # estimated_kp preferred
        assert r.kp_category == "Quiet"
        assert r.aurora_pct == pytest.approx(42.0)


class TestGDACS:
    def test_distance_filter(self, monkeypatch):
        from weather_providers.gdacs import alerts
        async def stub(url, **kw):
            return {"features": [
                {"geometry": {"coordinates": [10.5, 10.5]},
                 "properties": {"eventtype": "EQ", "name": "M5.0 quake",
                                "alertlevel": "Orange", "fromdate": "a", "todate": "b"}},
                {"geometry": {"coordinates": [100.0, 80.0]},
                 "properties": {"eventtype": "TC", "name": "Far cyclone",
                                "alertlevel": "Red"}},
            ]}
        _patch(monkeypatch, alerts, stub)
        r = asyncio.run(alerts.fetch(10.0, 10.0, "x"))
        assert isinstance(r, AlertsResult)
        assert len(r.alerts) == 1                    # far one filtered out
        assert r.alerts[0].event == "Earthquake"
        assert r.alerts[0].severity == "Orange"

    def test_antipodal_no_math_domain_error(self, monkeypatch):
        # Regression: haversine must clamp sqrt(a) so a near-antipodal event
        # doesn't raise ValueError(math domain error) mid-iteration.
        from weather_providers.gdacs import alerts
        async def stub(url, **kw):
            return {"features": [
                {"geometry": {"coordinates": [180.0, -1.0]},
                 "properties": {"eventtype": "EQ", "name": "Antipode quake",
                                "alertlevel": "Red"}},
            ]}
        _patch(monkeypatch, alerts, stub)
        r = asyncio.run(alerts.fetch(1.0, 0.0, "x"))   # must not raise
        assert isinstance(r, AlertsResult)


class TestNWSCoverage:
    """A non-US point must not be reported as an NWS failure.

    api.weather.gov answers a well-formed request for a point outside its
    coverage with HTTP 400 ('Parameter "point" is invalid: out of bounds').
    Raised as an HTTPError that reaches the dispatcher, it calls
    record_failure() and dings the NWS circuit breaker - so enough non-US
    queries could open it and degrade US alerts.  Observed live: `.al cirus
    cirus` geocoded to Spain and logged dispatch_fail for nws.
    """

    @pytest.mark.parametrize("status", [400, 404])
    def test_no_data_statuses_become_out_of_coverage(self, status):
        # /alerts/active?point= answers 400 "out of bounds"; /points/ answers
        # 404 "Data Unavailable For Requested Point". Both mean "not covered".
        import weather_providers.nws._scope as scope
        from weather_providers._http import HTTPError

        async def boom(url, **kw):
            raise HTTPError(f"HTTP {status} for ...", status=status)

        orig = scope.get_json
        try:
            scope.get_json = boom
            with pytest.raises(scope.OutOfCoverage):
                asyncio.run(scope.nws_json("https://api.weather.gov/x"))
        finally:
            scope.get_json = orig

    @pytest.mark.parametrize("status", [403, 429, 500, 503])
    def test_real_failures_still_propagate(self, status):
        # An outage, a rate-limit or an auth problem must stay a failure so the
        # breaker and the rate-limit accounting still see it.
        import weather_providers.nws._scope as scope
        from weather_providers._http import HTTPError

        async def boom(url, **kw):
            raise HTTPError(f"HTTP {status} for ...", status=status,
                            is_rate_limit=(status == 429))

        orig = scope.get_json
        try:
            scope.get_json = boom
            with pytest.raises(HTTPError):
                asyncio.run(scope.nws_json("https://api.weather.gov/x"))
        finally:
            scope.get_json = orig

    def test_inland_point_is_not_a_marine_failure(self, monkeypatch):
        # San Dimas is nowhere near a marine zone; NWS says so by omitting the
        # forecast zone. That is a normal answer, not a provider failure.
        from weather_providers.nws import NWSProvider, marine
        async def stub(url, **kw):
            return {"properties": {}}      # no forecastZone key
        _patch(monkeypatch, marine, stub)
        r = asyncio.run(NWSProvider().get_marine(34.1067, -117.8067, "San Dimas, CA"))
        assert r is None

    def test_provider_returns_none_outside_coverage(self):
        # None (not an exception) is what makes the dispatcher fall through
        # to a global provider without recording a failure.
        from weather_providers.nws import NWSProvider
        import weather_providers.nws._scope as scope
        from weather_providers._http import HTTPError

        async def boom(url, **kw):
            raise HTTPError("HTTP 400 for ...", status=400)

        orig = scope.get_json
        try:
            scope.get_json = boom
            p = NWSProvider()
            for call in (
                p.get_alerts(38.4697, -0.9729, "Monovar, Spain"),
                p.get_weather(38.4697, -0.9729, "Monovar, Spain"),
                p.get_forecast(38.4697, -0.9729, "Monovar, Spain"),
                p.get_hourly(38.4697, -0.9729, "Monovar, Spain"),
                p.get_marine(38.4697, -0.9729, "Monovar, Spain"),
            ):
                assert asyncio.run(call) is None
        finally:
            scope.get_json = orig

    def test_no_failure_recorded_when_provider_returns_none(self):
        # The dispatcher contract this fix depends on.
        from weather_providers._dispatch import Dispatcher

        class _Uncovered:
            name, requires_key = "uncovered", False
            async def get_weather(self, lat, lon, location, **kw):
                return None
            async def get_forecast(self, lat, lon, location, days=4, **kw):
                return None

        d = Dispatcher()
        d.register(_Uncovered(), "uncovered")
        assert asyncio.run(d.dispatch("current", 0.0, 0.0, "x")) is None
        health = d._providers["uncovered"].health
        assert health.is_callable(), "returning None must not trip the breaker"


class TestNWSAlertScope:
    """A state name must query the whole state, not one geocoded point.

    Reported live: with Tropical Storm Bertha's centre on the Mississippi
    coast, `.al mississippi` returned only a Heat Advisory from NWS Jackson.
    The point lookup landed inland, so every coastal warning was invisible -
    api.weather.gov returned 1 alert for that point and 15 for area=MS.
    """

    def test_point_lookup_is_the_default(self, monkeypatch):
        from weather_providers.nws import alerts
        seen = {}
        async def stub(url, **kw):
            seen.update(kw.get("params") or {})
            return {"features": []}
        _patch(monkeypatch, alerts, stub)
        asyncio.run(alerts.fetch(32.2988, -90.1848, "Jackson, MS"))
        assert seen.get("point") == "32.2988,-90.1848"
        assert "area" not in seen

    def test_area_lookup_replaces_the_point(self, monkeypatch):
        from weather_providers.nws import alerts
        seen = {}
        async def stub(url, **kw):
            seen.update(kw.get("params") or {})
            return {"features": []}
        _patch(monkeypatch, alerts, stub)
        asyncio.run(alerts.fetch(32.2988, -90.1848, "MS", area="MS"))
        assert seen.get("area") == "MS"
        # Sending both would narrow the query straight back to the point.
        assert "point" not in seen

    def test_user_agent_carries_a_contact(self):
        from weather_providers.nws import alerts
        ua = alerts._HEADERS["User-Agent"]
        assert "@" in ua or "http" in ua.lower(), ua


class TestNIFC:
    def test_no_fires_is_empty(self, monkeypatch):
        from weather_providers.nifc import wildfire
        async def stub(url, **kw):
            return {"features": []}
        _patch(monkeypatch, wildfire, stub)
        r = asyncio.run(wildfire.fetch(40.0, -80.0, "x"))
        assert isinstance(r, WildfireResult)
        assert r.fire_count == 0

    def test_nearest_and_count(self, monkeypatch):
        from weather_providers.nifc import wildfire
        async def stub(url, **kw):
            return {"features": [
                {"attributes": {"IncidentName": "Big Fire", "IncidentSize": 750},
                 "geometry": {"x": -80.0, "y": 40.1}},
                {"attributes": {"IncidentName": "Small Fire", "IncidentSize": 10},
                 "geometry": {"x": -81.0, "y": 41.0}},
            ]}
        _patch(monkeypatch, wildfire, stub)
        r = asyncio.run(wildfire.fetch(40.0, -80.0, "x"))
        assert r.fire_count == 2
        assert r.nearest_name == "Big Fire"
        assert r.max_acres == 750
        assert r.nearest_km is not None and r.nearest_km < 20

    def test_max_acres_reads_incident_size_not_discovery_acres(self, monkeypatch):
        # WFIGS `DiscoveryAcres` is the size at INITIAL REPORT and sits at a
        # dispatch default of 0.01 on nearly every record; `IncidentSize` is
        # the current size.  Reading the wrong one reported the 2690-acre
        # SUMMIT fire as "Largest 0 acres" next to 46 incidents.
        from weather_providers.nifc import wildfire
        async def stub(url, **kw):
            return {"features": [
                {"attributes": {"IncidentName": "SUMMIT",
                                "DiscoveryAcres": 0.01, "IncidentSize": 2690},
                 "geometry": {"x": -117.8, "y": 34.2}},
                {"attributes": {"IncidentName": "LAC-253228",
                                "DiscoveryAcres": 0.01, "IncidentSize": None},
                 "geometry": {"x": -117.85, "y": 34.11}},
            ]}
        _patch(monkeypatch, wildfire, stub)
        r = asyncio.run(wildfire.fetch(34.1067, -117.8067, "San Dimas, CA"))
        assert r.max_acres == 2690
        assert r.fire_count == 2
        assert r.sized_count == 1     # only SUMMIT carries a current size

    def test_max_acres_none_when_no_incident_is_sized(self, monkeypatch):
        # The common case near a metro area: dozens of 0.01-acre dispatch
        # stubs, none of them sized.  Report no acreage rather than a "0".
        from weather_providers.nifc import wildfire
        async def stub(url, **kw):
            return {"features": [
                {"attributes": {"IncidentName": f"LAC-{n}", "DiscoveryAcres": 0.01},
                 "geometry": {"x": -117.85, "y": 34.11}}
                for n in range(5)
            ]}
        _patch(monkeypatch, wildfire, stub)
        r = asyncio.run(wildfire.fetch(34.1067, -117.8067, "San Dimas, CA"))
        assert r.fire_count == 5
        assert r.sized_count == 0
        assert r.max_acres is None


class TestTideCheck:
    def test_builds_frozen_result_once(self, monkeypatch):
        # Regression: TideResult is frozen - fetch must construct it once with
        # locals, not assign attributes after the fact (FrozenInstanceError).
        from weather_providers.tidecheck import tides
        from weather_providers.base import TideResult
        async def stub(url, **kw):
            if "nearest" in url:
                return {"id": "S1", "name": "Test Harbor"}
            return {"extremes": [
                {"type": "High", "time": "2026-06-22T18:00:00Z", "height": 1.8},
                {"type": "Low", "time": "2026-06-22T12:00:00Z", "height": 0.3},
            ]}
        _patch(monkeypatch, tides, stub)
        r = asyncio.run(tides.fetch("k", 47.6, -122.3, "Seattle"))
        assert isinstance(r, TideResult)
        assert r.station == "Test Harbor"
        assert r.next_high_time and r.next_high_m == 1.8
        assert r.next_low_time and r.next_low_m == 0.3

    def test_no_station_raises(self, monkeypatch):
        from weather_providers.tidecheck import tides
        async def stub(url, **kw):
            return {}
        _patch(monkeypatch, tides, stub)
        with pytest.raises(HTTPError):
            asyncio.run(tides.fetch("k", 0.0, 0.0, "x"))


# Every new provider's get_* methods must accept **kwargs - the dispatcher
# forwards kwargs to them. Two shipped without it (tidecheck, swpc); guard it.
_NEW_PROVIDERS = [
    ("weather_providers.airnow", "AirNowProvider", "k"),
    ("weather_providers.purpleair", "PurpleAirProvider", "k"),
    ("weather_providers.sunrisesunset", "SunriseSunsetProvider", None),
    ("weather_providers.currentuvindex", "CurrentUVIndexProvider", None),
    ("weather_providers.gdacs", "GdacsProvider", None),
    ("weather_providers.eccc", "ECCCProvider", None),
    ("weather_providers.metno", "MetNoProvider", None),
    ("weather_providers.waqi", "WAQIProvider", "k"),
    ("weather_providers.openaq", "OpenAQProvider", "k"),
    ("weather_providers.iqair", "IQAirProvider", "k"),
    ("weather_providers.nasapower", "NasaPowerProvider", None),
    ("weather_providers.nifc", "NIFCProvider", None),
    ("weather_providers.firms", "FirmsProvider", "k"),
    ("weather_providers.swpc", "SWPCProvider", None),
    ("weather_providers.tidecheck", "TideCheckProvider", "k"),
    ("weather_providers.noaa_coops", "NoaaCoopsProvider", None),
    ("weather_providers.pollendotcom", "PollenDotComProvider", "ua"),
    ("weather_providers.google_pollen", "GooglePollenProvider", "k"),
]


class TestTimezoneWindows:
    """The hourly/nowcast window must be selected TZ-correctly, independent of
    the bot host's timezone (regression for the naive datetime.now() compares)."""

    def test_weatherapi_filters_past_by_epoch(self, monkeypatch):
        import time as _t
        from weather_providers.weatherapi import hourly
        now = _t.time()
        payload = {"forecast": {"forecastday": [{"hour": [
            {"time_epoch": now - 3600, "time": "past", "temp_c": 1.0,
             "condition": {"text": ""}},
            {"time_epoch": now + 3600, "time": "fut1", "temp_c": 2.0,
             "condition": {"text": ""}},
            {"time_epoch": now + 7200, "time": "fut2", "temp_c": 3.0,
             "condition": {"text": ""}},
        ]}]}}
        async def stub(url, **kw):
            return payload
        _patch(monkeypatch, hourly, stub)
        r = asyncio.run(hourly.fetch("k", 40.0, -80.0, "X", hours=12))
        assert [h.temp_c for h in r.hours] == [2.0, 3.0]   # past hour excluded

    def test_openmeteo_hourly_uses_utc_offset(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        from weather_providers.openmeteo import hourly
        off = 5 * 3600  # UTC+5 - unrelated to the test host's zone
        local_now = datetime.now(timezone.utc) + timedelta(seconds=off)

        def iso(dt):
            return dt.replace(tzinfo=None, microsecond=0, second=0).isoformat()

        times = [iso(local_now - timedelta(hours=1)),
                 iso(local_now + timedelta(hours=1)),
                 iso(local_now + timedelta(hours=2))]
        payload = {"utc_offset_seconds": off, "hourly": {
            "time": times, "temperature_2m": [1.0, 2.0, 3.0],
            "weather_code": [0, 0, 0], "precipitation": [0, 0, 0],
            "precipitation_probability": [0, 0, 0],
            "relative_humidity_2m": [0, 0, 0],
            "wind_speed_10m": [0, 0, 0], "wind_direction_10m": [0, 0, 0]}}
        async def stub(url, **kw):
            return payload
        _patch(monkeypatch, hourly, stub)
        r = asyncio.run(hourly.fetch(40.0, -80.0, "X", hours=12))
        temps = [h.temp_c for h in r.hours]
        assert temps and temps[0] == 2.0   # window starts at first future hour
        assert 1.0 not in temps            # the past hour was excluded


@pytest.mark.parametrize("modpath,cls,key", _NEW_PROVIDERS)
def test_get_methods_accept_kwargs(modpath, cls, key):
    import importlib
    import inspect
    klass = getattr(importlib.import_module(modpath), cls)
    inst = klass(key) if key is not None else klass()
    methods = [n for n in dir(inst) if n.startswith("get_")]
    assert methods, f"{cls} exposes no get_* method"
    for name in methods:
        sig = inspect.signature(getattr(inst, name))
        assert any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()), \
            f"{cls}.{name} must accept **kwargs (dispatcher forwards kwargs)"


# ── pollen providers (Pollen.com US, Google global, Open-Meteo Europe) ─────

class TestPollenProviders:
    def test_pollendotcom_us(self, monkeypatch):
        from weather_providers.pollendotcom import pollen
        async def stub(url, **kw):
            if "nominatim" in url:
                return {"address": {"country_code": "us", "postcode": "91773"}}
            return {"Location": {"periods": [
                {"Type": "Today", "Index": 4.6,
                 "Triggers": [{"Name": "Oak"}, {"Name": "Sagebrush"}]}]}}
        _patch(monkeypatch, pollen, stub)
        r = asyncio.run(pollen.fetch("ua", 34.1, -117.8, "San Dimas, CA"))
        assert r.source == "Pollen.com"
        assert r.overall_index == pytest.approx(4.6)
        assert r.category == "Low-Med"
        assert r.triggers == ("Oak", "Sagebrush")

    def test_pollendotcom_reverse_zoom_is_high_enough_for_a_zip(self, monkeypatch):
        """zoom=10 (city) omits the postcode for most US locations, so
        Pollen.com silently returned None for them: only a place whose OSM node
        carries a ZIP at city zoom worked (San Dimas did, Pasadena did not).
        Nominatim's reverse default is 18 and reliably includes the postcode.
        Empirically at zoom=10, 3 of 4 sampled US cities had no postcode.
        """
        from weather_providers.pollendotcom import pollen
        seen = {}
        async def stub(url, **kw):
            if "nominatim" in url:
                seen["zoom"] = (kw.get("params") or {}).get("zoom")
                return {"address": {"country_code": "us", "postcode": "91101"}}
            return {"Location": {"periods": [
                {"Type": "Today", "Index": 3.0, "Triggers": []}]}}
        _patch(monkeypatch, pollen, stub)
        asyncio.run(pollen.fetch("ua", 34.1476, -118.1441, "Pasadena, CA"))
        assert seen.get("zoom") is not None, "no reverse-geocode request was made"
        assert int(seen["zoom"]) >= 18, (
            f"reverse zoom {seen['zoom']!r} is too coarse to return a ZIP")

    def test_pollendotcom_non_us_returns_none(self, monkeypatch):
        from weather_providers.pollendotcom import pollen
        async def stub(url, **kw):
            return {"address": {"country_code": "de", "postcode": "10115"}}
        _patch(monkeypatch, pollen, stub)
        assert asyncio.run(pollen.fetch("ua", 52.5, 13.4, "Berlin")) is None

    def test_google_pollen(self, monkeypatch):
        from weather_providers.google_pollen import pollen
        async def stub(url, **kw):
            return {"dailyInfo": [{"pollenTypeInfo": [
                {"code": "TREE", "indexInfo": {"value": 3}},
                {"code": "GRASS", "indexInfo": {"value": 1}},
                {"code": "WEED", "indexInfo": {"value": 0}}]}]}
        _patch(monkeypatch, pollen, stub)
        r = asyncio.run(pollen.fetch("k", 34.1, -117.8, "x"))
        assert r.source == "Google Pollen"
        assert (r.tree_index, r.grass_index, r.weed_index) == (3.0, 1.0, 0.0)

    def test_google_pollen_no_data_returns_none(self, monkeypatch):
        from weather_providers.google_pollen import pollen
        async def stub(url, **kw):
            return {"dailyInfo": []}
        _patch(monkeypatch, pollen, stub)
        assert asyncio.run(pollen.fetch("k", 0.0, 0.0, "x")) is None

    def test_openmeteo_empty_returns_none(self, monkeypatch):
        from weather_providers.openmeteo import pollen
        async def stub(url, **kw):
            return {"current": {}}          # CAMS has no data outside Europe
        _patch(monkeypatch, pollen, stub)
        assert asyncio.run(pollen.fetch(34.1, -117.8, "x")) is None

    def test_formatter_renders_each_model(self):
        from modules.weather import _format_pollen
        from weather_providers.base import PollenResult
        com = _format_pollen(PollenResult(
            source="Pollen.com", location="x", overall_index=4.6,
            category="Low-Med", triggers=("Oak", "Sagebrush")))
        assert "4.6/12" in com and "Low-Med" in com and "Oak" in com
        goog = _format_pollen(PollenResult(
            source="Google Pollen", location="x",
            tree_index=3.0, grass_index=1.0, weed_index=0.0))
        assert "Tree" in goog and "3/5" in goog
        om = _format_pollen(PollenResult(source="Open-Meteo", location="x", grass=22.0))
        assert "Grass 22" in om and "grains/m³" in om
