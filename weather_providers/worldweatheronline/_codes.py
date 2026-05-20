"""World Weather Online helpers."""
from ..base import deg_to_card, ms_to_kph  # noqa: F401
def _val(obj, key):
    """Extract first value from WWO's nested list format."""
    v = obj.get(key)
    if isinstance(v, list) and v:
        return v[0].get("value") if isinstance(v[0], dict) else v[0]
    return v


# fix: was duplicated verbatim across current/forecast/hourly/astronomy/
# historical/marine. Moved here as the single source of truth.
def _float(v):
    """Parse a value as float, returning None on type/value error."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
