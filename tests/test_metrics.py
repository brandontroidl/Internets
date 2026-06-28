"""Tests for metrics.py - Prometheus text registry + loopback-only HTTP exporter.

No fixed/public port is ever bound: server tests use 127.0.0.1 with an
ephemeral port (port 0) and tear the server down. Most tests construct a
fresh ``MetricsRegistry`` so they never mutate the module-level singleton.
"""

from __future__ import annotations

import urllib.request
import urllib.error

import pytest

import metrics
from metrics import (
    MetricsRegistry,
    Counter,
    Gauge,
    _Metric,
    _escape_label_value,
    _normalize_labels,
    _format_labels,
    _format_value,
    enabled_metrics,
    registry,
)


# ── pure helpers ─────────────────────────────────────────────────────────

class TestEscapeLabelValue:
    def test_plain_unchanged(self):
        assert _escape_label_value("hello") == "hello"

    def test_backslash_doubled(self):
        assert _escape_label_value("a\\b") == "a\\\\b"

    def test_double_quote_escaped(self):
        assert _escape_label_value('a"b') == 'a\\"b'

    def test_newline_escaped(self):
        assert _escape_label_value("a\nb") == "a\\nb"

    def test_combination(self):
        # backslash is escaped first, so the others don't get re-escaped.
        assert _escape_label_value('a\\"\n') == 'a\\\\\\"\\n'

    def test_non_string_coerced(self):
        assert _escape_label_value(5) == "5"


class TestNormalizeLabels:
    def test_none_is_empty(self):
        assert _normalize_labels(None) == ()

    def test_empty_dict_is_empty(self):
        assert _normalize_labels({}) == ()

    def test_sorted_by_name(self):
        assert _normalize_labels({"b": "2", "a": "1"}) == (("a", "1"), ("b", "2"))

    def test_order_independent_coalesce(self):
        assert _normalize_labels({"a": "1", "b": "2"}) == \
            _normalize_labels({"b": "2", "a": "1"})

    def test_values_coerced_to_str(self):
        assert _normalize_labels({"a": 3}) == (("a", "3"),)

    def test_invalid_label_name_rejected(self):
        with pytest.raises(ValueError, match="invalid label name"):
            _normalize_labels({"1bad": "x"})

    def test_label_name_with_dash_rejected(self):
        with pytest.raises(ValueError):
            _normalize_labels({"a-b": "x"})


class TestFormatLabels:
    def test_empty_is_blank(self):
        assert _format_labels(()) == ""

    def test_single(self):
        assert _format_labels((("a", "1"),)) == '{a="1"}'

    def test_multiple(self):
        assert _format_labels((("a", "1"), ("b", "2"))) == '{a="1",b="2"}'

    def test_value_escaped(self):
        assert _format_labels((("a", 'x"y'),)) == '{a="x\\"y"}'


class TestFormatValue:
    def test_integer_float_plain(self):
        assert _format_value(5.0) == "5"

    def test_negative_integer(self):
        assert _format_value(-3.0) == "-3"

    def test_non_integer_float(self):
        assert _format_value(1.5) == "1.5"

    def test_bool_true(self):
        assert _format_value(True) == "1"

    def test_bool_false(self):
        assert _format_value(False) == "0"

    def test_int_input(self):
        assert _format_value(7) == "7"


# ── _Metric ──────────────────────────────────────────────────────────────

class TestMetric:
    def test_valid_name(self):
        m = _Metric("foo_bar", "help", "counter")
        assert m.name == "foo_bar"
        assert m.kind == "counter"

    def test_colon_allowed_in_metric_name(self):
        m = _Metric("foo:bar", "help", "gauge")
        assert m.name == "foo:bar"

    def test_invalid_name_rejected(self):
        with pytest.raises(ValueError, match="invalid metric name"):
            _Metric("1bad", "help", "counter")

    def test_dash_name_rejected(self):
        with pytest.raises(ValueError):
            _Metric("a-b", "help", "counter")

    def test_set_and_samples(self):
        m = _Metric("m", "h", "gauge")
        m._set(4.0)
        assert m.samples() == [((), 4.0)]

    def test_set_overwrites(self):
        m = _Metric("m", "h", "gauge")
        m._set(4.0)
        m._set(9.0)
        assert m.samples() == [((), 9.0)]

    def test_inc_accumulates(self):
        m = _Metric("m", "h", "counter")
        m._inc(1.0)
        m._inc(2.0)
        assert m.samples() == [((), 3.0)]

    def test_inc_creates_per_label_series(self):
        m = _Metric("m", "h", "counter")
        m._inc(1.0, {"x": "a"})
        m._inc(5.0, {"x": "b"})
        got = dict(m.samples())
        assert got[(("x", "a"),)] == 1.0
        assert got[(("x", "b"),)] == 5.0


