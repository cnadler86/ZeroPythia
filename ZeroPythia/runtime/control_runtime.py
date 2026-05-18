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
from collections import deque
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from ZeroPythia.controller.regulator import BatteryInverterProtocol, RegulatorBase

if TYPE_CHECKING:
    from ZeroPythia.services.updater import AutoUpdater

from .models import (
    AutoStatus,
    DashboardState,
    DeviceMode,
    GridSample,
    RegulatorInfo,
    ZFIState,
)
from .sampler import GridMeterProtocol, RuntimeSampler

logger: logging.Logger = logging.getLogger(__name__)


# ── Type alias ────────────────────────────────────────────────────────────────

StateCallback = Callable[[DashboardState], Awaitable[None]]


# ── Bypass resume guard ───────────────────────────────────────────────────────


class BypassResumeGuard:
    """Rolling-window guard for the full-battery bypass → discharge transition.

    When the battery is at 100 % SoC the inverter enters *bypass mode*: solar PV
    flows directly to the household load without passing through the battery.
    Starting a zero-feed discharge in this state can cause rapid toggling:

    1. Battery starts discharging.
    2. Solar + battery together over-supply the load → ZFI reduces battery to 0.
    3. Inverter returns to bypass.
    4. Repeat.

    The guard collects a rolling window of (theoretical_setpoint, solar) samples
    while the ZFI is paused due to a full battery.  It only signals *safe to
    start* when **all** samples in the window satisfy:

        theoretical_setpoint  >  max_solar_in_window  +  safety_offset_w

    where ``theoretical_setpoint = total_grid_w + solar_input_w`` (≈ household
    consumption when the battery is stopped).

    Window duration
    ---------------
    The window is derived automatically from the oscillation-holder settings
    registered in the active regulator:

        window_s = max_period_across_holders × max_min_rising_count + 1 s

    This guarantees the guard covers at least one full oscillation detection
    cycle, making it very unlikely that a newly started discharge will
    immediately re-trigger the bypass/discharge oscillation.
    """

    def __init__(self, window_s: float, safety_offset_w: float) -> None:
        #: Duration of the rolling observation window [s].
        self.window_s = window_s
        #: The theoretical battery setpoint must exceed ``max_solar + safety_offset_w``
        #: for every sample in the window before discharge is allowed.
        self.safety_offset_w = safety_offset_w
        # (timestamp, theoretical_setpoint_w, solar_w)
        self._buf: deque[tuple[float, float, float]] = deque()

    def add_sample(self, ts: float, theoretical_setpoint_w: float, solar_w: float) -> None:
        """Append a new measurement and evict samples older than *window_s*."""
        self._buf.append((ts, theoretical_setpoint_w, solar_w))
        cutoff = ts - self.window_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    def is_safe_to_start(self, now: float) -> bool:
        """Check if the guard conditions are satisfied for the current window.

        Return *True* when the full guard window is populated and the
        solar-vs-demand condition is satisfied for every sample.

        The window is considered *full* when the span from the oldest buffered
        sample to *now* is at least ``window_s`` seconds.
        """
        if len(self._buf) < 2:
            return False
        if now - self._buf[0][0] < self.window_s:
            return False  # window not yet filled
        solar_max = max(s[2] for s in self._buf)
        required = solar_max + self.safety_offset_w
        return all(s[1] > required for s in self._buf)

    def reset(self) -> None:
        """Clear the sample buffer (called on state transitions)."""
        self._buf.clear()


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
        # high_soc_charge_limit_w: throttled AC charge power [W];
        #   None => half of battery max charge power (from API/HW limits)
        high_soc_charge_limit_w: Optional[int] = None,
        # ── Feed-in watchdog (sustained export detection) ──────────────────────
        # watchdog_cycles: number of control cycles for feed-in detection
        # trigger_time = control_interval_s * watchdog_cycles + 1 [s]
        watchdog_cycles: int = 3,
        # watchdog_threshold_w: grid power threshold [W] for feed-in (must be negative)
        watchdog_threshold_w: float = -10.0,
        # ── Bypass resume guard ────────────────────────────────────────────────
        # bypass_resume_safety_offset_w: when resuming from a full-battery pause the
        #   theoretical battery setpoint must exceed max(solar_in_window) + offset [W]
        #   for the entire guard window before ZFI is allowed to start discharging.
        #   This prevents bypass/discharge toggling when solar production is high.
        bypass_resume_safety_offset_w: float = 30.0,
    ) -> None:
        self._battery: BatteryInverterProtocol = battery
        self._sampler = RuntimeSampler(grid_meter, battery)
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
        self._high_soc_charge_limit_w = high_soc_charge_limit_w
        # ZFI state machine – single source of truth for discharge-pause logic
        self._zfi_state: ZFIState = ZFIState.INACTIVE
        self._zfi_soc_limit_cap_w: int = 0
        # Regulator registry
        self._regulators: dict[str, RegulatorBase] = {}
        self._active_regulator: Optional[RegulatorBase] = None

        # Operating mode
        self._mode: DeviceMode = DeviceMode.IDLE
        self._charge_power_w: Optional[int] = None
        self._ac_charge_requested_power_w: Optional[int] = None

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
        # Calculate trigger time from control interval and cycle count + 1s tolerance
        self._watchdog_trigger_s: float = control_interval_s * watchdog_cycles + 1.0
        """Sustained feed-in beyond this duration [s] triggers the watchdog."""
        self._watchdog_cooldown_s: float = 2.0 * self._watchdog_trigger_s
        """Minimum interval [s] between two consecutive watchdog resets (2x trigger time)."""
        self._watchdog_threshold_w: float = watchdog_threshold_w
        """Grid values below this threshold (negative = feed-in) count as a violation."""

        # ── Bypass resume guard ────────────────────────────────────────────────
        # Window is initially set to a conservative default (25 s).
        # register_regulator() updates it automatically from the oscillation-holder
        # settings of the first registered regulator.
        self._bypass_guard: BypassResumeGuard = BypassResumeGuard(
            window_s=25.0,
            safety_offset_w=bypass_resume_safety_offset_w,
        )

        # Runtime tasks
        self._running = False
        self._main_task: Optional[asyncio.Task] = None
        self._update_task: Optional[asyncio.Task] = None
        self._updater: Optional[AutoUpdater] = None

    def attach_auto_mode_manager(self, manager: Any) -> None:
        """Register the AutoModeManager used when mode==AUTO."""
        self._auto_manager = manager

    def attach_updater(self, updater: "AutoUpdater") -> None:
        """Register an AutoUpdater instance to drive periodic update checks."""
        self._updater = updater

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
            # Derive bypass guard window from this regulator's oscillation config.
            self._reconfigure_bypass_guard(regulator)

    def _reconfigure_bypass_guard(self, regulator: RegulatorBase) -> None:
        """Update bypass guard window from *regulator*'s oscillation-holder configs.

        Calls ``regulator.bypass_resume_window_s()``; if it returns ``None``
        the current guard window is preserved (no change).
        """
        window_s = regulator.bypass_resume_window_s()
        if window_s is not None:
            self._bypass_guard = BypassResumeGuard(
                window_s=window_s,
                safety_offset_w=self._bypass_guard.safety_offset_w,
            )
            logger.info(
                "Bypass resume guard window: %.1f s (from regulator %s)",
                window_s,
                regulator.name,
            )

    def list_regulators(self) -> list[RegulatorInfo]:
        """Return metadata for all registered regulators."""
        return [
            RegulatorInfo(
                name=reg.name,
                description=reg.description,
                is_active=(reg is self._active_regulator),
                settings_schema=reg.settings_schema(),
                current_settings=reg.get_current_settings(),
            )
            for reg in self._regulators.values()
        ]

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
            self._ac_charge_requested_power_w = pw
            # Clear ZFI state
            self._zfi_state = ZFIState.INACTIVE
            self._zfi_soc_limit_cap_w = 0
            if self._active_regulator:
                self._active_regulator.reset()
            setpoint = await self._battery.start_charge()
            if setpoint > 0 and pw > setpoint:
                applied = await self._battery.set_ac_input_limit(pw)
                if applied < 0:
                    logger.error("set_mode(AC_CHARGE): set_ac_input_limit(%d) failed", pw)

        elif mode == DeviceMode.IDLE:
            self._ac_charge_requested_power_w = None
            # Clear ZFI state
            self._zfi_state = ZFIState.INACTIVE
            self._zfi_soc_limit_cap_w = 0
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.stop()

        elif mode == DeviceMode.DISCHARGE_ZERO_FEED:
            self._charge_power_w = None
            self._ac_charge_requested_power_w = None
            self._zfi_state = ZFIState.RUNNING
            self._zfi_soc_limit_cap_w = 0
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.start_discharge()

        elif mode == DeviceMode.AUTO:
            self._ac_charge_requested_power_w = None
            self._zfi_state = ZFIState.INACTIVE
            self._zfi_soc_limit_cap_w = 0
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
            # Clear ZFI state – AC charging fills the battery, so any prior
            # pause/limit state is no longer relevant.
            self._zfi_state = ZFIState.INACTIVE
            self._zfi_soc_limit_cap_w = 0
            # Explicit power wins; fall back to runtime minimum (never silently
            # resurrect a stale high-power setpoint via "pw = value or old").
            pw = charge_power_w if charge_power_w is not None else self._min_discharge_w
            self._charge_power_w = pw
            self._ac_charge_requested_power_w = pw
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
            self._ac_charge_requested_power_w = None
            self._zfi_state = ZFIState.INACTIVE
            self._zfi_soc_limit_cap_w = 0
            if self._active_regulator:
                self._active_regulator.reset()
            await self._battery.stop()

        elif mode == DeviceMode.DISCHARGE_ZERO_FEED:
            self._charge_power_w = None
            self._ac_charge_requested_power_w = None
            if self._auto_effective_mode != DeviceMode.DISCHARGE_ZERO_FEED:
                # Only reset/restart when actually switching into this mode.
                if self._active_regulator:
                    self._active_regulator.reset()
                self._zfi_state = ZFIState.RUNNING
                self._zfi_soc_limit_cap_w = 0
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
        self._reconfigure_bypass_guard(reg)

    async def update_regulator_settings(self, name: str, data: dict[str, Any]) -> None:
        """Apply settings to a (possibly non-active) regulator."""
        if name not in self._regulators:
            raise ValueError(f"Unknown regulator: {name!r}")
        self._regulators[name].apply_settings(data)
        logger.info("Settings updated for regulator %s", name)
        # Recompute bypass guard window when the active regulator's config changes
        # (oscillation holder max_period / min_rising_count may have been updated).
        if self._regulators[name] is self._active_regulator:
            self._reconfigure_bypass_guard(self._regulators[name])

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
        if self._updater is not None:
            self._update_task = asyncio.create_task(self._update_check_loop(), name="update-check")
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
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
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
                sample = await self._sampler.read()

                # 2. SoC-based ZFI state machine
                if sample is not None:
                    await self._update_soc_guards(sample, time.monotonic())

                # 3. No-grid fallback: no Shelly data while in ZFI mode
                _in_zfi = self._mode == DeviceMode.DISCHARGE_ZERO_FEED or (
                    self._mode == DeviceMode.AUTO
                    and self._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED
                )
                if _in_zfi:
                    if sample is None and self._zfi_state != ZFIState.PAUSED_NO_GRID:
                        logger.warning(
                            "ZFI: Shelly data unavailable – no-grid fallback "
                            "(battery held at %d W)",
                            self._min_discharge_w,
                        )
                        self._zfi_state = ZFIState.PAUSED_NO_GRID
                        if self._active_regulator:
                            self._active_regulator.reset()
                        await self._battery.start_discharge()
                    elif sample is not None and self._zfi_state == ZFIState.PAUSED_NO_GRID:
                        logger.info("ZFI: Shelly data restored – resuming zero-feed regulation")
                        self._zfi_state = ZFIState.RUNNING
                        if self._active_regulator:
                            self._active_regulator.reset()
                        await self._battery.start_discharge()
                        # Re-run SoC guards so this tick's sample is fully evaluated
                        await self._update_soc_guards(sample, time.monotonic())
                elif self._zfi_state == ZFIState.PAUSED_NO_GRID:
                    # Left ZFI mode while in no-grid fallback – clean up
                    self._zfi_state = ZFIState.INACTIVE

                # 4. Discharge-active: regulator runs only in RUNNING or SOFT_LIMITED
                _discharge_active = self._zfi_state in (ZFIState.RUNNING, ZFIState.SOFT_LIMITED)

                # 4a. Forward sample to regulator
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
                        # Apply soft-limit cap when in SoC hysteresis zone
                        effective_max_w = (
                            min(self._max_discharge_w, self._zfi_soc_limit_cap_w)
                            if self._zfi_state == ZFIState.SOFT_LIMITED
                            and self._zfi_soc_limit_cap_w > 0
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

    # ── Auto-update background task ───────────────────────────────────────────

    def _is_in_idle_state(self) -> bool:
        """Return True when the system is effectively idle (safe to restart)."""
        if self._mode == DeviceMode.IDLE:
            return True
        if self._mode == DeviceMode.AUTO and self._auto_effective_mode == DeviceMode.IDLE:
            return True
        return False

    async def _update_check_loop(self) -> None:
        """Periodically check for updates; apply only when the system is idle."""
        _CHECK_INTERVAL_S = 15 * 60  # poll every 15 min; updater rate-limits to 1×/day

        try:
            # Initial delay so startup is not stressed by an immediate fetch.
            await asyncio.sleep(60)
            while self._running:
                if self._updater is not None and self._is_in_idle_state():
                    try:
                        await self._updater.check_and_update()
                    except Exception:  # noqa: BLE001
                        logger.exception("update check failed")
                await asyncio.sleep(_CHECK_INTERVAL_S)
        except asyncio.CancelledError:
            pass

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
        """ZFI state machine: transition _zfi_state from SoC, PV, and grid data.

        Called every tick when a valid sample is available.  All ZFI pause and
        soft-limit logic is expressed as explicit state transitions; there are no
        separate boolean flags.

        State diagram (simplified)
        --------------------------
        INACTIVE ←→ (set by set_mode / apply_effective_mode)

        RUNNING → SOFT_LIMITED    when SoC ≤ min_soc, PV available
        RUNNING → PAUSED_LOW_SOC  when SoC ≤ min_soc, no PV
        RUNNING → PAUSED_FULL     when SoC ≥ max_soc, low household load

        SOFT_LIMITED → RUNNING        when SoC ≥ min_soc + hysteresis
        SOFT_LIMITED → PAUSED_LOW_SOC when SoC ≤ min_soc, PV disappears

        PAUSED_LOW_SOC → RUNNING      when SoC ≥ min_soc + hysteresis
        PAUSED_LOW_SOC → SOFT_LIMITED when SoC > min_soc OR PV appears

        PAUSED_FULL → RUNNING when SoC < max_soc OR household load > threshold

        PAUSED_NO_GRID transitions are handled in the main loop.
        """
        # Guard: limits must be initialised (start() calls _init_soc_limits)
        if (
            self._min_soc_pct is None
            or self._full_soc_pct is None
            or self._min_soc_resume_pct is None
        ):
            return

        in_zfi = self._mode == DeviceMode.DISCHARGE_ZERO_FEED or (
            self._mode == DeviceMode.AUTO
            and self._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED
        )

        if not in_zfi:
            # Leaving ZFI mode → reset state
            if self._zfi_state != ZFIState.INACTIVE:
                self._zfi_state = ZFIState.INACTIVE
                self._zfi_soc_limit_cap_w = 0
            return

        # PAUSED_NO_GRID is managed by the main loop; skip SoC transitions for it.
        if self._zfi_state == ZFIState.PAUSED_NO_GRID:
            return

        # Auto-initialise: if we're in ZFI mode but state is still INACTIVE (e.g.
        # when _mode was set directly without going through set_mode()), start RUNNING.
        if self._zfi_state == ZFIState.INACTIVE:
            self._zfi_state = ZFIState.RUNNING

        soc = sample.soc_percent
        solar_w = float(sample.solar_input_w or 0.0)

        # ── Low-SoC state machine ─────────────────────────────────────────────
        if soc is not None:
            if self._zfi_state == ZFIState.PAUSED_LOW_SOC:
                if soc >= self._min_soc_resume_pct:
                    logger.info(
                        "ZFI resumed: SoC %d%% >= resume threshold %d%%",
                        soc,
                        self._min_soc_resume_pct,
                    )
                    self._zfi_state = ZFIState.RUNNING
                    self._zfi_soc_limit_cap_w = 0
                    if self._active_regulator:
                        self._active_regulator.reset()
                    await self._battery.start_discharge()
                elif soc > self._min_soc_pct or solar_w > 0:
                    cap = max(self._min_discharge_w, min(int(solar_w), self._max_discharge_w))
                    logger.info(
                        "ZFI soft-limit (from pause): SoC %d%%, solar %.0fW → cap %dW",
                        soc,
                        solar_w,
                        cap,
                    )
                    self._zfi_state = ZFIState.SOFT_LIMITED
                    self._zfi_soc_limit_cap_w = cap
                    if self._active_regulator:
                        self._active_regulator.reset()
                    await self._battery.start_discharge()
                # else: still fully paused – no action

            elif self._zfi_state == ZFIState.SOFT_LIMITED:
                if soc >= self._min_soc_resume_pct:
                    logger.info(
                        "ZFI soft-limit ended: SoC %d%% >= %d%%",
                        soc,
                        self._min_soc_resume_pct,
                    )
                    self._zfi_state = ZFIState.RUNNING
                    self._zfi_soc_limit_cap_w = 0
                    if self._active_regulator:
                        self._active_regulator.reset()
                    # Battery is already running – no start_discharge() needed
                elif soc <= self._min_soc_pct and solar_w <= 0:
                    logger.info(
                        "ZFI full pause (from soft-limit): SoC %d%% <= min %d%%, no PV",
                        soc,
                        self._min_soc_pct,
                    )
                    self._zfi_state = ZFIState.PAUSED_LOW_SOC
                    self._zfi_soc_limit_cap_w = 0
                    await self._battery.stop()
                else:
                    # Still in hysteresis – refresh cap
                    self._zfi_soc_limit_cap_w = max(
                        self._min_discharge_w, min(int(solar_w), self._max_discharge_w)
                    )

            elif self._zfi_state == ZFIState.RUNNING:
                if soc <= self._min_soc_pct and solar_w <= 0:
                    logger.info(
                        "ZFI full pause: SoC %d%% <= min %d%%, no PV",
                        soc,
                        self._min_soc_pct,
                    )
                    self._zfi_state = ZFIState.PAUSED_LOW_SOC
                    await self._battery.stop()
                elif soc <= self._min_soc_pct:
                    cap = max(self._min_discharge_w, min(int(solar_w), self._max_discharge_w))
                    logger.info(
                        "ZFI soft-limit: SoC %d%% reached min %d%%, cap %dW",
                        soc,
                        self._min_soc_pct,
                        cap,
                    )
                    self._zfi_state = ZFIState.SOFT_LIMITED
                    self._zfi_soc_limit_cap_w = cap
                    # Battery already running – no start_discharge() needed

        # ── Full-battery guard (skip when in a low-SoC pause) ────────────────
        if self._zfi_state == ZFIState.PAUSED_LOW_SOC:
            return

        if soc is not None:
            household_w = sample.total_grid_w
            if soc >= self._full_soc_pct:
                if self._zfi_state == ZFIState.PAUSED_FULL:
                    # Already paused – feed the bypass guard and check if resuming is safe.
                    # Theoretical battery setpoint ≈ household consumption:
                    #   total_grid_w + solar_input_w   (solar goes directly to load in bypass)
                    solar_w = float(sample.solar_input_w or 0.0)
                    theoretical_sp_w = household_w + solar_w
                    self._bypass_guard.add_sample(sample.timestamp, theoretical_sp_w, solar_w)

                    if self._bypass_guard.is_safe_to_start(sample.timestamp):
                        # Guard cleared: demand has consistently exceeded solar + offset
                        # for the full observation window → safe to start discharging.
                        logger.info(
                            "ZFI full-battery pause ended: bypass guard cleared "
                            "(window=%.0fs, all setpoints > solar_max+%.0fW)",
                            self._bypass_guard.window_s,
                            self._bypass_guard.safety_offset_w,
                        )
                        self._zfi_state = ZFIState.RUNNING
                        self._bypass_guard.reset()
                        if self._active_regulator is not None:
                            self._active_regulator.reset()
                        await self._battery.start_discharge()
                else:
                    # Running (RUNNING / SOFT_LIMITED) at max SoC – enter pause when load is low.
                    if household_w <= self._full_soc_resume_threshold_w:
                        logger.info(
                            "ZFI full-battery pause: SoC %d%% >= max %d%%, "
                            "load %.0fW <= threshold %.0fW – bypass guard started "
                            "(window=%.0fs, offset=%.0fW)",
                            soc,
                            self._full_soc_pct,
                            household_w,
                            self._full_soc_resume_threshold_w,
                            self._bypass_guard.window_s,
                            self._bypass_guard.safety_offset_w,
                        )
                        self._zfi_state = ZFIState.PAUSED_FULL
                        # Reset guard so a clean observation window starts from now.
                        self._bypass_guard.reset()
                        await self._battery.stop()
            elif self._zfi_state == ZFIState.PAUSED_FULL:
                # SoC dropped below max → bypass mode ended → resume immediately.
                logger.info(
                    "ZFI full-battery pause ended: SoC %s%% < max %d%%",
                    soc,
                    self._full_soc_pct,
                )
                self._zfi_state = ZFIState.RUNNING
                self._bypass_guard.reset()
                if self._active_regulator is not None:
                    self._active_regulator.reset()
                await self._battery.start_discharge()

    async def _apply_high_soc_charge_limit(self, sample: "GridSample") -> None:
        """Reduce AC charge power when SoC exceeds the configured upper threshold."""
        soc = sample.soc_percent
        if soc is None:
            return
        current_pw = (
            self._charge_power_w if self._charge_power_w is not None else self._min_discharge_w
        )
        if soc > self._high_soc_charge_limit_pct:
            base_pw = (
                self._ac_charge_requested_power_w
                if self._ac_charge_requested_power_w is not None
                else current_pw
            )
            limit_w = (
                self._high_soc_charge_limit_w
                if self._high_soc_charge_limit_w is not None
                else max(50, self._resolve_default_high_soc_charge_limit_w(base_pw))
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

    def _resolve_default_high_soc_charge_limit_w(self, fallback_base_pw: int) -> int:
        """Return default high-SoC AC charge cap.

        Priority:
          1) Half of the battery's maximum AC charge power from API/HW limits.
          2) Fallback: half of the current requested/manual charge power.
        """
        max_charge_w = self._battery.max_charge_power

        if max_charge_w is not None and max_charge_w > 0:
            return max_charge_w // 2
        return int(fallback_base_pw) // 2

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
            zfi_state=self._zfi_state,
            zfi_soc_limit_cap_w=self._zfi_soc_limit_cap_w
            if self._zfi_state == ZFIState.SOFT_LIMITED
            else None,
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
