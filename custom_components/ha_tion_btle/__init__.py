"""The Tion breezer component."""
from __future__ import annotations

import asyncio
from bleak import exc as bleak_exc
from bleak.backends.device import BLEDevice
import datetime
import logging
import math
from datetime import timedelta
from functools import cached_property

import tion_btle
from bleak_retry_connector import (
    BleakConnectionError,
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
    establish_connection,
    wait_for_disconnect,
)
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothCallbackMatcher
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from tion_btle.tion import Tion, MaxTriesExceededError, TionException
from .const import DOMAIN, TION_SCHEMA, CONF_KEEP_ALIVE, CONF_AWAY_TEMP, CONF_MAC, PLATFORMS, PRESET_NONE, tion_preset_canonical
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)
_TION_PATCHED = False
COMMAND_ATTEMPTS = 3
COMMAND_RETRY_BASE_DELAY = 2
COMMAND_RETRY_MAX_DELAY = 8
POLL_RETRY_ATTEMPTS = 2


def _get_ble_device(tion: Tion) -> BLEDevice | None:
    """Return the freshest BLE device object known for the Tion instance."""
    next_device = getattr(tion, "_next_btle_device", None)
    if isinstance(next_device, BLEDevice):
        return next_device

    mac = getattr(tion, "_mac", None)
    if isinstance(mac, BLEDevice):
        return mac

    client_device = getattr(getattr(tion, "_btle", None), "_device", None)
    if isinstance(client_device, BLEDevice):
        return client_device

    return None


async def _establish_tion_connection(tion: Tion) -> bool:
    """Connect via bleak-retry-connector and proactively clear stale BlueZ links."""
    ble_device = _get_ble_device(tion)
    if ble_device is None:
        return await tion._btle.connect()

    try:
        await close_stale_connections_by_address(ble_device.address)
        await wait_for_disconnect(ble_device, 1.0)
    except Exception as err:  # pragma: no cover - best-effort BlueZ hygiene
        _LOGGER.debug("Failed to close stale BLE connections for %s: %s", ble_device.address, err)

    tion._btle = await establish_connection(
        BleakClientWithServiceCache,
        ble_device,
        name=f"Tion {tion.model} {ble_device.address}",
        ble_device_callback=lambda: _get_ble_device(tion) or ble_device,
        use_services_cache=True,
    )
    return tion._btle.is_connected


async def _patched_try_connect(self: Tion) -> bool:
    self.set_new_btle_device()
    return await _establish_tion_connection(self)


async def _patched_enable_notifications(self: Tion):
    _LOGGER.debug("Enabling notifications for %s. %s", self.mac, self.connection_status)
    if getattr(self, "_Tion__notifications_enabled", False):
        _LOGGER.debug("Notifications are already enabled for %s", self.mac)
        return

    try:
        await self._btle.start_notify(self.uuid_notify, self._delegation.handleNotification)
    except bleak_exc.BleakDBusError as err:
        if "Notify acquired" not in str(err):
            _LOGGER.warning("Got exception %s while enabling notifications!", str(err))
            raise

        _LOGGER.warning(
            "Notify acquired for %s while enabling notifications; reconnecting with stale cleanup",
            self.mac,
        )
        try:
            await self._btle.disconnect()
        except Exception as disconnect_err:  # pragma: no cover - best effort
            _LOGGER.debug("Disconnect after notify conflict failed for %s: %s", self.mac, disconnect_err)

        setattr(self, "_Tion__notifications_enabled", False)
        ble_device = _get_ble_device(self)
        await close_stale_connections_by_address(self.mac)
        if ble_device is not None:
            await wait_for_disconnect(ble_device, 1.0)
        await _establish_tion_connection(self)
        await self._btle.start_notify(self.uuid_notify, self._delegation.handleNotification)
    except bleak_exc.BleakError as err:
        _LOGGER.warning("Got exception %s while enabling notifications!", str(err))
        raise

    setattr(self, "_Tion__notifications_enabled", True)
    _LOGGER.debug("Notifications enabled for %s", self.mac)


async def _patched_disconnect(self: Tion):
    _LOGGER.debug("Disconnecting %s. %s", self.mac, self.connection_status)
    if self.connection_status != "disc":
        try:
            if getattr(self, "_Tion__notifications_enabled", False):
                try:
                    await self._btle.stop_notify(self.uuid_notify)
                except Exception as notify_err:  # pragma: no cover - best effort
                    _LOGGER.debug("stop_notify failed for %s: %s", self.mac, notify_err)
            await self._btle.disconnect()
        finally:
            setattr(self, "_Tion__notifications_enabled", False)
            async with self._semaphore:
                self.set_new_btle_device()

    _LOGGER.debug("Disconnect finished for %s. %s", self.mac, self.connection_status)


def _patch_tion_ble() -> None:
    """Patch tion-btle runtime to use HA's retry-aware BLE connector."""
    global _TION_PATCHED
    if _TION_PATCHED:
        return

    tion_btle.tion.Tion._try_connect = _patched_try_connect
    tion_btle.tion.Tion._enable_notifications = _patched_enable_notifications
    tion_btle.tion.Tion._disconnect = _patched_disconnect
    _TION_PATCHED = True


