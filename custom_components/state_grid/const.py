"""Constants for the 国家电网 integration."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

DOMAIN = "state_grid"
PLATFORMS = ["sensor"]

CONF_DEVICE_PROFILE = "device_profile"
CONF_LOGIN_SESSION = "login_session"
CONF_AUTH_ERROR = "auth_error"
CONF_SYNTHETIC_DEVICE = "synthetic_device"
CONF_HISTORY_MONTHS = "history_months"
CONF_UPDATE_INTERVAL_HOURS = "update_interval_hours"
CONF_TOU_STATISTICS = "tou_statistics"

TOU_STATISTICS = ("valley", "flat", "peak", "tip")
DEFAULT_TOU_STATISTICS = ("valley", "flat")

DEFAULT_HISTORY_MONTHS = 2
DEFAULT_UPDATE_INTERVAL_HOURS = 12
DEFAULT_UPDATE_INTERVAL = timedelta(hours=DEFAULT_UPDATE_INTERVAL_HOURS)


def enabled_tou_statistics(options: Mapping[str, Any]) -> tuple[str, ...]:
    """Keep configured time-of-use fields in order, including an empty selection."""
    selected = options.get(CONF_TOU_STATISTICS, DEFAULT_TOU_STATISTICS)
    return tuple(field for field in TOU_STATISTICS if field in selected)


SERVER_PUBLIC_KEY_HEX = (
    "0409C10D38CF7B4E28097EAAA519E3157C9B4E72194CD13BD11932CE40ED5624A"
    "FEFF27F78893E4C4FC2029DA147AB6CEAE0CDC8E3A547EFCDC5AB91757FE1CA60"
)
CLIENT_PRIVATE_KEY_HEX = (
    "50C4AF48DF75808050729977AA6BC127DC21A404755C660B94222F9D50FA4A75"
)
