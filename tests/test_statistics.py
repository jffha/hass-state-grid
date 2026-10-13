"""Regression tests for delayed, revised and overlapping provider history."""

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.state_grid import statistics
from custom_components.state_grid.const import TOU_STATISTICS
from custom_components.state_grid.models import AccountUsage, DailyReading, PowerAccount
from custom_components.state_grid.statistics import (
    EnergyStatisticsImporter,
    energy_statistic_id,
)


def _account(number="1234567890123"):
    return PowerAccount(
        account_id=number,
        cons_no=number,
        cons_no_src=number,
        pro_no="42101",
        org_no="org",
        elec_type="01",
        name="家庭",
        address="",
    )


def _usage(values, *, as_of=date(2026, 10, 13), number="1234567890123", tou=None):
    tou = tou or {}
    return AccountUsage(
        account=_account(number),
        readings=tuple(
            DailyReading(
                date.fromisoformat(day),
                value,
                None,
                *(tou.get(day, {}).get(field) for field in TOU_STATISTICS),
            )
            for day, value in values
        ),
        current_month_total=None,
        as_of=as_of,
    )


def _importer():
    return EnergyStatisticsImporter(SimpleNamespace(is_running=True))


@pytest.fixture
def recorder(monkeypatch):
    """Emulate queued upserts, persisting rows across importer instances."""
    rows = {}
    metadata_by_id = {}
    pending = []

    def submit(_hass, metadata, batch):
        pending.append((metadata, batch))

    async def flush():
        for metadata, batch in pending:
            statistic_id = metadata["statistic_id"]
            metadata_by_id[statistic_id] = dict(metadata)
            target = rows.setdefault(statistic_id, {})
            for row in batch:
                start = row["start"].timestamp()
                target[start] = {**row, "start": start}
        pending.clear()

    def query(_hass, _start, _end, ids, period, units, types):
        assert period == "hour"
        assert units == {"energy": "kWh"}
        assert types == {"state", "sum"}
        return {
            statistic_id: [row for _, row in sorted(rows.get(statistic_id, {}).items())]
            for statistic_id in ids
        }

    def metadata(_hass, *, statistic_ids):
        return {
            statistic_id: (1, value)
            for statistic_id, value in metadata_by_id.items()
            if statistic_id in statistic_ids
        }

    class Recorder(SimpleNamespace):
        @property
        def async_db_ready(self):
            future = asyncio.get_running_loop().create_future()
            future.set_result(True)
            return future

    recorder = Recorder(
        async_block_till_done=AsyncMock(side_effect=flush),
        async_add_executor_job=AsyncMock(side_effect=lambda job, *args: job(*args)),
        rows=rows,
        metadata=metadata_by_id,
        submit=Mock(side_effect=submit),
    )
    monkeypatch.setattr(statistics, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(statistics, "statistics_during_period", query)
    monkeypatch.setattr(statistics, "get_metadata", metadata)
    monkeypatch.setattr(statistics, "async_add_external_statistics", recorder.submit)
    return recorder


async def _sync(importer, recorder, usage):
    await importer.async_import({usage.account.account_id: usage})
    await recorder.async_block_till_done()
    return recorder.rows.get(energy_statistic_id(usage.account), {})


def _daily_rows(rows):
    return {
        datetime.fromtimestamp(start, statistics.CHINA_TZ).date().isoformat(): row
        for start, row in rows.items()
        if datetime.fromtimestamp(start, statistics.CHINA_TZ).hour == 23
    }


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (None, ("usage", "valley", "flat")),
        ([], ("usage",)),
        (["peak"], ("usage", "peak")),
        (TOU_STATISTICS, ("usage", *TOU_STATISTICS)),
    ],
)
def test_only_enabled_series_are_read_and_imported(recorder, selected, expected):
    async def run():
        hass = SimpleNamespace(is_running=True)
        importer = (
            EnergyStatisticsImporter(hass)
            if selected is None
            else EnergyStatisticsImporter(hass, selected)
        )
        usage = _usage(
            [("2026-10-11", 10)],
            tou={"2026-10-11": dict(zip(TOU_STATISTICS, (1, 2, 3, 4)))},
        )
        await _sync(importer, recorder, usage)
        ids = {energy_statistic_id(usage.account, field) for field in expected}
        assert set(recorder.rows) == ids
        assert recorder.async_add_executor_job.call_args.args[4] == ids
        assert recorder.async_add_executor_job.await_count == 2
        for field in expected:
            rows = recorder.rows[energy_statistic_id(usage.account, field)]
            expected_value = 10 if field == "usage" else TOU_STATISTICS.index(field) + 1
            assert _daily_rows(rows)["2026-10-11"]["sum"] == expected_value
        names = {call.args[1]["name"] for call in recorder.submit.call_args_list}
        assert names == {
            f"{statistics.STATISTIC_NAMES[field]}（0123）" for field in expected
        }

    asyncio.run(run())


