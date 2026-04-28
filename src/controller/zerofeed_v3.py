"""Zero-Feed Controller V3 – Phasen-bewusste Nulleinspeisung.

Architektur:
  1. Sampling-Loop (schnell, ~1s):
     - Liest alle 3 Phasen + Batterie-Output
     - Speist pro-Phasen Oszillationserkennung (gekapselt in Controllern)
     - Schreibt PhaseSamples in Queue

  2. Control-Loop (langsam, ~3s):
     - Liest PhaseSamples aus Queue
     - Pro-Phase-Controller: je ein gekapselter Controller mit
       Preprocessor, P-Regler und Oszillationsdetektoren
     - ZeroFeedManager addiert Korrekturen → Batterie-Setpoint

  Pro-Phase-Controller:
    - PhaseController (A, C): Feedforward – kompensiert Netzbezug
    - InverterPhaseController (B): Feedback – regelt auf Phase B mit A/C-Offset
    - Jeder Controller liefert einen Korrekturwert inkl. Oszillations-Limit

ZeroFeedManager:
    - Addiert alle Korrekturen
    - Appliziert Gesamt-Oszillationslimit
    - Batterie-Grenzen werden ganz am Ende im Control-Loop angewandt
"""

import asyncio
import logging
from dataclasses import dataclass, field
from math import ceil
from typing import Optional, Protocol

from .csv_logger import ControlLogEntry, SampleLogEntry, ZeroFeedCSVLogger
from .oscillation_detectorv2 import BaseloadHolderSettings, BaseloadPredictorSettings
from .phase_controller import (
    InverterPhaseController,
    InverterPhaseControllerSettings,
    PhaseController,
    PhaseControllerSettings,
    PhaseSample,
    ZeroFeedManager,
    ZeroFeedManagerSettings,
)

logger = logging.getLogger(__name__)


# ── Protocols für lose Kopplung ──────────────────────────────────────────────


class GridMeter(Protocol):
    """Protocol für Netz-Messung (Shelly oder Simulator)."""

    async def get_phase_powers(self) -> Optional[tuple[float, float, float]]:
        """Liefert (phase_a, phase_b, phase_c) in Watt. Positiv = Bezug."""
        ...

    async def get_total_power(self) -> Optional[float]:
        """Liefert Gesamt-Netzleistung in Watt. Positiv = Bezug."""
        ...


class BatteryInverter(Protocol):
    """Protocol für Batterie-Steuerung (Zendure oder Mock)."""

    async def get_ac_output_power(self) -> Optional[int]:
        """Aktuelle Ausgangsleistung."""
        ...

    async def set_ac_output_limit(self, limit_w: int) -> bool:
        """Setzt Output-Limit."""
        ...

    async def start_discharge(self, power_w: int) -> bool:
        """Startet Entladung."""
        ...

    async def start_charge(self, power_w: int) -> bool:
        """Startet Laden vom Netz."""
        ...

    async def stop(self) -> bool:
        """Stoppt den Inverter."""
        ...

    async def get_ac_output_limit(self) -> Optional[int]:
        """Aktuelles Output-Limit."""
        ...

    async def get_ac_input_limit(self) -> Optional[int]:
        """Aktuelles Input-Limit."""
        ...

    async def is_settled(self, *, use_cache: bool = True) -> Optional[bool]:
        """True wenn Inverter-Output nahe am gesetzten Setpoint."""
        ...

    async def get_bypass_state(self, *, use_cache: bool = True) -> Optional[bool]:
        """Liest den Bypass-Zustand."""
        ...

    async def disable_bypass(self, *, smart_mode: bool = True) -> bool:
        """Deaktiviert Bypass per passMode."""
        ...

    async def get_battery_discharge_power(self, *, use_cache: bool = True) -> Optional[int]:
        """Liest echte Batterie-Entladeleistung (pack_input_power)."""
        ...


# ── Helper ────────────────────────────────────────────────────────────────────


async def _check_bypass(battery: BatteryInverter) -> Optional[bool]:
    """Liest Bypass-Status vom Inverter wenn unterstützt.

    Returns:
        True = Bypass aktiv, False = kein Bypass, None = nicht abfragbar
    """
    try:
        return await battery.get_bypass_state(use_cache=False)
    except Exception:
        logger.debug("_check_bypass: Fehler beim Lesen des Bypass-Status", exc_info=True)
        return None


