"""Regression tests for authentication state persistence during polling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.state_grid.api import (
    StateGridApiError,
    StateGridAuthenticationError,
    StateGridInteractiveChallengeRequired,
    StateGridNetworkError,
)
from custom_components.state_grid.const import (
    CONF_AUTH_ERROR,
    CONF_LOGIN_SESSION,
    CONF_TOU_STATISTICS,
)
from custom_components.state_grid.coordinator import StateGridDataCoordinator
from custom_components.state_grid.models import LoginSession


@pytest.mark.parametrize(
    ("options", "fields"),
    [
        ({}, ("usage", "valley", "flat")),
        ({CONF_TOU_STATISTICS: []}, ("usage",)),
        ({CONF_TOU_STATISTICS: ["tip", "peak"]}, ("usage", "peak", "tip")),
    ],
)
def test_coordinator_passes_selected_statistics_to_importer(
    monkeypatch, options, fields
):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *args, **kwargs: None)
    coordinator = StateGridDataCoordinator(
        Mock(), SimpleNamespace(options=options), Mock()
    )
    assert coordinator.statistics.fields == fields


def _session(token: str) -> LoginSession:
    return LoginSession(token=token, user_id="test-user", expires_at=9999999999)


def _coordinator(*, saved_session=None, current_session=None, auth_error=None):
    entry = SimpleNamespace(data={"unrelated": "preserved"}, options={})
    if saved_session is not None:
        entry.data[CONF_LOGIN_SESSION] = saved_session.as_dict()
    if auth_error is not None:
        entry.data[CONF_AUTH_ERROR] = auth_error
    update_entry = Mock(side_effect=lambda entry, *, data: setattr(entry, "data", data))
    coordinator = object.__new__(StateGridDataCoordinator)
    coordinator.entry = entry
    coordinator.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update_entry)
    )
    coordinator.api = SimpleNamespace(
        login_session=current_session, async_query_history=AsyncMock(return_value={})
    )
    coordinator.statistics = SimpleNamespace(async_import=AsyncMock())
    return coordinator, entry, update_entry


@pytest.mark.parametrize(
    "failure",
    [
        StateGridNetworkError("electricity request timed out"),
        StateGridApiError("TEMPORARY", "electricity service unavailable"),
    ],
)
def test_new_login_is_saved_even_if_later_query_fails(failure) -> None:
    old_session = _session("old-token")
    new_session = _session("new-token")
    coordinator, entry, update_entry = _coordinator(
        saved_session=old_session, current_session=old_session
    )

    async def query_history(**_kwargs):
        coordinator.api.login_session = new_session
        # Device refresh may have persisted data while the query was running.
        entry.data = {**entry.data, "device_cache_revision": 2}
        raise failure

    coordinator.api.async_query_history = query_history
    with pytest.raises(UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert entry.data[CONF_LOGIN_SESSION] == new_session.as_dict()
    assert entry.data["device_cache_revision"] == 2
    assert entry.data["unrelated"] == "preserved"
    assert CONF_AUTH_ERROR not in entry.data
    update_entry.assert_called_once()


def test_invalidated_token_is_removed_when_relogin_needs_verification() -> None:
    coordinator, entry, update_entry = _coordinator(saved_session=_session("old"))
    expired = StateGridAuthenticationError("-200", "token expired", source="srvrt")
    challenge = StateGridInteractiveChallengeRequired(
        "RK008", "网络连接超时(RK008),请重试!", source="srvrt"
    )
    challenge.__cause__ = expired
    coordinator.api.async_query_history.side_effect = challenge

    with pytest.raises(ConfigEntryAuthFailed) as raised:
        asyncio.run(coordinator._async_update_data())

    message = str(raised.value)
    assert message.index("-200") < message.index("RK008")
    assert "srvrt" in message
    assert "token expired" in message
    assert challenge.message in message
    assert entry.data[CONF_AUTH_ERROR] == message
    assert CONF_LOGIN_SESSION not in entry.data
    update_entry.assert_called_once()


def test_invalidated_token_is_removed_if_relogin_has_network_failure() -> None:
    coordinator, entry, _ = _coordinator(saved_session=_session("old"))
    coordinator.api.async_query_history.side_effect = StateGridNetworkError("timeout")

    with pytest.raises(UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert CONF_LOGIN_SESSION not in entry.data
    assert CONF_AUTH_ERROR not in entry.data


def test_success_clears_previous_auth_error_without_losing_session() -> None:
    session = _session("valid")
    coordinator, entry, update_entry = _coordinator(
        saved_session=session,
        current_session=session,
        auth_error="previous RK008 failure",
    )

    assert asyncio.run(coordinator._async_update_data()) == {}

    assert CONF_AUTH_ERROR not in entry.data
    assert entry.data[CONF_LOGIN_SESSION] == session.as_dict()
    update_entry.assert_called_once()


def test_unchanged_success_does_not_write_entry() -> None:
    session = _session("valid")
    coordinator, _, update_entry = _coordinator(
        saved_session=session, current_session=session
    )

    asyncio.run(coordinator._async_update_data())

    update_entry.assert_not_called()


def test_success_imports_history_before_publishing_sensor_values() -> None:
    coordinator, entry, _ = _coordinator()
    entry.options["history_months"] = 1
    result = {"account": Mock()}
    coordinator.api.async_query_history.return_value = result

    assert asyncio.run(coordinator._async_update_data()) == result

    coordinator.api.async_query_history.assert_awaited_once_with(months=2)
    coordinator.statistics.async_import.assert_awaited_once_with(result)


def test_statistics_failure_keeps_sensors_and_new_login_available(caplog) -> None:
    session = _session("valid")
    coordinator, entry, _ = _coordinator(current_session=session)
    result = {"account": Mock()}
    coordinator.api.async_query_history.return_value = result
    coordinator.statistics.async_import.side_effect = OSError("Recorder unavailable")

    assert asyncio.run(coordinator._async_update_data()) == result

    assert "Unable to import historical energy statistics" in caplog.text
    assert entry.data[CONF_LOGIN_SESSION] == session.as_dict()
    assert CONF_AUTH_ERROR not in entry.data