def test_upgrade_renames_existing_series_without_changing_consumption(recorder):
    async def run():
        usage = _usage(
            [("2026-10-11", 10)],
            tou={"2026-10-11": dict(zip(TOU_STATISTICS, (1, 2, 3, 4)))},
        )
        await _sync(
            EnergyStatisticsImporter(SimpleNamespace(is_running=True), TOU_STATISTICS),
            recorder,
            usage,
        )
        old_rows = {sid: dict(rows) for sid, rows in recorder.rows.items()}
        for metadata in recorder.metadata.values():
            metadata["name"] = "国家电网 99:opaque-account-name 0123 每日用电量"
        recorder.submit.reset_mock()
        importer = EnergyStatisticsImporter(
            SimpleNamespace(is_running=True), TOU_STATISTICS
        )
        await _sync(importer, recorder, usage)
        assert recorder.rows == old_rows
        assert recorder.submit.call_count == 5
        for call in recorder.submit.call_args_list:
            assert call.args[2] == []
        for field in ("usage", *TOU_STATISTICS):
            sid = energy_statistic_id(usage.account, field)
            assert (
                recorder.metadata[sid]["name"]
                == f"{statistics.STATISTIC_NAMES[field]}（0123）"
            )
        recorder.submit.reset_mock()
        await _sync(importer, recorder, usage)
        recorder.submit.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("field", TOU_STATISTICS)
def test_missing_invalid_and_zero_tou_values_are_independent(recorder, field):
    async def run():
        importer = EnergyStatisticsImporter(SimpleNamespace(is_running=True), [field])
        usage = _usage([("2026-10-05", None)], tou={"2026-10-05": {field: 7}})
        await _sync(importer, recorder, usage)
        # A valid time-of-use reading is useful even when the total is missing.
        sid = energy_statistic_id(usage.account, field)
        assert energy_statistic_id(usage.account) not in recorder.rows
        await _sync(
            importer,
            recorder,
            _usage(
                [(f"2026-10-{day:02}", None) for day in range(5, 15)],
                tou={
                    f"2026-10-{day:02}": {field: value}
                    for day, value in zip(
                        range(5, 15),
                        (None, float("nan"), -1, float("inf"), None, 0, 2, 3, 5, 6),
                    )
                },
            ),
        )
        days = _daily_rows(recorder.rows[sid])
        assert set(days) == {"2026-10-05", "2026-10-10", "2026-10-11", "2026-10-12"}
        assert [row["state"] for row in days.values()] == [7, 0, 2, 3]
        assert days["2026-10-12"]["sum"] == 12

    asyncio.run(run())


