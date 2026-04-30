"""SolarFlow base classes – interface and shared implementation.

==================================================================

This module defines:
- ISolarFlowClient: abstract HAL interface (2 methods)
- SolarFlowBase: shared high-level implementation
- Pydantic models for processed / converted data

The base class works with abstract protocols (models.py) and converts
raw API data into type-safe Pydantic models with computed fields.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from time import time
from typing import Dict, List, Optional, cast

from pydantic import BaseModel, Field, TypeAdapter, computed_field

from .models import (
    MODEL_LIMITS,
    ACMode,
    APIResponse,
    APIResponseProtocol,
    BatteryLimits,
    BatteryModel,
    BatteryPackProtocol,
    BatteryState,
)

logger = logging.getLogger(__name__)


# ==================== Energy Counters ====================


@dataclass
class InverterEnergyCounters:
    """Monotonically increasing energy counters (dead-reckoning from setpoints).

    Values are computed by integrating the active power setpoints over time.
    They start at 0 on program start and only count upward.

    Formula:
        discharge_wh += output_setpoint_W × Δt_h
        charge_wh    += input_setpoint_W  × Δt_h

    Usage at a higher level (HEMS):
        delta_discharge = counters.discharge_wh - prev_discharge_wh
        delta_charge    = counters.charge_wh    - prev_charge_wh
        real_load_delta = grid_delta + delta_discharge - delta_charge
    """

    discharge_wh: float  # cumulative energy delivered to the house [Wh]
    charge_wh: float  # cumulative energy drawn from the grid (AC charge) [Wh]


# ==================== Processed Pydantic Models ====================
# These models contain processed/converted data with computed fields.


class ProcessedBatteryPack(BaseModel):
    """Processed battery-pack data with converted values.

    Conversions per Zendure zenSDK documentation:
    - maxTemp: Kelvin*10 → Celsius
    - totalVol: centivolts → Volt
    - batcur: 16-bit two's complement / 10 → Ampere
    - maxVol/minVol: centivolts → Volt
    """

    # ===== Raw Fields (direkt von API) =====
    serial_number: str = Field(description="Battery pack serial number")
    pack_type: int = Field(default=0, description="Battery pack model type identifier")
    soc_percent: int = Field(default=0, ge=0, le=100, description="Battery level 0-100 (%)")
    state: int = Field(default=0, description="0: Stopped, 1: Running, 2: Standby, 3: Shutdown")
    power_w: int = Field(default=0, description="Battery power in W")
    software_version: int = Field(default=0, description="Software version")

    # ===== Converted fields =====
    temperature_celsius: float = Field(default=0.0, description="Temperature in °C")
    voltage_v: float = Field(default=0.0, description="Total voltage in V")
    current_a: float = Field(default=0.0, description="Current in A (signed)")
    max_cell_voltage_v: float = Field(default=0.0, description="Max cell voltage in V")
    min_cell_voltage_v: float = Field(default=0.0, description="Min cell voltage in V")

    # ===== Computed Fields =====
    @computed_field
    @property
    def battery_state(self) -> BatteryState:
        """Battery state as enum."""
        try:
            return BatteryState(self.state)
        except ValueError:
            return BatteryState.STANDBY

    @computed_field
    @property
    def is_healthy(self) -> bool:
        """Returns True if battery metrics are within healthy limits."""
        if self.min_cell_voltage_v == 0:
            return True  # Keine Daten = assume healthy
        return (
            -10 < self.temperature_celsius < 45
            and 0.95 < self.max_cell_voltage_v / max(0.01, self.min_cell_voltage_v) < 1.05
        )

    @classmethod
    def from_protocol(cls, pack: BatteryPackProtocol) -> "ProcessedBatteryPack":
        """Build a ProcessedBatteryPack from raw protocol data."""
        # Temperature: Kelvin*10 → Celsius
        temp_celsius = (pack.max_temp - 2731) / 10.0 if pack.max_temp else 0.0

        # Voltage: totalVol ist in centivolts (0.01V)
        voltage_v = pack.total_vol / 100.0 if pack.total_vol else 0.0

        # Current: 16-bit two's complement / 10 = Ampere
        batcur = pack.batcur
        if batcur > 32767:
            signed_current = batcur - 65536
        else:
            signed_current = batcur
        current_a = signed_current / 10.0

        # Cell Voltages: centivolts → Volt
        max_cell_v = pack.max_vol / 100.0 if pack.max_vol else 0.0
        min_cell_v = pack.min_vol / 100.0 if pack.min_vol else 0.0

        return cls(
            serial_number=pack.sn,
            pack_type=pack.pack_type,
            soc_percent=pack.soc_level,
            state=pack.state,
            power_w=pack.power,
            software_version=pack.soft_version,
            temperature_celsius=temp_celsius,
            voltage_v=voltage_v,
            current_a=current_a,
            max_cell_voltage_v=max_cell_v,
            min_cell_voltage_v=min_cell_v,
        )


class DeviceState(BaseModel):
    """Processed device state with all relevant properties.

    Converts raw API data to usable values:
    - hyperTmp: Kelvin*10 → Celsius
    - minSoc: 0-500 → 0-50%
    - socSet: 700-1000 → 70-100%
    """

    # ===== Identifikation =====
    serial_number: str = Field(default="", description="Device serial number")
    product: str = Field(default="", description="Product model name")

    # ===== Power Values (W) =====
    solar_input_power: int = Field(default=0, description="Total solar input power in W")
    solar_power_1: int = Field(default=0, description="Solar line 1 input power in W")
    solar_power_2: int = Field(default=0, description="Solar line 2 input power in W")
    solar_power_3: int = Field(default=0, description="Solar line 3 input power in W")
    solar_power_4: int = Field(default=0, description="Solar line 4 input power in W")
    grid_input_power: int = Field(default=0, description="Grid input power in W")
    output_home_power: int = Field(default=0, description="Output power to home in W")
    output_pack_power: int = Field(default=0, description="Output power to battery pack in W")
    pack_input_power: int = Field(default=0, description="Battery pack input power in W")

    # ===== Battery Status =====
    battery_soc: int = Field(default=0, ge=0, le=100, description="Average battery SOC in %")
    battery_state: BatteryState = Field(default=BatteryState.STANDBY, description="Battery state")
    pack_count: int = Field(default=0, description="Number of battery packs")
    battery_packs: List[ProcessedBatteryPack] = Field(default_factory=list)

    # ===== Limits (converted to %) =====
    min_soc: int = Field(default=0, ge=0, le=50, description="Min SOC in % (0-50)")
    max_soc: int = Field(default=100, ge=70, le=100, description="Max SOC in % (70-100)")
    input_limit: int = Field(default=0, description="AC input limit in W")
    output_limit: int = Field(default=0, description="AC output limit in W")
    inverse_max_power: int = Field(default=0, description="Maximum output power in W")

    # ===== Mode & Config =====
    ac_mode: ACMode = Field(default=ACMode.OUTPUT, description="AC mode")
    smart_mode: bool = Field(default=True, description="Smart mode active")
    bypass_mode: bool = Field(default=False, description="Bypass mode active")
    grid_connected: bool = Field(default=True, description="Grid connected")
    heating_active: bool = Field(default=False, description="Heating active")

    # ===== Temperature (converted) =====
    temperature_celsius: Optional[float] = Field(default=None, description="Enclosure temp in °C")

    # ===== Status Flags =====
    data_ready: bool = Field(default=True)
    is_error: bool = Field(default=False)
    remain_out_time_min: int = Field(default=0, description="Remaining discharge time in min")

    @classmethod
    def from_response(cls, response: APIResponseProtocol) -> "DeviceState":
        """Build a DeviceState from an API response protocol object."""
        props = response.properties

        # Temperature: Kelvin*10 → Celsius
        temp_celsius = None
        if props.hyper_tmp > 0:
            temp_celsius = (props.hyper_tmp - 2731) / 10.0

        # SOC Limits: minSoc 0-500 → 0-50%, socSet 700-1000 → 70-100%
        min_soc_percent = props.min_soc // 10
        max_soc_percent = props.soc_set // 10

        # Process battery packs
        processed_packs = [ProcessedBatteryPack.from_protocol(pack) for pack in response.pack_data]

        return cls(
            serial_number=response.sn,
            product=response.product,
            solar_input_power=props.solar_input_power,
            solar_power_1=props.solar_power_1,
            solar_power_2=props.solar_power_2,
            solar_power_3=props.solar_power_3,
            solar_power_4=props.solar_power_4,
            grid_input_power=props.grid_input_power,
            output_home_power=props.output_home_power,
            output_pack_power=props.output_pack_power,
            pack_input_power=props.pack_input_power,
            battery_soc=props.electric_level,
            battery_state=BatteryState(props.pack_state)
            if props.pack_state in [0, 1, 2]
            else BatteryState.STANDBY,
            pack_count=props.pack_num or len(processed_packs),
            battery_packs=processed_packs,
            min_soc=min_soc_percent,
            max_soc=max_soc_percent,
            input_limit=props.input_limit,
            output_limit=props.output_limit,
            inverse_max_power=props.inverse_max_power,
            ac_mode=ACMode(props.ac_mode) if props.ac_mode in [1, 2] else ACMode.OUTPUT,
            smart_mode=bool(props.smart_mode),
            bypass_mode=bool(props.bypass),
            grid_connected=bool(props.grid_state),
            heating_active=bool(props.heat_state),
            temperature_celsius=temp_celsius,
            data_ready=bool(props.data_ready),
            is_error=bool(props.is_error),
            remain_out_time_min=props.remain_out_time,
        )


# ==================== Abstract Interface ====================


class ISolarFlowClient(ABC):
    """Abstract base class – minimal interface for SolarFlow clients (HAL).

    Clients only need to implement two low-level hardware methods:
    1. _fetch_response() – pure HW access without cache
    2. _set_properties() – write data to device

    Cache handling and all high-level methods are in SolarFlowBase.
    """

    @abstractmethod
    async def _fetch_response(self) -> Optional[APIResponseProtocol]:
        """Pure hardware access: fetch a response from the device (no cache).

        Hardware Abstraction Layer (HAL) – implemented by:
        - SolarFlowAsyncClient: HTTP GET request
        - SolarFlowAsyncMockClient: generate mock data

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