async def async_setup(hass, config):
    _patch_tion_ble()
    return True


async def async_setup_entry(hass, config_entry: ConfigEntry):
    _patch_tion_ble()
    _LOGGER.info("Setting up %s ", config_entry.unique_id)

    hass.data.setdefault(DOMAIN, {})

    instance = TionInstance(hass, config_entry)
    hass.data[DOMAIN][config_entry.unique_id] = instance
    config_entry.async_on_unload(
        bluetooth.async_register_callback(
            hass=hass,
            callback=instance.update_btle_device,
            match_dict=BluetoothCallbackMatcher(address=instance.config[CONF_MAC], connectable=True),
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    await hass.data[DOMAIN][config_entry.unique_id].async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    return True


class TionInstance(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):

        self._hass: HomeAssistant = hass
        self._config_entry: ConfigEntry = config_entry

        assert self.config[CONF_MAC] is not None
        # https://developers.home-assistant.io/docs/network_discovery/#fetching-the-bleak-bledevice-from-the-address
        btle_device = bluetooth.async_ble_device_from_address(hass, self.config[CONF_MAC], connectable=True)
        if btle_device is None:
            raise ConfigEntryNotReady

        self.__keep_alive: int = 60
        try:
            self.__keep_alive = self.config[CONF_KEEP_ALIVE]
        except KeyError:
            pass

        # delay before next update if we got btle.BTLEDisconnectError
        self._delay: int = 600

        self.__tion: Tion = self.getTion(self.model, btle_device)
        self._operation_lock = asyncio.Lock()
        self.__keep_alive = datetime.timedelta(seconds=self.__keep_alive)
        self._delay = datetime.timedelta(seconds=self._delay)
        self.rssi: int = 0

        if self._config_entry.unique_id is None:
            _LOGGER.critical(f"Unique id is None for {self._config_entry.title}! "
                             f"Will fix it by using {self.unique_id}")
            hass.config_entries.async_update_entry(
                entry=self._config_entry,
                unique_id=self.unique_id,
            )
            _LOGGER.critical("Done! Please restart Home Assistant.")

        super().__init__(
            name=self.config['name'] if 'name' in self.config else TION_SCHEMA['name']['default'],
            hass=hass,
            logger=_LOGGER,
            update_interval=self.__keep_alive,
            update_method=self.async_update_state,
        )

    def _refresh_btle_device(self) -> BLEDevice | None:
        """Refresh the BLEDevice object from Home Assistant's Bluetooth cache."""
        btle_device = bluetooth.async_ble_device_from_address(self._hass, self.config[CONF_MAC], connectable=True)
        if btle_device is not None:
            self.__tion.update_btle_device(btle_device)
        return btle_device

    @staticmethod
    def _retry_delay(attempt: int) -> int:
        """Return exponential retry delay for a one-based attempt number."""
        return min(COMMAND_RETRY_BASE_DELAY * (2 ** max(attempt - 1, 0)), COMMAND_RETRY_MAX_DELAY)

    @staticmethod
    def _is_retryable_error(err: Exception) -> bool:
        """Return whether a Tion operation should be retried."""
        return isinstance(
            err,
            (
                bleak_exc.BleakError,
                BleakConnectionError,
                MaxTriesExceededError,
                TionException,
                TimeoutError,
                asyncio.TimeoutError,
            ),
        )

    async def _prepare_ble_retry(self, operation_name: str, attempt: int, attempts: int, err: Exception) -> None:
        """Best-effort cleanup before retrying a BLE operation."""
        delay = self._retry_delay(attempt)
        mac = self.config[CONF_MAC]

        _LOGGER.warning(
            "%s failed for %s on attempt %s/%s: %s. Retrying in %ss",
            operation_name,
            self.name,
            attempt,
            attempts,
            err,
            delay,
        )

        try:
            await self.__tion.disconnect()
        except Exception as disconnect_err:  # pragma: no cover - best-effort BlueZ hygiene
            _LOGGER.debug("Retry cleanup disconnect failed for %s: %s", mac, disconnect_err)

        btle_device = self._refresh_btle_device()
        try:
            await close_stale_connections_by_address(mac)
            if btle_device is not None:
                await wait_for_disconnect(btle_device, 1.0)
        except Exception as cleanup_err:  # pragma: no cover - best-effort BlueZ hygiene
            _LOGGER.debug("Retry cleanup stale close failed for %s: %s", mac, cleanup_err)

        await asyncio.sleep(delay)

    async def _run_retryable_tion_operation(self, operation_name: str, operation, *, attempts: int, service_call: bool):
        """Run a Tion operation with command-level retries around BLE failures."""
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._refresh_btle_device()
            try:
                return await operation()
            except Exception as err:
                if not self._is_retryable_error(err):
                    raise

                last_error = err
                if attempt >= attempts:
                    break

                await self._prepare_ble_retry(operation_name, attempt, attempts, err)

        message = (
            f"{self.name}: не удалось выполнить {operation_name} после {attempts} command attempt(s). "
            f"Последняя ошибка Bluetooth: {last_error}"
        )
        _LOGGER.warning(message)
        if service_call:
            raise HomeAssistantError(message) from last_error
        raise last_error

    @property
    def config(self) -> dict:
        try:
            data = dict(self._config_entry.data or {})
        except AttributeError:
            data = {}

        try:
            options = self._config_entry.options or {}
            data.update(options)
        except AttributeError:
            pass
        return data

    @staticmethod
    def _decode_state(state: str) -> bool:
        return True if state == "on" else False

    async def async_update_state(self):
        self.logger.info("Tion instance update started")
        response: dict[str, str | bool | int] = {}
        preset_mode = PRESET_NONE
        if isinstance(self.data, dict):
            preset_mode = tion_preset_canonical(self.data.get("preset_mode", PRESET_NONE))

        async with self._operation_lock:
            try:
                response = await self._run_retryable_tion_operation(
                    "poll state",
                    self.__tion.get,
                    attempts=POLL_RETRY_ATTEMPTS,
                    service_call=False,
                )
                self.update_interval = self.__keep_alive

            except MaxTriesExceededError as e:
                _LOGGER.warning("Polling failed for %s: %s. Will delay next check", self.name, e)
                self.update_interval = self._delay
                raise UpdateFailed("MaxTriesExceededError") from e
            except Exception as e:
                if self._is_retryable_error(e):
                    _LOGGER.warning("Polling failed for %s: %s", self.name, e)
                    raise UpdateFailed(str(e)) from e
                _LOGGER.exception("Unexpected error while polling %s. response=%s", self.name, response)
                raise

        response["is_on"]: bool = self._decode_state(response["state"])
        response["heater"]: bool = self._decode_state(response["heater"])
        response["is_heating"] = self._decode_state(response["heating"])
        response["filter_remain"] = math.ceil(response["filter_remain"])
        response["fan_speed"] = int(response["fan_speed"])
        response["rssi"] = self.rssi
        response["preset_mode"] = tion_preset_canonical(preset_mode)

        self.logger.debug(f"Result is {response}")
        return response

    @property
    def away_temp(self) -> int:
        """Temperature for away mode"""
        return self.config[CONF_AWAY_TEMP] if CONF_AWAY_TEMP in self.config else TION_SCHEMA[CONF_AWAY_TEMP]['default']

    async def set(self, **kwargs):
        if "fan_speed" in kwargs:
            kwargs["fan_speed"] = int(kwargs["fan_speed"])

        original_args = kwargs.copy()
        if "is_on" in kwargs:
            kwargs["state"] = "on" if kwargs["is_on"] else "off"
            del kwargs["is_on"]
        if "heater" in kwargs:
            kwargs["heater"] = "on" if kwargs["heater"] else "off"

        args = ', '.join('%s=%r' % x for x in kwargs.items())
        _LOGGER.info("Need to set: " + args)
        async with self._operation_lock:
            await self._run_retryable_tion_operation(
                f"set {args}",
                lambda: self.__tion.set(kwargs.copy()),
                attempts=COMMAND_ATTEMPTS,
                service_call=True,
            )
        self.data.update(original_args)
        self.async_update_listeners()

    @staticmethod
    def getTion(model: str, mac: str | BLEDevice) -> tion_btle.TionS3 | tion_btle.TionLite | tion_btle.TionS4:
        if model == 'S3':
            from tion_btle.s3 import TionS3 as Breezer
        elif model == 'S4':
            from tion_btle.s4 import TionS4 as Breezer
        elif model == 'Lite':
            from tion_btle.lite import TionLite as Breezer
        else:
            raise NotImplementedError("Model '%s' is not supported!" % model)
        return Breezer(mac)

    async def connect(self):
        return await self._run_retryable_tion_operation(
            "connect",
            self.__tion.connect,
            attempts=COMMAND_ATTEMPTS,
            service_call=True,
        )

    async def disconnect(self):
        return await self.__tion.disconnect()

    @property
    def device_info(self):
        info = {"identifiers": {(DOMAIN, self.unique_id)}, "name": self.name, "manufacturer": "Tion",
                "model": self.data.get("model")}
        if self.data.get("fw_version") is not None:
            info['sw_version'] = self.data.get("fw_version")
        return info

    @cached_property
    def unique_id(self):
        return self.config[CONF_MAC]

    @cached_property
    def supported_air_sources(self) -> list[str]:
        if self.model == "S3":
            return ["outside", "mixed", "recirculation"]
        else:
            return ["outside", "recirculation"]

    @cached_property
    def model(self) -> str:
        try:
            model = self.config['model']
        except KeyError:
            _LOGGER.warning(f"Model was not found in config. "
                            f"Please update integration settings! Config is {self.config}")
            _LOGGER.warning("Assume that model is S3")
            model = 'S3'
        return model

    @callback
    def update_btle_device(
            self,
            service_info: bluetooth.BluetoothServiceInfoBleak,
            _change: bluetooth.BluetoothChange
    ) -> None:
        if service_info.device is not None:
            self.rssi = service_info.rssi
            self.__tion.update_btle_device(service_info.device)
