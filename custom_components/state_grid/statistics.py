"""Import delayed daily consumption using its actual date in China.

Recorder is also the durable daily ledger: the last hour of each published day
stores that day's consumption in ``state`` and the running total in ``sum``.
Rebuilding from these records preserves data outside the API's rolling window
and corrects all later totals when the provider revises an earlier day.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DEFAULT_TOU_STATISTICS, DOMAIN, TOU_STATISTICS
from .models import CHINA_TZ, AccountUsage, PowerAccount

STATISTIC_NAMES = {
    "usage": "每日用电量",
    "valley": "每日谷段用电量",
    "flat": "每日平段用电量",
    "peak": "每日峰段用电量",
    "tip": "每日尖段用电量",
}


def energy_statistic_id(account: PowerAccount, field: str = "usage") -> str:
    """Use the full consumption number, without exposing it in the statistic ID."""
    if field not in STATISTIC_NAMES:
        raise ValueError(f"Unknown energy statistic field: {field}")
    identity = hashlib.sha256(account.cons_no_src.encode()).hexdigest()[:24]
    suffix = "" if field == "usage" else f"_{field}"
    return f"{DOMAIN}:{identity}_energy{suffix}"


def _valid_usage(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value >= 0


def _daily_ledger(rows: Iterable[StatisticsRow]) -> dict[date, float]:
    """Recover completed days from this integration's own end-of-day records."""
    days = {}
    for row in rows:
        start = datetime.fromtimestamp(row["start"], CHINA_TZ)
        value = row.get("state")
        if start.time() == time(23) and _valid_usage(value):
            days[start.date()] = value
    return days


def _daily_statistics(days: Mapping[date, float]) -> list[StatisticData]:
    """Keep each daily increment inside that local day, with no invented hours.

    A zero-increment baseline at 00:00 prevents the first day's usage being lost
    and isolates each day across gaps. The increment at 23:00 is a daily bucket,
    not a claim that electricity was actually consumed in that hour.
    """
    result = []
    total = Decimal(0)
    for day, usage in sorted(days.items()):
        result.append(
            StatisticData(
                start=datetime.combine(day, time.min, CHINA_TZ).astimezone(UTC),
                state=0.0,
                sum=float(total),
            )
        )
        total += Decimal(str(usage))
        result.append(
            StatisticData(
                start=datetime.combine(day, time(23), CHINA_TZ).astimezone(UTC),
                state=usage,
                sum=float(total),
            )
        )
    return result


class EnergyStatisticsImporter:
    """Merge published days and upsert historical energy statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        tou_statistics: Iterable[str] = DEFAULT_TOU_STATISTICS,
    ) -> None:
        self.hass = hass
        selected = frozenset(tou_statistics)
        self.fields = ("usage",) + tuple(
            field for field in TOU_STATISTICS if field in selected
        )
        self._lock = asyncio.Lock()

    async def async_import(self, accounts: Mapping[str, AccountUsage]) -> None:
        async with self._lock:
            for usage in accounts.values():
                await self._async_import_account(usage)

    async def _async_import_account(self, usage: AccountUsage) -> None:
        # Today may be a partial day or a future placeholder in the API. Do not
        # treat it as settled history. Missing/invalid values preserve old data.
        published = {}
        for field in self.fields:
            days = {
                reading.day: getattr(reading, field)
                for reading in usage.readings
                if reading.day < usage.as_of and _valid_usage(getattr(reading, field))
            }
            if days:
                published[field] = days
        if not published:
            return

        statistic_ids = {
            energy_statistic_id(usage.account, field) for field in published
        }
        recorder = get_instance(self.hass)
        if not await asyncio.shield(recorder.async_db_ready):
            raise HomeAssistantError("Recorder database is not ready")
        # Imports are queued. Flush them before reading the ledger, including
        # when a reload/retry immediately follows a previous import. Recorder
        # only processes its queue after HA starts: waiting during first refresh
        # would deadlock startup, so startup imports are simply queued.
        if self.hass.is_running:
            await recorder.async_block_till_done()
        metadata = await recorder.async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids=statistic_ids)
        )
        existing = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(1970, 1, 1, tzinfo=UTC),
            None,
            statistic_ids,
            "hour",
            {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR},
            {"state", "sum"},
        )
        for field, days in published.items():
            statistic_id = energy_statistic_id(usage.account, field)
            previous_metadata = metadata.get(statistic_id)
            self._import_series(
                usage.account,
                field,
                days,
                existing.get(statistic_id, []),
                previous_metadata[1].get("name") if previous_metadata else None,
            )

    def _import_series(
        self,
        account: PowerAccount,
        field: str,
        published: Mapping[date, float],
        rows: list[StatisticsRow],
        previous_name: str | None,
    ) -> None:
        """Correct one enabled series without touching other time-of-use records."""
        days = _daily_ledger(rows)
        days.update(published)
        by_start = {row["start"]: row for row in rows}
        changed = []
        for statistic in _daily_statistics(days):
            previous = by_start.get(statistic["start"].timestamp())
            if previous is None or any(
                previous.get(field) != statistic[field] for field in ("state", "sum")
            ):
                changed.append(statistic)
        name = f"{STATISTIC_NAMES[field]}（{account.cons_no_src[-4:]}）"
        if not changed and name == previous_name:
            return

        metadata = StatisticMetaData(
            statistic_id=energy_statistic_id(account, field),
            source=DOMAIN,
            name=name,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
        )
        # An empty batch updates metadata only: existing names change even when
        # the provider has not published any new consumption since the upgrade.
        async_add_external_statistics(self.hass, metadata, changed)
