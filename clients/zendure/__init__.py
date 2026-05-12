"""Zendure SolarFlow client.

Architecture:
- SolarFlowBattery: Pure battery abstraction (aiozen.py)
- BatteryManager: Mode management layer (base.py)
- SolarFlowAsyncClient: HTTP implementation (http_client.py)
"""

from .aiozen import ISolarFlowClient, SolarFlowBattery
from .base import BatteryManager
from .http_client import SolarFlowAsyncClient

__all__ = [
    "ISolarFlowClient",
    "SolarFlowBattery",
    "BatteryManager",
    "SolarFlowAsyncClient",
]
