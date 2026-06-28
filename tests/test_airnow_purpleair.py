"""Tests for the AirNow and PurpleAir air-quality providers.

Covers the pure AQI math (EPA 2024 PM2.5 breakpoints + humidity
correction), the dominant-pollutant selection for AirNow, the
nearest-sensor selection for PurpleAir, and the no-coverage → raise
behaviour that lets the dispatcher fall back to a global provider.
HTTP is mocked - no network, no real keys.
"""

from __future__ import annotations

import asyncio

import pytest

from weather_providers._http import HTTPError
from weather_providers.base import AirQualityResult
from weather_providers.purpleair._codes import pm25_to_aqi, epa_correct


# ── pm25_to_aqi: EPA 2024 breakpoints ────────────────────────────────────

class TestPm25ToAqi:
    @pytest.mark.parametrize("conc,aqi", [
        (0.0,   0),
        (9.0,   50),    # 2024 Good/Moderate boundary (was 12.0)
        (35.4,  100),
        (35.5,  101),
        (55.4,  150),
        (125.4, 200),
        (225.4, 300),
        (325.4, 500),
    ])
    def test_breakpoint_anchors(self, conc, aqi):
        assert pm25_to_aqi(conc) == aqi

    def test_above_top_caps_at_500(self):
        assert pm25_to_aqi(400.0) == 500
        assert pm25_to_aqi(10_000.0) == 500

    def test_none_and_negative_are_none(self):
        assert pm25_to_aqi(None) is None
        assert pm25_to_aqi(-1.0) is None

    def test_2024_revision_12ug_is_moderate_not_good(self):
        # Under the pre-2024 table 12.0 µg/m³ was AQI 50 (Good); the 2024
        # revision pushes it into Moderate (>50).
        assert pm25_to_aqi(12.0) > 50

    def test_monotonic_non_decreasing(self):
        vals = [pm25_to_aqi(c) for c in (0, 5, 9, 20, 35, 50, 100, 200, 300)]
        assert vals == sorted(vals)


# ── epa_correct: Barkjohn 2021 US-wide correction ────────────────────────

class TestEpaCorrect:
    def test_formula_applied_with_humidity(self):
        # 0.524*10 - 0.0862*50 + 5.75 = 6.68
        assert epa_correct(10.0, 50.0) == pytest.approx(6.68, abs=1e-6)

    def test_passthrough_without_humidity(self):
        assert epa_correct(10.0, None) == 10.0

    def test_none_pm_is_none(self):
        assert epa_correct(None, 50.0) is None

    def test_clamped_non_negative(self):
        # 0.524*0 - 0.0862*100 + 5.75 = -2.87 → clamps to 0.0
        assert epa_correct(0.0, 100.0) == 0.0


# ── AirNow.fetch ─────────────────────────────────────────────────────────

def _patch_get_json(monkeypatch, module, payload):
    """Replace the get_json bound inside *module* with an async stub."""
    async def _stub(url, **kwargs):
        _stub.called_with = {"url": url, **kwargs}
        return payload
    monkeypatch.setattr(module, "get_json", _stub)
    return _stub


