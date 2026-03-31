"""Async SolarFlow Client - Lokale HTTP API (HAL Implementation).
==============================================================

Asynchroner Client für Zendure SolarFlow Geräte.
Implementiert Hardware Abstraction Layer (HAL) mit aiohttp.

Diese Klasse kümmert sich nur um:
- HTTP Session-Verwaltung
- Low-Level API-Calls (_fetch_response, _set_properties)

Alle High-Level Methoden werden von SolarFlowBase geerbt.
"""

import asyncio
import logging
from typing import Dict, Optional

import aiohttp

from .base import SolarFlowBase
from .models import APIResponseProtocol

logger = logging.getLogger(__name__)


class SolarFlowAsyncClient(SolarFlowBase):
    """Asynchroner Client für Zendure SolarFlow Local API.

    Implementiert HAL:
    - _fetch_response(): HTTP GET → APIResponse
    - _set_properties(): HTTP POST

    Erbt von SolarFlowBase:
    - get_state(), get_battery_packs()
    - get/set für: output_limit, input_limit, ac_mode, min_soc, max_soc
    - start_discharge(), start_charge(), stop()
    """

    def __init__(self, device_ip: str, *, timeout: float = 2.0, cache_ttl: float = 1.0):
        """Initialisierung des asynchronen Clients.

        Args:
            device_ip: IP-Adresse des SolarFlow Geräts
            timeout: HTTP Timeout in Sekunden
            cache_ttl: Cache Time-To-Live in Sekunden
        """
        super().__init__(device_ip=device_ip, cache_ttl=cache_ttl)

        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Stellt sicher dass eine HTTP Session existiert."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        """Schließt die HTTP Session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ==================== HAL Implementation (2 Methoden) ====================

    async def _fetch_response(self) -> Optional[APIResponseProtocol]:
        """Pure HW-Zugriff: HTTP GET Request.

        Verwendet pydantic TypeAdapter für schnelles JSON-Parsing.
        Cache-Handling erfolgt in SolarFlowBase._get_full_response()!

        Returns:
            APIResponse (implementiert APIResponseProtocol) oder None bei Fehler
        """
        try:
            session = await self._ensure_session()
            async with session.get(f"{self._base_url}/properties/report") as response:
                response.raise_for_status()
                json_bytes = await response.read()
                return self._decoder.validate_json(json_bytes)

        except asyncio.TimeoutError:
            logger.warning("SolarFlow API Timeout - Gerät antwortet nicht")
            return None
        except aiohttp.ClientError as e:
            logger.error("SolarFlow API Fehler (GET): %s", e)
            return None
        except Exception as e:
            logger.error("SolarFlow unerwarteter Fehler: %s", e)
            return None

    async def _set_properties(self, properties: Dict, smart_mode: bool = True) -> bool:
        """Properties setzen via HTTP POST.

        Args:
            properties: Dict mit zu setzenden Properties (camelCase Keys!)
            smart_mode: True = nur RAM (empfohlen), False = Flash schreiben
        """
        try:
            if self._sn is None:
                # Erste Anfrage um SN zu bekommen
                await self._get_full_response(use_cache=False)
                if self._sn is None:
                    logger.error("Seriennummer konnte nicht ermittelt werden")
                    return False

            payload = self._prepare_properties_payload(properties, smart_mode)

            session = await self._ensure_session()
            async with session.post(
                f"{self._base_url}/properties/write", json=payload
            ) as response:
                self._invalidate_cache()
                return response.status == 200

        except asyncio.TimeoutError:
            logger.warning("SolarFlow API Timeout beim Schreiben")
            return False
        except aiohttp.ClientError as e:
            logger.error("SolarFlow API Fehler (POST): %s", e)
            return False
        except Exception as e:
            logger.error("SolarFlow unerwarteter Fehler beim Schreiben: %s", e)
            return False
