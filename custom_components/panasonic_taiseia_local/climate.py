"""Climate platform for Panasonic TaiSEIA local."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    UnitOfTemperature,
)
from homeassistant.exceptions import HomeAssistantError

from .capability import filter_option_map, supported_values
from .catalog import (
    climate_fan_map,
    climate_hvac_mappings,
    climate_swing_map,
    climate_temp_limits,
)
from .const import (
    CLIMATE_AVAILABLE_FAN_MODE,
    CLIMATE_AVAILABLE_MODE,
    CLIMATE_AVAILABLE_SWING_MODE,
    CLIMATE_MAXIMUM_TEMPERATURE,
    CLIMATE_MINIMUM_TEMPERATURE,
    CLIMATE_TEMPERATURE_STEP,
    CONF_REMOTE_OFF_COMMAND,
    CONF_REMOTE_OFF_DEVICE,
    CONF_REMOTE_OFF_REFRESH_DELAY,
    CONF_REMOTE_OFF_ENTITY,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_PROFILE,
    DEFAULT_REMOTE_OFF_REFRESH_DELAY,
    DOMAIN,
    LEGACY_CONF_IR_OFF_COMMAND,
    LEGACY_CONF_IR_OFF_REFRESH_DELAY,
    LEGACY_CONF_IR_OFF_REMOTE,
    ICON_CLIMATE,
    LABEL_CLIMATE,
    STATUS_FAN,
    STATUS_MODE,
    STATUS_POWER,
    STATUS_SWING,
    STATUS_TEMP_IN,
    STATUS_TEMP_SET,
    SVC_FAN,
    SVC_MODE,
    SVC_POWER,
    SVC_SWING,
    SVC_TEMP_SET,
    TYPE_AC,
)
from .entity import TaiSeiaBaseEntity

_LOGGER = logging.getLogger(__package__)

PARALLEL_UPDATES = 1


def _key_from_dict(target: dict, mode_name: str):
    for key, value in target.items():
        if mode_name == value:
            return key
    return None


async def async_setup_entry(hass, entry, async_add_entities) -> bool:
    client = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    profile = hass.data[DOMAIN][entry.entry_id].get(DATA_PROFILE)
    # Only the probed / entry SA type decides climate — never trust catalog
    # DeviceType alone (wrong ModelType must not create a climate entity).
    if client.device.sa_type_id != TYPE_AC:
        return True
    async_add_entities(
        [TaiSeiaClimate(coordinator, client, entry.entry_id, profile)],
        True,
    )
    return True


class TaiSeiaClimate(TaiSeiaBaseEntity, ClimateEntity):
    _entity_key = "climate"
    _attr_icon = ICON_CLIMATE

    def __init__(self, coordinator, client, entry_id, profile) -> None:
        self._profile = profile
        self._remote_refresh_task: asyncio.Task | None = None
        super().__init__(coordinator, client, entry_id)

    def _remote_off_options(
        self,
    ) -> tuple[str | None, str | None, str | None, float]:
        """Return per-device remote power-off configuration."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return None, None, None, DEFAULT_REMOTE_OFF_REFRESH_DELAY

        options = entry.options
        remote_entity = str(
            options.get(CONF_REMOTE_OFF_ENTITY)
            or options.get(LEGACY_CONF_IR_OFF_REMOTE)
            or ""
        ).strip() or None
        remote_device = str(options.get(CONF_REMOTE_OFF_DEVICE) or "").strip() or None
        command = str(
            options.get(CONF_REMOTE_OFF_COMMAND)
            or options.get(LEGACY_CONF_IR_OFF_COMMAND)
            or ""
        ).strip() or None
        try:
            delay = float(
                options.get(
                    CONF_REMOTE_OFF_REFRESH_DELAY,
                    options.get(
                        LEGACY_CONF_IR_OFF_REFRESH_DELAY,
                        DEFAULT_REMOTE_OFF_REFRESH_DELAY,
                    ),
                )
            )
        except (TypeError, ValueError):
            delay = DEFAULT_REMOTE_OFF_REFRESH_DELAY
        return (
            remote_entity,
            remote_device,
            command,
            max(0.0, min(delay, 30.0)),
        )

    async def _async_refresh_after_remote_off(self, delay: float) -> None:
        """Refresh real appliance state after a remote power-off command."""
        try:
            await asyncio.sleep(delay)
            await self.coordinator.async_request_refresh()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            # The remote command already succeeded. Never force a TaiSEIA OFF only
            # because the follow-up read failed; the AC may be in mold-dry.
            _LOGGER.warning(
                "[%s] remote power-off command was sent, but delayed status "
                "refresh failed: %s",
                self.label,
                err,
            )

    def _schedule_remote_refresh(self, delay: float) -> None:
        """Keep only the newest delayed refresh after repeated OFF presses."""
        if (
            self._remote_refresh_task is not None
            and not self._remote_refresh_task.done()
        ):
            self._remote_refresh_task.cancel()
        if delay <= 0:
            self._remote_refresh_task = None
            return
        self._remote_refresh_task = self.hass.async_create_task(
            self._async_refresh_after_remote_off(delay)
        )

    async def _async_try_remote_off(self) -> bool:
        """Send configured remote power-off command.

        Return True only when Home Assistant accepts the service call.
        """
        remote_entity, remote_device, command, refresh_delay = (
            self._remote_off_options()
        )
        if not remote_entity or not command:
            return False

        remote_state = self.hass.states.get(remote_entity)
        # Do not reject state == "off": for some remote integrations it means
        # the target/activity is off, not that send_command is unavailable.
        if remote_state is None or remote_state.state == STATE_UNAVAILABLE:
            _LOGGER.warning(
                "[%s] power-off remote %s is unavailable; falling back to TaiSEIA",
                self.label,
                remote_entity,
            )
            return False

        try:
            service_data = {
                "entity_id": remote_entity,
                "command": command,
            }
            if remote_device:
                service_data["device"] = remote_device
            await self.hass.services.async_call(
                "remote",
                "send_command",
                service_data,
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "[%s] remote power-off via %s failed; falling back to TaiSEIA: %s",
                self.label,
                remote_entity,
                err,
            )
            return False

        # Do not optimistically change STATUS_POWER here. Panasonic ACs
        # can remain power=1 while running the post-shutdown mold-dry cycle.
        _LOGGER.debug(
            "[%s] remote power-off command sent via %s; refreshing state in %.1fs",
            self.label,
            remote_entity,
            refresh_delay,
        )
        self._schedule_remote_refresh(refresh_delay)
        return True

    async def async_will_remove_from_hass(self) -> None:
        if (
            self._remote_refresh_task is not None
            and not self._remote_refresh_task.done()
        ):
            self._remote_refresh_task.cancel()
        await super().async_will_remove_from_hass()

    def _mode_table(self) -> list[dict]:
        catalog = climate_hvac_mappings(self._profile)
        return catalog or CLIMATE_AVAILABLE_MODE

    @property
    def available(self) -> bool:
        return self.has_status(STATUS_POWER)

    @property
    def label(self) -> str:
        return LABEL_CLIMATE

    @property
    def icon(self) -> str:
        return ICON_CLIMATE

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = (
            ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TARGET_TEMPERATURE
        )
        if self.client.has_service(SVC_FAN) or self.has_status(STATUS_FAN):
            features |= ClimateEntityFeature.FAN_MODE
        if self.client.has_service(SVC_SWING) or self.has_status(STATUS_SWING):
            features |= ClimateEntityFeature.SWING_MODE
        return features

    @property
    def temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

    def _fan_map(self) -> dict[int, str]:
        base = climate_fan_map(self._profile) or CLIMATE_AVAILABLE_FAN_MODE
        return filter_option_map(self.client, SVC_FAN, base)

    def _swing_map(self) -> dict[int, str]:
        base = climate_swing_map(self._profile) or CLIMATE_AVAILABLE_SWING_MODE
        return filter_option_map(self.client, SVC_SWING, base)

    def _mode_codes(self) -> set[int]:
        table = self._mode_table()
        info = self.client.device.services.get(SVC_MODE)
        codes = [m["mappingCode"] for m in table if m["mappingCode"] >= 0]
        if not info:
            return set(codes)
        return set(supported_values(info, codes))

    @property
    def hvac_mode(self) -> HVACMode:
        if not self.status_bool(STATUS_POWER):
            return HVACMode.OFF
        if not self.has_status(STATUS_MODE):
            return HVACMode.OFF
        value = self.status_int(STATUS_MODE)
        for mode in self._mode_table():
            if mode["mappingCode"] == value:
                return mode["key"]
        return HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        allowed = self._mode_codes()
        table = self._mode_table()
        modes = [
            m["key"]
            for m in table
            if m["mappingCode"] >= 0 and m["mappingCode"] in allowed
        ]
        if not modes:
            modes = [m["key"] for m in table if m["mappingCode"] >= 0]
        modes.append(HVACMode.OFF)
        return modes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "control_mode": data.get("control_mode"),
            "control_path": data.get("control_path"),
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        _LOGGER.debug("[%s] set_hvac_mode %s", self.label, hvac_mode)
        try:
            if hvac_mode == HVACMode.OFF:
                if await self._async_try_remote_off():
                    return
                prev = self.device_status.get(STATUS_POWER)
                await self.async_write_with_rollback(SVC_POWER, 0, STATUS_POWER, prev)
                return

            mapping = next(m for m in self._mode_table() if m["key"] == hvac_mode)
            mode = mapping["mappingCode"]
            was_off = not self.status_bool(STATUS_POWER)
            prev_mode = self.device_status.get(STATUS_MODE)
            await self.async_write_with_rollback(SVC_MODE, mode, STATUS_MODE, prev_mode)
            if was_off:
                prev_pwr = self.device_status.get(STATUS_POWER)
                try:
                    await self.async_write_with_rollback(
                        SVC_POWER, 1, STATUS_POWER, prev_pwr
                    )
                except Exception:
                    # Mode already applied; restore previous mode if power-on fails
                    try:
                        if prev_mode not in (None, ""):
                            await self.async_write_with_rollback(
                                SVC_MODE,
                                int(prev_mode),
                                STATUS_MODE,
                                str(mode),
                            )
                        else:
                            self.set_local_status(STATUS_MODE, prev_mode)
                    except Exception:  # noqa: BLE001
                        self.set_local_status(STATUS_MODE, prev_mode)
                    raise
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err

    @property
    def fan_mode(self) -> str:
        fmap = self._fan_map()
        raw = self.status_int(STATUS_FAN, 0)
        return fmap.get(raw, next(iter(fmap.values()), "自動"))

    @property
    def fan_modes(self) -> list[str]:
        return list(self._fan_map().values())

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        mode_id = int(_key_from_dict(self._fan_map(), fan_mode))
        prev = self.device_status.get(STATUS_FAN)
        try:
            await self.async_write_with_rollback(SVC_FAN, mode_id, STATUS_FAN, prev)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err

    @property
    def swing_mode(self) -> str:
        smap = self._swing_map()
        raw = self.status_int(STATUS_SWING, 0)
        return smap.get(raw, next(iter(smap.values()), "自動"))

    @property
    def swing_modes(self) -> list[str]:
        return list(self._swing_map().values())

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        mode_id = int(_key_from_dict(self._swing_map(), swing_mode))
        prev = self.device_status.get(STATUS_SWING)
        try:
            await self.async_write_with_rollback(SVC_SWING, mode_id, STATUS_SWING, prev)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err

    @property
    def target_temperature(self) -> float:
        return float(self.status_int(STATUS_TEMP_SET, 0))

    @property
    def current_temperature(self) -> float:
        return float(self.status_int(STATUS_TEMP_IN, 0))

    async def async_set_temperature(self, **kwargs) -> None:
        target = kwargs.get(ATTR_TEMPERATURE)
        if target is None:
            return
        value = int(target)
        prev = self.device_status.get(STATUS_TEMP_SET)
        try:
            await self.async_write_with_rollback(
                SVC_TEMP_SET, value, STATUS_TEMP_SET, prev
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err

    @property
    def min_temp(self) -> float:
        clo, chi = climate_temp_limits(self._profile)
        lo, hi = self.client.service_range(SVC_TEMP_SET)
        if self.client.has_service(SVC_TEMP_SET) and 10 <= lo <= hi <= 40:
            return float(lo)
        if clo is not None:
            return float(clo)
        return float(CLIMATE_MINIMUM_TEMPERATURE)

    @property
    def max_temp(self) -> float:
        clo, chi = climate_temp_limits(self._profile)
        lo, hi = self.client.service_range(SVC_TEMP_SET)
        if self.client.has_service(SVC_TEMP_SET) and 10 <= lo <= hi <= 40:
            return float(hi)
        if chi is not None:
            return float(chi)
        return float(CLIMATE_MAXIMUM_TEMPERATURE)

    @property
    def target_temperature_step(self) -> float:
        return CLIMATE_TEMPERATURE_STEP
