"""Backward-compatibility re-exports from ``src.controller.regulator``.

The battery/inverter protocol and regulator base class live in the controller
layer.  This module simply re-exports them so that existing imports of the
form ``from src.dashboard.regulator import ...`` continue to work.

.. deprecated::
    Import directly from ``src.controller.regulator`` instead.
"""

from src.controller.regulator import (  # noqa: F401
    BatteryInverterProtocol,
    BatteryStateProtocol,
    RegulatorBase,
)
