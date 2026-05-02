"""Async Shelly client for Shelly 3EM energy meters.

Fully asynchronous client for Shelly 3EM (Pro).

Architecture (two layers):
  Layer 1 – hardware communication (_fetch_raw):
      Detects the device generation, calls the appropriate endpoint, and
      stores the raw JSON response in a cache.

  Layer 2 – data extraction (get_state, get_consumption, get_power):
      Reads from the raw cache and converts raw data into typed domain
      objects.  No HTTP call happens here.
"""

import logging
from dataclasses import dataclass
from time import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class GridState:
    """Current grid power (watts)."""

    total_power_w: float
    phase_a_power_w: float
    phase_b_power_w: float
    phase_c_power_w: float


@dataclass
class GridConsumption:
    """Cumulative grid energy consumption (Wh)."""

    total_power_wh: float = 0.0
    phase_a_power_wh: float = 0.0
    phase_b_power_wh: float = 0.0
    phase_c_power_wh: float = 0.0

    def __post_init__(self) -> None:
        if self.total_power_wh == 0 and (
            self.phase_a_power_wh or self.phase_b_power_wh or self.phase_c_power_wh
        ):
            self.total_power_wh = (
                self.phase_a_power_wh + self.phase_b_power_wh + self.phase_c_power_wh
            )


# ---------------------------------------------------------------------------
# Static parsers (Layer 2 – pure data transformation, no I/O)
# ---------------------------------------------------------------------------


def _parse_gen1_state(data: dict) -> GridState:
    return GridState(
        total_power_w=data["total_power"],
        phase_a_power_w=data["emeters"][0]["power"],
        phase_b_power_w=data["emeters"][1]["power"],
        phase_c_power_w=data["emeters"][2]["power"],
    )


def _parse_gen1_consumption(data: dict) -> GridConsumption:
    return GridConsumption(
        phase_a_power_wh=data["emeters"][0]["total"],
        phase_b_power_wh=data["emeters"][1]["total"],
        phase_c_power_wh=data["emeters"][2]["total"],
    )


