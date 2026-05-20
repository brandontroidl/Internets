"""Pirate Weather helpers."""
from ..base import deg_to_card, ms_to_kph  # noqa: F401
ICONS = {
    "clear-day": "Clear", "clear-night": "Clear",
    "rain": "Rain", "snow": "Snow", "sleet": "Sleet",
    "wind": "Windy", "fog": "Fog", "cloudy": "Cloudy",
    "partly-cloudy-day": "Partly Cloudy", "partly-cloudy-night": "Partly Cloudy",
    "hail": "Hail", "thunderstorm": "Thunderstorm", "tornado": "Tornado",
}
def icon_to_desc(icon): return ICONS.get(icon, icon or "Unknown")
