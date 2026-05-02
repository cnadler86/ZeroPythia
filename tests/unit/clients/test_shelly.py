"""Unit tests for ShellyClient.

Tests Layer-2 data extraction and cache / stale-data behaviour.
No real HTTP connections are made – ``_fetch_gen2_raw`` and ``_fetch_gen1_raw``
are patched with ``AsyncMock`` where needed.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from clients.shelly.shelly import ShellyClient


# ---------------------------------------------------------------------------
# Sample raw responses
# ---------------------------------------------------------------------------

_GEN2_DATA = {
    "total_act_power": 100.0,
    "a_act_power": 50.0,
    "b_act_power": 30.0,
    "c_act_power": 20.0,
}

_GEN1_DATA = {
    "total_power": 90.0,
    "emeters": [
        {"power": 40.0, "total": 1000.0},
        {"power": 30.0, "total": 800.0},
        {"power": 20.0, "total": 600.0},
    ],
}


def _client_with_cache(data: dict, gen: int = 2, age: float = 0.0) -> ShellyClient:
    """Return a ShellyClient pre-loaded with cached data."""
    client = ShellyClient("127.0.0.1", timeout=2.0, cache_ttl=0.5)
    client._gen = gen
    client._raw_cache = data
    client._cache_timestamp = time.time() - age
    return client


# ---------------------------------------------------------------------------
# get_phase_powers
# ---------------------------------------------------------------------------


async def test_get_phase_powers_gen2_returns_tuple() -> None:
    client = _client_with_cache(_GEN2_DATA, gen=2)
    result = await client.get_phase_powers()
    assert result == (50.0, 30.0, 20.0)
    await client.close()


async def test_get_phase_powers_gen1_returns_tuple() -> None:
    client = _client_with_cache(_GEN1_DATA, gen=1)
    result = await client.get_phase_powers()
    assert result == (40.0, 30.0, 20.0)
    await client.close()


async def test_get_phase_powers_returns_none_when_no_data() -> None:
    client = ShellyClient("127.0.0.1", timeout=0.5, cache_ttl=0.1)
    client._gen = 2
    with patch.object(client, "_fetch_gen2_raw", new=AsyncMock(return_value=None)):
        result = await client.get_phase_powers(use_cache=False)
    assert result is None
    await client.close()


# ---------------------------------------------------------------------------
# get_total_power
# ---------------------------------------------------------------------------


async def test_get_total_power_gen2() -> None:
    client = _client_with_cache(_GEN2_DATA, gen=2)
    result = await client.get_total_power()
    assert result == pytest.approx(100.0)
    await client.close()


async def test_get_total_power_gen1() -> None:
    client = _client_with_cache(_GEN1_DATA, gen=1)
    result = await client.get_total_power()
    assert result == pytest.approx(90.0)
    await client.close()


async def test_get_total_power_returns_none_on_error() -> None:
    client = ShellyClient("127.0.0.1", timeout=0.5, cache_ttl=0.1)
    client._gen = 2
    with patch.object(client, "_fetch_gen2_raw", new=AsyncMock(return_value=None)):
        result = await client.get_total_power(use_cache=False)
    assert result is None
    await client.close()


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


async def test_cache_hit_within_ttl_skips_http() -> None:
    """A second call within cache_ttl must NOT issue another HTTP request."""
    client = _client_with_cache(_GEN2_DATA, gen=2, age=0.0)  # cache fresh
    mock_fetch = AsyncMock(return_value=_GEN2_DATA)
    with patch.object(client, "_fetch_gen2_raw", new=mock_fetch):
        result = await client.get_phase_powers(use_cache=True)
    # HTTP mock must NOT have been called (cache was fresh)
    mock_fetch.assert_not_called()
    assert result == (50.0, 30.0, 20.0)
    await client.close()


async def test_cache_expired_triggers_http() -> None:
    """Once cache_ttl expires, the next call must issue a new HTTP request."""
    client = _client_with_cache(_GEN2_DATA, gen=2, age=10.0)  # cache stale
    mock_fetch = AsyncMock(return_value=_GEN2_DATA)
    with patch.object(client, "_fetch_gen2_raw", new=mock_fetch):
        result = await client.get_phase_powers(use_cache=True)
    mock_fetch.assert_called_once()
    assert result == (50.0, 30.0, 20.0)
    await client.close()


# ---------------------------------------------------------------------------
# Stale-data timeout
# ---------------------------------------------------------------------------


async def test_stale_data_returned_within_timeout() -> None:
    """HTTP failure + data within stale_timeout → return stale data."""
    client = ShellyClient("127.0.0.1", timeout=5.0, cache_ttl=0.1)
    client._gen = 2
    client._raw_cache = _GEN2_DATA
    client._cache_timestamp = time.time() - 1.0  # 1 s old (< 5 s timeout)

    with patch.object(client, "_fetch_gen2_raw", new=AsyncMock(return_value=None)):
        state = await client.get_state(use_cache=False)

    assert state is not None
    assert state.total_power_w == pytest.approx(100.0)
    await client.close()


async def test_none_after_stale_timeout_expires() -> None:
    """HTTP failure + data older than stale_timeout → return None."""
    client = ShellyClient("127.0.0.1", timeout=1.0, cache_ttl=0.1)
    client._gen = 2
    client._raw_cache = _GEN2_DATA
    client._cache_timestamp = time.time() - 5.0  # 5 s old (> 1 s timeout)

    with patch.object(client, "_fetch_gen2_raw", new=AsyncMock(return_value=None)):
        result = await client.get_phase_powers(use_cache=False)

    assert result is None
    await client.close()


async def test_stale_timeout_equals_zero_never_serves_stale() -> None:
    """timeout=0 means never serve stale data."""
    client = ShellyClient("127.0.0.1", timeout=0.0, cache_ttl=0.1)
    client._gen = 2
    client._raw_cache = _GEN2_DATA
    client._cache_timestamp = time.time() - 0.2  # older than cache_ttl

    with patch.object(client, "_fetch_gen2_raw", new=AsyncMock(return_value=None)):
        result = await client.get_phase_powers(use_cache=False)

    assert result is None
    await client.close()
