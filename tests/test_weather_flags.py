"""Tests for modules/weather.py — _parse_weather_flags and friends.

Covers the per-provider alias map (-aw, -vc, -nws, etc.), the -p/-l/-n
escape hatches, and the unknown-flag passthrough behaviour.
"""

from __future__ import annotations

import pytest

from modules.weather import (
    _PROVIDER_FLAGS,
    _parse_weather_flags,
    _flag_examples_for,
)


# ── _parse_weather_flags: shape contract ─────────────────────────────────

class TestParseShape:
    def test_returns_4tuple(self):
        out = _parse_weather_flags(None)
        assert isinstance(out, tuple) and len(out) == 4

    def test_empty_input(self):
        assert _parse_weather_flags(None) == (None, False, None, None)
        assert _parse_weather_flags("") == (None, False, None, None)
        assert _parse_weather_flags("   ") == (None, False, None, None)

    def test_plain_location_passes_through(self):
        provider, list_mode, rest, bad = _parse_weather_flags("New York, NY")
        assert provider is None
        assert list_mode is False
        assert rest == "New York, NY"
        assert bad is None


# ── -l list mode ────────────────────────────────────────────────────────

class TestListFlag:
    def test_l_alone(self):
        provider, list_mode, rest, bad = _parse_weather_flags("-l")
        assert list_mode is True
        assert provider is None
        assert rest is None
        assert bad is None

    def test_l_with_location_keeps_location(self):
        # -l mid-arg should still set list_mode and pass the location through.
        provider, list_mode, rest, _ = _parse_weather_flags("-l Boston")
        assert list_mode is True
        assert rest == "Boston"


# ── -p backwards-compat path ────────────────────────────────────────────

class TestPFlag:
    def test_p_with_alias(self):
        provider, _, rest, bad = _parse_weather_flags("-p wk Seattle")
        # -p resolves through the alias table just like -<alias>.
        assert provider == "weatherkit"
        assert rest == "Seattle"
        assert bad is None

    def test_p_with_canonical(self):
        provider, _, rest, _ = _parse_weather_flags("-p nws")
        assert provider == "nws"
        assert rest is None

    def test_p_passes_through_unknown_as_literal(self):
        # If the value isn't in the alias map it's kept literally — the
        # dispatcher will reject it downstream.
        provider, _, _, bad = _parse_weather_flags("-p totallyfake here")
        assert provider == "totallyfake"
        # "-p totallyfake" is recognized so it's NOT an unknown-flag warning.
        assert bad is None


# ── -n <nick> passthrough ───────────────────────────────────────────────

class TestNFlag:
    def test_n_kept_together(self):
        provider, list_mode, rest, _ = _parse_weather_flags("-n alice")
        # _resolve handles -n directly, so it must survive in rest unchanged.
        assert rest == "-n alice"
        assert provider is None
        assert list_mode is False

    def test_n_with_provider_flag(self):
        provider, _, rest, _ = _parse_weather_flags("-wk -n bob")
        assert provider == "weatherkit"
        assert rest == "-n bob"


# ── Per-provider alias map ──────────────────────────────────────────────

class TestProviderAliases:
    @pytest.mark.parametrize(
        "alias,canonical",
        [
            ("aw", "weatherkit"),
            ("apple", "weatherkit"),
            ("appleweather", "weatherkit"),
            ("wk", "weatherkit"),
            ("weatherkit", "weatherkit"),
            ("vc", "visualcrossing"),
            ("visualcrossing", "visualcrossing"),
            ("nws", "nws"),
            ("om", "openmeteo"),
            ("openmeteo", "openmeteo"),
            ("acc", "accuweather"),
            ("accuweather", "accuweather"),
            ("owm", "openweathermap"),
            ("openweathermap", "openweathermap"),
            ("wb", "weatherbit"),
            ("weatherbit", "weatherbit"),
            ("wapi", "weatherapi"),
            ("weatherapi", "weatherapi"),
            ("pw", "pirateweather"),
            ("pirate", "pirateweather"),
            ("pirateweather", "pirateweather"),
            ("sg", "stormglass"),
            ("stormglass", "stormglass"),
            ("tio", "tomorrowio"),
            ("tomorrow", "tomorrowio"),
            ("tomorrowio", "tomorrowio"),
            ("wwo", "worldweatheronline"),
            ("worldweatheronline", "worldweatheronline"),
            ("ws", "weatherstack"),
            ("weatherstack", "weatherstack"),
            ("mm", "meteomatics"),
            ("meteomatics", "meteomatics"),
        ],
    )
    def test_alias_resolves(self, alias, canonical):
        provider, _, _, bad = _parse_weather_flags(f"-{alias} Boston")
        assert provider == canonical, (
            f"-{alias} resolved to {provider!r}, expected {canonical!r}"
        )
        assert bad is None

    def test_aw_is_weatherkit_not_accuweather(self):
        # Regression guard: -aw is APPLE Weather (WeatherKit), -acc is
        # AccuWeather.  Easy mistake to make and a silent footgun.
        provider, _, _, _ = _parse_weather_flags("-aw Boston")
        assert provider == "weatherkit"
        assert provider != "accuweather"

    def test_case_insensitive(self):
        provider, _, _, _ = _parse_weather_flags("-WK -L Boston")
        assert provider == "weatherkit"

    def test_provider_flag_consumed_from_rest(self):
        # The recognized provider flag must not pollute the geocode query.
        _, _, rest, _ = _parse_weather_flags("-vc Tokyo")
        assert rest == "Tokyo"
        assert "vc" not in (rest or "").lower().split()


# ── Unknown flag handling ───────────────────────────────────────────────

