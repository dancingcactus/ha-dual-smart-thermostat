"""Adds support for dual smart thermostat units."""

import asyncio
from datetime import timedelta
import datetime
import logging

import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_AWAY,
    PRESET_ECO,
    PRESET_COMFORT,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
    SUPPORT_TARGET_TEMPERATURE_RANGE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    PRECISION_HALVES,
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_OPEN,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, CoreState, callback, HomeAssistant
from homeassistant.helpers import condition
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
    async_call_later,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.dual_smart_thermostat.opening_manager import OpeningManager

from .const import (
    CONF_AC_MODE,
    CONF_COLD_TOLERANCE,
    CONF_COOLER,
    CONF_FLOOR_SENSOR,
    CONF_HEATER,
    CONF_MIN_FLOOR_TEMP,
    CONF_SECONDARY_HEATER,
    CONF_SECONDARY_HEATING_TIMEOUT,
    CONF_HEAT_COOL_MODE,
    CONF_HOT_TOLERANCE,
    CONF_INITIAL_HVAC_MODE,
    CONF_KEEP_ALIVE,
    CONF_MAX_FLOOR_TEMP,
    CONF_MAX_TEMP,
    CONF_MIN_DUR,
    CONF_MIN_TEMP,
    CONF_OPENINGS,
    CONF_PRECISION,
    CONF_SENSOR,
    CONF_TARGET_TEMP,
    CONF_TARGET_TEMP_HIGH,
    CONF_TARGET_TEMP_LOW,
    CONF_TEMP_STEP,
    ATTR_TIMEOUT,
    DEFAULT_MAX_FLOOR_TEMP,
    DEFAULT_NAME,
    DEFAULT_TOLERANCE,
    TIMED_OPENING_SCHEMA,
    HVACAction,
    HVACMode,
    PRESET_ANTI_FREEZE,
    ToleranceDevice,
)
from . import DOMAIN, PLATFORMS

ATTR_PREV_TARGET = "prev_target_temp"
ATTR_PREV_TARGET_LOW = "prev_target_temp_low"
ATTR_PREV_TARGET_HIGH = "prev_target_temp_high"

CONF_PRESETS = {
    p: f"{p.replace(' ', '_').lower()}"
    for p in (
        PRESET_AWAY,
        PRESET_COMFORT,
        PRESET_ECO,
        PRESET_HOME,
        PRESET_SLEEP,
        PRESET_ANTI_FREEZE,
    )
}
CONF_PRESETS_OLD = {k: f"{v}_temp" for k, v in CONF_PRESETS.items()}

_LOGGER = logging.getLogger(__name__)

PRESET_SCHEMA = {
    vol.Optional(ATTR_TEMPERATURE): vol.Coerce(float),
    vol.Optional(ATTR_TARGET_TEMP_LOW): vol.Coerce(float),
    vol.Optional(ATTR_TARGET_TEMP_HIGH): vol.Coerce(float),
}

SECONDARY_HEATING_SCHEMA = {
    vol.Optional(CONF_SECONDARY_HEATER): cv.entity_id,
    vol.Optional(CONF_SECONDARY_HEATING_TIMEOUT): vol.All(
        cv.time_period, cv.positive_timedelta
    ),
}

FLOOR_TEMPERATURE_SCHEMA = {
    vol.Optional(CONF_FLOOR_SENSOR): cv.entity_id,
    vol.Optional(CONF_MAX_FLOOR_TEMP): vol.Coerce(float),
    vol.Optional(CONF_MIN_FLOOR_TEMP): vol.Coerce(float),
}

OPENINGS_SCHEMA = {
    vol.Optional(CONF_OPENINGS): [vol.Any(cv.entity_id, TIMED_OPENING_SCHEMA)]
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HEATER): cv.entity_id,
        vol.Optional(CONF_COOLER): cv.entity_id,
        vol.Required(CONF_SENSOR): cv.entity_id,
        vol.Optional(CONF_AC_MODE): cv.boolean,
        vol.Optional(CONF_HEAT_COOL_MODE): cv.boolean,
        vol.Optional(CONF_MAX_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MIN_DUR): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_MIN_TEMP): vol.Coerce(float),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_COLD_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_HOT_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_HIGH): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_LOW): vol.Coerce(float),
        vol.Optional(CONF_KEEP_ALIVE): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(
            [HVACMode.COOL, HVACMode.HEAT, HVACMode.OFF, HVACMode.HEAT_COOL]
        ),
        vol.Optional(CONF_PRECISION): vol.In(
            [PRECISION_TENTHS, PRECISION_HALVES, PRECISION_WHOLE]
        ),
        vol.Optional(CONF_TEMP_STEP): vol.In(
            [PRECISION_TENTHS, PRECISION_HALVES, PRECISION_WHOLE]
        ),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
).extend({vol.Optional(v): PRESET_SCHEMA for (k, v) in CONF_PRESETS.items()})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(SECONDARY_HEATING_SCHEMA)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(FLOOR_TEMPERATURE_SCHEMA)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(OPENINGS_SCHEMA)