def test_disabling_keeps_history_and_reenabling_corrects_each_series(recorder):
    async def run():
        hass = SimpleNamespace(is_running=True)
        usage = _usage(
            [("2026-10-09", 10), ("2026-10-10", 20)],
            tou={
                "2026-10-09": dict(zip(TOU_STATISTICS, (1, 2, 3, 4))),
                "2026-10-10": dict(zip(TOU_STATISTICS, (2, 4, 6, 8))),
            },
        )
        await _sync(EnergyStatisticsImporter(hass, TOU_STATISTICS), recorder, usage)
        disabled_ids = {
            energy_statistic_id(usage.account, field) for field in TOU_STATISTICS
        }
        old_rows = {sid: dict(recorder.rows[sid]) for sid in disabled_ids}
        revised = _usage(
            [("2026-10-09", 30)],
            tou={"2026-10-09": dict(zip(TOU_STATISTICS, (0, 7, 9, 14)))},
        )
        await _sync(EnergyStatisticsImporter(hass, []), recorder, revised)
        assert {sid: recorder.rows[sid] for sid in disabled_ids} == old_rows
        # Only the enabled total was read while the other series were disabled.
        assert recorder.async_add_executor_job.call_args.args[4] == {
            energy_statistic_id(usage.account)
        }
        # Re-enable with a limited API window: correct the old day while retaining
        # the later day that now exists only in Recorder's durable ledger.
        importer = EnergyStatisticsImporter(hass, TOU_STATISTICS)
        await _sync(importer, recorder, revised)
        for field, corrected, later in zip(TOU_STATISTICS, (0, 7, 9, 14), (2, 4, 6, 8)):
            days = _daily_rows(recorder.rows[energy_statistic_id(usage.account, field)])
            assert days["2026-10-09"]["state"] == corrected
            assert days["2026-10-10"]["state"] == later
            assert days["2026-10-10"]["sum"] == corrected + later
        count = recorder.submit.call_count
        await _sync(EnergyStatisticsImporter(hass, TOU_STATISTICS), recorder, revised)
        assert recorder.submit.call_count == count

    asyncio.run(run())


def test_series_ids_preserve_total_and_do_not_collide():
    account = _account()
    assert energy_statistic_id(account) == "state_grid:bca2b41a2b25e137c83fee34_energy"
    ids = {energy_statistic_id(account, field) for field in ("usage", *TOU_STATISTICS)}
    assert len(ids) == 5
    assert all(account.cons_no_src not in sid for sid in ids)
    with pytest.raises(ValueError):
        energy_statistic_id(account, "unknown")


def test_delayed_first_day_is_counted_on_actual_date(recorder):
    async def run():
        usage = _usage([("2026-10-11", 8)])
        rows = await _sync(_importer(), recorder, usage)
        assert len(rows) == 2
        baseline, end = list(rows.values())
        assert baseline["start"] == datetime(2026, 10, 10, 16, tzinfo=UTC).timestamp()
        assert baseline["sum"] == 0
        assert end["start"] == datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp()
        assert end["state"] == end["sum"] == 8
        assert end["sum"] - baseline["sum"] == 8
        assert set(_daily_rows(rows)) == {"2026-10-11"}

    asyncio.run(run())
    metadata = recorder.submit.call_args.args[1]
    assert metadata["source"] == "state_grid"
    assert metadata["unit_of_measurement"] == "kWh"
    assert metadata["unit_class"] == "energy"
    assert metadata["mean_type"] is statistics.StatisticMeanType.NONE
    assert metadata["has_sum"] is True


def test_duplicate_sync_and_restart_do_not_add_consumption(recorder):
    async def run():
        usage = _usage([("2026-10-11", 8), ("2026-10-12", 6)])
        importer = _importer()
        await _sync(importer, recorder, usage)
        await _sync(importer, recorder, usage)
        rows = await _sync(_importer(), recorder, usage)
        assert len(rows) == 4
        assert _daily_rows(rows)["2026-10-12"]["sum"] == 14

    asyncio.run(run())
    recorder.submit.assert_called_once()


@pytest.mark.parametrize("corrected, expected", [(5, 11), (1, 7), (0, 6)])
def test_revision_updates_later_totals_outside_returned_window(
    recorder, corrected, expected
):
    async def run():
        importer = _importer()
        await _sync(
            importer,
            recorder,
            _usage([("2026-10-01", 2), ("2026-10-02", 3), ("2026-10-03", 4)]),
        )
        rows = await _sync(importer, recorder, _usage([("2026-10-02", corrected)]))
        days = _daily_rows(rows)
        assert days["2026-10-01"]["sum"] == 2
        assert days["2026-10-02"]["state"] == corrected
        assert days["2026-10-03"]["state"] == 4
        assert days["2026-10-03"]["sum"] == expected
        # Correct both the next day's baseline and end-of-day total.
        batch = recorder.submit.call_args.args[2]
        assert any(
            row["start"] == datetime(2026, 10, 2, 16, tzinfo=UTC)
            and row["sum"] == 2 + corrected
            for row in batch
        )

    asyncio.run(run())


