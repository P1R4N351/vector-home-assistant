"""Robot data passed through as sensors: lifetime stats, settings, address, and status flags.

Stats, settings and the address come from the slow data coordinator in
manual.py (robotdata.py reads them); the status flags come from the robot_state reading the
main coordinator already makes, so they cost no extra request.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfLength, UnitOfTime
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import device_info
from .const import CONF_SERIAL
from .manual import ensure_extras

# (key in the stats, unique-id suffix, name, device class, unit, suggested unit, decimals shown, icon)
STAT_SENSORS = (
    ("time_on_s", "time_on", "Time switched on", SensorDeviceClass.DURATION, UnitOfTime.SECONDS,
     UnitOfTime.DAYS, 1, "mdi:timer-outline"),
    ("petted_s", "time_petted", "Time petted", SensorDeviceClass.DURATION, UnitOfTime.SECONDS,
     UnitOfTime.MINUTES, 0, "mdi:hand-heart-outline"),
    ("distance_m", "distance_driven", "Distance driven", SensorDeviceClass.DISTANCE, UnitOfLength.METERS,
     None, 1, "mdi:map-marker-distance"),
    ("wake_word_reactions", "wake_word_reactions", "Wake word reactions", None, None, None, None,
     "mdi:account-voice"),
)
# (key in the settings, unique-id suffix, name, icon)
SETTING_SENSORS = (
    ("locale", "locale", "Locale", "mdi:translate"),
    ("time_zone", "time_zone", "Time zone", "mdi:map-clock-outline"),
    ("default_location", "location", "Location", "mdi:map-marker"),
)
# (flag in robot_state, unique-id suffix, name, device class, icon)
FLAG_SENSORS = (
    ("picked_up", "picked_up", "Picked up", None, "mdi:hand-back-left"),
    ("being_held", "being_held", "Being held", None, "mdi:hand-okay"),
    ("button_pressed", "button_pressed", "Back button pressed", None, "mdi:gesture-tap-button"),
    ("moving", "moving", "Moving", BinarySensorDeviceClass.MOVING, "mdi:robot-mower-outline"),
    ("animating", "animating", "Animating", None, "mdi:emoticon-outline"),
    ("cliff_detected", "cliff_detected", "Edge detected", None, "mdi:slope-downhill"),
)


def extra_sensors(hass, entry: ConfigEntry) -> list[SensorEntity]:
    extras = ensure_extras(hass, entry)
    return [*[StatSensor(entry, extras, *spec) for spec in STAT_SENSORS],
            LifetimeCountersSensor(entry, extras),
            *[SettingSensor(entry, extras, *spec) for spec in SETTING_SENSORS],
            AddressSensor(entry, extras)]


def extra_binary_sensors(entry: ConfigEntry) -> list[BinarySensorEntity]:
    return [FlagSensor(entry, *spec) for spec in FLAG_SENSORS]


class _ExtrasSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, extras, suffix: str, name: str, icon: str) -> None:
        assert entry is not None and extras is not None and bool(suffix)
        super().__init__(extras)
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_{suffix}"
        self._attr_device_info = device_info(entry)

    def _part(self, part: str) -> dict | None:
        data = self.coordinator.data
        value = data.get(part) if isinstance(data, dict) else None
        return value if isinstance(value, dict) else None


class StatSensor(_ExtrasSensor):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, entry: ConfigEntry, extras, key: str, suffix: str, name: str, device_class,
                 unit, suggested, decimals: int | None, icon: str) -> None:
        super().__init__(entry, extras, suffix, name, icon)
        self._key = key
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        if suggested is not None:
            self._attr_suggested_unit_of_measurement = suggested
        if decimals is not None:
            self._attr_suggested_display_precision = decimals

    @property
    def native_value(self) -> float | int | None:
        stats = self._part("stats")
        return None if stats is None else stats.get(self._key)


class LifetimeCountersSensor(_ExtrasSensor):
    """Every lifetime counter the robot keeps, unchanged, as attributes."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, extras) -> None:
        super().__init__(entry, extras, "lifetime_counters", "Lifetime counters", "mdi:counter")

    @property
    def native_value(self) -> int | None:
        stats = self._part("stats")
        return None if stats is None else len(stats.get("counters") or {})

    @property
    def extra_state_attributes(self) -> dict:
        stats = self._part("stats") or {}
        return dict(stats.get("counters") or {})


class SettingSensor(_ExtrasSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, extras, key: str, suffix: str, name: str, icon: str) -> None:
        super().__init__(entry, extras, suffix, name, icon)
        self._key = key

    @property
    def native_value(self) -> str | None:
        settings = self._part("settings")
        return None if settings is None else settings.get(self._key)

    @property
    def extra_state_attributes(self) -> dict:
        if self._key != "locale":
            return {}
        settings = self._part("settings") or {}
        return {k: settings.get(k) for k in ("clock_24_hour", "dist_is_metric", "temp_is_fahrenheit")}


class AddressSensor(_ExtrasSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, extras) -> None:
        super().__init__(entry, extras, "ip_address", "IP address", "mdi:ip-network")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        return data.get("ip_address") if isinstance(data, dict) else None


class FlagSensor(CoordinatorEntity, BinarySensorEntity):
    """One of the robot's own status flags, from the reading the main poll already makes."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, flag: str, suffix: str, name: str, device_class, icon: str) -> None:
        assert entry is not None and bool(flag)
        super().__init__(entry.runtime_data.coordinator)
        self._flag = flag
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_class = device_class
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_{suffix}"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        state = data.get("robot_state")
        if not data.get("robot_reachable") or not isinstance(state, dict):
            return None
        value = state.get(self._flag)
        return value if isinstance(value, bool) else None