# ── Counter ──────────────────────────────────────────────────────────────

class TestCounter:
    def test_default_inc_is_one(self):
        m = _Metric("m", "h", "counter")
        c = Counter(m)
        c.inc()
        assert dict(m.samples())[()] == 1.0

    def test_inc_amount(self):
        m = _Metric("m", "h", "counter")
        c = Counter(m)
        c.inc(2.5)
        assert dict(m.samples())[()] == 2.5

    def test_inc_zero_allowed(self):
        m = _Metric("m", "h", "counter")
        c = Counter(m)
        c.inc(0)
        assert dict(m.samples())[()] == 0.0

    def test_negative_rejected(self):
        c = Counter(_Metric("m", "h", "counter"))
        with pytest.raises(ValueError, match="non-negative"):
            c.inc(-1)

    def test_labels_routed(self):
        m = _Metric("m", "h", "counter")
        c = Counter(m)
        c.inc(1, {"k": "v"})
        assert dict(m.samples())[(("k", "v"),)] == 1.0


# ── Gauge ────────────────────────────────────────────────────────────────

class TestGauge:
    def test_set(self):
        m = _Metric("m", "h", "gauge")
        Gauge(m).set(42)
        assert dict(m.samples())[()] == 42.0

    def test_inc(self):
        m = _Metric("m", "h", "gauge")
        g = Gauge(m)
        g.set(10)
        g.inc(5)
        assert dict(m.samples())[()] == 15.0

    def test_dec(self):
        m = _Metric("m", "h", "gauge")
        g = Gauge(m)
        g.set(10)
        g.dec(3)
        assert dict(m.samples())[()] == 7.0

    def test_dec_below_zero(self):
        m = _Metric("m", "h", "gauge")
        g = Gauge(m)
        g.dec(2)
        assert dict(m.samples())[()] == -2.0


# ── registration ─────────────────────────────────────────────────────────

class TestRegistration:
    def test_counter_returns_counter(self):
        reg = MetricsRegistry()
        c = reg.counter("custom_total", "help")
        assert isinstance(c, Counter)

    def test_counter_idempotent_same_instance(self):
        reg = MetricsRegistry()
        a = reg.counter("custom_total", "help")
        b = reg.counter("custom_total", "help")
        assert a is b

    def test_gauge_idempotent_same_instance(self):
        reg = MetricsRegistry()
        a = reg.gauge("custom_gauge", "help")
        b = reg.gauge("custom_gauge", "help")
        assert a is b

    def test_kind_conflict_rejected(self):
        reg = MetricsRegistry()
        reg.counter("dual", "help")
        with pytest.raises(ValueError, match="already registered as counter"):
            reg.gauge("dual", "help")

    def test_invalid_metric_name_rejected(self):
        reg = MetricsRegistry()
        with pytest.raises(ValueError):
            reg.counter("1nope", "help")

    def test_defaults_registered(self):
        reg = MetricsRegistry()
        assert isinstance(reg.commands_total, Counter)
        assert isinstance(reg.module_loaded, Gauge)

    def test_default_counter_works(self):
        reg = MetricsRegistry()
        reg.commands_total.inc(labels={"module": "w", "command": "weather"})
        body = reg.render()
        assert 'internets_commands_total{command="weather",module="w"} 1' in body


# ── lifecycle gate ───────────────────────────────────────────────────────

class TestEnable:
    def test_disabled_by_default(self):
        assert MetricsRegistry().is_enabled() is False

    def test_enable_sets_flag(self):
        reg = MetricsRegistry()
        reg.enable()
        assert reg.is_enabled() is True

    def test_enable_idempotent(self):
        reg = MetricsRegistry()
        reg.enable()
        reg.enable()
        assert reg.is_enabled() is True


# ── render ───────────────────────────────────────────────────────────────