class TestAirNowFetch:
    def test_dominant_pollutant_drives_aqi(self, monkeypatch):
        from weather_providers.airnow import air_quality
        payload = [
            {"ParameterName": "O3", "AQI": 42, "Category": {"Name": "Good"}},
            {"ParameterName": "PM2.5", "AQI": 55, "Category": {"Name": "Moderate"}},
            {"ParameterName": "PM10", "AQI": 20, "Category": {"Name": "Good"}},
        ]
        _patch_get_json(monkeypatch, air_quality, payload)
        r = asyncio.run(air_quality.fetch("k", 37.77, -122.41, "SF"))
        assert isinstance(r, AirQualityResult)
        assert r.aqi == 55
        assert r.category == "Moderate"
        assert r.source == "AirNow (PM2.5)"
        assert r.location == "SF"

    def test_key_is_query_param(self, monkeypatch):
        from weather_providers.airnow import air_quality
        stub = _patch_get_json(monkeypatch, air_quality,
                               [{"ParameterName": "PM2.5", "AQI": 10,
                                 "Category": {"Name": "Good"}}])
        asyncio.run(air_quality.fetch("secret-key", 37.0, -122.0, "X"))
        assert stub.called_with["params"]["API_KEY"] == "secret-key"

    def test_empty_list_raises_for_fallback(self, monkeypatch):
        from weather_providers.airnow import air_quality
        _patch_get_json(monkeypatch, air_quality, [])
        with pytest.raises(HTTPError):
            asyncio.run(air_quality.fetch("k", 0.0, 0.0, "Ocean"))

    def test_falls_back_to_computed_category(self, monkeypatch):
        from weather_providers.airnow import air_quality
        # No Category.Name in the payload → derive from aqi_category().
        _patch_get_json(monkeypatch, air_quality,
                        [{"ParameterName": "PM2.5", "AQI": 175}])
        r = asyncio.run(air_quality.fetch("k", 37.0, -122.0, "X"))
        assert r.aqi == 175
        assert r.category == "Unhealthy"


# ── PurpleAir.fetch ──────────────────────────────────────────────────────

class TestPurpleAirFetch:
    def test_picks_nearest_sensor_and_corrects(self, monkeypatch):
        from weather_providers.purpleair import air_quality
        payload = {
            "fields": ["latitude", "longitude", "pm2.5", "humidity"],
            "data": [
                [37.78, -122.41, 8.0, 50.0],    # ~1 km from query point
                [37.90, -122.50, 30.0, 40.0],   # ~15 km away
            ],
        }
        _patch_get_json(monkeypatch, air_quality, payload)
        r = asyncio.run(air_quality.fetch("k", 37.7749, -122.4194, "SF"))
        # Nearest sensor: pm 8.0, rh 50 → corrected 5.632 → AQI 31 (Good).
        assert r.pm25 == pytest.approx(5.6, abs=0.05)
        assert r.aqi == 31
        assert r.category == "Good"
        assert r.source.startswith("PurpleAir")

    def test_key_is_header(self, monkeypatch):
        from weather_providers.purpleair import air_quality
        stub = _patch_get_json(monkeypatch, air_quality, {
            "fields": ["latitude", "longitude", "pm2.5", "humidity"],
            "data": [[37.0, -122.0, 5.0, 50.0]],
        })
        asyncio.run(air_quality.fetch("read-key", 37.0, -122.0, "X"))
        assert stub.called_with["headers"]["X-API-Key"] == "read-key"

    def test_no_sensors_raises_for_fallback(self, monkeypatch):
        from weather_providers.purpleair import air_quality
        _patch_get_json(monkeypatch, air_quality, {"fields": [], "data": []})
        with pytest.raises(HTTPError):
            asyncio.run(air_quality.fetch("k", 0.0, 0.0, "Nowhere"))


# ── Capability discovery ─────────────────────────────────────────────────

class TestCapabilityDiscovery:
    def test_airnow_exposes_only_air_quality(self):
        from weather_providers._dispatch import Dispatcher
        from weather_providers.airnow import AirNowProvider
        caps = Dispatcher().register(AirNowProvider("k"), "airnow")
        assert caps == {"air_quality"}

    def test_purpleair_exposes_only_air_quality(self):
        from weather_providers._dispatch import Dispatcher
        from weather_providers.purpleair import PurpleAirProvider
        caps = Dispatcher().register(PurpleAirProvider("k"), "purpleair")
        assert caps == {"air_quality"}

    def test_both_require_a_key(self):
        from weather_providers.airnow import AirNowProvider
        from weather_providers.purpleair import PurpleAirProvider
        assert AirNowProvider("k").requires_key is True
        assert PurpleAirProvider("k").requires_key is True
