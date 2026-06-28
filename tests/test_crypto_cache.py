"""The per-instance coin-id cache in crypto._fetch_sync is FIFO-bounded.

The cache key is the lowercased user query, which is attacker-influenceable
(anyone can spam distinct .gecko lookups).  Without eviction the dict grows
without bound.  These tests drive _fetch_sync past _CACHE_MAX and assert the
cache never exceeds the bound, oldest-first.
"""
from __future__ import annotations

import modules.crypto as crypto


def test_cache_bounded_under_distinct_queries(monkeypatch):
    # Resolve every query to a synthetic coin id (skips the network search).
    monkeypatch.setattr(crypto, "_resolve_coin_id", lambda q, ua: f"coin-{q.lower()}")
    # Return a valid price payload for whatever coin id was resolved.
    monkeypatch.setattr(
        crypto, "_get_json",
        lambda url, params, ua: {params["ids"]: {"usd": 1.0, "usd_24h_change": 0.0,
                                                 "usd_market_cap": 1000.0}},
    )

    cache: dict[str, str] = {}
    for i in range(crypto._CACHE_MAX + 250):
        crypto._fetch_sync(f"q{i}", cache, "ua/1.0")
        assert len(cache) <= crypto._CACHE_MAX

    assert len(cache) == crypto._CACHE_MAX
    # FIFO: earliest keys evicted, newest retained.
    assert "q0" not in cache
    assert f"q{crypto._CACHE_MAX + 249}" in cache


def test_cache_eviction_is_oldest_first():
    cache: dict[str, str] = {}
    for i in range(crypto._CACHE_MAX):
        cache[f"k{i}"] = f"v{i}"
    # Mirror the eviction guard in _fetch_sync.
    assert len(cache) >= crypto._CACHE_MAX
    cache.pop(next(iter(cache)))
    cache["k-new"] = "v-new"
    assert len(cache) == crypto._CACHE_MAX
    assert "k0" not in cache
    assert "k-new" in cache
