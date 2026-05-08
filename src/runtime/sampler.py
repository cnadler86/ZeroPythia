"""Hardware sampling adapter for the control runtime."""

from __future__ import annotations

import logging
import time
from typing import Optional, Protocol

from src.controller.regulator import BatteryInverterProtocol

from .models import GridSample

logger = logging.getLogger(__name__)


class GridMeterProtocol(Protocol):
    async def get_phase_powers(self) -> Optional[tuple[float, float, float]]: ...
    async def get_total_power(self) -> Optional[float]: ...


class RuntimeSampler:
    """Reads one unified runtime sample from grid meter and battery."""

    def __init__(self, grid_meter: GridMeterProtocol, battery: BatteryInverterProtocol) -> None:
        self._grid_meter = grid_meter
        self._battery = battery
        self._last_bypass_state: Optional[bool] = None

    async def read(self) -> Optional[GridSample]:
        try:
            phases = await self._grid_meter.get_phase_powers()
            if phases is None:
                logger.debug("RuntimeSampler: grid meter returned no phase data")
                return None

            batt_output = await self._battery.get_ac_output_power()
            batt_state = await self._battery.get_state()

            soc: int | None = None
            charge_in: float | None = None
            bypass_active: bool | None = None
            solar_input_w: float | None = None

            if batt_state is not None:
                soc = batt_state.battery_soc
                charge_in = float(batt_state.grid_input_power or 0) or None
                bypass_active = batt_state.bypass_mode
                solar_input_w = float(batt_state.solar_input_power)

                if bypass_active != self._last_bypass_state:
                    if bypass_active:
                        logger.warning(
                            "Inverter entered BYPASS mode: PV (%d W) routed directly to house. "
                            "battery_output_w (%s W) reflects solar bypass, not battery output. "
                            "outputLimit commands are ignored!",
                            batt_state.solar_input_power,
                            batt_output,
                        )
                    elif self._last_bypass_state is not None:
                        logger.info("Inverter left bypass mode - battery control active")
                    self._last_bypass_state = bypass_active

            return GridSample(
                timestamp=time.time(),
                phase_a_w=phases[0],
                phase_b_w=phases[1],
                phase_c_w=phases[2],
                battery_output_w=float(batt_output) if batt_output is not None else 0.0,
                soc_percent=soc,
                charge_input_w=charge_in,
                bypass_active=bypass_active,
                solar_input_w=solar_input_w,
            )
        except Exception:
            logger.debug("RuntimeSampler: sample read failed", exc_info=True)
            return None
