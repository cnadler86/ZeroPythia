"""Zendure SolarFlow models.

==========================

This module defines:
- Abstract protocols for HAL-independent API responses
- Enums (ACMode, BatteryState)
- Limits (BatteryLimits, MODEL_LIMITS)
- Pydantic models for parsing (Properties, BatteryPack, APIResponse)

HAL implementations (aiozen, mock) can use any parser.
The base class works with the abstract protocols.

Reference: Zendure zenSDK documentation
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, List, Literal, Optional, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, computed_field

# ==================== Enums ====================


class BatteryState(IntEnum):
    """Battery states."""

    STANDBY = 0
    CHARGING = 1
    DISCHARGING = 2


class ACMode(IntEnum):
    """AC mode for SolarFlow."""

    INPUT = 1  # charge from grid
    OUTPUT = 2  # feed into grid / discharge to house


# ==================== Limits ====================


class BatteryLimits(BaseModel):
    """Model-specific battery limits."""

    charge_limit: int
    discharge_limit: int
    solar_limit: int
    min_power: int = 0


# Supported battery models
BatteryModel = Literal[
    "solarFlow800Pro", "solarFlow800Plus", "SF2400AC", "Hub1200", "Hub2000", "Hyper2000", "AIO2400"
]

# Model-specific limits
MODEL_LIMITS: Dict[BatteryModel, BatteryLimits] = {
    "solarFlow800Pro": BatteryLimits(
        charge_limit=1000, discharge_limit=800, solar_limit=1200, min_power=20
    ),
    "solarFlow800Plus": BatteryLimits(
        charge_limit=1000, discharge_limit=800, solar_limit=1200, min_power=20
    ),
}


# ==================== Abstract Protocols ====================


@runtime_checkable
class BatteryPackProtocol(Protocol):
    """Protocol für Battery Pack Daten (Raw API).

    Beschreibt die erwartete Struktur der Pack-Daten aus der API.
    Kann von Pydantic, Msgspec oder anderen Parsern implementiert werden.
    """

    @property
    def sn(self) -> str: ...
    @property
    def pack_type(self) -> int: ...
    @property
    def soc_level(self) -> int: ...
    @property
    def state(self) -> int: ...
    @property
    def power(self) -> int: ...
    @property
    def max_temp(self) -> int: ...
    @property
    def total_vol(self) -> int: ...
    @property
    def batcur(self) -> int: ...
    @property
    def max_vol(self) -> int: ...
    @property
    def min_vol(self) -> int: ...
    @property
    def soft_version(self) -> int: ...


@runtime_checkable
class PropertiesProtocol(Protocol):
    """Protocol für Device Properties (Raw API).

    Beschreibt die erwartete Struktur der Properties aus der API.
    Kann von Pydantic, Msgspec oder anderen Parsern implementiert werden.
    """

    # Power Values (W) - Read-Only
    @property
    def heat_state(self) -> int: ...
    @property
    def pack_input_power(self) -> int: ...
    @property
    def output_pack_power(self) -> int: ...
    @property
    def output_home_power(self) -> int: ...
    @property
    def remain_out_time(self) -> int: ...

    # Battery Status - Read-Only
    @property
    def pack_state(self) -> int: ...
    @property
    def electric_level(self) -> int: ...
    @property
    def pack_num(self) -> int: ...

    # Grid & Solar - Read-Only
    @property
    def grid_input_power(self) -> int: ...
    @property
    def solar_input_power(self) -> int: ...
    @property
    def solar_power_1(self) -> int: ...
    @property
    def solar_power_2(self) -> int: ...
    @property
    def solar_power_3(self) -> int: ...
    @property
    def solar_power_4(self) -> int: ...

    # Bypass & Reverse - Read-Only
    @property
    def bypass(self) -> int: ...
    @property
    def reverse_state(self) -> int: ...

    # Status - Read-Only
    @property
    def soc_status(self) -> int: ...
    @property
    def hyper_tmp(self) -> int: ...
    @property
    def grid_off_power(self) -> int: ...
    @property
    def dc_status(self) -> int: ...
    @property
    def pv_status(self) -> int: ...
    @property
    def ac_status(self) -> int: ...
    @property
    def data_ready(self) -> int: ...
    @property
    def grid_state(self) -> int: ...

    # Voltage & Monitoring - Read-Only
    @property
    def bat_volt(self) -> int: ...
    @property
    def soc_limit(self) -> int: ...
    @property
    def fault_level(self) -> int: ...
    @property
    def write_rsp(self) -> int: ...

    # Configuration - Read/Write
    @property
    def ac_mode(self) -> int: ...
    @property
    def input_limit(self) -> int: ...
    @property
    def output_limit(self) -> int: ...
    @property
    def soc_set(self) -> int: ...
    @property
    def min_soc(self) -> int: ...
    @property
    def grid_standard(self) -> int: ...
    @property
    def grid_reverse(self) -> int: ...
    @property
    def inverse_max_power(self) -> int: ...
    @property
    def lamp_switch(self) -> int: ...
    @property
    def grid_off_mode(self) -> int: ...

    # Network & System
    @property
    def iot_state(self) -> int: ...
    @property
    def fan_mode(self) -> int: ...
    @property
    def fan_speed(self) -> int: ...
    @property
    def bind_state(self) -> int: ...
    @property
    def factory_mode_state(self) -> int: ...
    @property
    def ota_state(self) -> int: ...
    @property
    def lcn_state(self) -> int: ...
    @property
    def old_mode(self) -> int: ...
    @property
    def volt_wakeup(self) -> int: ...

    # Time & Smart Mode
    @property
    def ts(self) -> int: ...
    @property
    def ts_zone(self) -> int: ...
    @property
    def smart_mode(self) -> int: ...
    @property
    def charge_max_limit(self) -> int: ...
    @property
    def phase_switch(self) -> int: ...

    # Signal
    @property
    def rssi(self) -> int: ...
    @property
    def is_error(self) -> int: ...


@runtime_checkable
class APIResponseProtocol(Protocol):
    """Protocol für vollständige API Response.

    Beschreibt die erwartete Struktur der gesamten API Response.
    Kann von Pydantic, Msgspec oder anderen Parsern implementiert werden.
    """

    @property
    def timestamp(self) -> int: ...
    @property
    def message_id(self) -> int: ...
    @property
    def sn(self) -> str: ...
    @property
    def version(self) -> int: ...
    @property
    def product(self) -> str: ...
    @property
    def properties(self) -> PropertiesProtocol: ...
    @property
    def pack_data(self) -> Sequence[BatteryPackProtocol]: ...


# ==================== Abstract Response Parser ====================


class IResponseParser(ABC):
    """Abstract Base Class für Response Parser.

    HAL-Implementierungen können verschiedene Parser nutzen:
    - Pydantic (Standard, typsicher)
    - Msgspec (schnell)
    - OrJSON (schnell)
    """

    @abstractmethod
    def parse_response(self, data: bytes) -> APIResponseProtocol:
        """Parst rohe JSON-Bytes zu APIResponse."""
        pass


# ==================== Pydantic Config ====================
# Gemeinsame ConfigDict für alle Pydantic Models


def _camel_case_generator(field_name: str) -> str:
    """Konvertiert snake_case zu camelCase."""
    words = field_name.split("_")
    return words[0] + "".join(word.capitalize() for word in words[1:])


_PYDANTIC_CONFIG = ConfigDict(
    alias_generator=_camel_case_generator,
    populate_by_name=True,
    validate_by_name=True,
    validate_by_alias=True,
)


# ==================== Pydantic Models (implementieren Protocols) ====================


class Properties(BaseModel):
    """SolarFlow Device Properties - Pydantic Implementation.

    Implementiert PropertiesProtocol mit pydantic v2.
    Raw fields in camelCase werden automatisch zu snake_case gemappt.

    Reference: Zendure zenSDK Documentation
    """

    model_config = _PYDANTIC_CONFIG

    # ==================== Power Values (W) - Read-Only ====================

    heat_state: int = Field(default=0, description="0: Not heating, 1: Heating")
    pack_input_power: int = Field(
        default=0, description="Battery pack input power (discharging) in W"
    )
    output_pack_power: int = Field(
        default=0, description="Output power to battery pack (charging) in W"
    )
    output_home_power: int = Field(default=0, description="Output power to home electricity in W")
    remain_out_time: int = Field(default=0, description="Remaining discharge time in minutes")

    # ==================== Battery Status - Read-Only ====================

    pack_state: int = Field(default=0, description="0: Standby, 1: Charging, 2: Discharging")
    electric_level: int = Field(default=0, description="Average battery pack charge level in %")
    pack_num: int = Field(default=0, description="Number of battery packs")

    # ==================== Grid & Solar - Read-Only ====================

    grid_input_power: int = Field(default=0, description="Grid input power in W")
    solar_input_power: int = Field(default=0, description="Total solar input power in W")
    solar_power_1: int = Field(default=0, description="Solar line 1 input power in W")
    solar_power_2: int = Field(default=0, description="Solar line 2 input power in W")
    solar_power_3: int = Field(default=0, description="Solar line 3 input power in W")
    solar_power_4: int = Field(default=0, description="Solar line 4 input power in W")
    solar_power_5: Optional[int] = Field(default=None, description="Solar line 5 input power in W")
    solar_power_6: Optional[int] = Field(default=None, description="Solar line 6 input power in W")

    # ==================== Bypass & Reverse - Read-Only ====================

    bypass: int = Field(
        default=0, alias="pass", description="0: No, 1: Yes – bypass currently active"
    )
    reverse_state: int = Field(default=0, description="0: No, 1: Reverse flow")
    pass_mode: int = Field(
        default=0,
        alias="passMode",
        description="Bypass mode setting: 0=auto, 1=always off, 2=always on",
    )

    # ==================== Status - Read-Only ====================

    soc_status: int = Field(default=0, description="0: Normal, 1: Calibrating")
    hyper_tmp: int = Field(default=0, description="Enclosure temperature (Kelvin*10)")
    grid_off_power: int = Field(default=0, description="Grid-off power setting")
    dc_status: int = Field(default=0, description="0: Stopped, 1: Battery input, 2: Battery output")
    pv_status: int = Field(default=0, description="0: Stopped, 1: Running (solar)")
    ac_status: int = Field(
        default=0, description="0: Stopped, 1: Grid-connected operation, 2: Charging operation"
    )
    data_ready: int = Field(default=1, description="0: Not ready, 1: Ready")
    grid_state: int = Field(default=1, description="0: Not connected, 1: Connected")

    # ==================== Voltage & Monitoring - Read-Only ====================

    bat_volt: int = Field(
        default=0, description="Battery voltage (0.01V units; divide by 100 for V)"
    )
    soc_limit: int = Field(
        default=0, description="0: Normal, 1: Charge limit reached, 2: Discharge limit reached"
    )
    fault_level: int = Field(default=0, description="Fault level indicator")
    write_rsp: int = Field(default=0, description="Read/write response acknowledgment")

    # ==================== Configuration - Read/Write ====================

    ac_mode: int = Field(default=2, description="1: Input (charging), 2: Output (discharging)")
    input_limit: int = Field(default=0, description="AC charging power limit in W")
    output_limit: int = Field(default=0, description="Output power limit in W")
    soc_set: int = Field(default=1000, description="Max SoC: 700-1000 (70%-100%)")
    min_soc: int = Field(default=0, description="Min SoC: 0-500 (0%-50%)")
    grid_standard: int = Field(
        default=0, description="Grid standard: 0=Germany, 1=France, 2=Austria"
    )
    grid_reverse: int = Field(
        default=0, description="0: Disabled, 1: Allowed reverse flow, 2: Forbidden reverse flow"
    )
    inverse_max_power: int = Field(default=0, description="Maximum output power limit in W")
    lamp_switch: int = Field(default=0, description="Lamp switch control")
    grid_off_mode: int = Field(default=0, description="Grid-off mode setting")

    # ==================== Network & System ====================

    iot_state: int = Field(default=0, alias="IOTState", description="IoT connection state")
    fan_mode: int = Field(default=0, alias="Fanmode", description="Fan mode setting")
    fan_speed: int = Field(default=0, alias="Fanspeed", description="Fan speed value")
    bind_state: int = Field(default=0, description="Device binding state")
    factory_mode_state: int = Field(default=0, description="Factory mode state")
    ota_state: int = Field(default=0, alias="OTAState", description="OTA update state")
    lcn_state: int = Field(default=0, alias="LCNState", description="LCN state")
    old_mode: int = Field(default=0, description="Previous mode")
    volt_wakeup: int = Field(default=0, alias="VoltWakeup", description="Voltage wakeup setting")

    # ==================== Time & Smart Mode ====================

    ts: int = Field(default=0, description="Timestamp")
    ts_zone: int = Field(default=0, description="Timezone offset")
    smart_mode: int = Field(
        default=1,
        description="1: Settings not written to flash (volatile), 0: Written to flash (persistent)",
    )
    charge_max_limit: int = Field(default=0, description="Maximum charge limit")
    phase_switch: int = Field(default=0, description="Phase switch setting")

    # ==================== Signal ====================

    rssi: int = Field(default=0, description="WiFi signal strength (RSSI)")
    is_error: int = Field(default=0, description="Error flag: 0=No error, 1=Error")

    # ==================== Device-specific optional (not all models) ====================

    fan_switch: Optional[int] = Field(default=None, description="Fan state: 0=off, 1=on (RO)")
    ac_coupling_state: Optional[int] = Field(
        default=None,
        description="AC Coupling bitfield: Bit0=input present (auto-cleared), Bit1=AC input flag, Bit2=overload, Bit3=excess power",
    )
    dry_node_state: Optional[int] = Field(
        default=None,
        description="Dry contact status (1=Connected, 0=Disconnected; may be reversed per wiring)",
    )
    fm_volt: Optional[int] = Field(
        default=None, alias="FMVolt", description="Voltage activation value"
    )
    bat_cal_time: Optional[int] = Field(
        default=None, description="Battery calibration time in minutes (RW)"
    )


class BatteryPack(BaseModel):
    """Battery Pack Data - Pydantic Implementation.

    Implementiert BatteryPackProtocol mit pydantic v2.
    Raw fields in camelCase werden automatisch zu snake_case gemappt.

    Official Field Descriptions:
    - packType: Battery pack model type identifier
    - socLevel: Battery level 0-100 (represents percentage)
    - state: 0=Stopped, 1=Running, 2=Standby, 3=Shutdown
    - power: Battery power in W (positive=charging, negative=discharging)
    - maxTemp: Maximum cell temperature (Kelvin*10)
    - totalVol: Total battery voltage (centivolts, 0.01V)
    - batcur: Battery current (16-bit two's complement / 10 = A)
    - maxVol: Maximum cell voltage (centivolts, 0.01V)
    - minVol: Minimum cell voltage (centivolts, 0.01V)
    - softVersion: Software version

    Reference: Zendure zenSDK Documentation
    """

    model_config = _PYDANTIC_CONFIG

    # ==================== Raw Fields from API ====================

    sn: str = Field(description="Battery pack serial number")
    pack_type: int = Field(default=0, description="Battery pack model type identifier")
    soc_level: int = Field(default=0, description="Battery level 0-100 (%)")
    state: int = Field(default=0, description="0: Stopped, 1: Running, 2: Standby, 3: Shutdown")
    power: int = Field(
        default=0, description="Battery power in W (positive=charging, negative=discharging)"
    )
    max_temp: int = Field(default=0, description="Maximum cell temperature (Kelvin*10)")
    total_vol: int = Field(default=0, description="Total battery voltage (centivolts, 0.01V)")
    batcur: int = Field(default=0, description="Battery current (16-bit two's complement, /10 = A)")
    max_vol: int = Field(default=0, description="Maximum cell voltage (centivolts, 0.01V)")
    min_vol: int = Field(default=0, description="Minimum cell voltage (centivolts, 0.01V)")
    soft_version: int = Field(default=0, description="Software version")


class APIResponse(BaseModel):
    """Vollständige API Response - Pydantic Implementation.

    Implementiert APIResponseProtocol mit pydantic v2.
    """

    model_config = _PYDANTIC_CONFIG

    timestamp: int
    message_id: int = Field(default=0)
    sn: str = ""
    version: int = 0
    product: str = ""
    properties: Properties = Field(default_factory=Properties)
    pack_data: List[BatteryPack] = Field(default_factory=list)


# ==================== Processed Data Models ====================
# These models contain processed/converted data with computed fields.
# Used by the battery abstraction layer and higher-level consumers.


@dataclass
class InverterEnergyCounters:
    """Monotonically increasing energy counters (dead-reckoning from setpoints).

    Values are computed by integrating the active power setpoints over time.
    They start at 0 on program start and only count upward.

    Formula:
        discharge_wh += output_setpoint_W × Δt_h
        charge_wh    += input_setpoint_W  × Δt_h
    """

    discharge_wh: float  # cumulative energy delivered to the house [Wh]
    charge_wh: float  # cumulative energy drawn from the grid (AC charge) [Wh]


class ProcessedBatteryPack(BaseModel):
    """Processed battery-pack data with converted values.

    Conversions per Zendure zenSDK documentation:
    - maxTemp: Kelvin*10 → Celsius
    - totalVol: centivolts → Volt
    - batcur: 16-bit two's complement / 10 → Ampere
    - maxVol/minVol: centivolts → Volt
    """

    serial_number: str = Field(description="Battery pack serial number")
    pack_type: int = Field(default=0, description="Battery pack model type identifier")
    soc_percent: int = Field(default=0, ge=0, le=100, description="Battery level 0-100 (%)")
    state: int = Field(default=0, description="0: Stopped, 1: Running, 2: Standby, 3: Shutdown")
    power_w: int = Field(default=0, description="Battery power in W")
    software_version: int = Field(default=0, description="Software version")

    temperature_celsius: float = Field(default=0.0, description="Temperature in °C")
    voltage_v: float = Field(default=0.0, description="Total voltage in V")
    current_a: float = Field(default=0.0, description="Current in A (signed)")
    max_cell_voltage_v: float = Field(default=0.0, description="Max cell voltage in V")
    min_cell_voltage_v: float = Field(default=0.0, description="Min cell voltage in V")

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
            return True
        return (
            -10 < self.temperature_celsius < 45
            and 0.95 < self.max_cell_voltage_v / max(0.01, self.min_cell_voltage_v) < 1.05
        )

    @classmethod
    def from_protocol(cls, pack: BatteryPackProtocol) -> "ProcessedBatteryPack":
        """Build a ProcessedBatteryPack from raw protocol data."""
        temp_celsius = (pack.max_temp - 2731) / 10.0 if pack.max_temp else 0.0
        voltage_v = pack.total_vol / 100.0 if pack.total_vol else 0.0
        batcur = pack.batcur & 0xFFFF  # mask to uint16 (defensive against oversized JSON ints)
        signed_current = batcur - 65536 if batcur > 32767 else batcur
        current_a = signed_current / 10.0
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

    serial_number: str = Field(default="", description="Device serial number")
    product: str = Field(default="", description="Product model name")

    solar_input_power: int = Field(default=0, description="Total solar input power in W")
    solar_power_1: int = Field(default=0, description="Solar line 1 input power in W")
    solar_power_2: int = Field(default=0, description="Solar line 2 input power in W")
    solar_power_3: int = Field(default=0, description="Solar line 3 input power in W")
    solar_power_4: int = Field(default=0, description="Solar line 4 input power in W")
    grid_input_power: int = Field(default=0, description="Grid input power in W")
    output_home_power: int = Field(default=0, description="Output power to home in W")
    output_pack_power: int = Field(default=0, description="Output power to battery pack in W")
    pack_input_power: int = Field(default=0, description="Battery pack input power in W")

    battery_soc: int = Field(default=0, ge=0, le=100, description="Average battery SOC in %")
    battery_state: BatteryState = Field(default=BatteryState.STANDBY, description="Battery state")
    pack_count: int = Field(default=0, description="Number of battery packs")
    battery_packs: List[ProcessedBatteryPack] = Field(default_factory=list)

    min_soc: int = Field(default=0, ge=0, le=50, description="Min SOC in % (0-50)")
    max_soc: int = Field(default=100, ge=70, le=100, description="Max SOC in % (70-100)")
    input_limit: int = Field(default=0, description="AC input limit in W")
    output_limit: int = Field(default=0, description="AC output limit in W")
    inverse_max_power: int = Field(default=0, description="Maximum output power in W")

    ac_mode: ACMode = Field(default=ACMode.OUTPUT, description="AC mode")
    smart_mode: bool = Field(default=True, description="Smart mode active")
    bypass_mode: bool = Field(default=False, description="Bypass mode active")
    grid_connected: bool = Field(default=True, description="Grid connected")
    heating_active: bool = Field(default=False, description="Heating active")

    temperature_celsius: Optional[float] = Field(default=None, description="Enclosure temp in °C")

    data_ready: bool = Field(default=True)
    is_error: bool = Field(default=False)
    remain_out_time_min: int = Field(default=0, description="Remaining discharge time in min")

    @classmethod
    def from_response(cls, response: APIResponseProtocol) -> "DeviceState":
        """Build a DeviceState from an API response protocol object."""
        props = response.properties

        temp_celsius = None
        if props.hyper_tmp > 0:
            temp_celsius = (props.hyper_tmp - 2731) / 10.0

        min_soc_percent = props.min_soc // 10
        max_soc_percent = props.soc_set // 10

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
