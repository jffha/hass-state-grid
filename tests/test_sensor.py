"""Tests for Home Assistant sensor metadata compatibility."""

from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.state_grid.const import CONF_TOU_STATISTICS
from custom_components.state_grid.models import AccountUsage, PowerAccount
from custom_components.state_grid.sensor import (
    REMOVED_SENSOR_KEYS,
    SENSORS,
    StateGridElectricitySensor,
)
from custom_components.state_grid.statistics import energy_statistic_id


def test_historical_daily_sensors_do_not_claim_live_statistics() -> None:
    descriptions = {description.key: description for description in SENSORS}

    daily_usage = descriptions["latest_daily_usage"]
    assert daily_usage.device_class is SensorDeviceClass.ENERGY
    assert daily_usage.state_class is None

    latest_month_usage = descriptions["latest_month_usage"]
    assert latest_month_usage.device_class is SensorDeviceClass.ENERGY
    assert latest_month_usage.state_class is None

    latest_month_charge = descriptions["latest_month_charge"]
    assert latest_month_charge.device_class is SensorDeviceClass.MONETARY
    assert latest_month_charge.state_class is None


def test_only_current_month_total_remains() -> None:
    descriptions = {description.key: description for description in SENSORS}

    assert descriptions["current_month_usage"].device_class is SensorDeviceClass.ENERGY
    assert (
        descriptions["current_month_usage"].state_class
        is SensorStateClass.TOTAL_INCREASING
    )
    assert REMOVED_SENSOR_KEYS.isdisjoint(descriptions)
    assert "latest_daily_charge" in REMOVED_SENSOR_KEYS


def test_current_year_sensors_are_totals() -> None:
    descriptions = {description.key: description for description in SENSORS}

    assert descriptions["current_year_usage"].device_class is SensorDeviceClass.ENERGY
    assert (
        descriptions["current_year_usage"].state_class
        is SensorStateClass.TOTAL_INCREASING
    )
    assert (
        descriptions["current_year_charge"].device_class is SensorDeviceClass.MONETARY
    )
    assert descriptions["current_year_charge"].state_class is SensorStateClass.TOTAL


def test_balance_and_meter_sensors_use_compatible_metadata() -> None:
    descriptions = {description.key: description for description in SENSORS}

    for key in ("account_balance", "amount_due"):
        assert descriptions[key].device_class is SensorDeviceClass.MONETARY
        assert descriptions[key].state_class is None
    assert descriptions["latest_month_meter_reading"].device_class is None
    assert descriptions["latest_month_meter_reading"].state_class is None


@pytest.mark.parametrize(
    ("options", "fields"),
    [
        ({}, ("usage", "valley", "flat")),
        ({CONF_TOU_STATISTICS: []}, ("usage",)),
        ({CONF_TOU_STATISTICS: ["peak", "tip"]}, ("usage", "peak", "tip")),
    ],
)
def test_month_sensor_exposes_its_historical_energy_statistic_id(
    options, fields
) -> None:
    account = PowerAccount(
        "account", "1234567890123", "1234567890123", "42101", "org", "01", "家庭", ""
    )
    sensor = object.__new__(StateGridElectricitySensor)
    sensor.account_id = account.account_id
    sensor.coordinator = SimpleNamespace(
        data={account.account_id: AccountUsage(account, (), 10)},
        entry=SimpleNamespace(options=options),
    )
    sensor.entity_description = next(
        description
        for description in SENSORS
        if description.key == "current_month_usage"
    )

    assert sensor.extra_state_attributes["energy_statistic_id"] == energy_statistic_id(
        account
    )
    assert sensor.extra_state_attributes["energy_statistic_ids"] == {
        field: energy_statistic_id(account, field) for field in fields
    }