async def _read_battery_discharge_power(battery: BatteryInverter) -> Optional[int]:
    """Liest tatsächliche Batterie-Entladeleistung (pack_input_power)."""
    try:
        return await battery.get_battery_discharge_power(use_cache=False)
    except Exception:
        logger.debug("_read_battery_discharge_power: Fehler", exc_info=True)
        return None


# ── Datenklassen ─────────────────────────────────────────────────────────────


@dataclass
class GridSample:
    """Ein Sample mit allen 3 Phasen + Batterie-Output."""

    timestamp: float
    phase_a: float
    phase_b: float
    phase_c: float
    battery_output: float

    @property
    def total_grid(self) -> float:
        return self.phase_a + self.phase_b + self.phase_c

    @property
    def real_consumption(self) -> float:
        """Realer Verbrauch = Grid + Batterie-Output."""
        return self.total_grid + self.battery_output


# ── Settings ─────────────────────────────────────────────────────────────────


@dataclass
class ZeroFeedV3Settings:
    """Einstellungen für den Zero-Feed V3 Controller."""

    # Manager
    manager: ZeroFeedManagerSettings = field(default_factory=ZeroFeedManagerSettings)

    # Pro-Phase Controller
    phase_controller: PhaseControllerSettings = field(default_factory=PhaseControllerSettings)
    inverter_controller: InverterPhaseControllerSettings = field(
        default_factory=InverterPhaseControllerSettings
    )

    # Oszillationserkennung (pro Phase)
    # Standardmäßig deaktiviert (None). Nur aktivieren wenn tatsächlich
    # oszillierende Lasten vorhanden sind (z.B. Waschmaschine, Klimaanlage).
    holder_settings: Optional[BaseloadHolderSettings] = None
    predictor_settings: Optional[BaseloadPredictorSettings] = None

    # Separate Oszillationserkennung für Phasen A+C (Feedforward) und Phase B (Feedback).
    # Überschreibt holder_settings / predictor_settings wenn gesetzt.
    holder_settings_ac: Optional[BaseloadHolderSettings] = None
    """Oszillationserkennung für Phase A und C (Feedforward). None = deaktiviert."""

    holder_settings_b: Optional[BaseloadHolderSettings] = None
    """Oszillationserkennung für Phase B (Feedback). None = deaktiviert."""

    # Timing
    sampling_interval: float = 1.0
    """Sampling-Intervall in Sekunden (Shelly-Abfrage)"""

    control_interval_s: float = 3.0
    """Regelzyklus in Sekunden"""

    continuous_feed_in_reset_s: float = 10.0
    """Ab dieser Dauer (s) kontinuierlicher Einspeisung wird der Regler zurückgesetzt."""

    continuous_feed_in_reset_cooldown_s: float = 20.0
    """Mindestabstand (s) zwischen zwei Watchdog-Resets."""

    def get_sample_queue_size(self) -> int:
        """Queue-Größe = Samples pro Regelzyklus + Puffer."""
        return ceil(self.control_interval_s / self.sampling_interval) + 1


# ── Controller ───────────────────────────────────────────────────────────────


