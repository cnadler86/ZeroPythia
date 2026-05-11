"""SolarFlowBattery – pure battery abstraction layer.

This module defines:
- ISolarFlowClient: abstract HAL interface (2 methods)
- SolarFlowBattery: battery-level implementation

The battery layer handles:
- Cache management
- Data validation and conversion
- SOC-based protection guards (capping)
- Energy accumulation (dead-reckoning counters)
- Property getters and setters

Mode management (start_discharge/start_charge/stop) lives in
``base.BatteryManager`` which inherits from this class.
"""

import logging
from abc import ABC, abstractmethod
from time import time
from typing import Dict, List, Optional, cast

from pydantic import TypeAdapter

from .models import (
    MODEL_LIMITS,
    ACMode,
    APIResponse,
    APIResponseProtocol,
    BatteryLimits,
    BatteryModel,
    DeviceState,
    InverterEnergyCounters,
    ProcessedBatteryPack,
)

logger = logging.getLogger(__name__)


# ==================== Abstract Interface ====================


class ISolarFlowClient(ABC):
    """Abstract base class – minimal interface for SolarFlow clients (HAL).

    Clients only need to implement two low-level hardware methods:
    1. _fetch_response() – pure HW access without cache
    2. _set_properties() – write data to device

    Cache handling and all high-level methods are in SolarFlowBattery.
    """

    @abstractmethod
    async def _fetch_response(self) -> Optional[APIResponseProtocol]:
        """Pure hardware access: fetch a response from the device (no cache).

        Returns:
            APIResponseProtocol or None on error
        """
        pass

    @abstractmethod
    async def _set_properties(self, properties: Dict, smart_mode: bool = True) -> bool:
        """Set device properties (low-level control).

        Args:
            properties: dict of properties to set (camelCase keys!)
            smart_mode: True = RAM only (recommended), False = write to flash (persistent)

        Returns:
            True on success, False on error
        """
        pass


# ==================== Battery Implementation ====================


