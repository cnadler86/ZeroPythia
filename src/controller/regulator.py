"""Abstract base class and battery/inverter protocol for all regulators.

These definitions belong to the controller layer – they describe the contract
between the hardware abstraction (Zendure client) and the control algorithms.
The dashboard runtime and all regulator implementations depend on this module;
the dashboard itself is NOT the owner of these interfaces.

Lifecycle
---------
The ``ControlRuntime`` calls:
  1. ``add_sample(sample)``   – every sampling tick  (~1 s)
  2. ``compute_setpoint(battery)``  – every control tick (~control_interval_s)

The regulator may keep internal state across calls (e.g. queued samples,
oscillation detectors, filter history).  ``reset()`` must clear all state.

Settings
--------
Override ``settings_schema()`` to return a JSON-Schema dict for the GUI to
render a dynamic settings form.  Override ``apply_settings()`` to accept
a flat ``{key: value}`` dict from the GUI.  ``get_current_settings()``
must return the current values in the same format.

Status
------
``get_control_status()`` returns a ``ControlStatus`` snapshot for the last
completed control cycle.  Called after ``compute_setpoint()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, runtime_checkable

from src.runtime.models import ControlStatus, GridSample


class BatteryStateProtocol(Protocol):
    """Typed subset of battery state used by runtime and regulators."""

    battery_soc: int
    grid_input_power: int
    bypass_mode: bool
    solar_input_power: int


@runtime_checkable
class BatteryInverterProtocol(Protocol):
    """Structural protocol for battery/inverter clients (e.g. Zendure SolarFlow).

    Marked @runtime_checkable so structural typing works:
    any class that provides these methods is considered compatible.
    """

    async def get_ac_output_power(self) -> Optional[int]: ...
    async def set_ac_output_limit(self, power_w: int) -> int: ...
    async def set_ac_input_limit(self, power_w: int) -> int: ...
    async def start_discharge(self) -> int: ...
    async def start_charge(self) -> int: ...
    async def stop(self) -> bool: ...
    async def get_ac_output_limit(self) -> Optional[int]: ...
    async def get_ac_input_limit(self) -> Optional[int]: ...
    async def is_settled(self, *, use_cache: bool = True) -> Optional[bool]: ...
    async def get_state(self, *, use_cache: bool = True) -> Optional[BatteryStateProtocol]: ...
    async def get_min_soc(self, *, use_cache: bool = True) -> Optional[int]: ...
    async def get_max_soc(self, *, use_cache: bool = True) -> Optional[int]: ...


class RegulatorBase(ABC):
    """Base class for all battery discharge regulators.

    Subclasses implement zero-feed, simple bang-bang, ML-based, or any
    other control strategy.  The ``ControlRuntime`` remains hardware-agnostic
    and calls these methods on every tick.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique short identifier, e.g. ``"zerofeed_v3"``."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description shown in the GUI."""
        return ""

    # ── Sampling ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def add_sample(self, sample: GridSample) -> None:
        """Receive one hardware sample (~1 s cadence).

        Implementations should buffer the sample for use in the next
        ``compute_setpoint()`` call.
        """
        ...

    # ── Control ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def compute_setpoint(
        self,
        battery: BatteryInverterProtocol,
        max_output_w: int,
        min_output_w: int,
    ) -> Optional[int]:
        """Compute and apply the next battery setpoint.

        Called every control interval (~3 s by default).  May return:
          - ``int`` – the new setpoint that was sent to the battery
          - ``None``  – no change (setpoint unchanged)

        The implementation is responsible for calling
        ``battery.set_ac_output_limit()`` or equivalent.

        Parameters
        ----------
        battery:       Hardware client to read/write.
        max_output_w:  Current discharge cap enforced by the runtime.
        min_output_w:  Minimum allowed discharge power.
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    def reset(self) -> None:
        """Reset all internal state (e.g. when mode changes or regulator is reselected)."""
        ...

    # ── Status ────────────────────────────────────────────────────────────────

    @abstractmethod
    def get_control_status(self) -> ControlStatus:
        """Return a snapshot of the last control cycle for dashboard display."""
        ...

    # ── Settings ──────────────────────────────────────────────────────────────

    def settings_schema(self) -> dict[str, Any]:
        """Return a JSON Schema dict describing the configurable settings.

        The dashboard GUI uses this to render a dynamic form.
        Return ``{}`` if the regulator has no user-configurable settings.
        """
        return {}

    def get_current_settings(self) -> dict[str, Any]:
        """Return current settings values matching the ``settings_schema()`` keys."""
        return {}

    @abstractmethod
    def apply_settings(self, data: dict[str, Any]) -> None:
        """Apply settings from a ``{key: value}`` dict (from the GUI form).

        Implementations should validate values and update internal state.
        Raise ``ValueError`` with a human-readable message on invalid input.
        """
        ...
