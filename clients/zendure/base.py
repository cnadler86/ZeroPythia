"""SolarFlow Base Classes - Interface und gemeinsame Implementierung.
==================================================================

Dieses Modul definiert:
- ISolarFlowClient: Abstrakte HAL-Schnittstelle (2 Methoden)
- SolarFlowBase: Gemeinsame High-Level Implementierung
- Pydantic Models für verarbeitete/konvertierte Daten

Die Base-Klasse arbeitet mit abstrakten Protokollen (models.py) und
wandelt Raw-API-Daten in typsichere Pydantic Models mit berechneten Feldern.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from time import time
from typing import Dict, List, Optional

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
    """Monoton steigende Energiezähler (Dead-Reckoning aus Setpoints).

    Die Werte werden durch Integration der gesetzten Leistungssetpoints über
    die Zeit berechnet.  Sie starten bei 0 beim Programmstart und zählen nur hoch.

    Formel:
        discharge_wh += output_setpoint_W × Δt_h
        charge_wh    += input_setpoint_W  × Δt_h

    Usage auf höherer Ebene (HEMS):
        delta_discharge = counters.discharge_wh - prev_discharge_wh
        delta_charge    = counters.charge_wh    - prev_charge_wh
        real_load_delta = grid_delta + delta_discharge - delta_charge
    """
    discharge_wh: float  # kumulierte Energie die der Inverter ins Haus geliefert hat [Wh]
    charge_wh: float     # kumulierte Energie die der Inverter aus dem Netz bezogen hat [Wh]


# ==================== Processed Pydantic Models ====================
# Diese Models enthalten verarbeitete/konvertierte Daten mit computed fields.

class ProcessedBatteryPack(BaseModel):
    """Verarbeitete Battery Pack Daten mit konvertierten Werten.

    Konvertierungen gemäß Zendure zenSDK Documentation:
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

    # ===== Konvertierte Felder =====
    temperature_celsius: float = Field(default=0.0, description="Temperature in °C")
    voltage_v: float = Field(default=0.0, description="Total voltage in V")
    current_a: float = Field(default=0.0, description="Current in A (signed)")
    max_cell_voltage_v: float = Field(default=0.0, description="Max cell voltage in V")
    min_cell_voltage_v: float = Field(default=0.0, description="Min cell voltage in V")

    # ===== Computed Fields =====
    @computed_field
    @property
    def battery_state(self) -> BatteryState:
        """Battery State als Enum."""
        try:
            return BatteryState(self.state)
        except ValueError:
            return BatteryState.STANDBY

    @computed_field
    @property
    def is_healthy(self) -> bool:
        """Prüft ob Batterie im gesunden Bereich."""
        if self.min_cell_voltage_v == 0:
            return True  # Keine Daten = assume healthy
        return (
            -10 < self.temperature_celsius < 45
            and 0.95 < self.max_cell_voltage_v / max(0.01, self.min_cell_voltage_v) < 1.05
        )

    @classmethod
    def from_protocol(cls, pack: BatteryPackProtocol) -> "ProcessedBatteryPack":
        """Erstellt ProcessedBatteryPack aus Raw Protocol Daten."""
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
    """Verarbeiteter Device State mit allen relevanten Properties.

    Konvertiert Raw API Daten zu nutzbaren Werten:
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

    # ===== Limits (konvertiert zu %) =====
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

    # ===== Temperature (konvertiert) =====
    temperature_celsius: Optional[float] = Field(default=None, description="Enclosure temp in °C")

    # ===== Status Flags =====
    data_ready: bool = Field(default=True)
    is_error: bool = Field(default=False)
    remain_out_time_min: int = Field(default=0, description="Remaining discharge time in min")

    @classmethod
    def from_response(cls, response: APIResponseProtocol) -> "DeviceState":
        """Erstellt DeviceState aus API Response Protocol."""
        props = response.properties

        # Temperature: Kelvin*10 → Celsius
        temp_celsius = None
        if props.hyper_tmp > 0:
            temp_celsius = (props.hyper_tmp - 2731) / 10.0

        # SOC Limits: minSoc 0-500 → 0-50%, socSet 700-1000 → 70-100%
        min_soc_percent = props.min_soc // 10
        max_soc_percent = props.soc_set // 10

        # Battery Packs verarbeiten
        processed_packs = [
            ProcessedBatteryPack.from_protocol(pack)
            for pack in response.pack_data
        ]

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
            battery_state=BatteryState(props.pack_state) if props.pack_state in [0, 1, 2] else BatteryState.STANDBY,
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
    """Abstract Base Class - Minimal Interface für SolarFlow Clients (HAL).

    Alle Clients müssen nur 2 Low-Level Hardware-Methoden implementieren:
    1. _fetch_response() - Pure HW-Zugriff ohne Cache
    2. _set_properties() - Daten schreiben

    Cache-Handling und alle High-Level Methoden sind in SolarFlowBase implementiert.
    """

    @abstractmethod
    async def _fetch_response(self) -> Optional[APIResponseProtocol]:
        """Pure Hardware-Zugriff: Response vom Device holen (ohne Cache!).

        Hardware Abstraction Layer (HAL) - Implementiert von:
        - SolarFlowAsyncClient: HTTP GET Request
        - SolarFlowAsyncMockClient: Mock-Daten generieren

        Returns:
            APIResponseProtocol oder None bei Fehler
        """
        pass

    @abstractmethod
    async def _set_properties(self, properties: Dict, smart_mode: bool = True) -> bool:
        """Device Properties setzen (Low-Level Control).

        Args:
            properties: Dict mit zu setzenden Properties (camelCase Keys!)
            smart_mode: True = nur RAM (empfohlen), False = in Flash schreiben (persistent)

        Returns:
            True bei Erfolg, False bei Fehler
        """
        pass


# ==================== Base Implementation ====================

class SolarFlowBase(ISolarFlowClient):
    """Basis-Klasse mit gemeinsamer Implementierung für alle SolarFlow Clients.

    Enthält:
    - Cache-Verwaltung
    - Validierungslogik
    - High-Level API (snake_case Methoden)
    - Konvertierung Raw → Processed Data

    Subklassen implementieren nur:
    - _fetch_response() - Pure HW-Zugriff
    - _set_properties() - HW-Schreibzugriff
    """

    # Pydantic TypeAdapter für JSON-Parsing (wird von HAL verwendet)
    _decoder = TypeAdapter(APIResponse)

    def __init__(self, device_ip: str, *, cache_ttl: float = 1.0):
        """Initialisierung der Basis-Komponenten.

        Args:
            device_ip: IP-Adresse des SolarFlow Geräts
            cache_ttl: Cache Time-To-Live in Sekunden
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
        self._limits: BatteryLimits = BatteryLimits(charge_limit=0, discharge_limit=0, solar_limit=0)

        # Dead-Reckoning Energiezähler
        # Ein einziges vorzeichenbehaftetes Setpoint: +W = Entladen, -W = Laden.
        # Batterie kann physikalisch nur eine Richtung gleichzeitig — kein paralleles
        # Laden+Entladen möglich.
        self._setpoint_w: int = 0          # aktueller effektiver Leistungssetpoint [W]
        self._setpoint_timestamp: float = time()
        self._accumulated_discharge_wh: float = 0.0
        self._accumulated_charge_wh: float = 0.0

    # ==================== Cache Management ====================

    def _is_cache_valid(self) -> bool:
        """Prüft ob Cache noch gültig ist."""
        return (time() - self._cache_timestamp) < self.cache_ttl

    def _update_cache(self, data: APIResponseProtocol) -> None:
        """Aktualisiert Cache, Serial Number und modellbezogene Limits."""
        self._response_cache = data
        self._cache_timestamp = time()
        if self._sn is None:
            self._sn = data.sn
        if self.model is None and data.product:
            self.model = data.product  # type: ignore[assignment]
            limits = MODEL_LIMITS.get(self.model)  # type: ignore[arg-type]
            if limits:
                self._limits = BatteryLimits(
                    charge_limit=min(limits.charge_limit, data.properties.charge_max_limit or limits.charge_limit),
                    discharge_limit=min(limits.discharge_limit, data.properties.inverse_max_power or limits.discharge_limit),
                    solar_limit=limits.solar_limit,
                    min_power=limits.min_power,
                )

    def _invalidate_cache(self) -> None:
        """Invalidiert Cache."""
        self._cache_timestamp = 0

    async def _get_full_response(self, use_cache: bool = True) -> Optional[APIResponseProtocol]:
        """Komplette API-Response abrufen (mit Cache-Handling!).

        Args:
            use_cache: True = Cache verwenden wenn gültig

        Returns:
            APIResponseProtocol oder None bei Fehler
        """
        # Cache-Hit?
        if use_cache and self._is_cache_valid():
            return self._response_cache

        # HW-Zugriff über HAL
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
        """Validiert und begrenzt Output Limit."""
        return max(0, min(self._limits.discharge_limit, int(power_w)))

    def validate_input_limit(self, power_w: int) -> int:
        """Validiert und begrenzt Input Limit."""
        return max(0, min(self._limits.charge_limit, int(power_w)))

    def validate_min_soc(self, soc_percent: int) -> int:
        """Validiert Min SoC (0-50%) und konvertiert zu API-Wert (0-500)."""
        if not 0 <= soc_percent <= 50:
            raise ValueError(f"Invalid min SOC: {soc_percent} (must be 0-50%)")
        return soc_percent * 10

    def validate_max_soc(self, soc_percent: int) -> int:
        """Validiert Max SoC (70-100%) und konvertiert zu API-Wert (700-1000)."""
        if not 70 <= soc_percent <= 100:
            raise ValueError(f"Invalid max SOC: {soc_percent} (must be 70-100%)")
        return soc_percent * 10

    def validate_ac_mode(self, mode: ACMode) -> ACMode:
        """Validiert AC Mode (INPUT oder OUTPUT)."""
        if mode not in [ACMode.INPUT, ACMode.OUTPUT]:
            raise ValueError(f"Invalid AC mode: {mode} (must be INPUT or OUTPUT)")
        return mode

    # ==================== Properties ====================

    @property
    def serial_number(self) -> Optional[str]:
        """Gibt Seriennummer zurück (nach erstem Request verfügbar)."""
        return self._sn

    @property
    def max_charge_power(self) -> int:
        """Maximale Ladeleistung für dieses Modell."""
        return self._limits.charge_limit

    @property
    def max_discharge_power(self) -> int:
        """Maximale Entladeleistung für dieses Modell."""
        return self._limits.discharge_limit

    @property
    def max_solar_power(self) -> int:
        """Maximale Solar-Eingangsleistung für dieses Modell."""
        return self._limits.solar_limit

    @property
    def min_power(self) -> int:
        """Minimale Ausgangsleistung für dieses Modell."""
        return self._limits.min_power

    # ==================== Helper ====================

    def _prepare_properties_payload(self, properties: Dict, smart_mode: bool = True) -> Dict:
        """Bereitet Properties Payload für Write-Request vor.

        Args:
            properties: Dict mit Properties (camelCase Keys!)
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        if "smartMode" not in properties:
            properties["smartMode"] = 1 if smart_mode else 0
        return {"sn": self._sn, "properties": properties}

    # ==================== Energy Accumulator ====================

    def _flush_energy_to_now(self) -> None:
        """Akkumuliert Energie für den Zeitraum seit dem letzten Setpoint-Wechsel.

        Muss synchron VOR jedem Setpoint-Wechsel aufgerufen werden, damit die
        bisher mit dem alten Setpoint gelaufene Zeit korrekt erfasst wird.
        Positiver Setpoint → Entladung, negativer → Ladung.
        """
        now = time()
        dt_h = (now - self._setpoint_timestamp) / 3600.0
        if self._setpoint_w > 0:
            self._accumulated_discharge_wh += self._setpoint_w * dt_h
        elif self._setpoint_w < 0:
            self._accumulated_charge_wh += (-self._setpoint_w) * dt_h
        self._setpoint_timestamp = now

    def get_energy_counters(self) -> InverterEnergyCounters:
        """Gibt aktuelle monoton steigende Energiezähler zurück.

        Flusht zuerst die seit dem letzten Setpoint-Wechsel aufgelaufene Energie,
        damit der Wert immer aktuell ist, auch wenn kein Setpoint-Wechsel stattfand.

        Returns:
            InverterEnergyCounters mit discharge_wh und charge_wh
        """
        self._flush_energy_to_now()
        return InverterEnergyCounters(
            discharge_wh=self._accumulated_discharge_wh,
            charge_wh=self._accumulated_charge_wh,
        )

    # ==================== High-Level API - Getters ====================

    async def get_state(self, *, use_cache: bool = True) -> Optional[DeviceState]:
        """Vollständigen Device State abrufen (verarbeitet).

        Args:
            use_cache: True = Cache verwenden wenn gültig

        Returns:
            DeviceState mit allen verarbeiteten Properties oder None bei Fehler
        """
        response = await self._get_full_response(use_cache)
        if not response:
            return None
        return DeviceState.from_response(response)

    async def get_battery_packs(self, *, use_cache: bool = True) -> List[ProcessedBatteryPack]:
        """Battery Pack Daten abrufen (verarbeitet).

        Args:
            use_cache: True = Cache verwenden wenn gültig

        Returns:
            Liste von ProcessedBatteryPack Objekten
        """
        response = await self._get_full_response(use_cache)
        if not response or not response.pack_data:
            return []
        return [ProcessedBatteryPack.from_protocol(pack) for pack in response.pack_data]

    async def get_solar_input_power(self, *, use_cache: bool = True) -> Optional[int]:
        """Solar-Eingangsleistung abrufen (W)."""
        response = await self._get_full_response(use_cache)
        return response.properties.solar_input_power if response else None

    async def get_ac_output_power(self, *, use_cache: bool = True) -> Optional[int]:
        """Aktuelle AC-Ausgangsleistung abrufen (W)."""
        response = await self._get_full_response(use_cache)
        return response.properties.output_home_power if response else None

    async def get_ac_output_limit(self, *, use_cache: bool = True) -> Optional[int]:
        """Output-Limit abrufen (W)."""
        response = await self._get_full_response(use_cache)
        return response.properties.output_limit if response else None

    async def get_ac_input_limit(self, *, use_cache: bool = True) -> Optional[int]:
        """Input-Limit abrufen (W)."""
        response = await self._get_full_response(use_cache)
        return response.properties.input_limit if response else None

    async def get_ac_mode(self, *, use_cache: bool = True) -> Optional[ACMode]:
        """AC-Modus abrufen."""
        response = await self._get_full_response(use_cache)
        if not response:
            return None
        try:
            return ACMode(response.properties.ac_mode)
        except ValueError:
            return ACMode.OUTPUT

    async def get_min_soc(self, *, use_cache: bool = True) -> Optional[int]:
        """Minimalen SoC abrufen (0-50%)."""
        response = await self._get_full_response(use_cache)
        return response.properties.min_soc // 10 if response else None

    async def get_max_soc(self, *, use_cache: bool = True) -> Optional[int]:
        """Maximalen SoC abrufen (70-100%)."""
        response = await self._get_full_response(use_cache)
        return response.properties.soc_set // 10 if response else None

    async def get_battery_soc(self, *, use_cache: bool = True) -> Optional[int]:
        """Aktuellen Batterie-SOC abrufen (0-100%)."""
        response = await self._get_full_response(use_cache)
        return response.properties.electric_level if response else None

    async def get_temperature_celsius(self, *, use_cache: bool = True) -> Optional[float]:
        """Gehäusetemperatur abrufen (°C)."""
        response = await self._get_full_response(use_cache)
        if not response or response.properties.hyper_tmp == 0:
            return None
        return (response.properties.hyper_tmp - 2731) / 10.0

    # ==================== High-Level API - Setters ====================

    async def set_ac_output_limit(self, power_w: int, *, smart_mode: bool = True) -> bool:
        """Ausgangsleistungslimit setzen.

        Args:
            power_w: Leistung in Watt
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        power_w = self.validate_output_limit(power_w)
        self._flush_energy_to_now()
        self._setpoint_w = power_w  # positiv = Entladen
        return await self._set_properties({"outputLimit": power_w}, smart_mode)

    async def set_ac_input_limit(self, power_w: int, *, smart_mode: bool = True) -> bool:
        """Eingangsleistungslimit setzen.

        Args:
            power_w: Leistung in Watt
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        power_w = self.validate_input_limit(power_w)
        self._flush_energy_to_now()
        self._setpoint_w = -power_w  # negativ = Laden
        return await self._set_properties({"inputLimit": power_w}, smart_mode)

    async def set_ac_mode(self, mode: ACMode, *, smart_mode: bool = True) -> bool:
        """AC-Modus setzen.

        Args:
            mode: ACMode.INPUT (Laden) oder ACMode.OUTPUT (Entladen)
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        mode = self.validate_ac_mode(mode)
        return await self._set_properties({"acMode": mode.value}, smart_mode)

    async def set_min_soc(self, soc_percent: int, *, smart_mode: bool = True) -> bool:
        """Minimalen SoC setzen.

        Args:
            soc_percent: SOC in Prozent (0-50%)
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        validated = self.validate_min_soc(soc_percent)
        return await self._set_properties({"minSoc": validated}, smart_mode)

    async def set_max_soc(self, soc_percent: int, *, smart_mode: bool = True) -> bool:
        """Maximalen SoC setzen.

        Args:
            soc_percent: SOC in Prozent (70-100%)
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        validated = self.validate_max_soc(soc_percent)
        return await self._set_properties({"socSet": validated}, smart_mode)

    # ==================== Convenience Methods ====================

    async def is_settled(self, *, use_cache: bool = True) -> Optional[bool]:
        """Prüft ob Inverter im aktuellen Setpoint "settled" ist (Leistung nahe Setpoint).

        Args:
            use_cache: True = Cache verwenden

        Returns:
            True wenn settled, False wenn nicht, None bei Fehler
        """
        state = await self.get_state(use_cache=use_cache)
        if not state:
            return None

        current_output = state.output_home_power
        return abs(abs(current_output) - abs(self._setpoint_w)) < 2

    async def start_discharge(self, power_w: int, *, smart_mode: bool = True) -> bool:
        """Entladung starten.

        Args:
            power_w: Entladeleistung in Watt
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        if self._limits.discharge_limit == 0:
            await self.get_state(use_cache=False)  # Force cache update to get limits
        clamped = min(power_w, self._limits.discharge_limit)
        self._flush_energy_to_now()
        self._setpoint_w = clamped  # positiv = Entladen
        return await self._set_properties(
            {
                "acMode": ACMode.OUTPUT.value,
                "outputLimit": clamped,
                "inputLimit": 0,
            },
            smart_mode
        )

    async def start_charge(self, power_w: int, *, smart_mode: bool = True) -> bool:
        """AC-Ladung starten.

        Args:
            power_w: Ladeleistung in Watt
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        clamped = min(power_w, self._limits.charge_limit)
        self._flush_energy_to_now()
        self._setpoint_w = -clamped  # negativ = Laden
        return await self._set_properties(
            {
                "acMode": ACMode.INPUT.value,
                "inputLimit": clamped,
                "outputLimit": 0,
            },
            smart_mode
        )

    async def stop(self, *, smart_mode: bool = True) -> bool:
        """Alle Aktivitäten stoppen.

        Args:
            smart_mode: True = nur RAM, False = Flash schreiben
        """
        self._flush_energy_to_now()
        self._setpoint_w = 0
        return await self._set_properties(
            {
                "acMode": ACMode.OUTPUT.value,
                "outputLimit": 0,
                "inputLimit": 0,
            },
            smart_mode
        )

    async def get_usable_energy_wh(self, battery_capacity_wh: int, *, use_cache: bool = True) -> Optional[float]:
        """Berechnet nutzbare Energie in der Batterie.

        Die nutzbare Energie berücksichtigt den min_soc, der nicht
        unterschritten werden darf.

        Args:
            battery_capacity_wh: Batteriekapazität in Wh
            use_cache: True = Cache verwenden

        Returns:
            Nutzbare Energie in Wh oder None bei Fehler
        """
        state = await self.get_state(use_cache=use_cache)
        if not state:
            return None

        current_soc_fraction = state.battery_soc / 100.0
        min_soc_fraction = state.min_soc / 100.0

        usable_soc = max(0, current_soc_fraction - min_soc_fraction)
        return battery_capacity_wh * usable_soc