class TestRender:
    def test_empty_metric_emits_zero_sample(self):
        reg = MetricsRegistry()
        reg.counter("lonely_total", "a lonely counter")
        body = reg.render()
        assert "# HELP lonely_total a lonely counter" in body
        assert "# TYPE lonely_total counter" in body
        assert "\nlonely_total 0\n" in body

    def test_help_and_type_lines(self):
        reg = MetricsRegistry()
        reg.counter("c_total", "the help text").inc()
        body = reg.render()
        assert "# HELP c_total the help text" in body
        assert "# TYPE c_total counter" in body
        assert "c_total 1" in body

    def test_ends_with_newline(self):
        assert MetricsRegistry().render().endswith("\n")

    def test_samples_sorted_by_labels(self):
        reg = MetricsRegistry()
        c = reg.counter("ordered_total", "h")
        c.inc(1, {"k": "z"})
        c.inc(1, {"k": "a"})
        body = reg.render()
        ia = body.index('ordered_total{k="a"}')
        iz = body.index('ordered_total{k="z"}')
        assert ia < iz

    def test_metrics_sorted_by_name(self):
        reg = MetricsRegistry()
        reg.counter("zzz_total", "h").inc()
        reg.counter("aaa_total", "h").inc()
        body = reg.render()
        assert body.index("aaa_total") < body.index("zzz_total")

    def test_float_value_rendered(self):
        reg = MetricsRegistry()
        reg.gauge("g_val", "h").set(1.5)
        assert "g_val 1.5" in reg.render()


# ── expose: guard branches (no real bind that succeeds on a public addr) ──

class TestExposeGuard:
    def test_refuses_when_not_enabled(self):
        reg = MetricsRegistry()
        # Not enabled -> returns without starting a server, never raises.
        reg.expose("127.0.0.1", 0)
        assert reg._server is None

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "::0", "::ffff:0.0.0.0", " 0.0.0.0 "])
    def test_rejects_all_interfaces(self, host):
        reg = MetricsRegistry()
        reg.enable()
        with pytest.raises(ValueError, match="loopback-only"):
            reg.expose(host, 0)
        assert reg._server is None

    def test_accepts_loopback_ephemeral(self):
        reg = MetricsRegistry()
        reg.enable()
        try:
            reg.expose("127.0.0.1", 0)
            assert reg._server is not None
            # Bound to an OS-assigned ephemeral port on loopback.
            assert reg._server.server_address[0] == "127.0.0.1"
            assert reg._server.server_address[1] != 0
        finally:
            reg.shutdown()

    def test_expose_idempotent(self):
        reg = MetricsRegistry()
        reg.enable()
        try:
            reg.expose("127.0.0.1", 0)
            first = reg._server
            reg.expose("127.0.0.1", 0)  # no-op, same server
            assert reg._server is first
        finally:
            reg.shutdown()

    def test_non_ip_host_passes_guard_then_bind_fails(self):
        # A non-IP host is not is_unspecified, so it passes the loopback
        # guard; the actual bind then fails -> OSError, not ValueError.
        reg = MetricsRegistry()
        reg.enable()
        with pytest.raises(OSError):
            reg.expose("definitely.not.a.real.host.invalid", 0)


# ── expose: live handler over loopback ephemeral port ────────────────────

class TestExposeHandler:
    @pytest.fixture
    def served(self):
        reg = MetricsRegistry()
        reg.enable()
        reg.commands_total.inc(labels={"module": "w", "command": "weather"})
        reg.expose("127.0.0.1", 0)
        port = reg._server.server_address[1]
        yield reg, port
        reg.shutdown()

    def test_metrics_endpoint_200(self, served):
        reg, port = served
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            assert r.status == 200
            ctype = r.headers.get("Content-Type")
            assert "text/plain" in ctype
            assert "version=0.0.4" in ctype
            body = r.read().decode("utf-8")
        assert "internets_commands_total" in body
        assert body == reg.render()

    def test_query_string_still_matches_metrics(self, served):
        reg, port = served
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/metrics?foo=bar", timeout=5
        ) as r:
            assert r.status == 200

    def test_unknown_path_404(self, served):
        reg, port = served
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert ei.value.code == 404
        assert ei.value.read() == b"not found\n"


# ── shutdown ─────────────────────────────────────────────────────────────

class TestShutdown:
    def test_shutdown_without_server_is_safe(self):
        MetricsRegistry().shutdown()  # no raise

    def test_shutdown_clears_server(self):
        reg = MetricsRegistry()
        reg.enable()
        reg.expose("127.0.0.1", 0)
        reg.shutdown()
        assert reg._server is None
        assert reg._server_thread is None

    def test_double_shutdown_safe(self):
        reg = MetricsRegistry()
        reg.enable()
        reg.expose("127.0.0.1", 0)
        reg.shutdown()
        reg.shutdown()  # idempotent, no raise
        assert reg._server is None


# ── module singleton / introspection ─────────────────────────────────────

class TestSingleton:
    def test_registry_is_metrics_registry(self):
        assert isinstance(registry, MetricsRegistry)

    def test_enabled_metrics_lists_defaults(self):
        names = list(enabled_metrics())
        assert "internets_commands_total" in names
        assert "internets_module_loaded" in names

    def test_enabled_metrics_returns_list(self):
        assert isinstance(enabled_metrics(), list)
