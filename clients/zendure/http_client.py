"""SolarFlowAsyncClient – local HTTP API implementation.

Asynchronous client for Zendure SolarFlow devices.
Implements the HAL with aiohttp:
- _fetch_response(): HTTP GET
- _set_properties(): HTTP POST

All high-level methods are inherited from BatteryManager → SolarFlowBattery.
"""

import asyncio
import json
import logging
from typing import Dict, Optional

import aiohttp

from .base import BatteryManager
from .models import APIResponseProtocol

logger = logging.getLogger(__name__)


class SolarFlowAsyncClient(BatteryManager):
    """Asynchronous HTTP client for the Zendure SolarFlow local API.

    Implements HAL:
    - _fetch_response(): HTTP GET → APIResponse
    - _set_properties(): HTTP POST

    Inherits from BatteryManager → SolarFlowBattery:
    - Mode management (start_discharge, start_charge, stop, is_settled)
    - Data access (get_state, get_battery_packs)
    - Limits (get/set output_limit, input_limit, ac_mode, min_soc, max_soc)
    - SOC guards, energy accumulation, validation
    """

    def __init__(self, device_ip: str, *, timeout: float = 2.0, cache_ttl: float = 1.0):
        """Initialise the asynchronous client.

        Args:
            device_ip: IP address of the SolarFlow device
            timeout: HTTP timeout in seconds
            cache_ttl: cache time-to-live in seconds
        """
        super().__init__(device_ip=device_ip, cache_ttl=cache_ttl)

        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure an HTTP session exists."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ==================== HAL Implementation ====================

    async def _fetch_response(self) -> Optional[APIResponseProtocol]:
        """Pure hardware access: HTTP GET request.

        Uses a Pydantic TypeAdapter for fast JSON parsing.
        Cache handling is done in SolarFlowBattery._get_full_response().

        Returns:
            APIResponse (implements APIResponseProtocol) or None on error
        """
        url = f"{self._base_url}/properties/report"
        try:
            session = await self._ensure_session()
            async with session.get(url) as response:
                response.raise_for_status()
                json_bytes = await response.read()
                parsed = self._decoder.validate_json(json_bytes)
                logger.debug(
                    "GET %s → %d B  sn=%s  solar=%dW  home=%dW  bypass=%d",
                    url,
                    len(json_bytes),
                    parsed.sn,
                    parsed.properties.solar_input_power,
                    parsed.properties.output_home_power,
                    parsed.properties.bypass,
                )
                return parsed

        except asyncio.TimeoutError:
            logger.warning("SolarFlow API timeout (GET %s) – device not responding", url)
            return None
        except aiohttp.ClientResponseError as e:
            logger.error("SolarFlow API HTTP error (GET %s): %s %s", url, e.status, e.message)
            return None
        except aiohttp.ClientError as e:
            logger.error("SolarFlow API connection error (GET %s): %s", url, e)
            return None
        except Exception as e:
            logger.error("SolarFlow unexpected error (GET %s): %s", url, e, exc_info=True)
            return None

    async def _set_properties(self, properties: Dict, smart_mode: bool = True) -> bool:
        """Set properties via HTTP POST.

        Args:
            properties: dict of properties to set (camelCase keys!)
            smart_mode: True = RAM only (recommended), False = write to flash
        """
        url = f"{self._base_url}/properties/write"
        try:
            if self._sn is None:
                await self._get_full_response(use_cache=False)
                if self._sn is None:
                    logger.error("_set_properties: serial number could not be determined")
                    return False

            payload = self._prepare_properties_payload(properties, smart_mode)
            payload_size = len(json.dumps(payload).encode())
            if payload_size > 512:
                raise ValueError(f"Payload size {payload_size} exceeds 512 bytes limit")
            prop_keys = list(properties.keys())
            logger.debug(
                "POST %s sn=%s props=%s",
                url,
                self._sn,
                prop_keys,
            )

            session = await self._ensure_session()
            async with session.post(url, json=payload) as response:
                self._invalidate_cache()
                ok = response.status == 200
                if ok:
                    logger.debug("POST %s → OK  %s", url, prop_keys)
                else:
                    logger.warning(
                        "POST %s → HTTP %d (expected 200)  props=%s",
                        url,
                        response.status,
                        prop_keys,
                    )
                return ok

        except asyncio.TimeoutError:
            logger.warning(
                "SolarFlow API timeout (POST %s) – props=%s", url, list(properties.keys())
            )
            return False
        except aiohttp.ClientResponseError as e:
            logger.error("SolarFlow API HTTP error (POST %s): %s %s", url, e.status, e.message)
            return False
        except aiohttp.ClientError as e:
            logger.error("SolarFlow API connection error (POST %s): %s", url, e)
            return False
        except Exception as e:
            logger.error("SolarFlow unexpected error (POST %s): %s", url, e, exc_info=True)
            return False