class SolarFlowBattery(ISolarFlowClient):
    """Pure battery abstraction – cache, validation, SOC guards, data layer.

    Provides:
    - Cache management
    - Validation logic
    - SOC-based protection guards (capping)
    - Energy accumulation (dead-reckoning)
    - High-level getters and setters
    - Bypass management

    Subclasses implement the HAL:
    - _fetch_response() – pure HW access
    - _set_properties() – HW write access

    Mode management (start_discharge/start_charge/stop) is in
    BatteryManager which inherits from this class.
    """

    # Pydantic TypeAdapter for JSON parsing (used by HAL)
    _decoder = TypeAdapter(APIResponse)

    def __init__(self, device_ip: str, *, cache_ttl: float = 1.0):
        """Initialise battery base components.

        Args:
            device_ip: IP address of the SolarFlow device
            cache_ttl: cache time-to-live in seconds
        """
        self.device_ip = device_ip
        self._port = 80
        self._base_url = f"http://{device_ip}:{self._port}"

        # Cache
        self.cache_ttl: float = cache_ttl
        self._response_cache: Optional[APIResponseProtocol] = None
        self._cache_timestamp: float = 0

        # Serial Number (read on first request)
        self.model: Optional[BatteryModel] = None
        self._sn: Optional[str] = None
        self._limits: BatteryLimits = BatteryLimits(
            charge_limit=0, discharge_limit=0, solar_limit=0
        )

        # Dead-reckoning energy counters
        self._setpoint_w: int = 0
        self._current_mode: ACMode = ACMode.OUTPUT
        self._setpoint_timestamp: float = time()
        self._accumulated_discharge_wh: float = 0.0
        self._accumulated_charge_wh: float = 0.0

        # SOC-based battery protection limits
        self.low_soc_threshold_pct: int = 25
        self.low_soc_output_limit_w: Optional[int] = None
        self.high_soc_threshold_pct: int = 90
        self.high_soc_input_limit_w: Optional[int] = None
        self._cached_soc: Optional[int] = None

    # ==================== Cache Management ====================

    def _is_cache_valid(self) -> bool:
        """Return True if the cache is still valid."""
        return (time() - self._cache_timestamp) < self.cache_ttl

    def _update_cache(self, data: APIResponseProtocol) -> None:
        """Update cache, serial number, model-specific limits, and cached SOC."""
        self._response_cache = data
        self._cache_timestamp = time()
        self._cached_soc = data.properties.electric_level
        if self._sn is None:
            self._sn = data.sn
        if self.model is None and data.product:
            if data.product in MODEL_LIMITS:
                self.model = cast(BatteryModel, data.product)

        if self.model is not None:
            limits = MODEL_LIMITS[self.model]
            self._limits = BatteryLimits(
                charge_limit=min(
                    limits.charge_limit,
                    data.properties.charge_max_limit or limits.charge_limit,
                ),
                discharge_limit=min(
                    limits.discharge_limit,
                    data.properties.inverse_max_power or limits.discharge_limit,
                ),
                solar_limit=limits.solar_limit,
                min_power=limits.min_power,
            )

    def _invalidate_cache(self) -> None:
        """Invalidate the cache."""
        self._cache_timestamp = 0

    async def _get_full_response(self, use_cache: bool = True) -> Optional[APIResponseProtocol]:
        """Fetch the complete API response (with cache handling).

        Args:
            use_cache: True = use cache when valid

        Returns:
            APIResponseProtocol or None on error
        """
        if use_cache and self._is_cache_valid():
            return self._response_cache

        try:
            response = await self._fetch_response()
            if response:
                self._update_cache(response)
                return response
            return None
        except Exception as e:
            logger.error("Unexpected error in _get_full_response: %s", e)
            return None

    # ==================== Validation ====================

    def validate_output_limit(self, power_w: int) -> int:
        """Validate and clamp the output limit."""
        return max(0, min(self._limits.discharge_limit, int(power_w)))

    def validate_input_limit(self, power_w: int) -> int:
        """Validate and clamp the input limit."""
        return max(0, min(self._limits.charge_limit, int(power_w)))

    def validate_min_soc(self, soc_percent: int) -> int:
        """Validate min SoC (0-50%) and convert to API value (0-500)."""
        if not 0 <= soc_percent <= 50:
            raise ValueError(f"Invalid min SOC: {soc_percent} (must be 0-50%)")
        return soc_percent * 10

    def validate_max_soc(self, soc_percent: int) -> int:
        """Validate max SoC (70-100%) and convert to API value (700-1000)."""
        if not 70 <= soc_percent <= 100:
            raise ValueError(f"Invalid max SOC: {soc_percent} (must be 70-100%)")
        return soc_percent * 10

    def validate_ac_mode(self, mode: ACMode) -> ACMode:
        """Validate AC mode (INPUT or OUTPUT)."""
        if mode not in [ACMode.INPUT, ACMode.OUTPUT]:
            raise ValueError(f"Invalid AC mode: {mode} (must be INPUT or OUTPUT)")
        return mode

    # ==================== SOC Protection ====================

    def _soc_discharge_cap(self) -> Optional[int]:
        """Return the effective SOC-based output cap (W), or None when not active.

        Active when cached SOC < ``low_soc_threshold_pct``.  Falls back to half
        of the model discharge limit when ``low_soc_output_limit_w`` is None.
        """
        if self._cached_soc is None or self._cached_soc >= self.low_soc_threshold_pct:
            return None
        if self.low_soc_output_limit_w is not None:
            return self.low_soc_output_limit_w
        if self._limits.discharge_limit > 0:
            return self._limits.discharge_limit // 2
        return None

    def _soc_charge_cap(self) -> Optional[int]:
        """Return the effective SOC-based input cap (W), or None when not active.

        Active when cached SOC > ``high_soc_threshold_pct``.  Falls back to half
        of the model charge limit when ``high_soc_input_limit_w`` is None.
        """
        if self._cached_soc is None or self._cached_soc <= self.high_soc_threshold_pct:
            return None
        if self.high_soc_input_limit_w is not None:
            return self.high_soc_input_limit_w
        if self._limits.charge_limit > 0:
            return self._limits.charge_limit // 2
        return None

    # ==================== Properties ====================

    @property
    def serial_number(self) -> Optional[str]:
        """Return the serial number (available after the first request)."""
        return self._sn

    @property
    def max_charge_power(self) -> int:
        """Maximum charge power for this model."""
        return self._limits.charge_limit

    @property
    def max_discharge_power(self) -> int:
        """Maximum discharge power for this model."""
        return self._limits.discharge_limit

    @property
    def max_solar_power(self) -> int:
        """Maximum solar input power for this model."""
        return self._limits.solar_limit

    @property
    def min_power(self) -> int:
        """Minimum output power for this model."""
        return self._limits.min_power

    # ==================== Helpers ====================

    def _prepare_properties_payload(self, properties: Dict, smart_mode: bool = True) -> Dict:
        """Prepare the properties payload for a write request."""
        if "smartMode" not in properties:
            properties["smartMode"] = 1 if smart_mode else 0
        return {"sn": self._sn, "properties": properties}

    # ==================== Energy Accumulator ====================

    def _flush_energy_to_now(self) -> None:
        """Accumulate energy for the period since the last setpoint change.

        Must be called synchronously BEFORE every setpoint change so that
        the time elapsed under the old setpoint is correctly accounted for.
        """
        now = time()
        dt_h = (now - self._setpoint_timestamp) / 3600.0
        if self._setpoint_w > 0:
            if self._current_mode == ACMode.OUTPUT:
                self._accumulated_discharge_wh += self._setpoint_w * dt_h
            else:
                self._accumulated_charge_wh += self._setpoint_w * dt_h
        self._setpoint_timestamp = now

    def get_energy_counters(self) -> InverterEnergyCounters:
        """Return the current monotonically increasing energy counters.

        Flushes accumulated energy since the last setpoint change first.

        Returns:
            InverterEnergyCounters with discharge_wh and charge_wh
        """
        self._flush_energy_to_now()
        return InverterEnergyCounters(
            discharge_wh=self._accumulated_discharge_wh,
            charge_wh=self._accumulated_charge_wh,
        )

    # ==================== Getters ====================

    async def get_state(self, *, use_cache: bool = True) -> Optional[DeviceState]:
        """Fetch the complete device state (processed).

        Returns:
            DeviceState with all processed properties, or None on error
        """
        response = await self._get_full_response(use_cache)
        if not response:
            return None
        return DeviceState.from_response(response)

    async def get_battery_packs(self, *, use_cache: bool = True) -> List[ProcessedBatteryPack]:
        """Fetch battery pack data (processed).

        Returns:
            List of ProcessedBatteryPack objects
        """
        response = await self._get_full_response(use_cache)
        if not response or not response.pack_data:
            return []
        return [ProcessedBatteryPack.from_protocol(pack) for pack in response.pack_data]

    async def get_solar_input_power(self, *, use_cache: bool = True) -> Optional[int]:
        """Fetch solar input power (W)."""
        response = await self._get_full_response(use_cache)
        return response.properties.solar_input_power if response else None

    async def get_ac_output_power(self, *, use_cache: bool = True) -> Optional[int]:
        """Fetch current AC output power (W)."""
        response = await self._get_full_response(use_cache)
        return response.properties.output_home_power if response else None

    async def get_ac_output_limit(self, *, use_cache: bool = True) -> Optional[int]:
        """Fetch output limit (W)."""
        response = await self._get_full_response(use_cache)
        return response.properties.output_limit if response else None

    async def get_ac_input_limit(self, *, use_cache: bool = True) -> Optional[int]:
        """Fetch input limit (W)."""
        response = await self._get_full_response(use_cache)
        return response.properties.input_limit if response else None

    async def get_ac_mode(self, *, use_cache: bool = True) -> Optional[ACMode]:
        """Fetch AC mode."""
        response = await self._get_full_response(use_cache)
        if not response:
            return None
        try:
            return ACMode(response.properties.ac_mode)
        except ValueError:
            return ACMode.OUTPUT

    async def get_min_soc(self, *, use_cache: bool = True) -> Optional[int]:
        """Fetch minimum SoC (0-50%)."""
        response = await self._get_full_response(use_cache)
        return response.properties.min_soc // 10 if response else None

    async def get_max_soc(self, *, use_cache: bool = True) -> Optional[int]:
        """Fetch maximum SoC (70-100%)."""
        response = await self._get_full_response(use_cache)
        return response.properties.soc_set // 10 if response else None

    async def get_battery_soc(self, *, use_cache: bool = True) -> Optional[int]:
        """Fetch current battery SoC (0-100%)."""
        response = await self._get_full_response(use_cache)
        return response.properties.electric_level if response else None

    async def get_temperature_celsius(self, *, use_cache: bool = True) -> Optional[float]:
        """Fetch enclosure temperature (°C)."""
        response = await self._get_full_response(use_cache)
        if not response or response.properties.hyper_tmp == 0:
            return None
        return (response.properties.hyper_tmp - 2731) / 10.0

    # ==================== Setters ====================

    async def set_ac_output_limit(self, power_w: int, *, smart_mode: bool = True) -> int:
        """Set the AC output power limit.

        The requested power is first clamped to the device discharge limit, then
        further capped by the SOC-based low-battery protection when active.

        Returns:
            Applied output setpoint in W (after all clamping),
            or ``-1`` on hardware/write error.
        """
        power_w = self.validate_output_limit(power_w)
        soc_cap = self._soc_discharge_cap()
        if soc_cap is not None:
            capped = min(power_w, soc_cap)
            if capped != power_w:
                logger.debug(
                    "set_ac_output_limit: SOC %d%% < %d%% → capped %dW → %dW",
                    self._cached_soc,
                    self.low_soc_threshold_pct,
                    power_w,
                    capped,
                )
            power_w = capped
        self._flush_energy_to_now()
        ok = await self._set_properties({"outputLimit": power_w}, smart_mode)
        if ok:
            self._current_mode = ACMode.OUTPUT
            self._setpoint_w = power_w
            return power_w
        return -1

    async def set_ac_input_limit(self, power_w: int, *, smart_mode: bool = True) -> int:
        """Set the AC input power limit.

        The requested power is first clamped to the device charge limit, then
        further capped by the SOC-based high-charge protection when active.

        Returns:
            Applied input setpoint in W (after all clamping),
            or ``-1`` on hardware/write error.
        """
        power_w = self.validate_input_limit(power_w)
        soc_cap = self._soc_charge_cap()
        if soc_cap is not None:
            capped = min(power_w, soc_cap)
            if capped != power_w:
                logger.debug(
                    "set_ac_input_limit: SOC %d%% > %d%% → capped %dW → %dW",
                    self._cached_soc,
                    self.high_soc_threshold_pct,
                    power_w,
                    capped,
                )
            power_w = capped
        self._flush_energy_to_now()
        ok = await self._set_properties({"inputLimit": power_w}, smart_mode)
        if ok:
            self._current_mode = ACMode.INPUT
            self._setpoint_w = power_w
            return power_w
        return -1

    async def set_ac_mode(self, mode: ACMode, *, smart_mode: bool = True) -> bool:
        """Set the AC mode (low-level).

        Args:
            mode: ACMode.INPUT (charge) or ACMode.OUTPUT (discharge)
            smart_mode: True = RAM only, False = write to flash
        """
        mode = self.validate_ac_mode(mode)
        return await self._set_properties({"acMode": mode.value}, smart_mode)

    async def set_min_soc(self, soc_percent: int, *, smart_mode: bool = True) -> bool:
        """Set the minimum SoC.

        Args:
            soc_percent: SoC in percent (0-50%)
            smart_mode: True = RAM only, False = write to flash
        """
        validated = self.validate_min_soc(soc_percent)
        return await self._set_properties({"minSoc": validated}, smart_mode)

    async def set_max_soc(self, soc_percent: int, *, smart_mode: bool = True) -> bool:
        """Set the maximum SoC.

        Args:
            soc_percent: SoC in percent (70-100%)
            smart_mode: True = RAM only, False = write to flash
        """
        validated = self.validate_max_soc(soc_percent)
        return await self._set_properties({"socSet": validated}, smart_mode)

    # ==================== Bypass ====================

    async def get_bypass_state(self, *, use_cache: bool = True) -> Optional[bool]:
        """Fetch bypass state.

        Returns:
            True = bypass active, False = no bypass, None = error
        """
        response = await self._get_full_response(use_cache)
        if response is None:
            return None
        return bool(response.properties.bypass)

    async def disable_bypass(self, *, smart_mode: bool = True) -> bool:
        """Disable bypass (passMode=1 = always off).

        Returns:
            True on success
        """
        logger.info("disable_bypass: setting passMode=1 (always off)…")
        success = await self._set_properties({"passMode": 1}, smart_mode)
        if success:
            self._invalidate_cache()
            logger.info("disable_bypass: command sent")
        else:
            logger.warning("disable_bypass: command not confirmed (HTTP error?)")
        return success

    async def get_battery_discharge_power(self, *, use_cache: bool = True) -> Optional[int]:
        """Fetch the actual battery discharge power (W).

        Returns packInputPower – the power actually supplied by the battery packs,
        regardless of bypass state.
        """
        response = await self._get_full_response(use_cache)
        return response.properties.pack_input_power if response else None

    # ==================== Utility ====================

    async def get_usable_energy_wh(
        self, battery_capacity_wh: int, *, use_cache: bool = True
    ) -> Optional[float]:
        """Compute usable energy in the battery.

        Takes min_soc into account, which must not be undercut.

        Returns:
            Usable energy in Wh, or None on error
        """
        state = await self.get_state(use_cache=use_cache)
        if not state:
            return None

        current_soc_fraction = state.battery_soc / 100.0
        min_soc_fraction = state.min_soc / 100.0

        usable_soc = max(0, current_soc_fraction - min_soc_fraction)
        return battery_capacity_wh * usable_soc
