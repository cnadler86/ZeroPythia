"""Async Shelly Client - Shelly 3EM Stromzähler.
============================================

Vollständig asynchroner Client für Shelly 3EM (Pro).

Architektur (zwei Layer):
  Layer 1 Hardware-Kommunikation (_fetch_raw):
      Erkennt die Gerätegeneration, ruft den passenden Endpunkt ab und
      speichert die rohe JSON-Antwort im Cache.

  Layer 2 Daten-Extraktion (get_state, get_consumption, get_power):
      Liest aus dem Raw-Cache und wandelt die Rohdaten in typisierte
      Domain-Objekte um.  Kein HTTP-Call findet hier statt.
"""

import logging
from dataclasses import dataclass
from time import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class GridState:
    """Aktuelle Netzleistung (Watt)."""

    total_power_w: float
    phase_a_power_w: float
    phase_b_power_w: float
    phase_c_power_w: float


@dataclass
class GridConsumption:
    """Kumulierter Netzverbrauch (Wh)."""

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
# Statische Parser (Layer 2 – reine Daten-Transformation, kein I/O)
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
    """Asynchroner Client für Shelly 3EM (Pro) Stromzähler.

    Unterstützt:
    - Shelly 3EM (Gen1)
    - Shelly Pro 3EM (Gen2)
    """

    def __init__(self, ip: str, timeout: float = 2.0, cache_ttl: float = 0.5):
        self._base_url = f"http://{ip}"
        self._http_timeout = aiohttp.ClientTimeout(total=cache_ttl)
        self._session: Optional[aiohttp.ClientSession] = None

        # Layer 1 – Raw-Cache: speichert die unveränderte JSON-Antwort des Geräts
        self._raw_cache: Optional[dict] = None
        self._cache_timestamp: float = 0
        self._cache_ttl: float = cache_ttl

        # Stale-Data-Timeout: wie lange alte Daten bei Verbindungsproblemen noch gültig sind
        self._stale_timeout: float = timeout

        # Gerätegeneration (wird beim ersten Abruf automatisch erkannt)
        self._gen: Optional[int] = None

    # ------------------------------------------------------------------
    # Session-Verwaltung
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Stellt sicher dass eine Session existiert."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._http_timeout)
        return self._session

    async def close(self) -> None:
        """Schließt die Session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ------------------------------------------------------------------
    # Layer 1 – Hardware-Kommunikation
    # ------------------------------------------------------------------

    async def _detect_generation(self) -> int:
        """Erkennt automatisch die Shelly Generation (wird nur einmal aufgerufen)."""
        if self._gen is not None:
            return self._gen

        session = await self._ensure_session()

        # Versuche Gen2 API (Shelly Pro 3EM)
        try:
            async with session.get(
                f"{self._base_url}/rpc/Shelly.GetDeviceInfo"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if "gen" in data:
                        self._gen = data.get("gen", 1)
                        logger.info("Shelly Gen%d erkannt", self._gen)
                        return self._gen # type: ignore[return-value]
        except Exception:
            pass

        # Fallback: Gen1 (Shelly 3EM)
        try:
            async with session.get(f"{self._base_url}/status") as response:
                if response.status == 200:
                    self._gen = 1
                    logger.info("Shelly Gen1 erkannt")
                    return self._gen
        except Exception:
            pass

        # Default
        self._gen = 1
        return self._gen

    async def _fetch_gen1_raw(self) -> Optional[dict]:
        """GET /status  →  rohe JSON-Antwort (Gen1)."""
        try:
            session = await self._ensure_session()
            async with session.get(f"{self._base_url}/status") as response:
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error("Shelly Gen1 Fehler: %s", e)
            return None

    async def _fetch_gen2_raw(self) -> Optional[dict]:
        """GET /rpc/EM.GetStatus?id=0  →  rohe JSON-Antwort (Gen2)."""
        try:
            session = await self._ensure_session()
            async with session.get(
                f"{self._base_url}/rpc/EM.GetStatus?id=0"
            ) as response:
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error("Shelly Gen2 Fehler: %s", e)
            return None

    async def _fetch_raw(self, use_cache: bool = True) -> Optional[tuple[int, dict]]:
        """Layer-1-Einstiegspunkt: liefert (generation, raw_data).

        Gibt gecachte Rohdaten zurück, solange sie frisch genug sind.
        Bei Verbindungsfehlern werden Stale-Daten bis zum stale_timeout
        weiterverwendet.  Gibt None zurück, wenn keine gültigen Daten
        verfügbar sind.
        """
        now = time()

        # Schneller Cache-Hit innerhalb cache_ttl
        if (
            use_cache
            and self._raw_cache is not None
            and self._gen is not None
            and (now - self._cache_timestamp) < self._cache_ttl
        ):
            return self._gen, self._raw_cache

        # Gerätegeneration (einmalig) ermitteln
        gen = await self._detect_generation()

        # Passenden Endpunkt abrufen
        raw = await (self._fetch_gen2_raw() if gen >= 2 else self._fetch_gen1_raw())

        if raw is not None:
            self._raw_cache = raw
            self._cache_timestamp = now
            return gen, raw

        # Fehler beim Abruf – Stale-Daten verwenden, falls noch gültig
        if self._raw_cache is not None and (now - self._cache_timestamp) < self._stale_timeout:
            logger.warning("Shelly: verwende veraltete Daten (%.1fs alt)", now - self._cache_timestamp)
            return gen, self._raw_cache

        return None

    # ------------------------------------------------------------------
    # Layer 2 – Daten-Extraktion aus dem Raw-Cache
    # ------------------------------------------------------------------

    async def get_state(self, use_cache: bool = True) -> Optional[GridState]:
        """Aktuelle Netzleistung als GridState.

        Returns:
            GridState oder None wenn keine (gültigen) Daten verfügbar sind.
        """
        result = await self._fetch_raw(use_cache)
        if result is None:
            return None
        gen, data = result
        try:
            return _parse_gen2_state(data) if gen >= 2 else _parse_gen1_state(data)
        except (KeyError, IndexError) as e:
            logger.error("Shelly GridState Parsing-Fehler: %s", e)
            return None

    async def get_consumption(self, use_cache: bool = True) -> Optional[GridConsumption]:
        """Kumulierter Netzverbrauch als GridConsumption.

        Aktuell nur für Gen1 verfügbar (Gen2 liefert diese Daten an
        einem separaten Endpunkt – noch nicht implementiert).

        Returns:
            GridConsumption oder None.
        """
        result = await self._fetch_raw(use_cache)
        if result is None:
            return None
        gen, data = result
        if gen >= 2:
            logger.debug("Shelly Gen2: GridConsumption noch nicht implementiert")
            return None
        try:
            return _parse_gen1_consumption(data)
        except (KeyError, IndexError) as e:
            logger.error("Shelly GridConsumption Parsing-Fehler: %s", e)
            return None

    async def get_power(self, use_cache: bool = True) -> Optional[float]:
        """Aktuelle Gesamtleistung in Watt.

        Returns:
            Positiv = Netzbezug, Negativ = Einspeisung, None bei Fehler.
        """
        state = await self.get_state(use_cache)
        return state.total_power_w if state else None
