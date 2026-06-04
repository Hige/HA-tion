"""
Fan controls for Tion breezers
"""
from __future__ import annotations

import logging
from datetime import timedelta
from functools import cached_property
from typing import Any

from homeassistant.components.climate.const import PRESET_AWAY, PRESET_BOOST, PRESET_NONE, PRESET_SLEEP
from homeassistant.components.fan import FanEntityDescription, FanEntity, DIRECTION_FORWARD, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TionInstance
from .const import DOMAIN, TION_PRESET_MODES, tion_preset_canonical, tion_preset_display

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)
TION_FAN_MODES = (1, 2, 3, 4, 5, 6)

config = FanEntityDescription(
    key="fan_speed",
    translation_key="fan_speed",
    entity_registry_enabled_default=True,
    icon="mdi:fan",
)

FAN_FEATURE_TURN_ON_OFF = (
    getattr(FanEntityFeature, "TURN_ON", FanEntityFeature(0))
    | getattr(FanEntityFeature, "TURN_OFF", FanEntityFeature(0))
)


async def async_setup_entry(hass: HomeAssistant, _config: ConfigEntry, async_add_entities):
    """Set up the sensor entry"""

    async_add_entities([TionFan(config, hass.data[DOMAIN][_config.unique_id], hass)])
    return True