class TestUnknownFlag:
    def test_first_unknown_flag_recorded(self):
        provider, _, rest, bad = _parse_weather_flags("-zzz Boston")
        assert provider is None
        assert bad == "-zzz"
        # The bad token must NOT pollute the geocode query.
        assert rest == "Boston"

    def test_only_first_unknown_recorded(self):
        _, _, _, bad = _parse_weather_flags("-zzz -yyy Boston")
        assert bad == "-zzz"  # only first

    def test_negative_number_not_treated_as_flag(self):
        # -33.86 should NOT be a flag — used for southern-hemisphere coords.
        _, _, rest, bad = _parse_weather_flags("-33.8688,151.2093")
        assert bad is None
        assert rest == "-33.8688,151.2093"


# ── _flag_examples_for ──────────────────────────────────────────────────

class TestFlagExamplesFor:
    def test_returns_dash_aliases(self):
        out = _flag_examples_for("weatherkit")
        # Must list every alias for weatherkit, each prefixed with `-`.
        aliases = [a for a, c in _PROVIDER_FLAGS.items() if c == "weatherkit"]
        for a in aliases:
            assert f"-{a}" in out

    def test_canonical_id_included(self):
        out = _flag_examples_for("nws")
        assert "-nws" in out

    def test_unknown_canonical_yields_empty(self):
        # A provider id with no alias mappings just returns empty string.
        assert _flag_examples_for("nonexistent_provider") == ""

    def test_sorted_short_to_long(self):
        # Sort order is by len so short codes appear first for terse help.
        out = _flag_examples_for("weatherkit")
        parts = out.split("/")
        lens = [len(p) for p in parts]
        assert lens == sorted(lens)


# ── Alias map invariants ────────────────────────────────────────────────

class TestAliasMapInvariants:
    def test_canonical_self_alias(self):
        # Every canonical id must be reachable by typing its own name.
        canonicals = set(_PROVIDER_FLAGS.values())
        for c in canonicals:
            assert _PROVIDER_FLAGS.get(c) == c, (
                f"canonical id {c!r} does not self-alias"
            )

    def test_all_aliases_lowercase(self):
        for alias in _PROVIDER_FLAGS:
            assert alias == alias.lower(), (
                f"alias {alias!r} is not lowercase — case-insensitive lookup "
                "would silently miss the upper-case form"
            )

    def test_aw_alias_distinct_from_acc(self):
        # The comment in modules/weather.py warns about this collision.
        assert _PROVIDER_FLAGS["aw"] != _PROVIDER_FLAGS["acc"]


# ── _send_provider_list and _validate_provider (behavioural) ────────────

class _FakeBot:
    """Minimal stand-in for IRCBot — captures preply/privmsg calls."""

    def __init__(self):
        self.lines: list[tuple[str, str, str]] = []  # (kind, target, msg)

    def preply(self, nick, reply_to, msg):
        self.lines.append(("preply", reply_to, msg))

    def privmsg(self, target, msg):
        self.lines.append(("privmsg", target, msg))

    def notice(self, nick, msg):
        self.lines.append(("notice", nick, msg))

    @property
    def text(self) -> str:
        return "\n".join(l[2] for l in self.lines)


def _make_module(bot=None):
    from modules.weather import WeatherModule
    m = WeatherModule.__new__(WeatherModule)
    m.bot = bot or _FakeBot()
    return m


@pytest.fixture
def keyless_dispatcher():
    """Reset the global dispatcher to just the keyless providers."""
    from configparser import ConfigParser
    from weather_providers import configure
    configure(ConfigParser())
    yield
    # Leave it as configure() left it; other tests will reconfigure as needed.


class TestSendProviderList:
    def test_unknown_capability_yields_error_line(self, keyless_dispatcher):
        bot = _FakeBot()
        m = _make_module(bot)
        m._send_provider_list("alice", "#chan", "bogus_cap")
        assert any("unknown capability" in line for _, _, line in bot.lines)

    def test_known_capability_lists_ranked_providers(self, keyless_dispatcher):
        bot = _FakeBot()
        m = _make_module(bot)
        m._send_provider_list("alice", "#chan", "current")
        text = bot.text
        # Should list nws and openmeteo in accuracy order — nws first.
        assert "nws" in text
        assert "openmeteo" in text
        assert text.index("nws") < text.index("openmeteo")
        # Legend line included.
        assert "[OK]" in text and "[?]" in text and "[X]" in text

    def test_provider_list_contains_flag_examples(self, keyless_dispatcher):
        bot = _FakeBot()
        m = _make_module(bot)
        m._send_provider_list("alice", "#chan", "current")
        # The flag-examples helper output must appear so users know how to
        # type the provider on the wire.
        assert "-om" in bot.text or "-openmeteo" in bot.text


class TestValidateProvider:
    def test_unregistered_provider_rejected(self, keyless_dispatcher):
        bot = _FakeBot()
        m = _make_module(bot)
        ok = m._validate_provider("alice", "#chan", "weatherapi", "current")
        assert ok is False
        assert "not active" in bot.text

    def test_registered_but_wrong_capability_rejected(self, keyless_dispatcher):
        bot = _FakeBot()
        m = _make_module(bot)
        # nws does not implement air_quality.
        ok = m._validate_provider("alice", "#chan", "nws", "air_quality")
        assert ok is False
        assert "doesn't support" in bot.text or "support" in bot.text

    def test_registered_with_capability_accepted(self, keyless_dispatcher):
        bot = _FakeBot()
        m = _make_module(bot)
        ok = m._validate_provider("alice", "#chan", "openmeteo", "current")
        assert ok is True
        # On success no reply line should be sent.
        assert bot.lines == []