# Add the old presets schema to avoid breaking change
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Optional(v): vol.Coerce(float) for (k, v) in CONF_PRESETS_OLD.items()}
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: None = None,
):
    """Set up the smart dual thermostat platform."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config[CONF_NAME]
    heater_entity_id = config[CONF_HEATER]
    secondary_heater_entity_id = config.get(CONF_SECONDARY_HEATER)
    secondary_heater_timeout = config.get(CONF_SECONDARY_HEATING_TIMEOUT)
    sensor_entity_id = config[CONF_SENSOR]
    if cooler_entity_id := config.get(CONF_COOLER):
        if cooler_entity_id == heater_entity_id:
            _LOGGER.warning(
                "'cooler' entity cannot be equal to 'heater' entity. "
                "'cooler' entity will be ignored"
            )
            cooler_entity_id = None
    sensor_floor_entity_id = config.get(CONF_FLOOR_SENSOR)
    openings = config.get(CONF_OPENINGS)
    min_temp = config.get(CONF_MIN_TEMP)
    max_temp = config.get(CONF_MAX_TEMP)
    max_floor_temp = config.get(CONF_MAX_FLOOR_TEMP)
    min_floor_temp = config.get(CONF_MIN_FLOOR_TEMP)
    target_temp = config.get(CONF_TARGET_TEMP)
    target_temp_high = config.get(CONF_TARGET_TEMP_HIGH)
    target_temp_low = config.get(CONF_TARGET_TEMP_LOW)
    ac_mode = config.get(CONF_AC_MODE)
    heat_cool_mode = config.get(CONF_HEAT_COOL_MODE)
    min_cycle_duration = config.get(CONF_MIN_DUR)
    cold_tolerance = config.get(CONF_COLD_TOLERANCE)
    hot_tolerance = config.get(CONF_HOT_TOLERANCE)
    keep_alive = config.get(CONF_KEEP_ALIVE)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    presets_dict = {
        key: config[value] for key, value in CONF_PRESETS.items() if value in config
    }
    presets = {
        key: values[ATTR_TEMPERATURE]
        for key, values in presets_dict.items()
        if ATTR_TEMPERATURE in values
    }
    presets_range = {
        key: [values[ATTR_TARGET_TEMP_LOW], values[ATTR_TARGET_TEMP_HIGH]]
        for key, values in presets_dict.items()
        if ATTR_TARGET_TEMP_LOW in values
        and ATTR_TARGET_TEMP_HIGH in values
        and values[ATTR_TARGET_TEMP_LOW] < values[ATTR_TARGET_TEMP_HIGH]
    }

    # Try to load presets in old format and use if new format not available in config
    old_presets = {k: config[v] for k, v in CONF_PRESETS_OLD.items() if v in config}
    if old_presets:
        _LOGGER.warning(
            "Found deprecated presets settings in configuration. "
            "Please remove and replace with new presets settings format. "
            "Read documentation in integration repository for more details"
        )
        if not presets_dict:
            presets = old_presets

    precision = config.get(CONF_PRECISION)
    target_temperature_step = config.get(CONF_TEMP_STEP)
    unit = hass.config.units.temperature_unit
    unique_id = config.get(CONF_UNIQUE_ID)

    async_add_entities(
        [
            DualSmartThermostat(
                name,
                heater_entity_id,
                secondary_heater_entity_id,
                secondary_heater_timeout,
                cooler_entity_id,
                sensor_entity_id,
                sensor_floor_entity_id,
                min_temp,
                max_temp,
                max_floor_temp,
                min_floor_temp,
                target_temp,
                target_temp_high,
                target_temp_low,
                ac_mode,
                heat_cool_mode,
                min_cycle_duration,
                cold_tolerance,
                hot_tolerance,
                keep_alive,
                initial_hvac_mode,
                presets,
                presets_range,
                precision,
                target_temperature_step,
                unit,
                unique_id,
                OpeningManager(hass, openings),
            )
        ]
    )


class DualSmartThermostat(ClimateEntity, RestoreEntity):
    """Representation of a Dual Smart Thermostat device."""

    def __init__(
        self,
        name,
        heater_entity_id,
        secondary_heater_entity_id,
        secondary_heater_timeout,
        cooler_entity_id,
        sensor_entity_id,
        sensor_floor_entity_id,
        min_temp,
        max_temp,
        max_floor_temp,
        min_floor_temp,
        target_temp,
        target_temp_high,
        target_temp_low,
        ac_mode,
        heat_cool_mode,
        min_cycle_duration,
        cold_tolerance,
        hot_tolerance,
        keep_alive,
        initial_hvac_mode,
        presets,
        presets_range,
        precision,
        target_temperature_step,
        unit,
        unique_id,
        opening_manager,
    ):
        """Initialize the thermostat."""
        self._name = name

        self.heater_entity_id = heater_entity_id
        self.secondary_heater_entity_id = secondary_heater_entity_id
        self.secondary_heater_timeout: timedelta = secondary_heater_timeout
        self.cooler_entity_id = cooler_entity_id
        self.sensor_entity_id = sensor_entity_id
        self.sensor_floor_entity_id = sensor_floor_entity_id
        self.opening_manager = opening_manager

        self.ac_mode = ac_mode
        self._heat_cool_mode = heat_cool_mode
        self.min_cycle_duration: timedelta = min_cycle_duration
        self._cold_tolerance = cold_tolerance
        self._hot_tolerance = hot_tolerance
        self._keep_alive = keep_alive
        self._saved_target_temp = target_temp or next(iter(presets.values()), None)
        self._saved_target_temp_low = None
        self._saved_target_temp_high = None
        self._temp_precision = precision
        self._temp_target_temperature_step = target_temperature_step
        self._target_temp = target_temp
        self._target_temp_high = target_temp_high
        self._target_temp_low = target_temp_low
        if self.heater_entity_id and self.cooler_entity_id:
            # if both switch entity are defined ac_mode must be false
            self.ac_mode = False
            self._hvac_list = [
                HVACMode.OFF,
                HVACMode.HEAT,
                HVACMode.COOL,
            ]
            if self._is_configured_for_heat_cool():
                self._hvac_list.append(HVACMode.HEAT_COOL)
        elif self.ac_mode:
            self._hvac_list = [HVACMode.COOL, HVACMode.OFF]
        else:
            self._hvac_list = [HVACMode.HEAT, HVACMode.OFF]
        if initial_hvac_mode in self._hvac_list:
            self._hvac_mode = initial_hvac_mode
        else:
            self._hvac_mode = None
        self._active = False
        self._cur_temp = None
        self._cur_floor_temp = None
        self._temp_lock = asyncio.Lock()
        self._min_temp = min_temp
        self._max_temp = max_temp
        self._max_floor_temp = max_floor_temp
        self._min_floor_temp = min_floor_temp
        self._unit = unit
        self._unique_id = unique_id
        self._support_flags = SUPPORT_TARGET_TEMPERATURE
        if len(presets):
            self._support_flags |= SUPPORT_PRESET_MODE
            self._preset_modes = [PRESET_NONE] + list(presets.keys())
        else:
            self._preset_modes = [PRESET_NONE]
        self._presets = presets
        if len(presets_range):
            self._preset_range_modes = [PRESET_NONE] + list(presets_range.keys())
        else:
            self._preset_range_modes = [PRESET_NONE]
        self._presets_range = presets_range
        self._preset_mode = PRESET_NONE

        # secondary heater last run time
        if self.secondary_heater_entity_id and self.secondary_heater_timeout:
            self._secondary_heater_last_run: datetime = None

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.sensor_entity_id], self._async_sensor_changed
            )
        )

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.heater_entity_id], self._async_switch_changed
            )
        )

        if self.cooler_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self.cooler_entity_id],
                    self._async_cooler_changed,
                )
            )

        if self.sensor_floor_entity_id is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self.sensor_floor_entity_id],
                    self._async_sensor_floor_changed,
                )
            )

        if self._keep_alive:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._async_control_climate, self._keep_alive
                )
            )

        if self.opening_manager.opening_entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    self.opening_manager.opening_entities,
                    self._async_opening_changed,
                )
            )

        @callback
        def _async_startup(*_):
            """Init on startup."""
            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if self.sensor_floor_entity_id:
                floor_sensor_state = self.hass.states.get(self.sensor_floor_entity_id)
            else:
                floor_sensor_state = None

            if sensor_state and sensor_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self._async_update_temp(sensor_state)
                self.async_write_ha_state()

            if floor_sensor_state and floor_sensor_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self._async_update_floor_temp(floor_sensor_state)
                self.async_write_ha_state()

            switch_state = self.hass.states.get(self.heater_entity_id)
            if switch_state and switch_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self.hass.create_task(self._check_switch_initial_state())

            if self.cooler_entity_id is not None and (
                cooler_switch_state := self.hass.states.get(self.cooler_entity_id)
            ):
                if switch_state and cooler_switch_state.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                ):
                    self.hass.create_task(self._check_switch_initial_state())

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

        # Check If we have an old state
        if (old_state := await self.async_get_last_state()) is not None:
            # If we have no initial temperature, restore
            if self._target_temp_low is None:
                old_target_min = old_state.attributes.get(ATTR_PREV_TARGET_LOW)
                if old_target_min is not None:
                    self._target_temp_low = float(old_target_min)
            if self._target_temp_high is None:
                old_target_max = old_state.attributes.get(ATTR_PREV_TARGET_HIGH)
                if old_target_max is not None:
                    self._target_temp_high = float(old_target_max)
            if self._target_temp is None:
                old_target = old_state.attributes.get(ATTR_PREV_TARGET)
                if old_target is None:
                    old_target = old_state.attributes.get(ATTR_TEMPERATURE)
                if old_target is not None:
                    self._target_temp = float(old_target)

            supp_feat = old_state.attributes.get(ATTR_SUPPORTED_FEATURES)
            hvac_mode = self._hvac_mode or old_state.state or HVACMode.OFF
            if (
                supp_feat & SUPPORT_TARGET_TEMPERATURE_RANGE
                and self._is_configured_for_heat_cool()
                and hvac_mode in (HVACMode.HEAT_COOL, HVACMode.OFF)
            ):
                self._support_flags = SUPPORT_TARGET_TEMPERATURE_RANGE
                if len(self._presets_range):
                    self._support_flags |= SUPPORT_PRESET_MODE
                self._set_default_target_temps()
            else:
                if hvac_mode not in self.hvac_modes:
                    hvac_mode = HVACMode.OFF
                self._set_default_target_temps()

            # restore previous preset mode if available
            old_pres_mode = old_state.attributes.get(ATTR_PRESET_MODE)
            if self._is_range_mode():
                if self.preset_modes and old_pres_mode in self._presets_range:
                    self._preset_mode = old_pres_mode
                    self._saved_target_temp_low = self._target_temp_low
                    self._saved_target_temp_high = self._target_temp_high
                    self._target_temp_low = self._presets_range[old_pres_mode][0]
                    self._target_temp_high = self._presets_range[old_pres_mode][1]
            elif self.preset_modes and old_pres_mode in self._presets:
                self._preset_mode = old_pres_mode
                self._saved_target_temp = self._target_temp
                self._target_temp = self._presets[old_pres_mode]

            self._hvac_mode = hvac_mode

        else:
            # No previous state, try and restore defaults
            if not self._hvac_mode:
                self._hvac_mode = HVACMode.OFF
            if self._hvac_mode == HVACMode.OFF:
                self._set_default_target_temps()

        # Set correct support flag
        self._set_support_flags()

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id of this thermostat."""
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system."""
        if self._temp_precision is not None:
            return self._temp_precision
        return super().precision

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        if self._temp_target_temperature_step is not None:
            return self._temp_target_temperature_step
        # if a target_temperature_step is not defined, fallback to equal the precision
        return self.precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_temperature(self):
        """Return the sensor temperature."""
        return self._cur_temp

    @property
    def current_floor_temperature(self):
        """Return the sensor temperature."""
        return self._cur_floor_temp

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if not self._is_device_active:
            return HVACAction.IDLE
        if self.ac_mode:
            return HVACAction.COOLING
        if self._is_cooler_active:
            return HVACAction.COOLING
        return HVACAction.HEATING

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temp

    @property
    def target_temperature_high(self):
        """Return the upper bound temperature."""
        return self._target_temp_high

    @property
    def target_temperature_low(self):
        """Return the lower bound temperature."""
        return self._target_temp_low

    @property
    def floor_temperature_limit(self):
        """Return the maximum floor temperature."""
        return self._max_floor_temp

    @property
    def floor_temperature_required(self):
        """Return the maximum floor temperature."""
        return self._min_floor_temp

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return self._hvac_list

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp."""
        if not self._support_flags & SUPPORT_PRESET_MODE:
            return None
        return self._preset_mode

    @property
    def preset_modes(self):
        """Return a list of available preset modes or PRESET_NONE."""
        if not self._support_flags & SUPPORT_PRESET_MODE:
            return None
        if self._is_range_mode():
            return self._preset_range_modes
        return self._preset_modes

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes to be saved."""
        attributes = {}
        if self._target_temp_low is not None:
            if self._preset_mode != PRESET_NONE and self._is_range_mode():
                attributes[ATTR_PREV_TARGET_LOW] = self._saved_target_temp_low
            else:
                attributes[ATTR_PREV_TARGET_LOW] = self._target_temp_low
        if self._target_temp_high is not None:
            if self._preset_mode != PRESET_NONE and self._is_range_mode():
                attributes[ATTR_PREV_TARGET_HIGH] = self._saved_target_temp_high
            else:
                attributes[ATTR_PREV_TARGET_HIGH] = self._target_temp_high
        if self._target_temp is not None:
            if self._preset_mode != PRESET_NONE and self._is_target_mode():
                attributes[ATTR_PREV_TARGET] = self._saved_target_temp
            else:
                attributes[ATTR_PREV_TARGET] = self._target_temp

        return attributes

    async def async_set_hvac_mode(self, hvac_mode):
        """Call climate mode based on current mode"""
        _LOGGER.debug("Setting hvac mode: %s", hvac_mode)
        match hvac_mode:
            case HVACMode.HEAT:
                self._hvac_mode = HVACMode.HEAT
                self._set_support_flags()
                await self._async_control_heating(force=True)

            case HVACMode.COOL:
                self._hvac_mode = HVACMode.COOL
                self._set_support_flags()
                await self._async_control_cooling(force=True)

            case HVACMode.HEAT_COOL:
                self._hvac_mode = HVACMode.HEAT_COOL
                self._set_support_flags()
                await self._async_control_heat_cool(force=True)

            case HVACMode.OFF:
                self._hvac_mode = HVACMode.OFF
                if self._is_device_active:
                    await self._async_heater_turn_off()
                if self.cooler_entity_id:
                    await self._async_cooler_turn_off()

            case _:
                _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
                return
        # Ensure we update the current operation after changing the mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        temp_low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        temp_high = kwargs.get(ATTR_TARGET_TEMP_HIGH)

        if self._is_target_mode():
            if temperature is None:
                return
            self._target_temp = temperature

        elif self._is_range_mode():
            if temp_low is None or temp_high is None:
                return
            self._target_temp_low = temp_low
            self._target_temp_high = temp_high

        if self._preset_mode != PRESET_NONE:
            self._preset_mode = PRESET_NONE

        await self._async_control_climate(force=True)
        self.async_write_ha_state()

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        if self._min_temp is not None:
            return self._min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        if self._max_temp is not None:
            return self._max_temp

        # Get default temp from super class
        return super().max_temp

    async def _async_sensor_changed(self, event):
        """Handle temperature changes."""
        new_state = event.data.get("new_state")
        _LOGGER.info("Sensor change: %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self._async_update_temp(new_state)
        await self._async_control_climate()
        self.async_write_ha_state()

    async def _async_sensor_floor_changed(self, event):
        """Handle floor temperature changes."""
        new_state = event.data.get("new_state")
        _LOGGER.info("Sensor floor change: %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self._async_update_floor_temp(new_state)
        await self._async_control_climate()
        self.async_write_ha_state()

    async def _check_switch_initial_state(self):
        """Prevent the device from keep running if HVACMode.OFF."""
        if self._hvac_mode == HVACMode.OFF and self._is_device_active:
            _LOGGER.warning(
                "The climate mode is OFF, but the switch device is ON. Turning off device %s",
                self.heater_entity_id,
            )
            await self._async_heater_turn_off()
            await self._async_cooler_turn_off()

    async def _async_opening_changed(self, event):
        """Handle opening changes."""
        new_state = event.data.get("new_state")
        _LOGGER.info("Opening changed: %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        opening_entity = event.data.get("entity_id")
        # get the opening timeout
        opening_timeout = None
        for opening in self.opening_manager.openings:
            if opening_entity == opening[ATTR_ENTITY_ID]:
                opening_timeout = opening[ATTR_TIMEOUT]
                break

        # schdule the closing of the opening
        if opening_timeout is not None and (
            new_state.state == STATE_OPEN or new_state.state == STATE_ON
        ):
            _LOGGER.debug(
                "Scheduling state open of opening %s in %s",
                opening_entity,
                opening_timeout,
            )
            self.async_on_remove(
                async_call_later(
                    self.hass,
                    opening_timeout,
                    self._async_control_climate_forced,
                )
            )
        else:
            await self._async_control_climate(force=True)

        self.async_write_ha_state()

    async def _async_control_climate(self, time=None, force=False):
        if self.cooler_entity_id is not None and self.hvac_mode == HVACMode.HEAT_COOL:
            await self._async_control_heat_cool(time, force)
        elif self.ac_mode is True or (
            self.cooler_entity_id is not None and self.hvac_mode == HVACMode.COOL
        ):
            await self._async_control_cooling(time, force)
        else:
            await self._async_control_heating(time, force)

    async def _async_control_climate_forced(self, time=None):
        _LOGGER.debug("_async_control_climate_forced, time %s", time)
        await self._async_control_climate(force=True, time=time)

    @callback
    def _async_switch_changed(self, event):
        """Handle heater switch state changes."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return
        if old_state is None:
            self.hass.create_task(self._check_switch_initial_state())
        self.async_write_ha_state()

    @callback
    def _async_cooler_changed(self, event):
        """Handle cooler switch state changes."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self.async_write_ha_state()

    @callback
    def _async_update_temp(self, state):
        """Update thermostat with latest state from sensor."""
        try:
            self._cur_temp = float(state.state)
        except ValueError as ex:
            _LOGGER.error("Unable to update from sensor: %s", ex)

    @callback
    def _async_update_floor_temp(self, state):
        """Update ermostat with latest floor temp state from floor temp sensor."""
        try:
            self._cur_floor_temp = float(state.state)
        except ValueError as ex:
            _LOGGER.error("Unable to update from floor temp sensor: %s", ex)

    async def _async_control_heating_forced(self, time=None):
        """Call turn_on heater device."""
        _LOGGER.debug("_async_control_heating_forced")
        _LOGGER.debug("Time check %s", time)
        await self._async_control_heating(time, force=True)

    async def _async_control_heating(self, time=None, force=False):
        """Check if we need to turn heating on or off."""
        async with self._temp_lock:
            _LOGGER.debug("_async_control_heating")
            self.set_self_active()

            if not self._needs_control(time, force):
                _LOGGER.debug("No need for control")
                return

            _LOGGER.debug("Needs control")
            too_cold = self._is_too_cold()
            too_hot = self._is_too_hot()

            await self._async_cooler_turn_off()

            if self._is_heater_active or self._is_secondary_heater_active:
                await self._async_control_heater_when_on(too_hot, time)
            else:
                await self._async_control_heater_when_off(too_cold, time)

    async def _async_control_heater_when_on(self, too_hot: bool, time=None):
        """Check if we need to turn heating on or off when theheater is on."""
        _LOGGER.debug(
            "_async_control_heater_when_on, floor cold: %s", self._is_floor_cold
        )

        if (
            (too_hot or self._is_floor_hot) or self.opening_manager.any_opening_open
        ) and not self._is_floor_cold:
            _LOGGER.info("Turning off heater %s", self.heater_entity_id)
            await self._async_heater_turn_off()
            await self._async_secondary_heater_turn_off()

        elif (
            self._is_secondary_heating_configured()
            and self._first_stage_heating_timed_out
            and not self._is_secondary_heater_active
        ):
            _LOGGER.info(
                "Turning on secondary heater %s", self.secondary_heater_entity_id
            )
            await self._async_heater_turn_off()
            await self._async_secondary_heater_turn_on()
        elif (
            time is not None
            and not self.opening_manager.any_opening_open
            and not self._is_floor_hot
        ):
            # The time argument is passed only in keep-alive case
            _LOGGER.info(
                "Keep-alive - Turning on heater (from active) %s",
                self.heater_entity_id,
            )
            await self._async_heater_turn_on()

    async def _async_control_heater_when_off(self, too_cold: bool, time=None):
        """Check if we need to turn heating on or off when the heater is off."""
        _LOGGER.debug("_async_control_heater_when_off")
        if (
            too_cold
            and not self.opening_manager.any_opening_open
            and not self._is_floor_hot
        ) or self._is_floor_cold:
            _LOGGER.info("Turning on heater (from inactive) %s", self.heater_entity_id)
            # here is where we need to handle secondary heating
            # need to check if last time secondary heater used was today
            # if it was used today, turn on secondary heating
            # else turn on primary heating
            # and start a timer that will potentially turn secondary heating on
            if (
                self._is_secondary_heating_configured()
                and self._has_secondary_heating_ran_today()
            ):
                await self._async_secondary_heater_turn_on()
            else:
                await self._async_heater_turn_on()
                if self._is_secondary_heating_configured():
                    self.async_on_remove(
                        async_call_later(
                            self.hass,
                            self.secondary_heater_timeout,
                            self._async_control_heating_forced,
                        )
                    )

        elif (
            time is not None
            or self.opening_manager.any_opening_open
            or self._is_floor_hot
        ):
            # The time argument is passed only in keep-alive case
            _LOGGER.info("Keep-alive - Turning off heater %s", self.heater_entity_id)
            await self._async_heater_turn_off()

    async def _async_control_cooling(self, time=None, force=False):
        """Check if we need to turn heating on or off."""
        async with self._temp_lock:
            _LOGGER.debug("_async_control_cooling time: %s. force: %s", time, force)
            self.set_self_active()

            if not self._needs_control(time, force, cool=True):
                return

            too_cold = self._is_too_cold()
            too_hot = self._is_too_hot()

            cooler_set = self.cooler_entity_id is not None
            control_entity = (
                self.cooler_entity_id if cooler_set else self.heater_entity_id
            )
            is_device_active = (
                self._is_cooler_active if cooler_set else self._is_heater_active
            )
            control_off = (
                self._async_cooler_turn_off
                if cooler_set
                else self._async_heater_turn_off
            )
            control_on = (
                self._async_cooler_turn_on if cooler_set else self._async_heater_turn_on
            )

            _LOGGER.info(
                "is device active: %s, is opening open: %s",
                is_device_active,
                self.opening_manager.any_opening_open,
            )

            any_opening_open = self.opening_manager.any_opening_open

            if is_device_active:
                if too_cold or any_opening_open:
                    _LOGGER.info("Turning off cooler %s", control_entity)
                    await control_off()
                elif time is not None and not any_opening_open:
                    # The time argument is passed only in keep-alive case
                    _LOGGER.info(
                        "Keep-alive - Turning on cooler (from active) %s",
                        control_entity,
                    )
                    await control_on()
            else:
                if too_hot and not any_opening_open:
                    _LOGGER.info("Turning on cooler (from inactive) %s", control_entity)
                    await control_on()
                elif time is not None or any_opening_open:
                    # The time argument is passed only in keep-alive case
                    _LOGGER.info("Keep-alive - Turning off cooler %s", control_entity)
                    await control_off()

    async def _async_control_heat_cool(self, time=None, force=False):
        """Check if we need to turn heating on or off."""
        async with self._temp_lock:
            _LOGGER.debug("_async_control_heat_cool")
            if (
                not self._active
                and self._is_configured_for_heat_cool()
                and self._cur_temp is not None
            ):
                self._active = True
            if not self._needs_control(time, force, dual=True):
                return

            too_cold, too_hot, tolerance_device = self._is_cold_or_hot()

            if self.opening_manager.any_opening_open:
                await self._async_heater_turn_off()
                await self._async_cooler_turn_off()
            elif self._is_floor_hot:
                await self._async_heater_turn_off()
            elif self._is_floor_cold:
                await self._async_heater_turn_on()
            else:
                await self.async_heater_cooler_toggle(
                    tolerance_device, too_cold, too_hot
                )

            if time is not None:
                # The time argument is passed only in keep-alive case
                _LOGGER.info(
                    "Keep-alive - Toggling on heater cooler %s, %s",
                    self.heater_entity_id,
                    self.cooler_entity_id,
                )
                await self.async_heater_cooler_toggle(
                    tolerance_device, too_cold, too_hot
                )

    async def async_heater_cooler_toggle(self, tolerance_device, too_cold, too_hot):
        """Toggle heater cooler based on temp and tolarance."""
        match tolerance_device:
            case ToleranceDevice.HEATER:
                await self._async_heater_toggle(too_cold, too_hot)
            case ToleranceDevice.COOLER:
                await self._async_cooler_toggle(too_cold, too_hot)
            case _:
                await self._async_auto_toggle(too_cold, too_hot)

    async def _async_heater_toggle(self, too_cold, too_hot):
        """Toggle heater based on temp."""
        if too_cold:
            await self._async_heater_turn_on()
        elif too_hot:
            await self._async_heater_turn_off()

    async def _async_cooler_toggle(self, too_cold, too_hot):
        """Toggle cooler based on temp."""
        if too_cold:
            await self._async_cooler_turn_off()
        elif too_hot:
            await self._async_cooler_turn_on()

    async def _async_auto_toggle(self, too_cold, too_hot):
        if too_cold:
            if not self.opening_manager.any_opening_open:
                await self._async_heater_turn_on()
            await self._async_cooler_turn_off()
        elif too_hot:
            if not self.opening_manager.any_opening_open:
                await self._async_cooler_turn_on()
            await self._async_heater_turn_off()
        else:
            await self._async_heater_turn_off()
            await self._async_cooler_turn_off()

    @property
    def _is_floor_hot(self):
        """If the floor temp is above limit."""
        if (
            (self.sensor_floor_entity_id is not None)
            and (self._max_floor_temp is not None)
            and (self._cur_floor_temp is not None)
        ):
            if self._cur_floor_temp >= self._max_floor_temp:
                return True
        return False

    @property
    def _is_floor_cold(self):
        """If the floor temp is below limit."""
        if (
            (self.sensor_floor_entity_id is not None)
            and (self._min_floor_temp is not None)
            and (self._cur_floor_temp is not None)
        ):
            _LOGGER.debug("floor temp: %s", self._cur_floor_temp)
            _LOGGER.debug("min floor temp: %s", self._min_floor_temp)
            if self._cur_floor_temp <= self._min_floor_temp:
                return True
        return False

    @property
    def _is_heater_active(self):
        """If the toggleable device is currently active."""
        return self.hass.states.is_state(self.heater_entity_id, STATE_ON)

    @property
    def _is_secondary_heater_active(self):
        """If the toggleable device is currently active."""
        return self.secondary_heater_entity_id and self.hass.states.is_state(
            self.secondary_heater_entity_id, STATE_ON
        )

    @property
    def _is_cooler_active(self):
        """If the toggleable cooler device is currently active."""
        if self.cooler_entity_id is not None and self.hass.states.is_state(
            self.cooler_entity_id, STATE_ON
        ):
            return True
        return False

    @property
    def _is_device_active(self):
        """If the toggleable device is currently active."""
        return (
            self._is_heater_active
            or self._is_secondary_heater_active
            or self._is_cooler_active
        )

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    async def _async_heater_turn_on(self):
        """Turn heater toggleable device on."""
        if self.heater_entity_id is not None and not self._is_heater_active:
            await self._async_switch_turn_on(self.heater_entity_id)

    async def _async_heater_turn_off(self):
        """Turn heater toggleable device off."""
        if self.heater_entity_id is not None and self._is_heater_active:
            await self._async_switch_turn_off(self.heater_entity_id)

    async def _async_secondary_heater_turn_on(self):
        """Turn secondary heater toggleable device on."""
        if (
            self.secondary_heater_entity_id is not None
            and not self._is_secondary_heater_active
        ):
            await self._async_switch_turn_on(self.secondary_heater_entity_id)
            self._secondary_heater_last_run = datetime.datetime.now()

    async def _async_secondary_heater_turn_off(self):
        """Turn secondary heater toggleable device off."""
        if (
            self.secondary_heater_entity_id is not None
            and self._is_secondary_heater_active
        ):
            await self._async_switch_turn_off(self.secondary_heater_entity_id)

    async def _async_cooler_turn_on(self):
        """Turn cooler toggleable device on."""
        if self.cooler_entity_id is not None and not self._is_cooler_active:
            await self._async_switch_turn_on(self.cooler_entity_id)

    async def _async_cooler_turn_off(self):
        """Turn cooler toggleable device off."""
        if self.cooler_entity_id is not None and self._is_cooler_active:
            await self._async_switch_turn_off(self.cooler_entity_id)

    async def _async_switch_turn_off(self, entity_id):
        """Turn toggleable device off."""
        data = {ATTR_ENTITY_ID: entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_OFF, data, context=self._context
        )

    async def _async_switch_turn_on(self, entity_id):
        """Turn toggleable device off."""
        data = {ATTR_ENTITY_ID: entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_ON, data, context=self._context
        )

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        if preset_mode not in (self.preset_modes or []):
            raise ValueError(
                f"Got unsupported preset_mode {preset_mode}. Must be one of {self.preset_modes}"
            )
        if preset_mode == self._preset_mode:
            # I don't think we need to call async_write_ha_state if we didn't change the state
            return
        if preset_mode == PRESET_NONE:
            self._set_presets_when_no_preset_mode()
        else:
            self._set_presets_when_have_preset_mode(preset_mode)

        await self._async_control_climate(force=True)
        self.async_write_ha_state()

    def _set_presets_when_no_preset_mode(self):
        self._preset_mode = PRESET_NONE
        if self._is_range_mode():
            self._target_temp_low = self._saved_target_temp_low
            self._target_temp_high = self._saved_target_temp_high
        else:
            self._target_temp = self._saved_target_temp

    def _set_presets_when_have_preset_mode(self, preset_mode: str):
        if self._is_range_mode():
            if self._preset_mode == PRESET_NONE:
                self._saved_target_temp_low = self._target_temp_low
                self._saved_target_temp_high = self._target_temp_high
            self._target_temp_low = self._presets_range[preset_mode][0]
            self._target_temp_high = self._presets_range[preset_mode][1]
        else:
            if self._preset_mode == PRESET_NONE:
                self._saved_target_temp = self._target_temp
            self._target_temp = self._presets[preset_mode]
        self._preset_mode = preset_mode

    def set_self_active(self):
        """checks if active state needs to be set true"""
        if (
            not self._active
            and None not in (self._cur_temp, self._target_temp)
            and self._hvac_mode != HVACMode.OFF
        ):
            self._active = True
            _LOGGER.info(
                "Obtained current and target temperature. "
                "Dual smart thermostat active. %s, %s",
                self._cur_temp,
                self._target_temp,
            )

    def _needs_control(self, time=None, force=False, *, dual=False, cool=False):
        """checks if the controller needs to continue"""
        if not self._active or self._hvac_mode == HVACMode.OFF:
            return False

        if not force and time is None:
            # If the `force` argument is True, we
            # ignore `min_cycle_duration`.
            # If the `time` argument is not none, we were invoked for
            # keep-alive purposes, and `min_cycle_duration` is irrelevant.
            if self.min_cycle_duration:
                return self._needs_cycle(dual, cool)
        return True

    def _needs_cycle(self, dual=False, cool=False):
        long_enough = self._ran_long_enough(cool)
        if not dual or cool or self.cooler_entity_id is None:
            return long_enough

        long_enough_cooler = self._ran_long_enough(True)
        return long_enough and long_enough_cooler

    def _is_too_cold(self, target_attr="_target_temp") -> bool:
        """checks if the current temperature is below target"""
        target_temp = getattr(self, target_attr)
        return target_temp >= self._cur_temp + self._cold_tolerance

    def _is_too_hot(self, target_attr="_target_temp") -> bool:
        """checks if the current temperature is above target"""
        target_temp = getattr(self, target_attr)
        return self._cur_temp >= target_temp + self._hot_tolerance

    def _is_cold_or_hot(self):
        if self._is_heater_active:
            too_cold = self._is_too_cold("_target_temp_low")
            too_hot = self._is_too_hot("_target_temp_low")
            tolerance_device = ToleranceDevice.HEATER
        elif self._is_cooler_active:
            too_cold = self._is_too_cold("_target_temp_high")
            too_hot = self._is_too_hot("_target_temp_high")
            tolerance_device = ToleranceDevice.COOLER
        else:
            too_cold = self._is_too_cold("_target_temp_low")
            too_hot = self._is_too_hot("_target_temp_high")
            tolerance_device = ToleranceDevice.AUTO
        return too_cold, too_hot, tolerance_device

    def _is_configured_for_heat_cool(self) -> bool:
        """checks if the configuration is complete for heat/cool mode"""
        return self._heat_cool_mode or (
            self._target_temp_high is not None and self._target_temp_low is not None
        )

    def _is_target_mode(self):
        """Check if current support flag is for target temp mode."""
        return self._support_flags & SUPPORT_TARGET_TEMPERATURE

    def _is_range_mode(self):
        """Check if current support flag is for range temp mode."""
        return self._support_flags & SUPPORT_TARGET_TEMPERATURE_RANGE

    def _set_default_target_temps(self):
        """Set default values for target temperatures."""
        if self._is_target_mode():
            self._set_defaukt_temps_target_mode()

        elif self._is_range_mode():
            self._set_default_temps_range_mode()

    def _set_defaukt_temps_target_mode(self):
        if self._target_temp is not None:
            return

        if self.ac_mode or self._hvac_mode == HVACMode.COOL:
            if self._target_temp_high is None:
                self._target_temp = self.max_temp
                _LOGGER.warning(
                    "Undefined target temperature, falling back to %s",
                    self._target_temp,
                )
            else:
                self._target_temp = self._target_temp_high
            return

        if self._target_temp_low is None:
            self._target_temp = self.min_temp
            _LOGGER.warning(
                "Undefined target temperature, falling back to %s",
                self._target_temp,
            )
        else:
            self._target_temp = self._target_temp_low

    def _set_default_temps_range_mode(self):
        if self._target_temp_low is not None and self._target_temp_high is not None:
            return

        if self._target_temp is None:
            self._target_temp_low = self.min_temp
            self._target_temp_high = self.max_temp
            _LOGGER.warning(
                "Undefined target temperature range, falling back to %s-%s",
                self._target_temp_low,
                self._target_temp_high,
            )
            return

        self._target_temp_low = self._target_temp
        self._target_temp_high = self._target_temp
        if self._target_temp + PRECISION_WHOLE >= self.max_temp:
            self._target_temp_low -= PRECISION_WHOLE
        else:
            self._target_temp_high += PRECISION_WHOLE

    def _set_support_flags(self) -> None:
        """set the correct support flags based on configuration"""
        if self._hvac_mode == HVACMode.OFF:
            return

        if not self._is_configured_for_heat_cool() or self._hvac_mode in (
            HVACMode.COOL,
            HVACMode.HEAT,
        ):
            if self._is_range_mode() and self._preset_mode != PRESET_NONE:
                self._preset_mode = PRESET_NONE
                self._target_temp_low = self._saved_target_temp_low
                self._target_temp_high = self._saved_target_temp_high
            self._support_flags = SUPPORT_TARGET_TEMPERATURE
            if len(self._presets):
                self._support_flags |= SUPPORT_PRESET_MODE
        else:
            if self._is_target_mode() and self._preset_mode != PRESET_NONE:
                self._preset_mode = PRESET_NONE
                self._target_temp = self._saved_target_temp
            self._support_flags = SUPPORT_TARGET_TEMPERATURE_RANGE
            if len(self._presets_range):
                self._support_flags |= SUPPORT_PRESET_MODE
        self._set_default_target_temps()

    def _ran_long_enough(self, cooler_entity=False):
        """determines if a switch with the passed property name has run long enough"""
        if cooler_entity and self.cooler_entity_id is not None:
            switch_entity_id = self.cooler_entity_id
            is_active = self._is_cooler_active
        else:
            switch_entity_id = self.heater_entity_id
            is_active = self._is_heater_active

        if is_active:
            current_state = STATE_ON
        else:
            current_state = HVACMode.OFF

        long_enough = condition.state(
            self.hass,
            switch_entity_id,
            current_state,
            self.min_cycle_duration,
        )

        return long_enough

    def _first_stage_heating_timed_out(self, timeout=None):
        """determines if the heater switch has been on for the timeout period"""
        if timeout is None:
            timeout = self.secondary_heater_timeout

        timed_out = condition.state(
            self.hass,
            self.heater_entity_id,
            STATE_ON,
            timeout,
        )

        return timed_out

    def _has_secondary_heating_ran_today(self):
        """determines if the secondary heater has been used today"""
        if not self._is_secondary_heating_configured():
            return False

        if self._secondary_heater_last_run is None:
            return False

        if self._secondary_heater_last_run.date() == datetime.datetime.now().date():
            return True

        return False

    def _is_secondary_heating_configured(self):
        """determines if the secondary heater is configured"""
        if self.secondary_heater_entity_id is None:
            return False

        if self.secondary_heater_timeout is None:
            return False

        return True
