"""Apple WeatherKit provider package — requires Developer membership + PyJWT."""
from __future__ import annotations
import time, logging
from pathlib import Path
from ..base import *
from . import current, forecast, hourly, alerts

log = logging.getLogger("internets.weather.weatherkit")
_JWT_LIFETIME = 55 * 60

def _make_jwt(team_id, service_id, key_id, private_key):
    import jwt
    now = int(time.time())
    return jwt.encode({"iss": team_id, "iat": now, "exp": now + _JWT_LIFETIME, "sub": service_id}, private_key, algorithm="ES256", headers={"alg": "ES256", "kid": key_id, "id": f"{team_id}.{service_id}"})

class WeatherKitProvider:
    name: str = "Apple Weather"
    requires_key: bool = True
    def __init__(self, team_id, service_id, key_id, key_file):
        self._team_id, self._service_id, self._key_id = team_id, service_id, key_id
        p = Path(key_file).resolve()
        if not p.is_file(): raise FileNotFoundError(f"Key not found: {p}")
        self._pk = p.read_text(encoding="utf-8").strip()
        self._token = ""; self._token_exp = 0.0
    def _headers(self):
        now = time.time()
        if not self._token or now >= self._token_exp - 60:
            self._token = _make_jwt(self._team_id, self._service_id, self._key_id, self._pk)
            self._token_exp = now + _JWT_LIFETIME
        return {"Authorization": f"Bearer {self._token}"}
    def _url(self, lat, lon): return f"https://weatherkit.apple.com/api/v1/weather/en/{lat:.4f}/{lon:.4f}"
    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._url(lat,lon), self._headers(), location)
    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._url(lat,lon), self._headers(), location, days)
    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(self._url(lat,lon), self._headers(), location, hours)
    async def get_alerts(self, lat, lon, location, **kw):
        return await alerts.fetch(self._url(lat,lon), self._headers(), location)
