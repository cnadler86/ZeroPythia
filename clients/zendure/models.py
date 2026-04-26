"""Zendure SolarFlow Modelle.

==========================

Dieses Modul definiert:
- Abstrakte Protokolle für HAL-unabhängige API Responses
- Enums (ACMode, BatteryState)
- Limits (BatteryLimits, MODEL_LIMITS)
- Pydantic Models für Parsing (Properties, BatteryPack, APIResponse)

Die HAL-Implementierungen (aiozen, mock) können beliebige Parser nutzen.
Die Base-Klasse arbeitet mit den abstrakten Protokollen.

Reference: Zendure zenSDK Documentation
"""

from abc import ABC, abstractmethod
from enum import IntEnum
from typing import Dict, List, Literal, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# ==================== Enums ====================


class BatteryState(IntEnum):
    """Batterie-Zustände."""

    STANDBY = 0
    CHARGING = 1
    DISCHARGING = 2


class ACMode(IntEnum):
    """AC-Modus für SolarFlow."""

    INPUT = 1  # Laden vom Netz
    OUTPUT = 2  # Einspeisung ins Netz


# ==================== Limits ====================


class BatteryLimits(BaseModel):
    """Batterie-spezifische Limits."""

    charge_limit: int
    discharge_limit: int
    solar_limit: int
    min_power: int = 0


# Unterstützte Batterie Modelle
BatteryModel = Literal[
    "solarFlow800Pro", "solarFlow800Plus", "SF2400AC", "Hub1200", "Hub2000", "Hyper2000", "AIO2400"
]

# Modell-spezifische Limits
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

    bat_volt: int = Field(default=0, description="Battery voltage")
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