class TionFan(FanEntity, CoordinatorEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = FanEntityFeature.PRESET_MODE | FanEntityFeature.SET_SPEED | FAN_FEATURE_TURN_ON_OFF
    _attr_oscillating = False
    _attr_preset_mode = tion_preset_display(PRESET_NONE)
    _attr_translation_key = "fan_speed"
    _attr_speed_count = len(TION_FAN_MODES)
    _attr_current_direction = DIRECTION_FORWARD
    _mode_percent_mapping = {
        0: 0,
        1: 17,
        2: 33,
        3: 50,
        4: 67,
        5: 83,
        6: 100,
    }
    _percent_mode_mapping = {
        0: 0,
        16: 1,
        17: 1,
        33: 2,
        50: 3,
        66: 4,
        67: 4,
        83: 5,
        100: 6,
    }
    # Home Assistant is using float speed step and ceil to determinate supported speed percents.

    def set_preset_mode(self, preset_mode: str) -> None:
        pass

    def set_direction(self, direction: str) -> None:
        raise NotImplemented

    def turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs) -> None:
        raise NotImplemented

    def oscillate(self, oscillating: bool) -> None:
        raise NotImplemented

    def turn_off(self, **kwargs: Any) -> None:
        pass

    def set_percentage(self, percentage: int) -> None:
        raise NotImplemented

    @property
    def is_on(self) -> bool | None:
        return self._attr_is_on

    def __init__(self, description: FanEntityDescription, instance: TionInstance, hass: HomeAssistant):
        """Initialize the fan."""

        CoordinatorEntity.__init__(self=self, coordinator=instance, )
        self.entity_description = description
        self._attr_device_info = instance.device_info
        self._attr_unique_id = f"{instance.unique_id}-{description.key}"
        self._saved_fan_mode = None
        self._saved_target_temp = None
        self._attr_preset_modes = TION_PRESET_MODES.copy()
        if instance.away_temp:
            pass
        else:
            self._attr_preset_modes.remove(tion_preset_display(PRESET_AWAY))

        _LOGGER.debug(f"Init of fan  {self.name} ({instance.unique_id})")
        _LOGGER.debug(f"Speed step is {self.percentage_step}")

        registry = async_get_entity_registry(hass=hass)
        entity = registry.async_get_or_create(
            domain=Platform.FAN,
            platform=DOMAIN,
            unique_id=self.unique_id,
            translation_key=self.translation_key,
        )
        _LOGGER.debug(f"{entity.entity_category=}, {entity.entity_id=}, {entity.options=} {entity.unique_id=}")
        if entity.entity_category == EntityCategory.CONFIG:
            import attr  # pylint: disable=import-outside-toplevel

            _LOGGER.debug(f"Updating {entity.entity_category=} for {entity.entity_id=}")
            new_value = {"entity_category": None}
            registry.entities[entity.entity_id] = attr.evolve(registry.entities[entity.entity_id], **new_value)
            registry.async_schedule_save()

        self._sync_attrs_from_coordinator()

    def percent2mode(self, percentage: int) -> int:
        result = 0
        try:
            return self._percent_mode_mapping[percentage]
        except KeyError:
            _LOGGER.warning(f"Could not to convert {percentage} to mode with {self._percent_mode_mapping}. "
                            f"Will use fall back method.")
            for i in range(len(TION_FAN_MODES)):
                if percentage < self.percentage_step * i:
                    break
                else:
                    result = i
            else:
                result = 6

            return result

    def mode2percent(self) -> int | None:
        current_fan_mode = self._current_fan_mode()
        return self._mode_percent_mapping[current_fan_mode] if current_fan_mode is not None else None

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed of the fan, as a percentage."""
        target_mode = self.percent2mode(percentage)
        target_is_on = percentage > 0
        current_is_on = self._current_is_on()

        if self._current_fan_mode() == target_mode and current_is_on == target_is_on:
            _LOGGER.debug(
                "Ignoring duplicate fan request for %s: mode=%s is_on=%s",
                self.entity_id,
                target_mode,
                target_is_on,
            )
            return

        if not target_is_on:
            await self.async_turn_off()
            return

        if not current_is_on:
            await self.coordinator.set(is_on=True)

        if self._current_fan_mode() != target_mode:
            await self.coordinator.set(fan_speed=target_mode)

    @cached_property
    def boost_fan_mode(self) -> int:
        return max(TION_FAN_MODES)

    @property
    def fan_mode(self):
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        return data.get(self.entity_description.key)

    def _current_fan_mode(self) -> int | None:
        fan_mode = self.fan_mode
        return int(fan_mode) if fan_mode is not None else None

    def _current_is_on(self) -> bool:
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        is_on = data.get("is_on")
        if isinstance(is_on, bool):
            return is_on
        if isinstance(is_on, str):
            return is_on.lower() == "on"
        return bool(is_on)

    @property
    def sleep_max_fan_mode(self) -> int:
        return 2

    def _save_current_fan_mode(self) -> None:
        current_fan_mode = self._current_fan_mode()
        if self._saved_fan_mode is None and current_fan_mode is not None:
            self._saved_fan_mode = current_fan_mode

    def _current_preset_mode(self) -> str:
        return tion_preset_canonical(self.coordinator.data.get("preset_mode", PRESET_NONE))

    def _set_shared_preset_mode(self, preset_mode: str, notify: bool = False) -> None:
        self.coordinator.data["preset_mode"] = tion_preset_canonical(preset_mode)
        self._attr_preset_mode = tion_preset_display(preset_mode)
        if notify:
            self.coordinator.async_update_listeners()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        canonical_preset = tion_preset_canonical(preset_mode)
        if tion_preset_display(canonical_preset) not in self._attr_preset_modes:
            _LOGGER.error("Unsupported Tion fan preset mode: %s", preset_mode)
            return

        actions = []
        current_preset = self._current_preset_mode()
        current_fan_mode = self._current_fan_mode() or self.sleep_max_fan_mode

        if canonical_preset == PRESET_AWAY and current_preset != PRESET_AWAY:
            if self._saved_target_temp is None:
                self._saved_target_temp = self.coordinator.data.get("heater_temp")
            actions.append({"heater_temp": self.coordinator.away_temp})

        if canonical_preset != PRESET_AWAY and current_preset == PRESET_AWAY and self._saved_target_temp is not None:
            actions.append({"heater_temp": self._saved_target_temp})
            self._saved_target_temp = None

        if canonical_preset == PRESET_SLEEP and current_preset != PRESET_SLEEP:
            self._save_current_fan_mode()
            actions.append({"fan_speed": min(current_fan_mode, self.sleep_max_fan_mode), "is_on": True})

        if canonical_preset == PRESET_BOOST and current_preset != PRESET_BOOST:
            self._save_current_fan_mode()
            actions.append({"fan_speed": self.boost_fan_mode, "is_on": True})

        if current_preset in [PRESET_BOOST, PRESET_SLEEP] and canonical_preset not in [PRESET_BOOST, PRESET_SLEEP]:
            if self._saved_fan_mode is not None:
                actions.append({"fan_speed": self._saved_fan_mode, "is_on": True})
                self._saved_fan_mode = None

        for action in actions:
            await self.coordinator.set(**action)

        self._set_shared_preset_mode(canonical_preset, notify=True)

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs, ) -> None:
        target_speed = None
        if percentage is not None and percentage > 0:
            target_speed = self.percent2mode(percentage)
        elif self._saved_fan_mode is not None:
            target_speed = self._saved_fan_mode

        self._saved_fan_mode = None
        if not self._current_is_on():
            await self.coordinator.set(is_on=True)

        if target_speed is not None and self._current_fan_mode() != target_speed:
            await self.coordinator.set(fan_speed=target_speed)

        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)

    async def async_turn_off(self, **kwargs: Any) -> None:
        current_fan_mode = self._current_fan_mode()
        if self._saved_fan_mode is None and current_fan_mode is not None and current_fan_mode > 0:
            self._saved_fan_mode = current_fan_mode

        await self.coordinator.set(is_on=False)

    def _sync_attrs_from_coordinator(self) -> None:
        self._attr_assumed_state = False if self.coordinator.last_update_success else True
        self._attr_is_on = self._current_is_on()
        percentage = self.mode2percent() if self._attr_is_on else 0
        self._attr_percentage = percentage if percentage is not None else 0
        current_fan_mode = self._current_fan_mode()
        current_preset = self._current_preset_mode()
        if current_preset == PRESET_BOOST and current_fan_mode != self.boost_fan_mode:
            self._set_shared_preset_mode(PRESET_NONE)
        if current_preset == PRESET_SLEEP and current_fan_mode is not None and current_fan_mode > self.sleep_max_fan_mode:
            self._set_shared_preset_mode(PRESET_NONE)
        self._attr_preset_mode = tion_preset_display(self._current_preset_mode())

    def _handle_coordinator_update(self) -> None:
        self._sync_attrs_from_coordinator()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True
