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
AC_CHARGE            → stop regulator, call battery.start_charge() then set_ac_input_limit(power_w)
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
from typing import Any, Awaitable, Callable, Optional, Protocol

from .models import (
    AutoStatus,
    DashboardState,
    DeviceMode,
    GridSample,
    RegulatorInfo,
)
from .regulator import BatteryInverterProtocol, RegulatorBase

logger: logging.Logger = logging.getLogger(__name__)


# ── Protocols (mirrors clients) ───────────────────────────────────────────────


class GridMeterProtocol(Protocol):
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
        # ── SoC thresholds for ZFI pause ──────────────────────────────────────
        # min_soc_hysteresis_pct: ZFI resumes when SoC >= device_min_soc + hysteresis
        min_soc_hysteresis_pct: int = 5,
        # ── Full-battery ZFI rest ─────────────────────────────────────────────
        # full_soc_resume_delay_s: grid draw must persist at least this long before ZFI resumes
        full_soc_resume_delay_s: float = 10.0,
        # full_soc_resume_threshold_w: minimum grid draw [W] for the wake-up timer
        # Condition: household consumption > PV output + threshold → total_grid_w > threshold
        full_soc_resume_threshold_w: int = 30,
        # ── Upper SoC AC charge limit ─────────────────────────────────────────
        # high_soc_charge_limit_pct: above this SoC the AC charge power is throttled
        high_soc_charge_limit_pct: int = 90,
        # high_soc_charge_limit_w: throttled AC charge power [W]; None = half of max_charge_w
        high_soc_charge_limit_w: Optional[int] = None,
    ) -> None:
        self._grid_meter: GridMeterProtocol = grid_meter
        self._battery: BatteryInverterProtocol = battery
        self._sampling_interval = sampling_interval_s
        self._control_interval = control_interval_s
        self._max_discharge_w = max_discharge_w
        self._min_discharge_w = min_discharge_w

        # SoC thresholds (min/max read from hardware in start())
        self._min_soc_pct: Optional[int] = None
        self._min_soc_hysteresis_pct = min_soc_hysteresis_pct
        self._min_soc_resume_pct: Optional[int] = None
        self._full_soc_pct: Optional[int] = None
        self._full_soc_resume_delay_s = full_soc_resume_delay_s
        self._full_soc_resume_threshold_w = full_soc_resume_threshold_w
        self._high_soc_charge_limit_pct = high_soc_charge_limit_pct
        self._high_soc_charge_limit_w = high_soc_charge_limit_w  # None = half of max_charge_w
        # Bypass state tracking (for change logging)
        self._last_bypass_state: Optional[bool] = None
        # ZFI pause states
        self._zfi_paused_low_soc: bool = False
        self._zfi_paused_full_battery: bool = False
        self._full_battery_resume_since: Optional[float] = (
            None  # monotonic ts when threshold first met
        )
        # ZFI no-grid fallback: Shelly data unavailable
        self._zfi_paused_no_grid: bool = False
        # ZFI soft-limit: SOC in hysteresis, output capped at PV power
        self._zfi_soc_limited: bool = False
        self._zfi_soc_limit_cap_w: int = 0
        # ZFI soft-limit but report as IDLE to MQTT: SoC closer to min_soc than to resume threshold
        self._zfi_soc_limited_report_idle: bool = False
        # Cooldown after resuming from full-battery pause: prevents immediate re-entry
        # when bypass clears slowly (1-2 ticks after resume command).
        self._full_battery_resumed_at: float = float("-inf")  # monotonic ts
        self._full_battery_resume_cooldown_s: float = 10.0

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

        # Feed-in watchdog (only active during ZFI regulation)
        self._watchdog_violation_since: Optional[float] = None
        self._watchdog_last_reset: float = float("-inf")
        self._watchdog_trigger_s: float = 10.0
        """Sustained feed-in beyond this duration [s] triggers the watchdog."""
        self._watchdog_cooldown_s: float = 30.0
        """Minimum interval [s] between two consecutive watchdog resets."""
        self._watchdog_threshold_w: float = -10.0
        """Grid values below this threshold (negative = feed-in) count as a violation."""

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
            # Use the explicitly supplied power; fall back to the previous charge
            # power or the runtime minimum (avoids the silent "0 or 400" trap where
            # a falsy 0 would resurrect a stale high-power setpoint).
            pw: int
            if charge_power_w is not None:
                pw = charge_power_w
            elif self._charge_power_w is not None:
                pw = self._charge_power_w
            else:
                pw = self._min_discharge_w  # device minimum as sensible default
            self._charge_power_w = pw
            # Clear all ZFI transient states
            self._zfi_paused_low_soc = False
            self._zfi_paused_full_battery = False
            self._zfi_soc_limited = False
            self._zfi_soc_limit_cap_w = 0
            self._zfi_soc_limited_report_idle = False
            if self._active_regulator:
                self._active_regulator.reset()
            setpoint = await self._battery.start_charge()
            if setpoint > 0 and pw > setpoint:
                applied = await self._battery.set_ac_input_limit(pw)
                if applied < 0:
                    logger.error("set_mode(AC_CHARGE): set_ac_input_limit(%d) failed", pw)

        elif mode == DeviceMode.IDLE:
            # Clear all ZFI transient states
            self._zfi_paused_low_soc = False
            self._zfi_paused_full_battery = False
            self._zfi_soc_limited = False
            self._zfi_soc_limit_cap_w = 0
            self._zfi_soc_limited_report_idle = False
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.stop()

        elif mode == DeviceMode.DISCHARGE_ZERO_FEED:
            self._charge_power_w = None
            if self._active_regulator:
                self._active_regulator.reset()
            # The regulator loop will start the battery on its first compute_setpoint call.
            await self._battery.start_discharge()

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
            # AC charging is always permitted – it fills the battery, so the
            # low-SoC ZFI pause is no longer relevant.  Clear all ZFI pause/limit
            # flags so the dashboard reflects the actual state and the guards do
            # not re-interfere on the next tick.
            self._zfi_paused_low_soc = False
            self._zfi_paused_full_battery = False
            self._zfi_paused_no_grid = False
            self._zfi_soc_limited = False
            self._zfi_soc_limit_cap_w = 0
            self._zfi_soc_limited_report_idle = False
            self._full_battery_resume_since = None
            # Explicit power wins; fall back to runtime minimum (never silently
            # resurrect a stale high-power setpoint via "pw = value or old").
            pw = charge_power_w if charge_power_w is not None else self._min_discharge_w
            self._charge_power_w = pw
            if self._active_regulator:
                self._active_regulator.reset()
            setpoint = await self._battery.start_charge()
            if setpoint > 0 and pw > setpoint:
                applied = await self._battery.set_ac_input_limit(pw)
                if applied < 0:
                    logger.error(
                        "apply_effective_mode(AC_CHARGE): set_ac_input_limit(%d) failed", pw
                    )

        elif mode == DeviceMode.IDLE:
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.stop()

        elif mode == DeviceMode.DISCHARGE_ZERO_FEED:
            self._charge_power_w = None
            if self._auto_effective_mode != DeviceMode.DISCHARGE_ZERO_FEED:
                # Only reset/restart when actually switching into this mode.
                # Respect the low-SoC guard: if the pause is active, keep the
                # battery stopped instead of starting a (suppressed) discharge.
                if self._active_regulator:
                    self._active_regulator.reset()
                if self._zfi_paused_low_soc:
                    await self._battery.stop()
                else:
                    await self._battery.start_discharge()

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
        # Read hardware SoC limits (min_soc / max_soc configured on the device)
        await self._init_soc_limits()
        self._running = True
        self._main_task = asyncio.create_task(self._main_loop(), name="control-runtime")
        logger.info(
            "ControlRuntime started (sample=%.1fs  control=%.1fs  min_soc=%s%%  max_soc=%s%%)",
            self._sampling_interval,
            self._control_interval,
            self._min_soc_pct,
            self._full_soc_pct,
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

                # 2. SoC-based guard logic (updates _zfi_paused_low_soc / _zfi_paused_full_battery)
                if sample is not None:
                    await self._update_soc_guards(sample, time.monotonic())

                # 3. Derive whether ZFI regulation is actually active (mode + not paused)
                _discharge_mode = self._mode == DeviceMode.DISCHARGE_ZERO_FEED or (
                    self._mode == DeviceMode.AUTO
                    and self._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED
                )
                _zfi_suppressed = (
                    self._zfi_paused_low_soc
                    or self._zfi_paused_full_battery
                    or self._zfi_paused_no_grid
                )
                _discharge_active = _discharge_mode and not _zfi_suppressed

                # 3b. Shelly/grid-meter fallback: no valid data → hold at min_discharge_w
                if _discharge_mode:
                    if sample is None and not self._zfi_paused_no_grid:
                        logger.warning(
                            "ZFI: Shelly data unavailable – entering no-grid fallback "
                            "(battery held at %d W)",
                            self._min_discharge_w,
                        )
                        self._zfi_paused_no_grid = True
                        if self._active_regulator:
                            self._active_regulator.reset()
                        await self._battery.start_discharge()
                        _discharge_active = False  # regulator suspended
                    elif sample is not None and self._zfi_paused_no_grid:
                        logger.info("ZFI: Shelly data restored – resuming zero-feed regulation")
                        self._zfi_paused_no_grid = False
                        if self._active_regulator:
                            self._active_regulator.reset()
                        await self._battery.start_discharge()
                        # Recalculate; now data is available so no longer suppressed
                        _zfi_suppressed = self._zfi_paused_low_soc or self._zfi_paused_full_battery
                        _discharge_active = _discharge_mode and not _zfi_suppressed
                elif self._zfi_paused_no_grid:
                    # Left ZFI mode (e.g. switched to AC_CHARGE/IDLE) – clear flag
                    self._zfi_paused_no_grid = False

                # 4. Forward sample to regulator (only when actively regulating)
                if sample is not None and _discharge_active and self._active_regulator is not None:
                    await self._active_regulator.add_sample(sample)

                # 4b. Feed-in watchdog – resets stuck regulator on sustained export
                if sample is not None and _discharge_active:
                    await self._check_feed_in_watchdog(sample, time.monotonic())

                # 5. Control tick – fires every control_interval_s
                now = time.monotonic()
                _due = (now - last_control) >= self._control_interval

                if _due:
                    # 5a. Regulator setpoint (only when discharge active + regulator present)
                    if _discharge_active and self._active_regulator is not None:
                        # Apply SOC-based soft-limit cap when in hysteresis zone
                        effective_max_w = (
                            min(self._max_discharge_w, self._zfi_soc_limit_cap_w)
                            if self._zfi_soc_limited and self._zfi_soc_limit_cap_w > 0
                            else self._max_discharge_w
                        )
                        try:
                            await self._active_regulator.compute_setpoint(
                                self._battery,
                                effective_max_w,
                                self._min_discharge_w,
                            )
                        except Exception:
                            logger.exception("Regulator compute_setpoint failed")

                    # 5b. AUTO plan tick – always fires when AUTO mode is active
                    if self._mode == DeviceMode.AUTO and self._auto_manager is not None:
                        try:
                            await self._auto_manager.tick(self.apply_effective_mode)
                        except Exception:
                            logger.exception("AutoModeManager tick failed")

                    # 5c. AC charge SoC limit – check while in charge mode
                    if sample is not None and self._mode in (DeviceMode.AC_CHARGE,):
                        await self._apply_high_soc_charge_limit(sample)

                    last_control = time.monotonic()

                # 6. Update state snapshot
                ctrl_status = (
                    self._active_regulator.get_control_status()
                    if (self._active_regulator is not None and _discharge_active)
                    else None
                )
                self._update_state(sample=sample, control=ctrl_status)

                # 7. Broadcast to WebSocket clients
                await self._broadcast()

                # 8. Sleep for remaining interval
                elapsed = time.monotonic() - tick_start
                sleep_time = max(0.0, self._sampling_interval - elapsed)
                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("ControlRuntime main loop crashed")

    # ── Helpers ───────────────────────────────────────────────────────────────
    async def _init_soc_limits(self) -> None:
        """Read min_soc / max_soc from the device and store as instance state."""
        try:
            min_soc = await self._battery.get_min_soc()
            max_soc = await self._battery.get_max_soc()
        except Exception:
            logger.warning("Could not read SoC limits from hardware", exc_info=True)
            min_soc = None
            max_soc = None

        self._min_soc_pct = min_soc if min_soc is not None else 10
        self._full_soc_pct = max_soc if max_soc is not None else 100
        self._min_soc_resume_pct = self._min_soc_pct + self._min_soc_hysteresis_pct
        if min_soc is None or max_soc is None:
            logger.warning(
                "SoC limits unavailable (min=%d%%  max=%d%%) – using fallback values",
                self._min_soc_pct,
                self._full_soc_pct,
            )

    async def _update_soc_guards(self, sample: "GridSample", now_mono: float) -> None:
        """Update ZFI pause/limit flags based on current SoC, PV power, and grid.

        Low-SoC soft-bypass (NEW)
        -------------------------
        Within the hysteresis band [min_soc, min_soc + hysteresis):

        * **Full pause** – SoC ≤ min_soc AND no PV available:
          Stop battery output completely.  Resumes to soft-limit as soon as
          SoC rises above min_soc OR PV becomes available.

        * **Soft-limit** – in hysteresis zone (SoC > min_soc OR PV available):
          ZFI regulator still runs but max_output_w is capped at
          ``max(min_discharge_w, min(solar_input_w, max_discharge_w))``.
          The battery can only discharge up to what PV currently produces,
          ensuring the battery slowly charges from PV surplus rather than
          drawing from the grid.  If solar_input_w == 0 the cap equals
          min_discharge_w (minimum discharge only).

        * **Full resume** – SoC ≥ min_soc + hysteresis:
          Remove the cap and run ZFI at full power.

        Full-battery rest
        -----------------
        * Enter rest when SoC >= device max_soc.
        * Resume when total_grid_w >= full_soc_resume_threshold_w has been
          sustained for full_soc_resume_delay_s seconds.
        * Leaving ZFI mode (AC charge / IDLE) also clears the rest flag.
        """
        soc = sample.soc_percent
        solar_w = float(sample.solar_input_w or 0.0)

        _discharge_mode = self._mode == DeviceMode.DISCHARGE_ZERO_FEED or (
            self._mode == DeviceMode.AUTO
            and self._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED
        )

        # Guard: limits must be initialised (start() calls _init_soc_limits)
        if (
            self._min_soc_pct is None
            or self._full_soc_pct is None
            or self._min_soc_resume_pct is None
        ):
            return

        # ── Low-SoC soft-bypass ───────────────────────────────────────────────
        if soc is not None:
            # Calculate midpoint of hysteresis band: if SoC <= midpoint, report as IDLE to MQTT
            soc_midpoint = self._min_soc_pct + self._min_soc_hysteresis_pct / 2

            if self._zfi_paused_low_soc:
                # Currently in full pause – check whether we can leave it
                if soc >= self._min_soc_resume_pct:
                    # Directly to full resume (hysteresis exceeded)
                    logger.info(
                        "ZFI fully resumed from full pause: SoC %d%% >= %d%%",
                        soc,
                        self._min_soc_resume_pct,
                    )
                    self._zfi_paused_low_soc = False
                    self._zfi_soc_limited = False
                    self._zfi_soc_limit_cap_w = 0
                    if _discharge_mode:
                        if self._active_regulator:
                            self._active_regulator.reset()
                        await self._battery.start_discharge()

                elif soc > self._min_soc_pct or solar_w > 0:
                    # SoC rose above min_soc OR PV appeared → enter soft-limit
                    cap = max(self._min_discharge_w, min(int(solar_w), self._max_discharge_w))
                    logger.info(
                        "ZFI soft-limit (from full pause): SoC %d%% or solar %.0fW → cap %dW",
                        soc,
                        solar_w,
                        cap,
                    )
                    self._zfi_paused_low_soc = False
                    self._zfi_soc_limited = True
                    self._zfi_soc_limit_cap_w = cap
                    self._zfi_soc_limited_report_idle = soc <= soc_midpoint
                    if _discharge_mode:
                        if self._active_regulator:
                            self._active_regulator.reset()
                        await self._battery.start_discharge()

            elif self._zfi_soc_limited:
                # Currently in soft-limit – check for transitions
                if soc >= self._min_soc_resume_pct:
                    # Fully exit soft-limit
                    logger.info(
                        "ZFI soft-limit ended: SoC %d%% >= %d%%",
                        soc,
                        self._min_soc_resume_pct,
                    )
                    self._zfi_soc_limited = False
                    self._zfi_soc_limit_cap_w = 0
                    self._zfi_soc_limited_report_idle = False
                    if _discharge_mode and self._active_regulator:
                        self._active_regulator.reset()
                    if _discharge_mode:
                        await self._battery.start_discharge()

                elif soc <= self._min_soc_pct and solar_w <= 0:
                    # Dropped to min_soc without PV → escalate to full pause
                    logger.info(
                        "ZFI full pause (from soft-limit): SoC %d%% <= min %d%% and no PV",
                        soc,
                        self._min_soc_pct,
                    )
                    self._zfi_soc_limited = False
                    self._zfi_soc_limit_cap_w = 0
                    self._zfi_soc_limited_report_idle = False
                    self._zfi_paused_low_soc = True
                    if _discharge_mode:
                        await self._battery.stop()

                else:
                    # Still in hysteresis – update cap based on current PV and report status
                    cap = max(self._min_discharge_w, min(int(solar_w), self._max_discharge_w))
                    self._zfi_soc_limit_cap_w = cap
                    self._zfi_soc_limited_report_idle = soc <= soc_midpoint

            else:
                # Currently normal – check if we should enter hysteresis handling
                if soc <= self._min_soc_pct and solar_w <= 0:
                    # At or below min_soc with no PV → full pause
                    logger.info(
                        "ZFI full pause: SoC %d%% <= min %d%% and no PV",
                        soc,
                        self._min_soc_pct,
                    )
                    self._zfi_paused_low_soc = True
                    if _discharge_mode:
                        await self._battery.stop()

                elif soc <= self._min_soc_pct:
                    # At or below min_soc with PV available → soft-limit
                    cap = max(self._min_discharge_w, min(int(solar_w), self._max_discharge_w))
                    logger.info(
                        "ZFI soft-limit: SoC %d%% reached min_soc [%d%%] → cap %dW",
                        soc,
                        self._min_soc_pct,
                        cap,
                    )
                    self._zfi_soc_limited = True
                    self._zfi_soc_limit_cap_w = cap
                    self._zfi_soc_limited_report_idle = soc <= soc_midpoint

        # ── Full-battery rest (only relevant in ZFI modes) ───────────────────
        if not _discharge_mode:
            # Not in a ZFI mode → clear all ZFI-transient states entirely
            if self._zfi_paused_full_battery:
                self._zfi_paused_full_battery = False
                self._full_battery_resume_since = None
            if self._zfi_soc_limited:
                self._zfi_soc_limited = False
                self._zfi_soc_limit_cap_w = 0
                self._zfi_soc_limited_report_idle = False
            return

        _solar_w = float(sample.solar_input_w or 0.0)
        _batt_out = abs(sample.battery_output_w)
        _in_cooldown = (
            now_mono - self._full_battery_resumed_at
        ) < self._full_battery_resume_cooldown_s

        # "Battery not delivering" – reliable bypass proxy without using the unreliable
        # bypass_active flag.  Condition: battery output near 0 while PV covers our minimum
        # discharge request AND we are outside the post-resume cooldown window.
        # When this is True the inverter is effectively routing PV directly to the house
        # (bypass / full-battery state) and the battery cannot contribute.
        _battery_not_delivering = (
            not _in_cooldown
            and _batt_out < self._min_discharge_w * 0.5
            and _solar_w >= self._min_discharge_w
        )

        if not self._zfi_paused_full_battery and (
            (soc is not None and soc >= self._full_soc_pct) or _battery_not_delivering
        ):
            reason = (
                f"battery not delivering (out={_batt_out:.0f}W < "
                f"{self._min_discharge_w * 0.5:.0f}W threshold, "
                f"solar={_solar_w:.0f}W \u2265 {self._min_discharge_w}W min)"
                if _battery_not_delivering
                else f"SoC {soc}% >= hardware maximum {self._full_soc_pct}%"
            )
            logger.info("ZFI full-battery pause: %s", reason)
            self._zfi_paused_full_battery = True
            self._full_battery_resume_since = None
            await self._battery.stop()

        elif self._zfi_paused_full_battery:
            # Immediate exit: solar dropped below our minimum AND SOC is below the hardware
            # maximum.  Neither the SOC-full condition nor the PV-bypass condition applies.
            if _solar_w < self._min_discharge_w and (soc is None or soc < self._full_soc_pct):
                logger.info(
                    "ZFI full-battery pause ended immediately: "
                    "solar %.0fW < %dW min and SoC %s%% < max",
                    _solar_w,
                    self._min_discharge_w,
                    soc,
                )
                self._zfi_paused_full_battery = False
                self._full_battery_resume_since = None
                self._full_battery_resumed_at = now_mono
                if self._active_regulator is not None:
                    self._active_regulator.reset()
                await self._battery.start_discharge()
            else:
                # PV still available or SOC still high: use sustained grid-draw timer
                grid_w = sample.total_grid_w
                if grid_w >= self._full_soc_resume_threshold_w:
                    if self._full_battery_resume_since is None:
                        self._full_battery_resume_since = now_mono
                        logger.debug(
                            "Full-battery pause: grid draw %.0fW \u2013 wake-up timer started",
                            grid_w,
                        )
                    elif (
                        now_mono - self._full_battery_resume_since >= self._full_soc_resume_delay_s
                    ):
                        logger.info(
                            "Full-battery pause ended: grid draw %.0fW sustained for >= %.0fs",
                            grid_w,
                            self._full_soc_resume_delay_s,
                        )
                        self._zfi_paused_full_battery = False
                        self._full_battery_resume_since = None
                        self._full_battery_resumed_at = now_mono
                        if self._active_regulator is not None:
                            self._active_regulator.reset()
                        await self._battery.start_discharge()
                else:
                    # Grid draw below threshold \u2192 reset the timer
                    if self._full_battery_resume_since is not None:
                        logger.debug(
                            "Full-battery pause: grid draw too low \u2013 wake-up timer reset"
                        )
                    self._full_battery_resume_since = None
                    self._full_battery_resume_since = None

    async def _apply_high_soc_charge_limit(self, sample: "GridSample") -> None:
        """Reduce AC charge power when SoC exceeds the configured upper threshold."""
        soc = sample.soc_percent
        if soc is None:
            return
        current_pw = (
            self._charge_power_w if self._charge_power_w is not None else self._min_discharge_w
        )
        if soc > self._high_soc_charge_limit_pct:
            limit_w = (
                self._high_soc_charge_limit_w
                if self._high_soc_charge_limit_w is not None
                else max(50, current_pw // 2)
            )
            if current_pw > limit_w:
                logger.info(
                    "AC charge throttled: SoC %d%% > %d%% → %d W (was %d W)",
                    soc,
                    self._high_soc_charge_limit_pct,
                    limit_w,
                    current_pw,
                )
                self._charge_power_w = limit_w
                applied = await self._battery.set_ac_input_limit(limit_w)
                if applied < 0:
                    logger.error("AC charge throttle: set_ac_input_limit(%d) failed", limit_w)

    async def _check_feed_in_watchdog(self, sample: "GridSample", now_mono: float) -> None:
        """Detect sustained feed-in and reset the regulator if triggered.

        Called every sampling tick while ZFI is active.  On trigger: regulator
        reset + setpoint reduced to min_discharge_w.
        """
        total_w = sample.total_grid_w

        if total_w >= self._watchdog_threshold_w:
            # No feed-in problem – reset violation timer
            self._watchdog_violation_since = None
            return

        # Feed-in below threshold
        if self._watchdog_violation_since is None:
            self._watchdog_violation_since = now_mono
            return

        duration_s = now_mono - self._watchdog_violation_since
        if duration_s < self._watchdog_trigger_s:
            return

        if now_mono - self._watchdog_last_reset < self._watchdog_cooldown_s:
            # Still in cooldown – avoid resetting too frequently
            return

        # ── Watchdog triggered ────────────────────────────────────────────────
        logger.warning(
            "Feed-in watchdog: %.0f s sustained feed-in (total=%.0f W, "
            "threshold=%.0f W) – resetting regulator and reducing setpoint to %d W.",
            duration_s,
            total_w,
            self._watchdog_threshold_w,
            self._min_discharge_w,
        )

        if self._active_regulator is not None:
            self._active_regulator.reset()

        applied = await self._battery.set_ac_output_limit(self._min_discharge_w)
        if applied < 0:
            logger.error(
                "Feed-in watchdog: set_ac_output_limit(%d) failed.",
                self._min_discharge_w,
            )

        self._watchdog_violation_since = None
        self._watchdog_last_reset = now_mono

    async def _read_sample(self) -> Optional[GridSample]:
        try:
            phases = await self._grid_meter.get_phase_powers()
            if phases is None:
                logger.debug("_read_sample: grid meter returned no phase data")
                return None

            batt_output = await self._battery.get_ac_output_power()
            batt_state = await self._battery.get_state()

            soc: int | None = None
            charge_in: float | None = None
            bypass_active: bool | None = None
            if batt_state is not None:
                soc = batt_state.battery_soc
                charge_in = float(batt_state.grid_input_power or 0) or None
                bypass_active = batt_state.bypass_mode

                # Log bypass state changes
                if bypass_active != self._last_bypass_state:
                    if bypass_active:
                        solar_w = batt_state.solar_input_power
                        logger.warning(
                            "Inverter entered BYPASS mode: PV (%d W) routed directly to house. "
                            "battery_output_w (%s W) reflects solar bypass, not battery output. "
                            "outputLimit commands are ignored!",
                            solar_w,
                            batt_output,
                        )
                    elif self._last_bypass_state is not None:
                        logger.info("Inverter left bypass mode – battery control active")
                    self._last_bypass_state = bypass_active

                if bypass_active:
                    logger.debug(
                        "Bypass active: solar=%d W  home_output=%s W  soc=%s%%",
                        batt_state.solar_input_power,
                        batt_output,
                        soc,
                    )

            return GridSample(
                timestamp=time.time(),
                phase_a_w=phases[0],
                phase_b_w=phases[1],
                phase_c_w=phases[2],
                battery_output_w=float(batt_output) if batt_output is not None else 0.0,
                soc_percent=soc,
                charge_input_w=charge_in,
                bypass_active=bypass_active,
                solar_input_w=float(batt_state.solar_input_power)
                if batt_state is not None
                else None,
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
            zfi_paused_low_soc=self._zfi_paused_low_soc,
            zfi_paused_full_battery=self._zfi_paused_full_battery,
            zfi_paused_no_grid=self._zfi_paused_no_grid,
            zfi_soc_limited=self._zfi_soc_limited,
            zfi_soc_limit_cap_w=self._zfi_soc_limit_cap_w if self._zfi_soc_limited else None,
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
