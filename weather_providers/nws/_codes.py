"""NWS helpers."""
from ..base import deg_to_card, ms_to_kph  # noqa: F401
def m_to_m(v): return v  # NWS uses meters for visibility
_SEVERITY_MAP = {"extreme": "extreme", "severe": "severe", "moderate": "moderate", "minor": "minor"}
def map_severity(s): return _SEVERITY_MAP.get((s or "").lower(), "unknown")
