"""Regression tests for optional remote-assisted AC power-off."""

from __future__ import annotations

import asyncio
from enum import IntFlag
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]


def _stub_homeassistant() -> None:
    class _HVACMode:
        OFF = "off"
        COOL = "cool"
        DRY = "dry"
        FAN_ONLY = "fan_only"
        AUTO = "auto"
        HEAT = "heat"

    class _ClimateEntityFeature(IntFlag):
        TARGET_TEMPERATURE = 1
        FAN_MODE = 2
        SWING_MODE = 4
        TURN_ON = 8
        TURN_OFF = 16

    class _ClimateEntity:
        async def async_will_remove_from_hass(self) -> None:
            return None

    class _CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

        async def async_will_remove_from_hass(self) -> None:
            return None

    def pkg(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__path__ = []
        sys.modules[name] = mod
        return mod

    ha = pkg("homeassistant")
    components = pkg("homeassistant.components")
    climate = pkg("homeassistant.components.climate")
    climate.HVACMode = _HVACMode
    climate.ClimateEntityFeature = _ClimateEntityFeature
    climate.ClimateEntity = _ClimateEntity
    ha.components = components
    components.climate = climate

    const = pkg("homeassistant.const")
    const.ATTR_TEMPERATURE = "temperature"
    const.STATE_OFF = "off"
    const.STATE_UNAVAILABLE = "unavailable"
    const.UnitOfTemperature = SimpleNamespace(CELSIUS="°C")

    exceptions = pkg("homeassistant.exceptions")
    exceptions.HomeAssistantError = Exception

    helpers = pkg("homeassistant.helpers")
    update_coordinator = pkg("homeassistant.helpers.update_coordinator")
    update_coordinator.CoordinatorEntity = _CoordinatorEntity
    helpers.update_coordinator = update_coordinator

    device_registry = pkg("homeassistant.helpers.device_registry")
    device_registry.CONNECTION_NETWORK_MAC = "mac"
    helpers.device_registry = device_registry

    pkg_name = "panasonic_taiseia_local"
    pkg_mod = types.ModuleType(pkg_name)
    pkg_mod.__path__ = [str(ROOT / "custom_components" / "panasonic_taiseia_local")]
    sys.modules[pkg_name] = pkg_mod


_stub_homeassistant()
sys.path.insert(0, str(ROOT / "custom_components"))

from panasonic_taiseia_local.climate import TaiSeiaClimate  # noqa: E402
from panasonic_taiseia_local.const import (  # noqa: E402
    CONF_REMOTE_OFF_COMMAND,
    CONF_REMOTE_OFF_DEVICE,
    CONF_REMOTE_OFF_REFRESH_DELAY,
    CONF_REMOTE_OFF_ENTITY,
    LEGACY_CONF_IR_OFF_COMMAND,
    LEGACY_CONF_IR_OFF_REFRESH_DELAY,
    LEGACY_CONF_IR_OFF_REMOTE,
    STATUS_POWER,
)
from homeassistant.components.climate import HVACMode  # noqa: E402


class _States:
    def __init__(self, state: str | None = "on") -> None:
        self._state = state

    def get(self, _entity_id: str):
        if self._state is None:
            return None
        return SimpleNamespace(state=self._state)


class _Services:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple] = []

    async def async_call(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error


class _ConfigEntries:
    def __init__(self, options: dict) -> None:
        self.entry = SimpleNamespace(options=options)

    def async_get_entry(self, _entry_id: str):
        return self.entry


def _make_entity(
    *,
    remote_state: str | None = "on",
    service_error: Exception | None = None,
    options: dict | None = None,
):
    coordinator = SimpleNamespace(
        data={"status": {STATUS_POWER: "1"}},
        async_request_refresh=AsyncMock(),
    )
    client = SimpleNamespace(
        device=SimpleNamespace(unique_id="test-device", services={}),
    )
    entity = TaiSeiaClimate(coordinator, client, "entry-1", None)
    if options is None:
        options = {
            CONF_REMOTE_OFF_ENTITY: "remote.test",
            CONF_REMOTE_OFF_COMMAND: "b64:test-command",
            CONF_REMOTE_OFF_REFRESH_DELAY: 0,
        }
    hass = SimpleNamespace(
        config_entries=_ConfigEntries(options),
        states=_States(remote_state),
        services=_Services(service_error),
        async_create_task=asyncio.create_task,
    )
    entity.hass = hass
    return entity, hass


class RemoteOffTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_success_returns_true_without_changing_power(self) -> None:
        entity, hass = _make_entity()

        result = await entity._async_try_remote_off()

        self.assertTrue(result)
        self.assertEqual(entity.device_status[STATUS_POWER], "1")
        self.assertEqual(len(hass.services.calls), 1)
        args, kwargs = hass.services.calls[0]
        self.assertEqual(args[0:2], ("remote", "send_command"))
        self.assertEqual(
            args[2],
            {
                "entity_id": "remote.test",
                "command": "b64:test-command",
            },
        )
        self.assertTrue(kwargs["blocking"])

    async def test_optional_remote_device_is_forwarded(self) -> None:
        entity, hass = _make_entity(
            options={
                CONF_REMOTE_OFF_ENTITY: "remote.test",
                CONF_REMOTE_OFF_DEVICE: "Living Room TV",
                CONF_REMOTE_OFF_COMMAND: "PowerOff",
                CONF_REMOTE_OFF_REFRESH_DELAY: 0,
            }
        )

        result = await entity._async_try_remote_off()

        self.assertTrue(result)
        args, _kwargs = hass.services.calls[0]
        self.assertEqual(args[2]["device"], "Living Room TV")

    async def test_legacy_ir_options_still_work(self) -> None:
        entity, hass = _make_entity(
            options={
                LEGACY_CONF_IR_OFF_REMOTE: "remote.test",
                LEGACY_CONF_IR_OFF_COMMAND: "b64:legacy-command",
                LEGACY_CONF_IR_OFF_REFRESH_DELAY: 0,
            }
        )

        result = await entity._async_try_remote_off()

        self.assertTrue(result)
        args, _kwargs = hass.services.calls[0]
        self.assertEqual(args[2]["command"], "b64:legacy-command")

    async def test_delayed_refresh_runs_after_remote_success(self) -> None:
        entity, _hass = _make_entity(
            options={
                CONF_REMOTE_OFF_ENTITY: "remote.test",
                CONF_REMOTE_OFF_COMMAND: "PowerOff",
                CONF_REMOTE_OFF_REFRESH_DELAY: 0.001,
            }
        )

        result = await entity._async_try_remote_off()
        await asyncio.sleep(0.01)

        self.assertTrue(result)
        entity.coordinator.async_request_refresh.assert_awaited_once()

    async def test_refresh_failure_does_not_turn_into_send_failure(self) -> None:
        entity, _hass = _make_entity()
        entity.coordinator.async_request_refresh.side_effect = RuntimeError(
            "refresh failed"
        )

        await entity._async_refresh_after_remote_off(0)

        entity.coordinator.async_request_refresh.assert_awaited_once()

    async def test_unavailable_remote_requests_fallback(self) -> None:
        entity, hass = _make_entity(remote_state="unavailable")

        result = await entity._async_try_remote_off()

        self.assertFalse(result)
        self.assertEqual(hass.services.calls, [])

    async def test_off_remote_is_still_allowed_for_generic_remote(self) -> None:
        entity, hass = _make_entity(remote_state="off")

        result = await entity._async_try_remote_off()

        self.assertTrue(result)
        self.assertEqual(len(hass.services.calls), 1)

    async def test_send_exception_requests_fallback(self) -> None:
        entity, _hass = _make_entity(service_error=RuntimeError("send failed"))

        result = await entity._async_try_remote_off()

        self.assertFalse(result)

    async def test_missing_remote_config_requests_fallback(self) -> None:
        entity, hass = _make_entity(options={CONF_REMOTE_OFF_REFRESH_DELAY: 0})

        result = await entity._async_try_remote_off()

        self.assertFalse(result)
        self.assertEqual(hass.services.calls, [])

    async def test_hvac_off_falls_back_to_taiseia_when_remote_fails(self) -> None:
        entity, _hass = _make_entity(remote_state="unavailable")
        entity.async_write_with_rollback = AsyncMock()

        await entity.async_set_hvac_mode(HVACMode.OFF)

        entity.async_write_with_rollback.assert_awaited_once()

    async def test_hvac_off_skips_taiseia_after_remote_success(self) -> None:
        entity, _hass = _make_entity()
        entity.async_write_with_rollback = AsyncMock()

        await entity.async_set_hvac_mode(HVACMode.OFF)

        entity.async_write_with_rollback.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