def test_backfilled_gap_and_earlier_history_keep_daily_totals(recorder):
    async def run():
        importer = _importer()
        rows = await _sync(
            importer, recorder, _usage([("2026-10-01", 2), ("2026-10-03", 4)])
        )
        assert "2026-10-02" not in _daily_rows(rows)
        rows = await _sync(
            importer, recorder, _usage([("2026-09-30", 1), ("2026-10-02", 3)])
        )
        days = _daily_rows(rows)
        assert {day: row["state"] for day, row in days.items()} == {
            "2026-09-30": 1,
            "2026-10-01": 2,
            "2026-10-02": 3,
            "2026-10-03": 4,
        }
        assert days["2026-10-03"]["sum"] == 10
        assert len(rows) == 8

    asyncio.run(run())


def test_invalid_missing_and_unfinished_days_do_not_overwrite_history(recorder):
    async def run():
        importer = _importer()
        await _sync(importer, recorder, _usage([("2026-10-10", 7)]))
        rows = await _sync(
            importer,
            recorder,
            _usage(
                [
                    ("2026-10-09", None),
                    ("2026-10-10", float("nan")),
                    ("2026-10-11", -1),
                    ("2026-10-11", float("inf")),
                    ("2026-10-12", 0),
                    ("2026-10-13", 5),
                    ("2026-10-14", 6),
                ]
            ),
        )
        days = _daily_rows(rows)
        assert set(days) == {"2026-10-10", "2026-10-12"}
        assert days["2026-10-10"]["state"] == 7
        assert days["2026-10-12"]["state"] == 0
        assert days["2026-10-12"]["sum"] == 7
        await _sync(importer, recorder, _usage([]))
        assert set(_daily_rows(rows)) == set(days)

    asyncio.run(run())


def test_month_and_year_rollover_do_not_reset_statistics(recorder):
    async def run():
        importer = _importer()
        await _sync(
            importer,
            recorder,
            _usage([("2026-12-31", 3)], as_of=date(2027, 1, 2)),
        )
        rows = await _sync(
            importer,
            recorder,
            _usage([("2027-01-01", 5)], as_of=date(2027, 1, 3)),
        )
        assert _daily_rows(rows)["2027-01-01"]["sum"] == 8

    asyncio.run(run())


def test_failed_import_can_be_retried_without_losing_days(recorder):
    async def run():
        importer = _importer()
        usage = _usage([("2026-10-11", 8)])
        submit = recorder.submit.side_effect
        recorder.submit.side_effect = HomeAssistantError("Recorder unavailable")
        with pytest.raises(HomeAssistantError):
            await importer.async_import({usage.account.account_id: usage})
        recorder.submit.side_effect = submit
        rows = await _sync(importer, recorder, usage)
        assert _daily_rows(rows)["2026-10-11"]["sum"] == 8

    asyncio.run(run())


def test_empty_history_does_not_require_recorder(recorder):
    asyncio.run(_importer().async_import({"account": _usage([])}))
    recorder.async_add_executor_job.assert_not_awaited()
    recorder.submit.assert_not_called()


def test_multiple_accounts_with_same_suffix_are_isolated(recorder):
    async def run():
        importer = _importer()
        first = _usage([("2026-10-11", 8)], number="1234567890123")
        second = _usage([("2026-10-11", 2)], number="9876543890123")
        await importer.async_import({"first": first, "second": second})
        await recorder.async_block_till_done()
        assert energy_statistic_id(first.account) != energy_statistic_id(second.account)
        assert len(recorder.rows) == 2
        assert (
            _daily_rows(recorder.rows[energy_statistic_id(first.account)])[
                "2026-10-11"
            ]["sum"]
            == 8
        )
        assert (
            _daily_rows(recorder.rows[energy_statistic_id(second.account)])[
                "2026-10-11"
            ]["sum"]
            == 2
        )

    asyncio.run(run())


def test_initial_refresh_does_not_wait_for_recorder_startup_queue(recorder):
    async def run():
        importer = EnergyStatisticsImporter(SimpleNamespace(is_running=False))
        usage = _usage([("2026-10-11", 8)])
        await importer.async_import({usage.account.account_id: usage})
        recorder.async_block_till_done.assert_not_awaited()
        assert not recorder.rows
        await recorder.async_block_till_done()
        assert (
            _daily_rows(recorder.rows[energy_statistic_id(usage.account)])[
                "2026-10-11"
            ]["sum"]
            == 8
        )

    asyncio.run(run())
