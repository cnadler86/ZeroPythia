"""ControlRuntime – central sampling and control loop.

Single source of truth for hardware data and operating state.

Architecture
------------
One asyncio task runs the main loop (~1 s):
  1. Read Shelly 3EM → phase powers
  2. Read Zendure → output, SoC
  3. Build GridSample (shared data, single poll per tick)
  4. Forward sample to the active regulator
  5. Every ``control_interval_s``: call regulator.compute_setpoint()
  6. Broadcast DashboardState to all registered callbacks (WebSocket clients)

Device modes
------------
AC_CHARGE            → stop regulator, call battery.start_charge(power_w)
IDLE                 → stop regulator, call battery.stop()
DISCHARGE_ZERO_FEED  → run active regulator loop

Regulator registry
------------------
Any number of ``RegulatorBase`` implementations can be registered.
Only one is active at a time.  Switching resets the incoming regulator's
internal state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from .models import (
    AutoStatus,
    DashboardState,
    DeviceMode,
    GridSample,
    RegulatorInfo,
)
from .regulator import BatteryInverterProtocol, RegulatorBase

logger = logging.getLogger(__name__)


# ── Protocols (mirrors clients) ───────────────────────────────────────────────


class GridMeterProtocol:
    """Structural protocol for Shelly 3EM (or simulator)."""

    async def get_phase_powers(self) -> Optional[tuple[float, float, float]]: ...
    async def get_total_power(self) -> Optional[float]: ...


# ── Type alias ────────────────────────────────────────────────────────────────

StateCallback = Callable[[DashboardState], Awaitable[None]]


# ── Runtime ───────────────────────────────────────────────────────────────────


class ControlRuntime:
    """Central sampling + control runtime for the dashboard server.

    Parameters
    ----------
    grid_meter:
        Shelly 3EM client (or compatible simulator).
    battery:
        Zendure SolarFlow client (or compatible mock).
    sampling_interval_s:
        How often Shelly + Zendure are polled [s].
    control_interval_s:
        How often the active regulator's ``compute_setpoint()`` is called [s].
    max_discharge_w:
        Initial discharge cap [W].
    min_discharge_w:
        Minimum allowed battery output [W].
    """

    def __init__(
        self,
        grid_meter: GridMeterProtocol,
        battery: BatteryInverterProtocol,
        *,
        sampling_interval_s: float = 1.0,
        control_interval_s: float = 3.0,
        max_discharge_w: int = 800,
        min_discharge_w: int = 20,
    ) -> None:
        self._grid_meter = grid_meter
        self._battery = battery
        self._sampling_interval = sampling_interval_s
        self._control_interval = control_interval_s
        self._max_discharge_w = max_discharge_w
        self._min_discharge_w = min_discharge_w

        # Regulator registry
        self._regulators: dict[str, RegulatorBase] = {}
        self._active_regulator: Optional[RegulatorBase] = None

        # Operating mode
        self._mode: DeviceMode = DeviceMode.IDLE
        self._charge_power_w: Optional[int] = None

        # AUTO mode: effective mode used for sample routing and control
        self._auto_effective_mode: DeviceMode = DeviceMode.IDLE
        self._auto_manager: Optional[Any] = None  # AutoModeManager (avoid circular import)

        # State snapshot (updated every sample tick)
        self._state: DashboardState = DashboardState(
            timestamp=time.time(),
            mode=self._mode,
            max_discharge_w=self._max_discharge_w,
        )

        # WebSocket broadcast callbacks
        self._callbacks: list[StateCallback] = []

        # Runtime tasks
        self._running = False
        self._main_task: Optional[asyncio.Task] = None

    def attach_auto_mode_manager(self, manager: Any) -> None:
        """Register the AutoModeManager used when mode==AUTO."""
        self._auto_manager = manager

    async def enable_auto_mode(
        self,
        mqtt_broker: str,
        device_id: str,
        topic_prefix: str = "gridpythia",
        status_interval_s: float = 60.0,
    ) -> None:
        """Create an AutoModeManager, attach it, and switch to AUTO mode."""
        from .auto_mode import AutoModeManager  # late import – avoids circular dep

        # Stop any existing manager first
        if self._auto_manager is not None:
            await self._auto_manager.stop_reporter_task()
            self._auto_manager.stop()

        manager = AutoModeManager(
            mqtt_broker=mqtt_broker,
            device_id=device_id,
            battery=self._battery,
            config_max_w=self._max_discharge_w,
            config_min_w=self._min_discharge_w,
            topic_prefix=topic_prefix,
            status_interval_s=status_interval_s,
        )
        manager.start()
        await manager.start_reporter_task()
        self.attach_auto_mode_manager(manager)
        await self.set_mode(DeviceMode.AUTO)

    async def disable_auto_mode(self) -> None:
        """Stop the AutoModeManager and switch back to IDLE."""
        if self._auto_manager is not None:
            await self._auto_manager.stop_reporter_task()
            self._auto_manager.stop()
            self._auto_manager = None
        await self.set_mode(DeviceMode.IDLE)

    # ── Regulator registry ────────────────────────────────────────────────────

    def register_regulator(self, regulator: RegulatorBase) -> None:
        """Register a regulator. First registered is set as default active."""
        self._regulators[regulator.name] = regulator
        if self._active_regulator is None:
            self._active_regulator = regulator
            logger.info("Default regulator: %s", regulator.name)

    def list_regulators(self) -> list[RegulatorInfo]:
        """Return metadata for all registered regulators."""
        result = []
        for reg in self._regulators.values():
            result.append(
                RegulatorInfo(
                    name=reg.name,
                    description=reg.description,
                    is_active=(reg is self._active_regulator),
                    settings_schema=reg.settings_schema(),
                    current_settings=reg.get_current_settings(),
                )
            )
        return result

    # ── Mode / regulator control ──────────────────────────────────────────────

    async def set_mode(
        self,
        mode: DeviceMode,
        *,
        charge_power_w: Optional[int] = None,
        max_discharge_w: Optional[int] = None,
    ) -> None:
        """Switch operating mode.  Applies battery command immediately."""
        logger.info(
            "Mode change: %s → %s (charge_w=%s  max_dis_w=%s)",
            self._mode.value,
            mode.value,
            charge_power_w,
            max_discharge_w,
        )

        if max_discharge_w is not None:
            self._max_discharge_w = max_discharge_w

        if mode == DeviceMode.AC_CHARGE:
            pw = charge_power_w or self._charge_power_w or 400
            self._charge_power_w = pw
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.start_charge(pw)

        elif mode == DeviceMode.IDLE:
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.stop()

        elif mode == DeviceMode.DISCHARGE_ZERO_FEED:
            self._charge_power_w = None
            if self._active_regulator:
                self._active_regulator.reset()
            # The regulator loop will start the battery on its first compute_setpoint call.
            await self._battery.start_discharge(self._min_discharge_w)

        elif mode == DeviceMode.AUTO:
            # AUTO: don't touch the battery yet – the AutoModeManager drives it
            if self._active_regulator:
                self._active_regulator.reset()
            self._auto_effective_mode = DeviceMode.IDLE

        self._mode = mode
        self._update_state()

    async def apply_effective_mode(
        self,
        mode: DeviceMode,
        charge_power_w: Optional[int] = None,
        max_discharge_w: Optional[int] = None,
    ) -> None:
        """Apply an effective device mode while staying in AUTO. Called by AutoModeManager."""
        if max_discharge_w is not None:
            self._max_discharge_w = max_discharge_w

        if mode == DeviceMode.AC_CHARGE:
            pw = charge_power_w or 400
            self._charge_power_w = pw
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.start_charge(pw)

        elif mode == DeviceMode.IDLE:
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.stop()

        elif mode == DeviceMode.DISCHARGE_ZERO_FEED:
            self._charge_power_w = None
            if self._auto_effective_mode != DeviceMode.DISCHARGE_ZERO_FEED:
                # Only reset/restart when actually switching into this mode
                if self._active_regulator:
                    self._active_regulator.reset()
                await self._battery.start_discharge(self._min_discharge_w)

        self._auto_effective_mode = mode

    async def set_active_regulator(self, name: str) -> None:
        """Switch the active regulator by name (resets its state)."""
        if name not in self._regulators:
            raise ValueError(f"Unknown regulator: {name!r}")
        reg = self._regulators[name]
        reg.reset()
        self._active_regulator = reg
        logger.info("Active regulator → %s", name)
        self._update_state()

    async def update_regulator_settings(self, name: str, data: dict[str, Any]) -> None:
        """Apply settings to a (possibly non-active) regulator."""
        if name not in self._regulators:
            raise ValueError(f"Unknown regulator: {name!r}")
        self._regulators[name].apply_settings(data)
        logger.info("Settings updated for regulator %s", name)

    # ── State access ──────────────────────────────────────────────────────────

    def get_state(self) -> DashboardState:
        return self._state

    def add_state_callback(self, callback: StateCallback) -> None:
        """Register a coroutine to be called after every state update."""
        self._callbacks.append(callback)

    def remove_state_callback(self, callback: StateCallback) -> None:
        self._callbacks.remove(callback)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._main_task = asyncio.create_task(self._main_loop(), name="control-runtime")
        logger.info(
            "ControlRuntime started (sample=%.1fs  control=%.1fs)",
            self._sampling_interval,
            self._control_interval,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
        logger.info("ControlRuntime stopped")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        last_control = 0.0
        try:
            while self._running:
                tick_start = time.monotonic()

                # 1. Sample hardware
                sample = await self._read_sample()

                # 2. Forward to regulator when in DISCHARGE_ZERO_FEED mode
                #    (either directly or as effective mode under AUTO)
                _discharge_active = (
                    self._mode == DeviceMode.DISCHARGE_ZERO_FEED
                    or (
                        self._mode == DeviceMode.AUTO
                        and self._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED
                    )
                )
                if sample is not None and _discharge_active and self._active_regulator is not None:
                    await self._active_regulator.add_sample(sample)

                # 3. Control tick
                now = time.monotonic()
                if (
                    _discharge_active
                    and self._active_regulator is not None
                    and (now - last_control) >= self._control_interval
                ):
                    try:
                        await self._active_regulator.compute_setpoint(
                            self._battery,
                            self._max_discharge_w,
                            self._min_discharge_w,
                        )
                    except Exception:
                        logger.exception("Regulator compute_setpoint failed")
                    last_control = time.monotonic()

                    # AUTO: tick plan dispatcher on the same interval
                    if self._mode == DeviceMode.AUTO and self._auto_manager is not None:
                        try:
                            await self._auto_manager.tick(self.apply_effective_mode)
                        except Exception:
                            logger.exception("AutoModeManager tick failed")

                elif (
                    self._mode == DeviceMode.AUTO
                    and self._auto_manager is not None
                    and not _discharge_active
                    and (now - last_control) >= self._control_interval
                ):
                    # Also tick in non-discharge AUTO sub-modes (IDLE, AC_CHARGE)
                    try:
                        await self._auto_manager.tick(self.apply_effective_mode)
                    except Exception:
                        logger.exception("AutoModeManager tick failed")
                    last_control = time.monotonic()

                # 4. Update state snapshot
                ctrl_status = (
                    self._active_regulator.get_control_status()
                    if (self._active_regulator is not None and _discharge_active)
                    else None
                )
                self._update_state(sample=sample, control=ctrl_status)

                # 5. Broadcast to WebSocket clients
                await self._broadcast()

                # 6. Sleep for remaining interval
                elapsed = time.monotonic() - tick_start
                sleep_time = max(0.0, self._sampling_interval - elapsed)
                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("ControlRuntime main loop crashed")

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _read_sample(self) -> Optional[GridSample]:
        try:
            phases = await self._grid_meter.get_phase_powers()
            if phases is None:
                return None

            batt_output = await self._battery.get_ac_output_power()
            batt_state = (
                await self._battery.get_state() if hasattr(self._battery, "get_state") else None
            )

            soc: Optional[int] = None
            charge_in: Optional[float] = None
            if batt_state is not None:
                soc = getattr(batt_state, "battery_soc", None)
                charge_in = float(getattr(batt_state, "grid_input_power", 0) or 0) or None

            return GridSample(
                timestamp=time.time(),
                phase_a_w=phases[0],
                phase_b_w=phases[1],
                phase_c_w=phases[2],
                battery_output_w=float(batt_output) if batt_output is not None else 0.0,
                soc_percent=soc,
                charge_input_w=charge_in,
            )
        except Exception:
            logger.debug("Sample read failed", exc_info=True)
            return None

    def _update_state(
        self,
        sample: Optional[GridSample] = None,
        control=None,
    ) -> None:
        auto_status: Optional[AutoStatus] = None
        if self._auto_manager is not None:
            auto_status = self._auto_manager.get_auto_status()
        self._state = DashboardState(
            timestamp=time.time(),
            mode=self._mode,
            charge_power_w=self._charge_power_w,
            max_discharge_w=self._max_discharge_w,
            active_regulator=(self._active_regulator.name if self._active_regulator else None),
            sample=sample,
            control=control,
            auto_status=auto_status,
        )

    async def _broadcast(self) -> None:
        if not self._callbacks:
            return
        state = self._state
        dead: list[StateCallback] = []
        for cb in self._callbacks:
            try:
                await cb(state)
            except Exception:
                logger.debug("Broadcast callback failed – removing", exc_info=True)
                dead.append(cb)
        for cb in dead:
            self._callbacks.remove(cb)
