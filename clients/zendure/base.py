"""BatteryManager – mode management and operational layer.

This module defines:
- BatteryManager: inherits SolarFlowBattery, adds mode management

The manager layer handles:
- Mode transitions (start_discharge, start_charge, stop)
- Two-step startup coordination
- Settled-state detection
- Bypass overhead configuration

Battery-level functionality (cache, validation, SOC guards, getters/setters)
is in ``aiozen.SolarFlowBattery``.
"""

import asyncio
import logging
from time import time
from typing import Optional

from ZeroPythia.controller.regulator import BatteryInverterProtocol

from .aiozen import SolarFlowBattery
from .models import ACMode

logger = logging.getLogger(__name__)


class BatteryManager(SolarFlowBattery, BatteryInverterProtocol):
    """Mode management layer on top of SolarFlowBattery.

    Adds:
    - start_discharge / start_charge / stop (with two-step startup)
    - is_settled detection
    - bypass_overhead_w configuration

    Subclasses implement the HAL:
    - _fetch_response() – pure HW access
    - _set_properties() – HW write access
    """

    def __init__(self, device_ip: str, *, cache_ttl: float = 1.0):
        """Initialise manager components.

        Args:
            device_ip: IP address of the SolarFlow device
            cache_ttl: cache time-to-live in seconds
        """
        super().__init__(device_ip=device_ip, cache_ttl=cache_ttl)
        self.bypass_overhead_w: int = 30  # W added above solar during bypass kick

    # ==================== Two-step startup helpers ====================

    async def _await_power_settled(
        self,
        target_w: int,
        *,
        is_charge: bool = False,
        timeout_s: float = 15.0,
        poll_s: float = 1.0,
    ) -> bool:
        """Wait until the measured output/input power is within 2 W of *target_w*.

        Used during phase 1 of the two-step mode startup to confirm the inverter
        has physically reached minimum power before ramping up to the actual
        setpoint.

        Returns:
            True when settled within tolerance, False on timeout.
        """
        deadline = time() + timeout_s
        while time() < deadline:
            state = await self.get_state(use_cache=False)
            if state is not None:
                current = state.grid_input_power if is_charge else state.output_home_power
                if abs(current - target_w) <= 2:
                    logger.debug(
                        "_await_power_settled: %s=%dW target=%dW ✓",
                        "grid_in" if is_charge else "output",
                        current,
                        target_w,
                    )
                    return True
            await asyncio.sleep(poll_s)
        return False

    async def _await_setpoint_confirmed(
        self,
        expected_w: int,
        *,
        is_charge: bool = False,
        timeout_s: float = 5.0,
        poll_s: float = 0.5,
    ) -> bool:
        """Wait until the device API reports the correct setpoint limit.

        Used during phase 2 of the two-step startup to confirm the device has
        registered the new setpoint.

        Returns:
            True when confirmed, False on timeout.
        """
        deadline = time() + timeout_s
        while time() < deadline:
            if is_charge:
                actual = await self.get_ac_input_limit(use_cache=False)
            else:
                actual = await self.get_ac_output_limit(use_cache=False)
            if actual is not None and actual == expected_w:
                logger.debug(
                    "_await_setpoint_confirmed: %s=%dW ✓",
                    "inputLimit" if is_charge else "outputLimit",
                    actual,
                )
                return True
            await asyncio.sleep(poll_s)
        return False

    # ==================== Mode Management ====================

    async def is_settled(self, *, use_cache: bool = True) -> Optional[bool]:
        """Check whether the inverter is settled at the current setpoint.

        Discharge mode: ``output_home_power`` ≈ setpoint_w
        Charge mode:    ``grid_input_power``  ≈ setpoint_w
        Idle:           output ≈ 0

        Returns:
            True if settled, False if not, None on error or when bypass active.
        """
        state = await self.get_state(use_cache=use_cache)
        if not state:
            return None

        if self._current_mode == ACMode.INPUT:
            current_power = state.grid_input_power
            target = self._setpoint_w
        else:
            if state.bypass_mode and self._setpoint_w > 0:
                logger.debug(
                    "is_settled: bypass active (solar=%d W) – not meaningful",
                    state.solar_input_power,
                )
                return None
            current_power = state.output_home_power
            target = self._setpoint_w

        settled = abs(current_power - target) < 2
        logger.debug(
            "is_settled: %s=%dW target=%dW diff=%dW → %s",
            "grid_in" if self._current_mode == ACMode.INPUT else "output",
            current_power,
            target,
            abs(current_power - target),
            settled,
        )
        return settled

    async def start_discharge(self) -> int:
        """Start discharging.  Returns the initial setpoint set (W), 0 on hardware error.

        The start setpoint is determined automatically:

        * **Already discharging**: re-send current setpoint.
        * **Bypass active**: target = ``solar_input_power + bypass_overhead_w``.
        * **Cold start**: target = ``min_power`` (at least 1 W).

        After this method returns the caller may use :meth:`set_ac_output_limit` to
        fine-tune the setpoint without another startup sequence.
        """
        if self._limits.discharge_limit == 0:
            logger.debug("start_discharge: limits not yet known – reading device state…")
            await self.get_state(use_cache=False)

        state = await self.get_state(use_cache=False)
        if state is None:
            logger.error("start_discharge: cannot read device state")
            return 0

        solar_w = state.solar_input_power
        already_discharging = self._current_mode == ACMode.OUTPUT and self._setpoint_w > 0
        in_bypass = state.bypass_mode

        target: int
        if already_discharging:
            target = self._setpoint_w
            logger.info("start_discharge: already at %dW – re-confirming setpoint", target)
        elif in_bypass:
            raw = solar_w + self.bypass_overhead_w
            lo = self._limits.min_power or 1
            hi = self._limits.discharge_limit or 800
            target = min(max(lo, raw), hi)
            logger.info(
                "start_discharge: bypass active (solar=%dW) → %dW to force bypass off",
                solar_w,
                target,
            )
        else:
            target = max(1, self._limits.min_power)
            logger.info("start_discharge: cold start at %dW (min_power)", target)

        # Apply SOC-based discharge cap
        soc_cap = self._soc_discharge_cap()
        if soc_cap is not None and target > soc_cap:
            logger.info(
                "start_discharge: SOC %d%% < %d%% → start target capped %dW → %dW",
                self._cached_soc,
                self.low_soc_threshold_pct,
                target,
                soc_cap,
            )
            target = soc_cap

        self._flush_energy_to_now()
        props = {"acMode": ACMode.OUTPUT.value, "outputLimit": target, "inputLimit": 0}
        min_pw = self._limits.min_power or 1
        ok: bool

        if already_discharging or in_bypass:
            ok = await self._set_properties(props, smart_mode=True)
            if ok and in_bypass:
                if not await self._await_setpoint_confirmed(target, is_charge=False):
                    logger.warning(
                        "start_discharge: bypass-kick setpoint %dW not confirmed", target
                    )
        elif target > min_pw:
            # Cold start above minimum: two-step
            logger.info("start_discharge: phase 1 – %dW (min), waiting to settle…", min_pw)
            ok = await self._set_properties(
                {"acMode": ACMode.OUTPUT.value, "outputLimit": min_pw, "inputLimit": 0},
                smart_mode=True,
            )
            if not ok:
                logger.error("start_discharge: phase 1 failed")
                return 0
            if not await self._await_power_settled(min_pw, is_charge=False):
                logger.warning(
                    "start_discharge: inverter did not settle at %dW – proceeding", min_pw
                )
            logger.info("start_discharge: phase 2 – %dW (target)", target)
            ok = await self._set_properties(props, smart_mode=True)
            if ok and not await self._await_setpoint_confirmed(target, is_charge=False):
                logger.warning("start_discharge: setpoint %dW not confirmed in API", target)
        else:
            ok = await self._set_properties(props, smart_mode=True)
            if ok:
                if not await self._await_setpoint_confirmed(target, is_charge=False):
                    logger.warning("start_discharge: setpoint %dW not confirmed", target)
                if not await self._await_power_settled(target, is_charge=False):
                    logger.warning(
                        "start_discharge: inverter did not settle at %dW – proceeding", target
                    )

        if not ok:
            logger.error("start_discharge: hardware error – command failed")
            return 0

        self._current_mode = ACMode.OUTPUT
        self._setpoint_w = target
        logger.info("start_discharge: started at %dW", target)
        return target

    async def start_charge(self) -> int:
        """Start AC charging.  Returns the initial setpoint set (W), 0 on hardware error.

        The start setpoint is determined automatically:

        * **Already charging**: re-send current setpoint.
        * **Cold start**: target = ``min_power`` (at least 1 W).

        After this method returns the caller may use :meth:`set_ac_input_limit` to
        raise the charge power without another startup sequence.
        """
        if self._limits.charge_limit == 0:
            logger.debug("start_charge: limits not yet known – reading device state…")
            await self.get_state(use_cache=False)

        already_charging = self._current_mode == ACMode.INPUT and self._setpoint_w > 0
        target: int
        if already_charging:
            target = self._setpoint_w
            logger.info("start_charge: already at %dW – re-confirming setpoint", target)
        else:
            target = max(1, self._limits.min_power)
            logger.info("start_charge: cold start at %dW (min_power)", target)

        # Apply SOC-based charge cap
        soc_cap = self._soc_charge_cap()
        if soc_cap is not None and target > soc_cap:
            logger.info(
                "start_charge: SOC %d%% > %d%% → start target capped %dW → %dW",
                self._cached_soc,
                self.high_soc_threshold_pct,
                target,
                soc_cap,
            )
            target = soc_cap

        self._flush_energy_to_now()
        props = {"acMode": ACMode.INPUT.value, "inputLimit": target, "outputLimit": 0}
        min_pw = self._limits.min_power or 1
        ok: bool

        if already_charging:
            ok = await self._set_properties(props, smart_mode=True)
        elif target > min_pw:
            # Cold start above minimum: two-step
            logger.info("start_charge: phase 1 – %dW (min), waiting to settle…", min_pw)
            ok = await self._set_properties(
                {"acMode": ACMode.INPUT.value, "inputLimit": min_pw, "outputLimit": 0},
                smart_mode=True,
            )
            if not ok:
                logger.error("start_charge: phase 1 failed")
                return 0
            if not await self._await_power_settled(min_pw, is_charge=True):
                logger.warning("start_charge: inverter did not settle at %dW – proceeding", min_pw)
            logger.info("start_charge: phase 2 – %dW (target)", target)
            ok = await self._set_properties(props, smart_mode=True)
            if ok and not await self._await_setpoint_confirmed(target, is_charge=True):
                logger.warning("start_charge: setpoint %dW not confirmed in API", target)
        else:
            ok = await self._set_properties(props, smart_mode=True)
            if ok and not await self._await_setpoint_confirmed(target, is_charge=True):
                logger.warning("start_charge: setpoint %dW not confirmed", target)

        if not ok:
            logger.error("start_charge: hardware error – command failed")
            return 0

        self._current_mode = ACMode.INPUT
        self._setpoint_w = target
        logger.info("start_charge: started at %dW", target)
        return target

    async def stop(self, *, smart_mode: bool = True) -> bool:
        """Stop all activity.

        Args:
            smart_mode: True = RAM only, False = write to flash
        """
        logger.info("stop: acMode=OUTPUT outputLimit=0 inputLimit=0")
        self._flush_energy_to_now()
        success = await self._set_properties(
            {
                "acMode": ACMode.OUTPUT.value,
                "outputLimit": 0,
                "inputLimit": 0,
            },
            smart_mode,
        )
        if success:
            self._current_mode = ACMode.OUTPUT
            self._setpoint_w = 0
        if not success:
            logger.warning("stop: command not confirmed")
        return success