# ==================== Base Implementation ====================


class SolarFlowBase(ISolarFlowClient):
    """Base class with shared implementation for all SolarFlow clients.

    Provides:
    - Cache management
    - Validation logic
    - High-level API (snake_case methods)
    - Conversion raw → processed data

    Subclasses only implement:
    - _fetch_response() – pure HW access
    - _set_properties() – HW write access
    """

    # Pydantic TypeAdapter for JSON parsing (used by HAL)
    _decoder = TypeAdapter(APIResponse)

    def __init__(self, device_ip: str, *, cache_ttl: float = 1.0):
        """Initialise base components.

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

        # Serial Number (wird bei erstem Request gelesen)
        self.model: Optional[BatteryModel] = None
        self._sn: Optional[str] = None
        self._limits: BatteryLimits = BatteryLimits(
            charge_limit=0, discharge_limit=0, solar_limit=0
        )

        # Dead-reckoning energy counters.
        # A single signed setpoint: +W = discharging, -W = charging.
        # The battery can only go in one direction at a time – no simultaneous
        # charge + discharge.
        self._setpoint_w: int = 0  # current effective power setpoint [W]
        self._setpoint_timestamp: float = time()
        self._accumulated_discharge_wh: float = 0.0
        self._accumulated_charge_wh: float = 0.0

    # ==================== Cache Management ====================

    def _is_cache_valid(self) -> bool:
        """Return True if the cache is still valid."""
        return (time() - self._cache_timestamp) < self.cache_ttl

    def _update_cache(self, data: APIResponseProtocol) -> None:
        """Update cache, serial number, and model-specific limits."""
        self._response_cache = data
        self._cache_timestamp = time()
        if self._sn is None:
            self._sn = data.sn
        if self.model is None and data.product:
            if data.product in MODEL_LIMITS:
                self.model = cast(BatteryModel, data.product)
                limits = MODEL_LIMITS[self.model]
                self._limits = BatteryLimits(
                    charge_limit=min(
                        limits.charge_limit, data.properties.charge_max_limit or limits.charge_limit
                    ),
                    discharge_limit=min(
                        limits.discharge_limit,
                        data.properties.inverse_max_power or limits.discharge_limit,
                    ),
                    solar_limit=limits.solar_limit,
                    min_power=limits.min_power,
                )
            else:
                logger.warning("Unknown SolarFlow model from API: %s", data.product)

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
        # Cache hit?
        if use_cache and self._is_cache_valid():
            return self._response_cache

        # Hardware access via HAL
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

    # ==================== Helper ====================

    def _prepare_properties_payload(self, properties: Dict, smart_mode: bool = True) -> Dict:
        """Prepare the properties payload for a write request.

        Args:
            properties: dict of properties (camelCase keys!)
            smart_mode: True = RAM only, False = write to flash
        """
        if "smartMode" not in properties:
            properties["smartMode"] = 1 if smart_mode else 0
        return {"sn": self._sn, "properties": properties}

    # ==================== Energy Accumulator ====================

    def _flush_energy_to_now(self) -> None:
        """Accumulate energy for the period since the last setpoint change.

        Must be called synchronously BEFORE every setpoint change so that
        the time elapsed under the old setpoint is correctly accounted for.
        Positive setpoint → discharge; negative → charge.
        """
        now = time()
        dt_h = (now - self._setpoint_timestamp) / 3600.0
        if self._setpoint_w > 0:
            self._accumulated_discharge_wh += self._setpoint_w * dt_h
        elif self._setpoint_w < 0:
            self._accumulated_charge_wh += (-self._setpoint_w) * dt_h
        self._setpoint_timestamp = now

    def get_energy_counters(self) -> InverterEnergyCounters:
        """Return the current monotonically increasing energy counters.

        Flushes accumulated energy since the last setpoint change first,
        so the value is always up-to-date even without a setpoint change.

        Returns:
            InverterEnergyCounters with discharge_wh and charge_wh
        """
        self._flush_energy_to_now()
        return InverterEnergyCounters(
            discharge_wh=self._accumulated_discharge_wh,
            charge_wh=self._accumulated_charge_wh,
        )

    # ==================== High-Level API - Getters ====================

    async def get_state(self, *, use_cache: bool = True) -> Optional[DeviceState]:
        """Fetch the complete device state (processed).

        Args:
            use_cache: True = use cache when valid

        Returns:
            DeviceState with all processed properties, or None on error
        """
        response = await self._get_full_response(use_cache)
        if not response:
            return None
        return DeviceState.from_response(response)

    async def get_battery_packs(self, *, use_cache: bool = True) -> List[ProcessedBatteryPack]:
        """Fetch battery pack data (processed).

        Args:
            use_cache: True = use cache when valid

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

    # ==================== High-Level API - Setters ====================

    async def set_ac_output_limit(self, power_w: int, *, smart_mode: bool = True) -> bool:
        """Set the AC output power limit.

        Args:
            power_w: power in watts
            smart_mode: True = RAM only, False = write to flash
        """
        power_w = self.validate_output_limit(power_w)
        self._flush_energy_to_now()
        self._setpoint_w = power_w  # positive = discharging
        return await self._set_properties({"outputLimit": power_w}, smart_mode)

    async def set_ac_input_limit(self, power_w: int, *, smart_mode: bool = True) -> bool:
        """Set the AC input power limit.

        Args:
            power_w: power in watts
            smart_mode: True = RAM only, False = write to flash
        """
        power_w = self.validate_input_limit(power_w)
        self._flush_energy_to_now()
        self._setpoint_w = -power_w  # negative = charging
        return await self._set_properties({"inputLimit": power_w}, smart_mode)

    async def set_ac_mode(self, mode: ACMode, *, smart_mode: bool = True) -> bool:
        """Set the AC mode.

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

    # ==================== Bypass / Convenience ====================

    async def get_bypass_state(self, *, use_cache: bool = True) -> Optional[bool]:
        """Fetch bypass state.

        When bypass is active, the SF800Pro routes PV energy directly to the house
        and ignores outputLimit commands.  output_home_power then reflects the
        solar bypass value, not the battery output.

        Returns:
            True = bypass active, False = no bypass, None = error
        """
        response = await self._get_full_response(use_cache)
        if response is None:
            return None
        return bool(response.properties.bypass)

    async def disable_bypass(self, *, smart_mode: bool = True) -> bool:
        """Disable bypass (passMode=1 = always off).

        Useful when the inverter is in bypass mode and therefore
        ignores discharge commands.

        Args:
            smart_mode: True = RAM only, False = write to flash

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
        In bypass mode this value is 0 while output_home_power shows the
        solar bypass power.
        """
        response = await self._get_full_response(use_cache)
        return response.properties.pack_input_power if response else None

    # ==================== Convenience Methods ====================

    async def is_settled(self, *, use_cache: bool = True) -> Optional[bool]:
        """Check whether the inverter is settled at the current setpoint (output ≈ setpoint).

        In bypass mode output_home_power reflects the solar bypass power, not the
        battery output – returns None when bypass is active.

        Args:
            use_cache: True = use cache

        Returns:
            True if settled, False if not, None on error or when bypass is active
        """
        state = await self.get_state(use_cache=use_cache)
        if not state:
            return None

        if state.bypass_mode:
            logger.debug(
                "is_settled: bypass active (solar=%d W) – setpoint comparison not meaningful",
                state.solar_input_power,
            )
            return None

        current_output = state.output_home_power
        settled = abs(abs(current_output) - abs(self._setpoint_w)) < 2
        logger.debug(
            "is_settled: output=%dW setpoint=%dW diff=%dW → %s",
            current_output,
            self._setpoint_w,
            abs(current_output - self._setpoint_w),
            settled,
        )
        return settled

    async def start_discharge(self, power_w: int, *, smart_mode: bool = True) -> bool:
        """Start discharging.

        Args:
            power_w: discharge power in watts
            smart_mode: True = RAM only, False = write to flash
        """
        if self._limits.discharge_limit == 0:
            logger.debug("start_discharge: limits not yet known – reading device state…")
            await self.get_state(use_cache=False)  # Force cache update to get limits
        clamped = (
            min(power_w, self._limits.discharge_limit)
            if self._limits.discharge_limit > 0
            else power_w
        )
        if clamped != power_w:
            logger.info(
                "start_discharge: %d W → clipped to %d W (discharge limit: %d W)",
                power_w,
                clamped,
                self._limits.discharge_limit,
            )
        else:
            logger.info("start_discharge: %d W (acMode=OUTPUT, inputLimit=0)", clamped)
        self._flush_energy_to_now()
        self._setpoint_w = clamped  # positive = discharging
        success = await self._set_properties(
            {
                "acMode": ACMode.OUTPUT.value,
                "outputLimit": clamped,
                "inputLimit": 0,
            },
            smart_mode,
        )
        if not success:
            logger.warning("start_discharge: setpoint command not confirmed (HTTP error?)")
        return success

    async def start_charge(self, power_w: int, *, smart_mode: bool = True) -> bool:
        """Start AC charging.

        Args:
            power_w: charge power in watts
            smart_mode: True = RAM only, False = write to flash
        """
        clamped = (
            min(power_w, self._limits.charge_limit) if self._limits.charge_limit > 0 else power_w
        )
        if clamped != power_w:
            logger.info(
                "start_charge: %d W → clipped to %d W (charge limit: %d W)",
                power_w,
                clamped,
                self._limits.charge_limit,
            )
        else:
            logger.info("start_charge: %d W (acMode=INPUT, outputLimit=0)", clamped)
        self._flush_energy_to_now()
        self._setpoint_w = -clamped  # negative = charging
        success = await self._set_properties(
            {
                "acMode": ACMode.INPUT.value,
                "inputLimit": clamped,
                "outputLimit": 0,
            },
            smart_mode,
        )
        if not success:
            logger.warning("start_charge: setpoint command not confirmed")
        return success

    async def stop(self, *, smart_mode: bool = True) -> bool:
        """Stop all activity.

        Args:
            smart_mode: True = RAM only, False = write to flash
        """
        logger.info("stop: acMode=OUTPUT outputLimit=0 inputLimit=0")
        self._flush_energy_to_now()
        self._setpoint_w = 0
        success = await self._set_properties(
            {
                "acMode": ACMode.OUTPUT.value,
                "outputLimit": 0,
                "inputLimit": 0,
            },
            smart_mode,
        )
        if not success:
            logger.warning("stop: command not confirmed")
        return success

    async def get_usable_energy_wh(
        self, battery_capacity_wh: int, *, use_cache: bool = True
    ) -> Optional[float]:
        """Compute usable energy in the battery.

        Takes min_soc into account, which must not be undercut.

        Args:
            battery_capacity_wh: total battery capacity in Wh
            use_cache: True = use cache

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
