"""Apple WeatherKit provider package — requires Developer membership + PyJWT."""
from __future__ import annotations
import os
import stat
import time, logging
from pathlib import Path
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AlertsResult, AlertEntry,
)
from . import current, forecast, hourly, alerts

log = logging.getLogger("internets.weather.weatherkit")
_JWT_LIFETIME = 55 * 60

# Valid PEM headers for an ES256 private key per RFC 7468.
# Apple ships the .p8 in PKCS#8 form ("BEGIN PRIVATE KEY"); some
# operators convert to SEC1 ("BEGIN EC PRIVATE KEY") — both are
# acceptable to PyJWT's cryptography backend.
_VALID_KEY_HEADERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
)

# Allowed POSIX modes for the .p8 key file — matches secret_store.perms_ok
# semantics (owner-only access).  0o400 (read-only) is also fine because
# we only ever read the file.
_ALLOWED_KEY_MODES = frozenset({0o600, 0o400})


def _check_key_perms(path: Path) -> None:
    """Refuse to load *path* if its POSIX mode allows group/other read.

    POSIX-only.  Windows ACLs are handled by the filesystem; ``os.name``
    of ``'nt'`` short-circuits this check (the secret_store module makes
    the same trade-off — POSIX bits are advisory on Windows).
    """
    if os.name == "nt":
        return
    try:
        st = path.stat()
    except OSError as e:
        raise PermissionError(f"cannot stat key file {path}: {e}") from e
    mode = stat.S_IMODE(st.st_mode)
    if mode not in _ALLOWED_KEY_MODES:
        raise PermissionError(
            f"REFUSING to load {path}: mode is {oct(mode)}, expected 0o600 "
            f"or 0o400 (group/other must not have read access) — "
            f"run `chmod 600 {path}`"
        )


def _read_private_key(path: Path) -> str:
    """Read and validate the .p8 private key from disk.

    Trade-off: we re-read on every token refresh (once per ~55 minutes)
    rather than holding the key in memory for the lifetime of the
    process.  CPython's immutable ``str`` cannot be reliably zeroed —
    the interned/copied buffers in PyJWT, cryptography, and the GC live
    well beyond a `del`.  Re-reading limits the in-memory residency
    window to the few milliseconds the JWT signing call takes, at the
    cost of one extra ~1 KB file read per hour.  Cheap.
    """
    _check_key_perms(path)
    text = path.read_text(encoding="utf-8").strip()
    if not text.startswith(_VALID_KEY_HEADERS):
        # Don't include the file contents in the error — even the header
        # line of a wrong file may leak info (e.g. a public key marker).
        raise ValueError(
            f"key file {path} does not start with a recognised PEM header "
            f"(expected one of: {', '.join(_VALID_KEY_HEADERS)})"
        )
    return text


def _make_jwt(team_id, service_id, key_id, private_key):
    import jwt
    now = int(time.time())
    # NB: PyJWT derives the "alg" header field from the ``algorithm=``
    # kwarg automatically.  Passing it again in ``headers=`` is redundant
    # but harmless — PyJWT does not double-set on conflict, it uses the
    # one from ``algorithm=``.  Left in for explicitness; do not remove
    # ``algorithm="ES256"`` (that one is load-bearing).
    return jwt.encode(
        {"iss": team_id, "iat": now, "exp": now + _JWT_LIFETIME, "sub": service_id},
        private_key,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": key_id, "id": f"{team_id}.{service_id}"},
    )

class WeatherKitProvider:
    name: str = "Apple Weather"
    requires_key: bool = True
    def __init__(self, team_id, service_id, key_id, key_file):
        self._team_id, self._service_id, self._key_id = team_id, service_id, key_id
        p = Path(key_file).resolve()
        if not p.is_file(): raise FileNotFoundError(f"Key not found: {p}")
        # Validate perms + PEM header at init so misconfigurations fail
        # loudly at startup rather than at first weather lookup.  Don't
        # retain the key text — _headers() re-reads on each refresh.
        _read_private_key(p)
        self._key_path = p
        self._token = ""; self._token_exp = 0.0
    def _headers(self):
        now = time.time()
        if not self._token or now >= self._token_exp - 60:
            # Re-read the key file on each refresh (once per ~hour).  See
            # _read_private_key() for the rationale on why we don't cache.
            pk = _read_private_key(self._key_path)
            try:
                self._token = _make_jwt(self._team_id, self._service_id, self._key_id, pk)
            finally:
                # Best-effort: drop our local reference immediately.
                # CPython may keep a copy inside PyJWT's signer; this
                # at least removes one of the live references so the
                # GC can reclaim sooner.
                del pk
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
