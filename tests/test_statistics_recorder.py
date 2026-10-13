"""Verify imported daily changes through Home Assistant's real Recorder."""

import asyncio
from datetime import UTC, date, datetime
from functools import partial

import pytest
from homeassistant import loader
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import async_initialize_recorder
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.state_grid.const import DEFAULT_TOU_STATISTICS, TOU_STATISTICS
from custom_components.state_grid.models import AccountUsage, DailyReading, PowerAccount
from custom_components.state_grid.statistics import (
    STATISTIC_NAMES,
    EnergyStatisticsImporter,
    energy_statistic_id,
)


@pytest.mark.parametrize("fields", [DEFAULT_TOU_STATISTICS, TOU_STATISTICS, ()])
def test_actual_recorder_counts_first_day_and_upserts_corrections(tmp_path, fields):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        async_initialize_recorder(hass)
        hass.config_entries = ConfigEntries(hass, {})
        hass.config.skip_pip = True
        original_zone = dt_util.DEFAULT_TIME_ZONE
        await hass.config.async_set_time_zone("Asia/Shanghai")
        account = PowerAccount(
            account_id="account",
            cons_no="1234567890123",
            cons_no_src="1234567890123",
            pro_no="42101",
            org_no="org",
            elec_type="01",
            name="家庭",
            address="",
        )
        statistic_id = energy_statistic_id(account)
        ratios = {"usage": 1, "valley": 0.25, "flat": 0.5, "peak": 0.125, "tip": 0.125}
        all_ids = {energy_statistic_id(account, field) for field in ratios}

        def history(values):
            return {
                account.account_id: AccountUsage(
                    account,
                    tuple(
                        DailyReading(
                            date(2026, 10, day),
                            value,
                            None,
                            *(value * ratios[field] for field in TOU_STATISTICS),
                        )
                        for day, value in values
                    ),
                    None,
                    as_of=date(2026, 10, 13),
                )
            }

        async def changes():
            # Recorder imports are queued; the test barrier also waits when an
            # import has already left the queue but is still being committed.
            await hass.async_add_executor_job(recorder.block_till_done)
            return await recorder.async_add_executor_job(
                statistics_during_period,
                hass,
                datetime(2026, 9, 30, 16, tzinfo=UTC),
                datetime(2026, 10, 13, 16, tzinfo=UTC),
                all_ids,
                "day",
                {"energy": "kWh"},
                {"change"},
            )

        try:
            assert await async_setup_component(
                hass,
                "recorder",
                {
                    "recorder": {"auto_purge": False, "commit_interval": 0},
                },
            )
            recorder = get_instance(hass)
            importer = EnergyStatisticsImporter(hass, fields)
            # This is the same startup order as config-entry first refresh.
            await asyncio.wait_for(
                importer.async_import(history([(11, 8), (12, 6)])), 10
            )
            await hass.async_start()
            initial = await changes()
            daily = {
                datetime.fromtimestamp(
                    row["start"], dt_util.DEFAULT_TIME_ZONE
                ).day: row["change"]
                for row in initial[statistic_id]
            }
            assert daily == {11: 8, 12: 6}
            assert set(initial) == {
                energy_statistic_id(account, field) for field in ("usage", *fields)
            }
            for field in fields:
                assert [
                    row["change"]
                    for row in initial[energy_statistic_id(account, field)]
                ] == [8 * ratios[field], 6 * ratios[field]]

            # Simulate already-installed statistics with the old long names.
            metadata = await recorder.async_add_executor_job(
                partial(get_metadata, hass, statistic_ids=all_ids)
            )
            for _, value in metadata.values():
                async_add_external_statistics(
                    hass,
                    {
                        **value,
                        "name": "国家电网 99:opaque-account-name 0123 每日用电量",
                    },
                    [],
                )
            assert await changes() == initial
            await EnergyStatisticsImporter(hass, fields).async_import(
                history([(11, 8), (12, 6)])
            )
            assert await changes() == initial
            renamed = await recorder.async_add_executor_job(
                partial(get_metadata, hass, statistic_ids=all_ids)
            )
            assert renamed.keys() == metadata.keys()
            for field in ("usage", *fields):
                sid = energy_statistic_id(account, field)
                assert renamed[sid][1]["name"] == f"{STATISTIC_NAMES[field]}（0123）"

            # Re-instantiating simulates reload/restart with no in-memory ledger.
            await EnergyStatisticsImporter(hass, fields).async_import(
                history([(11, 10)])
            )
            updated = await changes()
            assert [row["change"] for row in updated[statistic_id]] == [10, 6]
            for field in fields:
                assert [
                    row["change"]
                    for row in updated[energy_statistic_id(account, field)]
                ] == [10 * ratios[field], 6 * ratios[field]]
            await importer.async_import(history([(11, 10), (12, 6)]))
            assert await changes() == updated

            # Changing configuration must leave disabled historical series intact.
            await EnergyStatisticsImporter(hass, []).async_import(history([(11, 20)]))
            disabled = await changes()
            assert [row["change"] for row in disabled[statistic_id]] == [20, 6]
            for field in fields:
                sid = energy_statistic_id(account, field)
                assert disabled[sid] == updated[sid]

            # Re-enable with only the newer day still in the response. The older
            # time-of-use reading survives and is not inferred from the total.
            await EnergyStatisticsImporter(hass, fields).async_import(
                history([(12, 8)])
            )
            enabled = await changes()
            assert [row["change"] for row in enabled[statistic_id]] == [20, 8]
            for field in fields:
                assert [
                    row["change"]
                    for row in enabled[energy_statistic_id(account, field)]
                ] == [10 * ratios[field], 8 * ratios[field]]
        finally:
            await hass.async_stop()
            dt_util.set_default_time_zone(original_zone)

    asyncio.run(run())
