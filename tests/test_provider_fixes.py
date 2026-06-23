"""Regression tests for review-confirmed provider bugs.

  * weatherbit historical wind was stored raw m/s into a km/h field;
  * tomorrowio air-quality returned a hollow (all-None) result instead of
    failing over to a provider that actually has data.
"""

from __future__ import annotations

import asyncio

import pytest

from weather_providers._http import HTTPError
from weather_providers.base import HistoricalResult, AirQualityResult, ms_to_kph


def _patch(monkeypatch, module, payload):
    async def stub(url, **kw):
        return payload
    monkeypatch.setattr(module, "get_json", stub)


class TestWeatherbitHistoricalWind:
    def test_wind_converted_from_ms(self, monkeypatch):
        from weather_providers.weatherbit import historical
        _patch(monkeypatch, historical, {"data": [{
            "max_temp": 25, "min_temp": 15, "temp": 20,
            "precip": 1.0, "max_wind_spd": 10.0, "rh": 50}]})
        r = asyncio.run(historical.fetch("k", 40.0, -80.0, "X", "2020-07-15"))
        assert isinstance(r, HistoricalResult)
        # 10 m/s -> 36 km/h (previously reported raw as 10, ~3.6x too low).
        assert r.max_wind_kph == pytest.approx(ms_to_kph(10.0))
        assert r.max_wind_kph == pytest.approx(36.0)


class TestTomorrowioAirQualityFallthrough:
    def test_all_none_raises_for_fallback(self, monkeypatch):
        from weather_providers.tomorrowio import air_quality
        # weather/realtime without AQ entitlement: values present, no AQ fields.
        _patch(monkeypatch, air_quality, {"data": {"values": {"temperature": 20}}})
        with pytest.raises(HTTPError):
            asyncio.run(air_quality.fetch("k", 40.0, -80.0, "X"))

    def test_with_aq_data_returns_result(self, monkeypatch):
        from weather_providers.tomorrowio import air_quality
        _patch(monkeypatch, air_quality, {"data": {"values": {
            "epaIndex": 42, "particulateMatter25": 8.0}}})
        r = asyncio.run(air_quality.fetch("k", 40.0, -80.0, "X"))
        assert isinstance(r, AirQualityResult)
        assert r.aqi == 42
        assert r.pm25 == 8.0
