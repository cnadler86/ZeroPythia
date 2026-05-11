"""Async Mock SolarFlow Client - Simuliert Zendure SolarFlow Verhalten.

=====================================================================

Timer-basierte Simulation mit realistischem Timing-Verhalten.
Implementiert HAL-Interface, erbt alle High-Level Methoden von SolarFlowBase.

Timing-Parameter kalibriert aus echten Messungen (20W→120W Sprung):
- Shelly sieht Änderung nach ~1.0-1.3s (dead time)
- Zendure API meldet Änderung nach ~1.5s (setpoint delay)
- Rampe: gestuft mit Zwischenwerten, Settling nach 1.5-3.5s
- Rauschen: ±1W auf gemeldeter Ausgangsleistung
"""

import asyncio
import logging
import math
import random
from time import time
from typing import Dict, Optional

from ..base import SolarFlowBase
from ..models import (
    MODEL_LIMITS,
    ACMode,
    APIResponse,
    APIResponseProtocol,
    BatteryModel,
    BatteryPack,
    BatteryState,
    Properties,
)

logger = logging.getLogger(__name__)


class SetpointState:
    """Aktueller Setpoint-Zustand für Timer-Berechnung."""

    def __init__(
        self,
        ac_mode: ACMode,
        input_limit: int,
        output_limit: int,
        was_in_standby: bool,
        start_output: int = 0,
        start_input: int = 0,
        current_time: Optional[float] = None,
    ):
        self.created_at = (
            current_time if current_time is not None else time()
        )  # Echte Erstellungszeit (für skip_delay-Check)
        self.timestamp = self.created_at  # Simulation-Startzeit (kann backdatiert werden)
        self.ac_mode = ac_mode
        self.input_limit = input_limit
        self.output_limit = output_limit
        self.was_in_standby = was_in_standby  # War im Standby beim Setzen?
        self.start_output = start_output  # Output-Startwert für PT1
        self.start_input = start_input  # Input-Startwert für PT1


