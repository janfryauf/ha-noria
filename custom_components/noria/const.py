"""Constants for the Noria Online Monitoring integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "noria"
LOGGER = logging.getLogger(__package__)

MANUFACTURER: Final = "Noria Technology"

UPDATE_INTERVAL: Final = timedelta(minutes=5)
PUMP_TIMES_INTERVAL: Final = timedelta(hours=6)

STORAGE_VERSION: Final = 1
STORE_SAVE_DELAY: Final = 10
