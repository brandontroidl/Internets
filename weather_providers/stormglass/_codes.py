"""Stormglass.io helpers."""
from ..base import deg_to_card, ms_to_kph  # noqa: F401
def _sg_val(d, key):
    """Extract a value from Stormglass nested source format."""
    obj = d.get(key)
    if obj is None: return None
    if isinstance(obj, dict):
        for src in ("sg", "noaa", "icon", "meteo", "meto", "dwd"):
            if src in obj: return obj[src]
        vals = list(obj.values())
        return vals[0] if vals else None
    return obj
