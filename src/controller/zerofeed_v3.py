"""Zero-Feed Controller V3 – Phasen-bewusste Nulleinspeisung.
===========================================================

Architektur:
  1. Sampling-Loop (schnell, ~1s):
     - Liest alle 3 Phasen + Batterie-Output
     - Speist pro-Phasen Oszillationserkennung
     - Schreibt PhaseSamples in Queue

  2. Control-Loop (langsam, ~3s):
     - Liest PhaseSamples aus Queue
     - Phase-Controller: Feedforward (A+C) + Feedback (total)
     - Oszillations-Limits begrenzen maximalen Setpoint
     - Setzt Batterie-Output

Oszillationserkennung:
  - Pro Phase: BaseloadHolder (schnelle Schwingungen < 10s)
  - Pro Phase: BaseloadPredictor (langsame periodische Lasten 8-120s)
  - Liefern Limits: wenn Lastwechsel schneller als Totzeit → Grundlast halten
  - Laufen mit Sampling-Rate für schnelle Erkennung

Verbesserung gegenüber V1/V2:
  - Phasen-getrennte Regelung (nicht nur Summe)
  - Feedforward auf Fremdphasen beschleunigt Reaktion
  - Pro-Phase Oszillationserkennung (genauer als Summen-Erkennung)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from math import ceil
from typing import Optional, Protocol

from .csv_logger import ControlLogEntry, SampleLogEntry, ZeroFeedCSVLogger
from .oscillation_detectorv2 import (
    BaseloadHolder,
    BaseloadHolderSettings,
    BaseloadPredictor,
    BaseloadPredictorSettings,
)
from .phase_controllers import (
    BatteryPhaseControllerSettings,
    DisturbanceControllerSettings,
    PhaseManager,
    PhaseManagerSettings,
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
    def other_phases(self) -> float:
        """Summe der Fremdphasen (A + C)."""
        return self.phase_a + self.phase_c

    @property
    def real_consumption(self) -> float:
        """Realer Verbrauch = Grid + Batterie-Output."""
        return self.total_grid + self.battery_output


@dataclass
class PerPhaseOscillationDetectors:
    """Oszillationsdetektoren für eine einzelne Phase."""

    holder: Optional[BaseloadHolder] = None
    predictor: Optional[BaseloadPredictor] = None

    @property
    def is_oscillating(self) -> bool:
        return (self.holder is not None and self.holder.is_oscillating) or (
            self.predictor is not None and self.predictor.is_oscillating
        )

    def get_limit(self) -> float:
        """Minimales Limit aller aktiven Detektoren."""
        limits = []
        if self.holder and self.holder.is_oscillating:
            limits.append(self.holder.get_limit())
        if self.predictor and self.predictor.is_oscillating:
            limits.append(self.predictor.get_limit())
        return min(limits) if limits else float("inf")

    def add_sample(self, value: float, timestamp: float) -> None:
        if value > 0:
            if self.holder:
                self.holder.add_sample(value, timestamp)
            if self.predictor:
                self.predictor.add_sample(value, timestamp)


# ── Settings ─────────────────────────────────────────────────────────────────


@dataclass
class ZeroFeedV3Settings:
    """Einstellungen für den Zero-Feed V3 Controller."""

    # Phase Manager
    manager: PhaseManagerSettings = field(default_factory=PhaseManagerSettings)

    # Sub-Controller
    disturbance: DisturbanceControllerSettings = field(
        default_factory=DisturbanceControllerSettings
    )
    battery_phase: BatteryPhaseControllerSettings = field(
        default_factory=BatteryPhaseControllerSettings
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

    # Fallbacks
    base_load_w: float = 30.0
    """Basis-Last Fallback in Watt"""

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

        # Phase Manager (enthält Feedforward + Feedback Controller)
        self.phase_manager = PhaseManager(
            manager_settings=settings.manager,
            disturbance_settings=settings.disturbance,
            battery_phase_settings=settings.battery_phase,
        )

        # Pro-Phase Oszillationsdetektoren
        self._phase_detectors: dict[str, PerPhaseOscillationDetectors] = {}
        for phase_name in ("A", "B", "C"):
            self._phase_detectors[phase_name] = PerPhaseOscillationDetectors(
                holder=BaseloadHolder(settings.holder_settings)
                if settings.holder_settings
                else None,
                predictor=BaseloadPredictor(settings.predictor_settings)
                if settings.predictor_settings
                else None,
            )

        # Gesamt-Oszillationsdetektoren (auf Summe)
        self._total_detectors = PerPhaseOscillationDetectors(
            holder=BaseloadHolder(settings.holder_settings) if settings.holder_settings else None,
            predictor=BaseloadPredictor(settings.predictor_settings)
            if settings.predictor_settings
            else None,
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

        # Pro-Phase Oszillationserkennung (läuft mit Sampling-Rate!)
        real_a = sample.phase_a  # Phase A hat keine Batterie
        real_c = sample.phase_c  # Phase C hat keine Batterie
        real_b = sample.phase_b + sample.battery_output  # Phase B hat Batterie

        self._phase_detectors["A"].add_sample(real_a, sample.timestamp)
        self._phase_detectors["B"].add_sample(real_b, sample.timestamp)
        self._phase_detectors["C"].add_sample(real_c, sample.timestamp)

        # Gesamt-Oszillationserkennung
        real_total = sample.real_consumption
        self._total_detectors.add_sample(real_total, sample.timestamp)

        # Letztes Sample merken für Logging
        self._last_sample = sample

        # CSV-Logging: sample-Zeile schreiben
        if self._csv_logger is not None:
            det = self._phase_detectors
            tot = self._total_detectors
            self._csv_logger.log_sample(
                SampleLogEntry(
                    unix_ts=sample.timestamp,
                    phase_a_w=sample.phase_a,
                    phase_b_w=sample.phase_b,
                    phase_c_w=sample.phase_c,
                    battery_output_w=sample.battery_output,
                    osc_A_oscillating=det["A"].is_oscillating,
                    osc_A_limit_w=det["A"].get_limit(),
                    osc_B_oscillating=det["B"].is_oscillating,
                    osc_B_limit_w=det["B"].get_limit(),
                    osc_C_oscillating=det["C"].is_oscillating,
                    osc_C_limit_w=det["C"].get_limit(),
                    osc_total_oscillating=tot.is_oscillating,
                    osc_total_limit_w=tot.get_limit(),
                )
            )

    # ── Regelung durchführen (für Simulation von außen) ───────────────

    async def perform_control(self) -> Optional[int]:
        """Führt einen Regelzyklus durch.

        Returns:
            Neuer Setpoint oder None wenn keine Änderung
        """
        if self._sample_queue.empty():
            return None

        # Samples aus Queue holen
        total_grid_history: list[float] = []
        other_phases_history: list[float] = []
        last_battery_output: float = 0.0

        while not self._sample_queue.empty():
            try:
                sample = self._sample_queue.get_nowait()
                total_grid_history.append(sample.total_grid)
                other_phases_history.append(sample.other_phases)
                if sample.battery_output >= 0:
                    last_battery_output = sample.battery_output
            except asyncio.QueueEmpty:
                break

        if not total_grid_history:
            return None

        # Oszillations-Limit berechnen (Minimum aller Detektoren)
        osc_limit = float(self.settings.manager.max_output_w)
        for name, det in self._phase_detectors.items():
            if det.is_oscillating:
                phase_limit = det.get_limit()
                osc_limit = min(osc_limit, phase_limit)
                logger.debug("Phase %s oszilliert, limit=%.0fW", name, phase_limit)

        if self._total_detectors.is_oscillating:
            total_limit = self._total_detectors.get_limit()
            osc_limit = min(osc_limit, total_limit)
            logger.debug("Total oszilliert, limit=%.0fW", total_limit)

        # Settlement prüfen: Feedback-Regler nur aktualisieren wenn Inverter
        # am vorherigen Setpoint angekommen ist. None (Fehler) = settled annehmen.
        settled = await self.battery.is_settled(use_cache=True)
        battery_settled = settled is not False

        # Phase Manager berechnet Setpoint + Zwischenwerte
        raw_setpoint, dbg = self.phase_manager.calculate_debug(
            total_grid_power_w=total_grid_history,
            other_phases_power_w=other_phases_history,
            current_battery_output_w=last_battery_output,
            battery_settled=battery_settled,
        )

        # Oszillations-Limit anwenden
        new_setpoint = min(raw_setpoint, int(osc_limit))
        new_setpoint = max(self.settings.manager.min_output_w, new_setpoint)

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
                    "Setpoint: %dW → %dW (Δ=%+dW, osc_limit=%.0fW)",
                    old,
                    new_setpoint,
                    new_setpoint - old,
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
        return (
            any(d.is_oscillating for d in self._phase_detectors.values())
            or self._total_detectors.is_oscillating
        )

    @property
    def is_running(self) -> bool:
        return self._running
