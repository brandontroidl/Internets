"""Tests for the stocks module helper functions."""

from modules.stocks import _fmt_change, _fmt_number
import modules.stocks as stocks


def test_fmt_change_positive():
    result = _fmt_change(2.50, 1.25)
    assert "+2.50" in result
    assert "+1.25%" in result


def test_fmt_change_negative():
    result = _fmt_change(-3.10, -2.00)
    assert "-3.10" in result
    assert "-2.00%" in result


def test_fmt_number_billions():
    assert _fmt_number(1_500_000_000) == "1.50B"


def test_fmt_number_millions():
    assert _fmt_number(42_000_000) == "42.00M"


def test_fmt_number_thousands():
    assert _fmt_number(8_500) == "8.50K"


def test_fmt_number_small():
    assert _fmt_number(123.45) == "123.45"


class TestProviderErrorsDoNotLeakKeys:
    """`.stock` must not print an API key into the channel.

    `_try_providers()` used to interpolate `str(exception)` into the reply. A
    requests exception embeds the full prepared URL, and every finance provider
    here carries its credential in the query string, so any provider failure
    published the key. The trigger is not exotic: `raise_for_status()` renders
    the URL, so an expired or quota-exhausted key does it on the next call -
    which means a botched key rotation publishes the replacement.
    """

    _KEY = "SECRETKEY123"

    def _providers(self, exc):
        def boom(symbol, key, ua):
            raise exc
        return [("Finnhub", "finnhub_key", boom)]

    def test_transport_error_text_does_not_reach_the_reply(self):
        import requests
        exc = requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='finnhub.io', port=443): Max retries "
            f"exceeded with url: /api/v1/quote?symbol=AAPL&token={self._KEY}")
        out = stocks._try_providers(
            self._providers(exc), "AAPL", {"finnhub_key": self._KEY}, "ua")
        assert self._KEY not in out
        assert "Finnhub" in out

    def test_http_error_text_does_not_reach_the_reply(self):
        import requests
        exc = requests.exceptions.HTTPError(
            f"401 Client Error: Unauthorized for url: "
            f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={self._KEY}")
        out = stocks._try_providers(
            self._providers(exc), "AAPL", {"finnhub_key": self._KEY}, "ua")
        assert self._KEY not in out

    def test_reply_still_identifies_what_failed(self):
        """Scrubbing must not reduce the reply to a bare 'it failed'."""
        out = stocks._try_providers(
            self._providers(ValueError("no data")), "AAPL",
            {"finnhub_key": self._KEY}, "ua")
        assert "Finnhub" in out
        assert "ValueError" in out or "no data" in out

    def test_no_keys_configured_message_is_unchanged(self):
        out = stocks._try_providers(
            self._providers(ValueError("x")), "AAPL", {}, "ua")
        assert "no finance API keys configured" in out

    def test_key_is_scrubbed_from_the_debug_log(self, caplog):
        import logging
        import requests
        exc = requests.exceptions.HTTPError(
            f"401 for url: https://finnhub.io/q?token={self._KEY}")
        with caplog.at_level(logging.DEBUG, logger="internets"):
            stocks._try_providers(
                self._providers(exc), "AAPL", {"finnhub_key": self._KEY}, "ua")
        assert self._KEY not in caplog.text