def _parse_gen2_state(data: dict) -> GridState:
    return GridState(
        total_power_w=data["total_act_power"],
        phase_a_power_w=data["a_act_power"],
        phase_b_power_w=data["b_act_power"],
        phase_c_power_w=data["c_act_power"],
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ShellyClient:
    """Asynchronous client for Shelly 3EM (Pro) energy meters.

    Supports:
    - Shelly 3EM (Gen1)
    - Shelly Pro 3EM (Gen2)
    """

    def __init__(self, ip: str, timeout: float = 2.0, cache_ttl: float = 0.5):
        self._base_url = f"http://{ip}"
        self._http_timeout = aiohttp.ClientTimeout(total=cache_ttl)
        self._session: Optional[aiohttp.ClientSession] = None

        # Layer 1 – raw cache: stores the unmodified JSON response from the device
        self._raw_cache: Optional[dict] = None
        self._cache_timestamp: float = 0
        self._cache_ttl: float = cache_ttl

        # Stale-data timeout: how long old data remains valid when the connection fails
        self._stale_timeout: float = timeout

        # Device generation (auto-detected on first fetch)
        self._gen: Optional[int] = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure a session exists."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._http_timeout)
        return self._session

    async def close(self) -> None:
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ------------------------------------------------------------------
    # Layer 1 – hardware communication
    # ------------------------------------------------------------------

    async def _detect_generation(self) -> int:
        """Auto-detect the Shelly generation (called only once)."""
        if self._gen is not None:
            return self._gen

        session = await self._ensure_session()

        # Try Gen2 API (Shelly Pro 3EM)
        try:
            async with session.get(f"{self._base_url}/rpc/Shelly.GetDeviceInfo") as response:
                if response.status == 200:
                    data = await response.json()
                    if "gen" in data:
                        self._gen = data.get("gen", 1)
                        logger.info("Shelly Gen%d detected", self._gen)
                        return self._gen  # type: ignore[return-value]
        except Exception:
            logger.debug("Shelly Gen2 detection failed", exc_info=True)

        # Fallback: Gen1 (Shelly 3EM)
        try:
            async with session.get(f"{self._base_url}/status") as response:
                if response.status == 200:
                    self._gen = 1
                    logger.info("Shelly Gen1 detected")
                    return self._gen
        except Exception:
            logger.debug("Shelly Gen1 detection failed", exc_info=True)

        # Default
        self._gen = 1
        return self._gen

    async def _fetch_gen1_raw(self) -> Optional[dict]:
        """GET /status → raw JSON response (Gen1)."""
        try:
            session = await self._ensure_session()
            async with session.get(f"{self._base_url}/status") as response:
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error("Shelly Gen1 error: %s", e)
            return None

    async def _fetch_gen2_raw(self) -> Optional[dict]:
        """GET /rpc/EM.GetStatus?id=0 → raw JSON response (Gen2)."""
        try:
            session = await self._ensure_session()
            async with session.get(f"{self._base_url}/rpc/EM.GetStatus?id=0") as response:
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error("Shelly Gen2 error: %s", e)
            return None

    async def _fetch_raw(self, use_cache: bool = True) -> Optional[tuple[int, dict]]:
        """Layer-1 entry point: returns (generation, raw_data).

        Returns cached raw data as long as it is fresh enough.
        On connection errors, stale data is reused up to stale_timeout.
        Returns None when no valid data is available.
        """
        now = time()

        # Fast cache hit within cache_ttl
        if (
            use_cache
            and self._raw_cache is not None
            and self._gen is not None
            and (now - self._cache_timestamp) < self._cache_ttl
        ):
            return self._gen, self._raw_cache

        # Detect device generation (once)
        gen = await self._detect_generation()

        # Fetch from the appropriate endpoint
        raw = await (self._fetch_gen2_raw() if gen >= 2 else self._fetch_gen1_raw())

        if raw is not None:
            self._raw_cache = raw
            self._cache_timestamp = now
            return gen, raw

        # Fetch failed – use stale data if still within timeout
        if self._raw_cache is not None and (now - self._cache_timestamp) < self._stale_timeout:
            logger.warning("Shelly: using stale data (%.1f s old)", now - self._cache_timestamp)
            return gen, self._raw_cache

        return None

    # ------------------------------------------------------------------
    # Layer 2 – data extraction from the raw cache
    # ------------------------------------------------------------------

    async def get_state(self, use_cache: bool = True) -> Optional[GridState]:
        """Current grid power as a GridState.

        Returns:
            GridState or None when no (valid) data is available.
        """
        result = await self._fetch_raw(use_cache)
        if result is None:
            return None
        gen, data = result
        try:
            return _parse_gen2_state(data) if gen >= 2 else _parse_gen1_state(data)
        except (KeyError, IndexError) as e:
            logger.error("Shelly GridState parsing error: %s", e)
            return None

    async def get_consumption(self, use_cache: bool = True) -> Optional[GridConsumption]:
        """Cumulative grid consumption as a GridConsumption object.

        Currently only available for Gen1 (Gen2 provides this data at a
        separate endpoint – not yet implemented).

        Returns:
            GridConsumption or None.
        """
        result = await self._fetch_raw(use_cache)
        if result is None:
            return None
        gen, data = result
        if gen >= 2:
            logger.debug("Shelly Gen2: GridConsumption not yet implemented")
            return None
        try:
            return _parse_gen1_consumption(data)
        except (KeyError, IndexError) as e:
            logger.error("Shelly GridConsumption parsing error: %s", e)
            return None

    async def get_power(self, use_cache: bool = True) -> Optional[float]:
        """Current total power in watts.

        Returns:
            Positive = grid draw, negative = feed-in, None on error.
        """
        state = await self.get_state(use_cache)
        return state.total_power_w if state else None

    async def get_phase_powers(
        self, use_cache: bool = True
    ) -> Optional[tuple[float, float, float]]:
        """Current phase powers as (A, B, C) in watts.

        Returns None when no valid data is available (connection failed and
        stale-data timeout has expired).  The runtime should treat None as
        a signal to enter the grid-meter fallback mode.
        """
        state = await self.get_state(use_cache)
        if state is None:
            return None
        return (state.phase_a_power_w, state.phase_b_power_w, state.phase_c_power_w)

    async def get_total_power(self, use_cache: bool = True) -> Optional[float]:
        """Current total grid power in watts.  Delegates to get_power()."""
        return await self.get_power(use_cache)
