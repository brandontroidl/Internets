"""NWS helpers."""
from ..base import deg_to_card, ms_to_kph  # noqa: F401
# fix: removed dead ``m_to_m`` identity function — nothing imported it.
# (NWS already returns visibility in metres, so no conversion needed.)
_SEVERITY_MAP = {"extreme": "extreme", "severe": "severe", "moderate": "moderate", "minor": "minor"}
def map_severity(s): return _SEVERITY_MAP.get((s or "").lower(), "unknown")