class ZeroFeedV3Controller:
    """Zero-Feed V3 Controller – phasen-bewusste Nulleinspeisung.

    Zwei asyncio Tasks:
      - Sampling-Task: schnell (~1s), liest 3 Phasen, Oszillationserkennung
      - Control-Task: langsam (~3s), Phase-Controller, Batterie-Setpoint

    Oszillationserkennung pro Phase:
      - Holder: schnelle Schwingungen → halte Grundlast
      - Predictor: periodische Lasten → vorausschauend reduzieren
      → Limits werden auf den Batterie-Setpoint angewandt
    """

    def __init__(
        self,
        settings: ZeroFeedV3Settings,
        grid_meter: GridMeter,
        battery: BatteryInverter,
        csv_logger: Optional[ZeroFeedCSVLogger] = None,
    ):
        self.settings = settings
        self.grid_meter = grid_meter
        self.battery = battery
        self._csv_logger = csv_logger

        self.manager = self._create_manager()
        logger.info(
            "ZeroFeedV3Controller initialisiert: feedback_enabled=%s  osc_ac=%s  osc_b=%s",
            settings.inverter_controller.feedback_enabled,
            settings.holder_settings_ac is not None or settings.holder_settings is not None,
            settings.holder_settings_b is not None or settings.holder_settings is not None,
        )

        # Sample Queue
        self._sample_queue: asyncio.Queue[GridSample] = asyncio.Queue(
            maxsize=settings.get_sample_queue_size()
        )

        # State
        self._current_output_limit: int = 0
        self._last_set_time: float = 0.0
        self._running: bool = False
        self._sampling_task: Optional[asyncio.Task] = None
        self._control_task: Optional[asyncio.Task] = None
        self._last_sample: Optional[GridSample] = None  # für Logging

        # Watchdog: kontinuierliche Einspeisung außerhalb Hysterese
        self._feed_in_violation_since: Optional[float] = None
        self._last_watchdog_reset_ts: float = float("-inf")

    def _create_manager(self) -> ZeroFeedManager:
        """Erzeugt einen frischen Manager inkl. neuer Controller-Instanzen."""
        _holder_ac = (
            self.settings.holder_settings_ac
            if self.settings.holder_settings_ac is not None
            else self.settings.holder_settings
        )
        _holder_b = (
            self.settings.holder_settings_b
            if self.settings.holder_settings_b is not None
            else self.settings.holder_settings
        )

        phase_a = PhaseController(
            settings=self.settings.phase_controller,
            holder_settings=_holder_ac,
            predictor_settings=self.settings.predictor_settings,
        )
        phase_b = InverterPhaseController(
            settings=self.settings.inverter_controller,
            holder_settings=_holder_b,
            predictor_settings=self.settings.predictor_settings,
        )
        phase_c = PhaseController(
            settings=self.settings.phase_controller,
            holder_settings=_holder_ac,
            predictor_settings=self.settings.predictor_settings,
        )
        return ZeroFeedManager(
            manager_settings=self.settings.manager,
            phase_a=phase_a,
            phase_b=phase_b,
            phase_c=phase_c,
            total_holder_settings=self.settings.holder_settings,
            total_predictor_settings=self.settings.predictor_settings,
        )

    def _clear_sample_queue(self) -> None:
        """Leert die Sample-Queue vollständig."""
        while not self._sample_queue.empty():
            try:
                self._sample_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _is_export_outside_hysteresis(self, sample: GridSample) -> bool:
        """True wenn Gesamtleistung kontinuierlich unterhalb der Hysterese liegt."""
        hyst = max(
            self.settings.phase_controller.hysteresis_w,
            self.settings.inverter_controller.hysteresis_w,
        )
        lower_bound = self.settings.manager.target_power_w - hyst
        return sample.total_grid < lower_bound

    async def _reset_stuck_regulator(self, sample: GridSample, duration_s: float) -> None:
        """Setzt den Reglerzustand zurück, wenn anhaltende Einspeisung erkannt wurde."""
        logger.warning(
            "Watchdog: %.1fs kontinuierliche Einspeisung außerhalb Hysterese erkannt "
            "(grid_total=%.1fW). Setze Reglerzustand zurück.",
            duration_s,
            sample.total_grid,
        )

        self.manager = self._create_manager()
        self._clear_sample_queue()
        self._feed_in_violation_since = None

        min_output = self.settings.manager.min_output_w
        if self._current_output_limit != min_output:
            success = await self.battery.set_ac_output_limit(min_output)
            if success:
                old = self._current_output_limit
                self._current_output_limit = min_output
                self._last_set_time = asyncio.get_event_loop().time()
                logger.warning(
                    "Watchdog-Reset: Setpoint %dW -> %dW (sicherer Neustart)",
                    old,
                    min_output,
                )
            else:
                logger.error(
                    "Watchdog-Reset: Rücksetzen auf min_output=%dW fehlgeschlagen",
                    min_output,
                )

    async def _update_feed_in_watchdog(self, sample: GridSample) -> None:
        """Überwacht anhaltende Einspeisung und triggert ggf. Regler-Reset."""
        if self.settings.continuous_feed_in_reset_s <= 0:
            return

        if not self._is_export_outside_hysteresis(sample):
            self._feed_in_violation_since = None
            return

        if self._feed_in_violation_since is None:
            self._feed_in_violation_since = sample.timestamp
            return

        duration_s = sample.timestamp - self._feed_in_violation_since
        if duration_s < self.settings.continuous_feed_in_reset_s:
            return

        if (
            sample.timestamp - self._last_watchdog_reset_ts
            < self.settings.continuous_feed_in_reset_cooldown_s
        ):
            return

        await self._reset_stuck_regulator(sample=sample, duration_s=duration_s)
        self._last_watchdog_reset_ts = sample.timestamp

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Startet den Controller (Sampling + Control Tasks)."""
        if self._running:
            logger.warning("Controller läuft bereits")
            return

        self._running = True
        min_output = self.settings.manager.min_output_w

        logger.info(
            "ZeroFeed V3: Starte Initialisierung (min_output=%dW, max_output=%dW, "
            "control_interval=%.1fs)...",
            min_output,
            self.settings.manager.max_output_w,
            self.settings.control_interval_s,
        )

        # ── Bypass deaktivieren wenn nötig ─────────────────────────────
        await self._ensure_bypass_inactive()

        # ── Prüfen ob Batterie bereits entlädt ────────────────────────
        # Maßgeblich ist pack_input_power (Zellen-Output), NICHT output_home_power
        # (das kann Solar-Bypass sein).  Bei laufender Entladung keinen neuen
        # Befehl senden – das würde den Inverter kurz auf 0 setzen.
        batt_discharge_w = await _read_battery_discharge_power(self.battery)
        if batt_discharge_w is not None and batt_discharge_w >= min_output:
            current_output = await self.battery.get_ac_output_limit() or min_output
            self._current_output_limit = current_output
            logger.info(
                "Batterie entlädt bereits: pack_input=%dW  output_limit=%dW – "
                "kein Neustart nötig, übernehme laufenden Setpoint",
                batt_discharge_w,
                current_output,
            )
        else:
            # Batterie ist inaktiv (Standby, Bypass, oder erst kurz gestartet) →
            # explizit starten und auf echten Battery-Output warten
            logger.info(
                "Batterie nicht aktiv (pack_input=%s W) – sende start_discharge(%dW)...",
                batt_discharge_w,
                min_output,
            )
            success = await self.battery.start_discharge(min_output)
            if not success:
                logger.warning("start_discharge-Befehl nicht bestätigt – starte trotzdem")

            # Warte bis echte Batterie-Energie fließt (pack_input_power > 0)
            # Nicht auf output_home_power warten – das könnte Solar-Bypass sein!
            timeout_s = int(self.settings.control_interval_s * 10)
            logger.info("Warte auf Batterie-Entladung (timeout=%ds)...", timeout_s)
            started = False
            for attempt in range(10):
                bypass = await _check_bypass(self.battery)
                if bypass:
                    logger.warning(
                        "Start-Check %d/10: Inverter im Bypass – warte auf Bypass-Ende...",
                        attempt + 1,
                    )
                    await asyncio.sleep(self.settings.control_interval_s)
                    continue

                pack_w = await _read_battery_discharge_power(self.battery)
                logger.debug(
                    "Start-Check %d/10: pack_input=%s W  bypass=%s",
                    attempt + 1,
                    pack_w,
                    bypass,
                )
                if pack_w is not None and pack_w >= min_output:
                    started = True
                    logger.info(
                        "Batterie bestätigt Entladung nach %d Check(s): pack_input=%dW",
                        attempt + 1,
                        pack_w,
                    )
                    break
                await asyncio.sleep(self.settings.control_interval_s)

            if not started:
                bypass_final = await _check_bypass(self.battery)
                if bypass_final:
                    logger.warning(
                        "Inverter ist nach %ds noch im Bypass. ZFI startet trotzdem, "
                        "Regelung greift sobald Bypass endet.",
                        timeout_s,
                    )
                else:
                    pack_w = await _read_battery_discharge_power(self.battery)
                    logger.warning(
                        "Batterie hat nach %ds kein pack_input >= %dW gemeldet "
                        "(aktuell: %s W). Mögliche Ursachen: SoC-Limit, "
                        "Batterie nicht verbunden, oder API-Verzögerung. "
                        "Starte Controller trotzdem.",
                        timeout_s,
                        min_output,
                        pack_w,
                    )
            self._current_output_limit = min_output

        self._last_set_time = asyncio.get_event_loop().time()
        self._sampling_task = asyncio.create_task(self._sampling_loop())
        self._control_task = asyncio.create_task(self._control_loop())
        logger.info("ZeroFeed V3 gestartet (setpoint=%dW)", self._current_output_limit)

    async def _ensure_bypass_inactive(self) -> bool:
        """Prüft Bypass-Status und versucht ihn zu deaktivieren.

        Returns:
            True wenn Bypass inaktiv (oder nicht prüfbar), False wenn Bypass noch aktiv.
        """
        bypass = await _check_bypass(self.battery)
        if bypass is None:
            logger.debug("_ensure_bypass_inactive: kein get_bypass_state – überspringe")
            return True
        if not bypass:
            logger.debug("_ensure_bypass_inactive: Bypass inaktiv – OK")
            return True

        logger.warning(
            "Inverter ist im BYPASS-Modus! PV-Energie direkt ans Haus, "
            "outputLimit-Befehle werden ignoriert. Versuche Bypass zu deaktivieren..."
        )

        success = await self.battery.disable_bypass()
        if not success:
            logger.error("Bypass-Deaktivierung fehlgeschlagen (API-Fehler)")
            return False

        for i in range(5):
            await asyncio.sleep(2.0)
            bypass = await _check_bypass(self.battery)
            logger.debug("Bypass-Check %d/5 nach disable: bypass=%s", i + 1, bypass)
            if bypass is False:
                logger.info("Bypass erfolgreich deaktiviert nach %ds", (i + 1) * 2)
                return True

        logger.error(
            "Bypass ist nach 10s noch aktiv. Controller startet trotzdem, "
            "Regelung greift erst wenn Bypass endet."
        )
        return False

    async def stop(self) -> None:
        """Stoppt den Controller."""
        if not self._running:
            return

        self._running = False
        tasks = [t for t in (self._sampling_task, self._control_task) if t]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
            except asyncio.TimeoutError:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.battery.stop()
        except Exception as e:
            logger.error("Fehler beim Stoppen: %s", e)

        if self._csv_logger is not None:
            self._csv_logger.close()

        logger.info("ZeroFeed V3 gestoppt")

    # ── Sample hinzufügen (für Simulation von außen) ──────────────────

    async def add_sample(self, sample: GridSample) -> None:
        """Fügt ein Sample in die Queue ein."""
        # Queue befüllen
        try:
            self._sample_queue.put_nowait(sample)
        except asyncio.QueueFull:
            try:
                self._sample_queue.get_nowait()
                self._sample_queue.put_nowait(sample)
            except asyncio.QueueEmpty:
                pass

        # Letztes Sample merken für Logging
        self._last_sample = sample

        # CSV-Logging: sample-Zeile schreiben
        if self._csv_logger is not None:
            pa = self.manager._phase_a
            pb = self.manager._phase_b
            pc = self.manager._phase_c
            self._csv_logger.log_sample(
                SampleLogEntry(
                    unix_ts=sample.timestamp,
                    phase_a_w=sample.phase_a,
                    phase_b_w=sample.phase_b,
                    phase_c_w=sample.phase_c,
                    battery_output_w=sample.battery_output,
                    osc_A_oscillating=pa.is_oscillating,
                    osc_A_limit_w=pa.get_osc_limit(),
                    osc_B_oscillating=pb.is_oscillating,
                    osc_B_limit_w=pb.get_osc_limit(),
                    osc_C_oscillating=pc.is_oscillating,
                    osc_C_limit_w=pc.get_osc_limit(),
                    osc_total_oscillating=self.manager.total_is_oscillating,
                    osc_total_limit_w=self.manager.get_total_osc_limit(),
                )
            )

    def needs_fast_recontrol(self, sample: GridSample, eps_w: float = 0.5) -> bool:
        """True wenn gehaltene Feedforward-Korrektur für aktuelles Sample zu hoch ist.

        Das verhindert kurze Einspeise-Spikes zwischen zwei regulären
        Regelzyklen, wenn die Last auf Phase A/C schnell fällt.
        """
        target = self.settings.manager.target_power_w
        max_a = max(sample.phase_a - target, 0.0)
        max_c = max(sample.phase_c - target, 0.0)

        a_too_high = self.manager._phase_a.last_output > max_a + eps_w
        c_too_high = self.manager._phase_c.last_output > max_c + eps_w
        return a_too_high or c_too_high

    # ── Regelung durchführen (für Simulation von außen) ───────────────

    async def perform_control(self) -> Optional[int]:
        """Führt einen Regelzyklus durch.

        Returns:
            Neuer Setpoint oder None wenn keine Änderung
        """
        if self._sample_queue.empty():
            return None

        # Samples aus Queue holen (pro Phase)
        phase_a_osc_samples: list[PhaseSample] = []
        phase_b_osc_samples: list[PhaseSample] = []
        phase_c_osc_samples: list[PhaseSample] = []
        total_osc_samples: list[PhaseSample] = []
        battery_output_history: list[float] = []
        last_battery_output: float = 0.0

        while not self._sample_queue.empty():
            try:
                sample = self._sample_queue.get_nowait()
                phase_a_osc_samples.append(
                    PhaseSample(timestamp=sample.timestamp, value=sample.phase_a)
                )
                phase_b_osc_samples.append(
                    PhaseSample(timestamp=sample.timestamp, value=sample.phase_b)
                )
                phase_c_osc_samples.append(
                    PhaseSample(timestamp=sample.timestamp, value=sample.phase_c)
                )
                total_osc_samples.append(
                    PhaseSample(timestamp=sample.timestamp, value=sample.real_consumption)
                )
                battery_output_history.append(sample.battery_output)
                if sample.battery_output >= 0:
                    last_battery_output = sample.battery_output
            except asyncio.QueueEmpty:
                break

        if not phase_b_osc_samples:
            return None

        # Settlement prüfen: Feedback-Regler nur aktualisieren wenn Inverter
        # am vorherigen Setpoint angekommen ist. None (Fehler) = settled annehmen.
        settled = await self.battery.is_settled(use_cache=False)
        battery_settled = settled is not False

        # WICHTIG: Frische Batterie-Ausgabe holen (nicht aus veralteten Samples).
        # Die Samples können bis zu control_interval_s alt sein – wenn die Batterie
        # gerade erst einen neuen Setpoint bekommen hat, wäre last_battery_output
        # noch der alte Wert und würde den Feedback-Regler destabilisieren.
        fresh_output = await self.battery.get_ac_output_power()
        current_battery_output_w = (
            float(fresh_output) if fresh_output is not None else last_battery_output
        )

        logger.debug(
            "perform_control: setpoint=%dW  settled=%s"
            "  batt_sample=%.0fW  batt_live=%.0fW  n_samples=%d"
            "  phase_b_last=%.0fW  phase_a_last=%.0fW  phase_c_last=%.0fW",
            self._current_output_limit,
            battery_settled,
            last_battery_output,
            current_battery_output_w,
            len(phase_b_osc_samples),
            phase_b_osc_samples[-1].value if phase_b_osc_samples else float("nan"),
            phase_a_osc_samples[-1].value if phase_a_osc_samples else float("nan"),
            phase_c_osc_samples[-1].value if phase_c_osc_samples else float("nan"),
        )

        # Manager berechnet Ziel-Leistung (ohne Batterie-Min/Max)
        target_output_w, dbg = self.manager.calculate_debug(
            phase_a_samples=phase_a_osc_samples,
            phase_b_samples=phase_b_osc_samples,
            phase_c_samples=phase_c_osc_samples,
            current_battery_output_w=current_battery_output_w,
            battery_settled=battery_settled,
            phase_battery_output_samples_w=battery_output_history,
            total_osc_samples=total_osc_samples,
        )
        osc_limit = dbg.osc_limit_w

        # Batterie-Grenzen ganz am Ende anwenden (kein Rate-Limiting)
        new_setpoint = int(
            round(
                max(
                    self.settings.manager.min_output_w,
                    min(self.settings.manager.max_output_w, target_output_w),
                )
            )
        )

        # Setpoint setzen wenn geändert
        changed = False
        if new_setpoint != self._current_output_limit:
            success = await self.battery.set_ac_output_limit(new_setpoint)
            if success:
                old = self._current_output_limit
                self._current_output_limit = new_setpoint
                self._last_set_time = asyncio.get_event_loop().time()
                changed = True
                logger.info(
                    "Setpoint: %dW -> %dW (D=%+dW, target=%.0fW, ff=%.0fW, fb=%.0fW, osc_limit=%.0fW)",
                    old,
                    new_setpoint,
                    new_setpoint - old,
                    target_output_w,
                    dbg.ff_output_w,
                    dbg.feedback_output_w,
                    osc_limit,
                )

        # CSV-Logging: control-Zeile schreiben
        if self._csv_logger is not None:
            import time as _time

            ctrl_ts = self._last_sample.timestamp if self._last_sample is not None else _time.time()
            self._csv_logger.log_control(
                ControlLogEntry(
                    unix_ts=ctrl_ts,
                    feedback_output_w=dbg.feedback_output_w,
                    ff_output_w=dbg.ff_output_w,
                    raw_setpoint_w=dbg.raw_setpoint_w,
                    osc_limit_w=osc_limit,
                    final_setpoint_w=new_setpoint,
                    setpoint_changed=changed,
                )
            )

        return new_setpoint if changed else None

    # ── Interne Loops ─────────────────────────────────────────────────

    async def _sampling_loop(self) -> None:
        """Schnelle Abfrage aller Phasen + Oszillationserkennung."""
        import time as time_mod

        logger.info("Sampling-Loop gestartet (%.1fs)", self.settings.sampling_interval)
        _bypass_check_interval = 30.0
        _last_bypass_check: float = 0.0
        _bypass_was_active: Optional[bool] = None
        try:
            last_time = time_mod.time()
            while self._running:
                sleep = max(
                    0.0,
                    self.settings.sampling_interval - (time_mod.time() - last_time),
                )
                await asyncio.sleep(sleep)
                last_time = time_mod.time()

                # Periodischer Bypass-Check (alle ~30s)
                if (last_time - _last_bypass_check) >= _bypass_check_interval:
                    _last_bypass_check = last_time
                    bypass_now = await _check_bypass(self.battery)
                    if bypass_now is not None and bypass_now != _bypass_was_active:
                        if bypass_now:
                            logger.warning(
                                "Sampling-Loop: Inverter wechselt in Bypass-Modus! "
                                "PV-Energie direkt ans Haus, Battery-Output = 0. "
                                "ZFI-Regelung ohne Wirkung bis Bypass endet."
                            )
                        elif _bypass_was_active:
                            logger.info("Sampling-Loop: Bypass deaktiviert – Battery-Control aktiv")
                        _bypass_was_active = bypass_now

                try:
                    phases = await self.grid_meter.get_phase_powers()
                    if phases is None:
                        logger.debug("Sampling: Grid-Meter lieferte keine Daten")
                        continue

                    phase_a, phase_b, phase_c = phases
                    output = await self.battery.get_ac_output_power() or 0

                    # Im Bypass-Modus ist output_home_power = Solar-Bypass, nicht Batterie.
                    # Wir loggen das, können aber nichts dagegen tun da pack_input_power
                    # der echte Battery-Output ist.
                    if _bypass_was_active:
                        batt_discharge = await self.battery.get_battery_discharge_power()
                        output = batt_discharge if batt_discharge is not None else 0

                    sample = GridSample(
                        timestamp=last_time,
                        phase_a=phase_a,
                        phase_b=phase_b,
                        phase_c=phase_c,
                        battery_output=float(output),
                    )
                    await self.add_sample(sample)

                    # Watchdog: falls die Regelung in anhaltender Einspeisung hängen bleibt,
                    # Reglerzustand und Queue zurücksetzen.
                    await self._update_feed_in_watchdog(sample)

                    # Zusätzlich zum festen Control-Takt sofort nachregeln,
                    # wenn A/C-Korrekturen für das aktuelle Sample zu hoch sind.
                    if self.needs_fast_recontrol(sample):
                        await self.perform_control()

                except Exception as e:
                    logger.error("Sampling-Fehler: %s", e, exc_info=True)
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Sampling-Loop beendet")

    async def _control_loop(self) -> None:
        """Langsame Regelung."""
        import time as time_mod

        logger.info("Control-Loop gestartet (%.1fs)", self.settings.control_interval_s)
        try:
            last_time = time_mod.time()
            while self._running:
                sleep = max(
                    0.0,
                    self.settings.control_interval_s - (time_mod.time() - last_time),
                )
                await asyncio.sleep(sleep)
                last_time = time_mod.time()

                try:
                    await self.perform_control()
                except Exception as e:
                    logger.error("Control-Fehler: %s", e, exc_info=True)
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Control-Loop beendet")

    # ── Properties ────────────────────────────────────────────────────

    @property
    def current_output_limit(self) -> int:
        return self._current_output_limit

    @property
    def is_oscillating(self) -> bool:
        return self.manager.is_oscillating

    @property
    def is_running(self) -> bool:
        return self._running
