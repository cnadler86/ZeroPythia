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


# ── Datenklassen ─────────────────────────────────────────────────────────────


@dataclass
class PhaseSample:
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
    holder_settings: Optional[BaseloadHolderSettings] = field(
        default_factory=BaseloadHolderSettings
    )
    predictor_settings: Optional[BaseloadPredictorSettings] = field(
        default_factory=BaseloadPredictorSettings
    )

    # Timing
    sampling_interval: float = 1.0
    """Sampling-Intervall in Sekunden (Shelly-Abfrage)"""

    control_interval_s: float = 3.0
    """Regelzyklus in Sekunden"""

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

        # Gekapselte Phasen-Controller + Manager
        phase_a = PhaseController(
            settings=settings.phase_controller,
            holder_settings=settings.holder_settings,
            predictor_settings=settings.predictor_settings,
        )
        phase_b = InverterPhaseController(
            settings=settings.inverter_controller,
            holder_settings=settings.holder_settings,
            predictor_settings=settings.predictor_settings,
        )
        phase_c = PhaseController(
            settings=settings.phase_controller,
            holder_settings=settings.holder_settings,
            predictor_settings=settings.predictor_settings,
        )
        self.manager = ZeroFeedManager(
            manager_settings=settings.manager,
            phase_a=phase_a,
            phase_b=phase_b,
            phase_c=phase_c,
            total_holder_settings=settings.holder_settings,
            total_predictor_settings=settings.predictor_settings,
        )

        # Sample Queue
        self._sample_queue: asyncio.Queue[PhaseSample] = asyncio.Queue(
            maxsize=settings.get_sample_queue_size()
        )

        # State
        self._current_output_limit: int = 0
        self._last_set_time: float = 0.0
        self._running: bool = False
        self._sampling_task: Optional[asyncio.Task] = None
        self._control_task: Optional[asyncio.Task] = None
        self._last_sample: Optional[PhaseSample] = None  # für Logging

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Startet den Controller (Sampling + Control Tasks)."""
        if self._running:
            logger.warning("Controller läuft bereits")
            return

        self._running = True

        # Batterie starten
        min_output = self.settings.manager.min_output_w
        await self.battery.start_discharge(min_output)

        # Warte auf tatsächlichen Start
        for _ in range(10):
            power = await self.battery.get_ac_output_power()
            if power is not None and power >= min_output:
                break
            await asyncio.sleep(self.settings.control_interval_s)

        self._current_output_limit = min_output
        self._last_set_time = asyncio.get_event_loop().time()

        self._sampling_task = asyncio.create_task(self._sampling_loop())
        self._control_task = asyncio.create_task(self._control_loop())
        logger.info("ZeroFeed V3 gestartet (min=%dW)", min_output)

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

    async def add_sample(self, sample: PhaseSample) -> None:
        """Fügt ein Sample hinzu und führt Oszillationserkennung durch."""
        # Queue befüllen
        try:
            self._sample_queue.put_nowait(sample)
        except asyncio.QueueFull:
            try:
                self._sample_queue.get_nowait()
                self._sample_queue.put_nowait(sample)
            except asyncio.QueueEmpty:
                pass

        # Pro-Phase Oszillationserkennung (gekapselt in Controllern)
        self.manager._phase_a.add_sample(sample.phase_a, sample.timestamp)
        self.manager._phase_b.add_sample(
            sample.phase_b,
            sample.battery_output,
            self.manager.last_feedforward_output_w,
            sample.timestamp,
        )
        self.manager._phase_c.add_sample(sample.phase_c, sample.timestamp)

        # Gesamt-Oszillationserkennung (im Manager)
        self.manager.add_total_sample(sample.real_consumption, sample.timestamp)

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

    def needs_fast_recontrol(self, sample: PhaseSample, eps_w: float = 0.5) -> bool:
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
        phase_a_history: list[float] = []
        phase_b_history: list[float] = []
        phase_c_history: list[float] = []
        last_battery_output: float = 0.0

        while not self._sample_queue.empty():
            try:
                sample = self._sample_queue.get_nowait()
                phase_a_history.append(sample.phase_a)
                phase_b_history.append(sample.phase_b)
                phase_c_history.append(sample.phase_c)
                if sample.battery_output >= 0:
                    last_battery_output = sample.battery_output
            except asyncio.QueueEmpty:
                break

        if not phase_b_history:
            return None

        # Settlement prüfen: Feedback-Regler nur aktualisieren wenn Inverter
        # am vorherigen Setpoint angekommen ist. None (Fehler) = settled annehmen.
        settled = await self.battery.is_settled(use_cache=False)
        battery_settled = settled is not False

        # Manager berechnet Ziel-Leistung (ohne Batterie-Min/Max)
        target_output_w, dbg = self.manager.calculate_debug(
            phase_a_power_w=phase_a_history,
            phase_b_power_w=phase_b_history,
            phase_c_power_w=phase_c_history,
            current_battery_output_w=last_battery_output,
            battery_settled=battery_settled,
        )
        osc_limit = dbg.osc_limit_w

        # Batterie-Grenzen ganz am Ende anwenden
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
                    "Setpoint: %dW → %dW (Δ=%+dW, target=%.0fW, osc_limit=%.0fW)",
                    old,
                    new_setpoint,
                    new_setpoint - old,
                    target_output_w,
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
        try:
            last_time = time_mod.time()
            while self._running:
                sleep = max(
                    0.0,
                    self.settings.sampling_interval - (time_mod.time() - last_time),
                )
                await asyncio.sleep(sleep)
                last_time = time_mod.time()

                try:
                    phases = await self.grid_meter.get_phase_powers()
                    if phases is None:
                        continue

                    phase_a, phase_b, phase_c = phases
                    output = await self.battery.get_ac_output_power() or 0

                    sample = PhaseSample(
                        timestamp=last_time,
                        phase_a=phase_a,
                        phase_b=phase_b,
                        phase_c=phase_c,
                        battery_output=float(output),
                    )
                    await self.add_sample(sample)

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
