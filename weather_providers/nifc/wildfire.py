"""NIFC - active US wildfire incidents near a point (WFIGS, ArcGIS).

Queries the WFIGS Incident Locations (Current) FeatureServer with a
point + radius spatial filter and summarises the nearby incidents.
"""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import WildfireResult, haversine_km as _haversine_km

_BASE = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)
_RADIUS_MI = 80  # search radius


async def fetch(lat, lon, location):
    # Acreage: the layer has no `DailyAcres` (it 400s if requested), but it
    # does carry `IncidentSize` - the CURRENT size - alongside
    # `DiscoveryAcres`, which is the size at initial report and sits at a
    # dispatch default of 0.01 on nearly every record.  Reading
    # `DiscoveryAcres` reported the 2690-acre SUMMIT fire as "Largest 0
    # acres".  `IncidentSize` is null on most records (small incidents nobody
    # sized), so we also count how many carry one rather than implying the
    # whole set is measured.
    data = await get_json(_BASE, params={
        "where": "1=1",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "distance": _RADIUS_MI,
        "units": "esriSRUnit_StatuteMile",
        "outFields": "IncidentName,IncidentSize,IncidentTypeCategory,POOState",
        "returnGeometry": "true",
        "f": "json",
    })
    if isinstance(data, dict) and data.get("error"):
        msg = (data["error"].get("message") or "query error")
        raise HTTPError(f"NIFC: {msg}", status=None, provider_hint="nifc")
    features = data.get("features") if isinstance(data, dict) else None
    if not features:
        # Valid "no active fires within the radius" - return empty result.
        return WildfireResult(source="NIFC", location=location, fire_count=0)

    nearest_km: float | None = None
    nearest_name = ""
    max_acres: float | None = None
    sized_count = 0
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        lat_f = lon_f = None

    for feat in features:
        if not isinstance(feat, dict):
            continue
        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}
        gx, gy = geom.get("x"), geom.get("y")
        if lat_f is not None and gx is not None and gy is not None:
            try:
                d = _haversine_km(lat_f, lon_f, float(gy), float(gx))
            except (TypeError, ValueError):
                d = None
            if d is not None and (nearest_km is None or d < nearest_km):
                nearest_km = d
                nearest_name = (attrs.get("IncidentName") or "").strip()
        acres = attrs.get("IncidentSize")
        if acres is not None:
            try:
                af = float(acres)
            except (TypeError, ValueError):
                af = None
            if af is not None:
                sized_count += 1
                if max_acres is None or af > max_acres:
                    max_acres = af

    return WildfireResult(
        source="NIFC",
        location=location,
        fire_count=len(features),
        nearest_km=round(nearest_km, 1) if nearest_km is not None else None,
        nearest_name=nearest_name,
        max_acres=max_acres,
        sized_count=sized_count,
    )