class SolarFlowAsyncMockClient(SolarFlowBase):
    """Async Mock Client für Zendure SolarFlow - Timer-basierte Simulation.

    Timer-Verhalten:
    - Setpoint Timer: 1.5s bis Setpoint verfügbar
    - Output Timer: 0.5s/10s + 0.5s PT1-Ramping (abhängig von Standby)
    - SOC-Berechnung basierend auf Energiefluss
    - Auto-Standby bei Min/Max SOC

    Implementiert:
    - get_full_response() - Mock-Simulation
    - set_properties() - Mock-Simulation

    Erbt von SolarFlowBase:
    - Alle High-Level Methoden (get_state, get/set properties, control methods)
    """

    # Timing-Konstanten (kalibriert aus Messungen)
    SETPOINT_DELAY = 1.5  # Sekunden bis Setpoint in API sichtbar
    ACTIVE_REACTION_DELAY = 0.0  # Sekunden Totzeit nach Setpoint wenn aktiv
    STANDBY_REACTION_DELAY = 10.0  # Sekunden Totzeit nach Setpoint von Standby
    PT1_TIME_CONSTANT = 0.5  # Sekunden für PT1-Ramping (63% in dieser Zeit)

    # Realistische Rausch-/Stufenparameter (aus Messungen)
    OUTPUT_NOISE_W = 1.0  # ±W Rauschen auf gemeldeter Ausgangsleistung
    GRID_DEAD_TIME = 1.1  # Sekunden bis tatsächliche Leistung am Netz ankommt
    STEP_NOISE_FRACTION = 0.15  # 15% Zwischenwert-Streuung während Rampe

    EFFICIENCY = 0.92
    DEFAULT_MODEL = "solarFlow800Pro"

    def __init__(
        self,
        device_ip: str = "127.0.0.1",
        model: BatteryModel = DEFAULT_MODEL,
        *,
        initial_soc: int = 50,
        battery_capacity_wh: int = 1920,
        **kwargs,
    ):
        """Initialisiert Async Mock Client.

        Args:
            model: Modellbezeichnung (für Limits)
            initial_soc: Anfangs-SOC in Prozent (0-100)
            device_ip: Dummy IP (wird nicht verwendet)
            battery_capacity_wh: Batteriekapazität in Wh
            timeout: Dummy Timeout (wird nicht verwendet)
        """
        super().__init__(device_ip=device_ip, **kwargs)

        # Batterie-State
        self._soc = max(0, min(100, initial_soc))
        self._battery_capacity_wh = battery_capacity_wh
        self._sn = f"MOCK_{model.upper()}"
        self.model = model

        # Limits direkt setzen (da self.model schon gesetzt ist)
        limits = MODEL_LIMITS.get(model)
        if not limits:
            raise ValueError(f"Unbekanntes SolarFlow Modell: {model}")
        self._limits = limits

        # Timer-Parameter
        self.to_active_delay = self.STANDBY_REACTION_DELAY
        self.setpoint_delay = self.SETPOINT_DELAY
        self.reaction_delay = self.ACTIVE_REACTION_DELAY
        self.pt1_time_constant = self.PT1_TIME_CONSTANT

        # Setpoints (vom User gesetzt)
        self._min_soc_percent = 10
        self._max_soc_percent = 100
        self._smart_mode = True

        # Aktueller Zustand (was tatsächlich am Gerät anliegt)
        self._actual_output_power = 0
        self._actual_input_power = 0
        self._solar_input_power = 0

        # Bypass: True wenn SOC=100 und solar durch bypass geleitet wird
        self._bypass: bool = initial_soc >= 100

        # Sichtbare Limits (was get_ac_output_limit() zurückgibt)
        self._visible_output_limit = 0
        self._visible_input_limit = 0

        # Letzter Setpoint (für Timer-Berechnung)
        self._current_setpoint: Optional[SetpointState] = None

        # Time Travel für Simulation
        self._simulation_time: Optional[float] = None

        # Letztes Update
        self._last_update = self._get_time()

        # Initialer State
        self._update_cache(self._generate_response())

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def _ensure_session(self):
        """Dummy method for compatibility with SolarFlowAsyncClient."""
        pass

    async def close(self):
        """Dummy method for compatibility with SolarFlowAsyncClient."""
        pass

    # ==================== Time Travel ====================

    def _get_time(self) -> float:
        """Gibt aktuelle Zeit zurück (simuliert oder echt)."""
        return self._simulation_time if self._simulation_time is not None else time()

    def set_simulation_time(self, sim_time: float) -> None:
        """Setzt simulierte Zeit für Time Travel (None = echte Zeit)."""
        self._simulation_time = sim_time

    # ==================== Timer-basierte Simulation ====================

    def _set_new_setpoint(self, ac_mode: ACMode, input_limit: int, output_limit: int) -> None:
        """Setzt neuen Setpoint und startet Timer."""
        # Aktuelle Power berechnen (vor dem neuen Setpoint!)
        current_input, current_output = self._calculate_actual_power()
        self._actual_input_power = current_input
        self._actual_output_power = current_output

        # War der VORHERIGE Setpoint im Standby? (Limits waren 0)
        # Wichtig: Prüfen auf SETPOINT, nicht auf aktuelle Power!
        if self._current_setpoint:
            was_in_standby = (
                self._current_setpoint.output_limit == 0 and self._current_setpoint.input_limit == 0
            )
        else:
            # Kein vorheriger Setpoint = wir starten aus Standby
            was_in_standby = True

        # Für skip_delay-Check: "echte" Zeit seit LETZTEM Setpoint
        # (nicht backdatierter Timestamp!)
        current_time = self._get_time()

        # Wenn wir bereits einen Setpoint haben und noch nicht gesettled sind,
        # dann überspringen wir den Setpoint-Delay (sofortige Reaktion)
        skip_setpoint_delay = False
        if self._current_setpoint and not was_in_standby:
            # Prüfe echte Zeit seit letztem Setpoint (created_at, nicht timestamp!)
            elapsed_real = current_time - self._current_setpoint.created_at
            # Wenn der letzte Setpoint noch keine 2.5s alt ist, überspringen wir den Delay
            if elapsed_real < (
                self.setpoint_delay + self.reaction_delay + 2.0 * self.pt1_time_constant
            ):
                skip_setpoint_delay = True

        self._current_setpoint = SetpointState(
            ac_mode=ac_mode,
            input_limit=input_limit,
            output_limit=output_limit,
            was_in_standby=was_in_standby,
            start_output=self._actual_output_power,
            start_input=self._actual_input_power,
            current_time=current_time,
        )

        # Wenn wir den Setpoint-Delay überspringen, backdaten wir den Timestamp
        if skip_setpoint_delay:
            self._current_setpoint.timestamp -= self.setpoint_delay

        # Bypass erzwungen aus wenn Output > Solar (Batterie muss liefern)
        if output_limit > self._solar_input_power and self._bypass:
            logger.info(
                "Mock: outputLimit=%dW > solar=%dW → bypass FORCED OFF",
                output_limit,
                self._solar_input_power,
            )
            self._bypass = False

        logger.debug(
            "New Setpoint: mode=%s, input=%dW, output=%dW, was_standby=%s, start_output=%dW, skip_delay=%s",
            ac_mode.name,
            input_limit,
            output_limit,
            was_in_standby,
            self._actual_output_power,
            skip_setpoint_delay,
        )

    def _calculate_setpoint_availability(self) -> tuple[int, int]:
        """Berechnet verfügbare Setpoints (nach SETPOINT_DELAY).

        Returns:
            (input_limit, output_limit) die aktuell verfügbar sind
        """
        if not self._current_setpoint:
            return (self._visible_input_limit, self._visible_output_limit)

        elapsed = self._get_time() - self._current_setpoint.timestamp

        # Nach SETPOINT_DELAY ist Setpoint verfügbar
        if elapsed >= self.setpoint_delay:
            # Neuer Setpoint ist sichtbar
            self._visible_input_limit = self._current_setpoint.input_limit
            self._visible_output_limit = self._current_setpoint.output_limit
        # Sonst bleiben die alten sichtbaren Limits erhalten

        return (self._visible_input_limit, self._visible_output_limit)

    def _calculate_pt1_response(
        self, target: float, start: float, elapsed: float, tau: float
    ) -> float:
        """Berechnet PT1-Antwort (Tiefpass 1. Ordnung).

        PT1: y(t) = target * (1 - e^(-t/tau)) + start * e^(-t/tau)

        Args:
            target: Zielwert
            start: Startwert
            elapsed: Verstrichene Zeit
            tau: Zeitkonstante (63% nach tau Sekunden)

        Returns:
            Aktueller Wert
        """
        # Wenn tau = 0, sofort zum Ziel springen (keine Zeitkonstante)
        if tau == 0.0:
            return target

        factor = 1.0 - math.exp(-elapsed / tau)
        return start + (target - start) * factor

    def _calculate_actual_power(self) -> tuple[int, int]:
        """Berechnet tatsächliche Power basierend auf Timern und gestufter Rampe.

        Die gemeldete Ausgangsleistung folgt einem realistischen Stufenverhalten:
        - Nach Setpoint-Delay + Reaction-Delay beginnt die Rampe
        - PT1-basierte Rampe mit Rauschen und Zwischenwerten
        - Ganzzahlige Ausgabe mit ±1W Rauschen

        Returns:
            (actual_input_power, actual_output_power)
        """
        if not self._current_setpoint:
            return (0, 0)

        elapsed = self._get_time() - self._current_setpoint.timestamp

        # 1. Phase: Setpoint noch nicht verfügbar
        if elapsed < self.setpoint_delay:
            return (
                self._current_setpoint.start_input,
                self._current_setpoint.start_output,
            )

        # 2. Phase: Setpoint verfügbar, warten auf Reaktion
        reaction_delay = (
            self.to_active_delay if self._current_setpoint.was_in_standby else self.reaction_delay
        )

        reaction_start = self.setpoint_delay + reaction_delay

        if elapsed < reaction_start:
            return (
                self._current_setpoint.start_input,
                self._current_setpoint.start_output,
            )

        # 3. Phase: Gestufte Rampe mit Rauschen
        pt1_elapsed = elapsed - reaction_start

        # Output
        if self._current_setpoint.output_limit > 0:
            raw_output = self._calculate_pt1_response(
                target=float(self._current_setpoint.output_limit),
                start=float(self._current_setpoint.start_output),
                elapsed=pt1_elapsed,
                tau=self.pt1_time_constant,
            )
            # Stufenrauschen: während der Rampe stärkeres Rauschen
            progress = 1.0 - math.exp(-pt1_elapsed / max(self.pt1_time_constant, 0.01))
            step_range = abs(
                self._current_setpoint.output_limit - self._current_setpoint.start_output
            )
            if progress < 0.95 and step_range > 10:
                noise = random.gauss(0, step_range * self.STEP_NOISE_FRACTION)
            else:
                noise = random.gauss(0, self.OUTPUT_NOISE_W)
            actual_output = int(round(max(0, raw_output + noise)))
        else:
            actual_output = 0

        # Input
        if self._current_setpoint.input_limit > 0:
            raw_input = self._calculate_pt1_response(
                target=float(self._current_setpoint.input_limit),
                start=float(self._current_setpoint.start_input),
                elapsed=pt1_elapsed,
                tau=self.pt1_time_constant,
            )
            noise = random.gauss(0, self.OUTPUT_NOISE_W)
            actual_input = int(round(max(0, raw_input + noise)))
        else:
            actual_input = 0

        return (actual_input, actual_output)

    # ==================== SOC Berechnung ====================

    def _update_soc(self) -> None:
        """Berechnet SOC basierend auf verstrichener Zeit und Energiefluss."""
        # Aktuelle Power erst berechnen!
        self._actual_input_power, self._actual_output_power = self._calculate_actual_power()

        now = self._get_time()
        elapsed_hours = (now - self._last_update) / 3600.0

        if elapsed_hours <= 0:
            return

        # Energiefluss berechnen (AC + Solar)
        energy_flow_wh = 0

        # AC Input (Netz-Laden)
        if self._actual_input_power > 0:
            energy_flow_wh += self._actual_input_power * elapsed_hours * self.EFFICIENCY

        # AC Output (Entladen) – entfällt im Bypass (Batterie liefert nicht)
        if self._actual_output_power > 0 and not self._bypass:
            energy_flow_wh -= self._actual_output_power * elapsed_hours / self.EFFICIENCY

        # Solar Input (immer Laden mit Effizienz)
        if self._solar_input_power > 0:
            energy_flow_wh += self._solar_input_power * elapsed_hours

        # SOC aktualisieren
        soc_change = (energy_flow_wh / self._battery_capacity_wh) * 100
        old_soc = self._soc
        self._soc = max(0, min(100, self._soc + soc_change))

        if abs(old_soc - self._soc) > 0.01:
            logger.debug(
                "SOC Update: %.1f%% → %.1f%% (%.1fWh in %.1fs, Solar: %dW)",
                old_soc,
                self._soc,
                energy_flow_wh,
                elapsed_hours * 3600,
                self._solar_input_power,
            )

        self._last_update = now
        self._check_auto_standby()
        self._update_bypass()

    def _update_bypass(self) -> None:
        """Aktualisiert Bypass-Zustand: SOC=100 → bypass an, SOC<100 → bypass aus."""
        soc_int = int(self._soc)
        if soc_int >= 100 and not self._bypass:
            self._bypass = True
            logger.info("Mock: SOC=%d%% → bypass ON (Batterie voll)", soc_int)
        elif soc_int < 100 and self._bypass:
            self._bypass = False
            logger.info("Mock: SOC=%d%% → bypass OFF (SOC unter 100%%)", soc_int)

    def _check_auto_standby(self) -> None:
        """Prüft ob Auto-Standby aktiviert werden muss."""
        if not self._current_setpoint:
            return

        # Bei Output: Wenn SOC zu niedrig
        if self._current_setpoint.ac_mode == ACMode.OUTPUT and self._soc <= self._min_soc_percent:
            logger.info("Min SOC %.0f%% erreicht - Auto-Standby", self._soc)
            self._set_new_setpoint(ACMode.OUTPUT, 0, 0)

        # Bei Input: Wenn SOC zu hoch
        elif self._current_setpoint.ac_mode == ACMode.INPUT and self._soc >= self._max_soc_percent:
            logger.info("Max SOC %.0f%% erreicht - Auto-Standby", self._soc)
            self._set_new_setpoint(ACMode.INPUT, 0, 0)

    def _get_current_status(self) -> str:
        """Ermittelt aktuellen Status basierend auf Setpoint."""
        if not self._current_setpoint:
            return "standby"

        # Status basiert auf Setpoint, nicht auf tatsächlichem Output
        if self._current_setpoint.output_limit == 0 and self._current_setpoint.input_limit == 0:
            return "standby"
        elif self._current_setpoint.output_limit > 0:
            return "discharging"
        elif self._current_setpoint.input_limit > 0:
            return "charging"
        else:
            return "standby"

    # ==================== Response Generation ====================

    def _generate_response(self) -> APIResponse:
        """Generiert Mock API-Response als msgspec Struct."""
        # SOC aktualisieren
        self._update_soc()

        # Aktuelle Power aus Timern berechnen
        self._actual_input_power, self._actual_output_power = self._calculate_actual_power()

        # Setpoint-Verfügbarkeit prüfen
        available_input, available_output = self._calculate_setpoint_availability()

        # Battery State
        if self._actual_input_power > 0:
            battery_state = BatteryState.CHARGING
        elif self._actual_output_power > 0:
            battery_state = BatteryState.DISCHARGING
        else:
            battery_state = BatteryState.STANDBY

        # AC Mode
        if self._current_setpoint:
            ac_mode = self._current_setpoint.ac_mode
        else:
            ac_mode = ACMode.OUTPUT

        # Properties Struct erstellen
        properties = Properties.model_validate(
            {
                "solar_input_power": self._solar_input_power,
                "solar_power_1": self._solar_input_power,
                "solar_power_2": 0,
                "grid_input_power": self._actual_input_power,
                "output_home_power": self._solar_input_power
                if self._bypass
                else self._actual_output_power,
                "output_pack_power": 0,
                "pack_input_power": 0 if self._bypass else self._actual_output_power,
                "electric_level": int(self._soc),
                "pack_state": battery_state.value,
                "pack_num": 1,
                "input_limit": available_input,
                "output_limit": available_output,
                "min_soc": self._min_soc_percent * 10,
                "soc_set": self._max_soc_percent * 10,
                "ac_mode": ac_mode.value,
                "smart_mode": 1 if self._smart_mode else 0,
                "pass": 1 if self._bypass else 0,
                "grid_state": 1,
                "heat_state": 0,
                "hyper_tmp": 2981,
                "data_ready": 1,
                "remain_out_time": 0,
                "reverse_state": 0,
                "soc_status": 0,
                "dc_status": 1
                if self._actual_output_power > 0 or self._actual_input_power > 0
                else 0,
                "pv_status": 0,
                "ac_status": 1
                if self._actual_output_power > 0 or self._actual_input_power > 0
                else 0,
                "soc_limit": 0,
            }
        )

        # PackData mit BatteryPack (hat computed fields!)
        pack_data = [
            BatteryPack(
                sn=f"{self._sn}_PACK1",
                soc_level=int(self._soc),
                max_temp=2981,  # ~25°C in Kelvin*10
                total_vol=5400,  # 54V in mV
                batcur=0,
                max_vol=540,  # 5.4V in cV
                min_vol=540,
                pack_type=1000,
                state=battery_state.value,
                power=0,
                soft_version=1000,
            )
        ]

        return APIResponse(
            timestamp=int(self._get_time()),
            message_id=int(self._get_time()),
            sn=self._sn if self._sn else f"MOCK_{self.DEFAULT_MODEL}",
            version=2,
            product=self.model if isinstance(self.model, str) else self.DEFAULT_MODEL,
            properties=properties,
            pack_data=pack_data,
        )

    # ==================== HAL Implementation (2 Methoden) ====================

    async def _fetch_response(self) -> Optional[APIResponseProtocol]:
        """Pure HW-Zugriff: Mock-Response generieren.

        Implementiert HAL-Interface. Cache-Handling erfolgt in Base.

        Returns:
            APIResponse (implementiert APIResponseProtocol)
        """
        await asyncio.sleep(0)  # Yield control
        self._update_soc()
        return self._generate_response()

    async def _set_properties(self, properties: Dict, smart_mode: bool = True) -> bool:
        """Properties setzen (async)."""
        await asyncio.sleep(0)  # Yield control
        self._update_soc()
        ac_mode = ACMode.OUTPUT
        input_limit = 0
        output_limit = 0

        if "acMode" in properties:
            ac_mode = ACMode(properties["acMode"])
        elif self._current_setpoint:
            ac_mode = self._current_setpoint.ac_mode

        if "inputLimit" in properties:
            input_limit = self.validate_input_limit(properties["inputLimit"])
        elif self._current_setpoint:
            input_limit = self._current_setpoint.input_limit

        if "outputLimit" in properties:
            output_limit = self.validate_output_limit(properties["outputLimit"])
        elif self._current_setpoint:
            output_limit = self._current_setpoint.output_limit

        if "minSoc" in properties:
            self._min_soc_percent = properties["minSoc"] // 10
        if "socSet" in properties:
            self._max_soc_percent = properties["socSet"] // 10
        if "smartMode" in properties:
            self._smart_mode = bool(properties["smartMode"])

        # Neuen Setpoint setzen wenn Input/Output geändert wurde
        if "inputLimit" in properties or "outputLimit" in properties:
            self._set_new_setpoint(ac_mode, input_limit, output_limit)

        self._invalidate_cache()
        return True

    # ==================== Mock-Specific Methods ====================

    async def set_solar_input_power(self, power_w: int) -> bool:
        """Setzt Solar-Input-Leistung (Mock-spezifisch).

        Simuliert Solar-Einspeisung in die Batterie. Die Energie wird über
        die Zeit akkumuliert und wirkt sich auf den SOC aus.

        Args:
            power_w: Solar-Leistung in Watt (0 bis max_solar)

        Returns:
            True wenn erfolgreich gesetzt, False wenn:
            - Leistung außerhalb Limits (0 bis max_solar)
            - SOC bereits bei max_soc
        """
        await asyncio.sleep(0)  # Yield control

        # Validierung
        if power_w < 0 or power_w > self._limits.solar_limit:
            logger.warning(
                "Solar input %dW außerhalb Limits (0-%dW)",
                power_w,
                self._limits.solar_limit,
            )
            return False

        # SOC prüfen (vor Update!)
        self._update_soc()

        if self._soc >= self._max_soc_percent:
            logger.warning(
                "Solar input abgelehnt: SOC %.1f%% >= Max SOC %d%%",
                self._soc,
                self._max_soc_percent,
            )
            return False

        # Solar-Leistung setzen
        self._solar_input_power = power_w

        logger.debug("Solar input gesetzt: %dW", power_w)
        return True

    # ==================== Grid-Power (für virtuelle Shelly-Kopplung) ==============

    def get_grid_output_power(self) -> float:
        """Tatsächliche Leistung, die am Netz/Shelly ankommt (mit Grid-Dead-Time).

        Die Leistung, die der Wechselrichter tatsächlich ins Hausnetz einspeist,
        hinkt der gemeldeten Leistung um GRID_DEAD_TIME hinterher:
        - Command → GRID_DEAD_TIME → Leistung am Netz sichtbar (Shelly misst)
        - Command → SETPOINT_DELAY → Leistung in API sichtbar (get_ac_output_power)

        Returns:
            Leistung in Watt (float, mit Rauschen), die der Shelly auf Phase B sieht.
        """
        if not self._current_setpoint:
            return 0.0

        elapsed = self._get_time() - self._current_setpoint.created_at

        # Vor Grid-Dead-Time: alte Leistung
        if elapsed < self.GRID_DEAD_TIME:
            return float(self._current_setpoint.start_output)

        # Nach Grid-Dead-Time: PT1-Rampe zum neuen Wert
        pt1_elapsed = elapsed - self.GRID_DEAD_TIME

        if self._current_setpoint.output_limit > 0 or self._current_setpoint.start_output > 0:
            raw = self._calculate_pt1_response(
                target=float(self._current_setpoint.output_limit),
                start=float(self._current_setpoint.start_output),
                elapsed=pt1_elapsed,
                tau=self.pt1_time_constant,
            )
            # Realistisches Rauschen (Shelly misst ~±0.5W)
            noise = random.gauss(0, 0.5)
            return max(0.0, raw + noise)
        else:
            return 0.0

    # ==================== Debug Helpers ====================

    def get_debug_info(self) -> Dict:
        """Debug-Informationen für Tests (sync)."""
        input_avail, output_avail = self._calculate_setpoint_availability()
        actual_input, actual_output = self._calculate_actual_power()

        return {
            "soc": self._soc,
            "actual_output_power": actual_output,
            "actual_input_power": actual_input,
            "available_output_limit": output_avail,
            "available_input_limit": input_avail,
            "target_output_limit": self._current_setpoint.output_limit
            if self._current_setpoint
            else 0,
            "target_input_limit": self._current_setpoint.input_limit
            if self._current_setpoint
            else 0,
            "current_status": self._get_current_status(),
            "was_in_standby": (self._actual_output_power == 0 and self._actual_input_power == 0),
        }
