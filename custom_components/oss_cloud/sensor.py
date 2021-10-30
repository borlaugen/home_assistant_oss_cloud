"""Support for OSS Cloud."""
import asyncio
import datetime
import json
import logging
import time

import aiohttp
import async_timeout
import voluptuous as vol
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_API_TOKEN
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import Entity
from homeassistant.components.sensor import (
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_ENERGY,
    DEVICE_CLASS_MONETARY,
    DEVICE_CLASS_POWER,
    DEVICE_CLASS_POWER_FACTOR,
    DEVICE_CLASS_SIGNAL_STRENGTH,
    DEVICE_CLASS_VOLTAGE,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import (
    ELECTRIC_CURRENT_AMPERE,
    ELECTRIC_POTENTIAL_VOLT,
    ENERGY_KILO_WATT_HOUR,
    EVENT_HOMEASSISTANT_STOP,
    PERCENTAGE,
    POWER_WATT,
    SIGNAL_STRENGTH_DECIBELS,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "oss_cloud"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_API_TOKEN): cv.string}
)

SENSOR_TYPES = {
    "activepower": [POWER_WATT, DEVICE_CLASS_POWER]
}


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up Airthings."""
    api_token = config[CONF_API_TOKEN]
    oss_data = OSSData(api_token, async_get_clientsession(hass))

    if not await oss_data.update_data():
        _LOGGER.error("Failed to get data from OSS")

    dev = []
    for sensor_id, sensor in oss_data.sensors.items():
        if sensor[1] in SENSOR_TYPES:
            dev.append(OSSEntity(sensor_id, sensor, oss_data))

    async_add_entities(dev)


class OSSEntity(Entity):
    """Representation of a meter sensor."""

    _attr_state_class = STATE_CLASS_MEASUREMENT

    def __init__(self, sensor_id, sensor, oss_data):
        """Initialize the sensor."""
        self._sensor_id = sensor_id
        self._sensor = sensor
        self._oss_data = oss_data
        self._unit_of_measurement = SENSOR_TYPES[sensor[1]][0]
        self._device_class = SENSOR_TYPES[sensor[1]][1]

    @property
    def name(self):
        """Return the name of the sensor."""
        return f'OSS {self._sensor[0].get("meterAddress", {}).get("streetAddress1", "")} {self._sensor[1]}'

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._sensor_id

    @property
    def device_state_attributes(self):
        """Return the state attributes of the sensor."""
        return {
            "timestamp": datetime.datetime.strptime(
                self._sensor[3], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(
                tzinfo=datetime.timezone.utc
            ),
            "max_in": self._sensor[2]["input"]["max"],
            "min_in": self._sensor[2]["input"]["min"],
            "avg_in": self._sensor[2]["input"]["avg"],
            "max_out": self._sensor[2]["output"]["max"],
            "min_out": self._sensor[2]["output"]["min"],
            "avg_out": self._sensor[2]["output"]["avg"]
        }

    @property
    def state(self):
        """Return the state of the device."""
        return self._sensor[2]["input"]["avg"]

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return self._unit_of_measurement

    async def async_update(self):
        """Get the latest data."""
        await self._oss_data.update()
        self._sensor = self._oss_data.sensors.get(self._sensor_id)

    @property
    def device_class(self):
        """Return the device class of this entity, if any."""
        return self._device_class


class OSSData:
    RESOLUTION = {
        "minute": 1,
        "hour": 2,
        "day": 3
    }

    def __init__(self, token, session):
        self._token = token

        self._session = session

        self._poll_period = 30  # seconds
        self._timeout = 10  # minutes
        self._updated_at = datetime.datetime.utcnow()

        self.sensors = {}

    async def update(self, _=None, force_update=False):
        now = datetime.datetime.utcnow()
        elapsed = now - self._updated_at
        if elapsed < datetime.timedelta(seconds=self._poll_period) and not force_update:
            return
        self._updated_at = now
        await self.update_data()

    async def update_data(self):
        now = datetime.datetime.utcnow()
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {self._token}",
        }

        try:
            with async_timeout.timeout(self._timeout):
                resp = await self._session.get(
                    "https://api.services.oss.no/api/Meter",
                    headers=headers,
                )
            if resp.status != 200:
                _LOGGER.error(
                    "Error connecting to OSS, resp code: %s %s",
                    resp.status,
                    resp.reason,
                )
                return False
            result = await resp.json()
        except aiohttp.ClientError as err:
            _LOGGER.error("Error connecting to OSS: %s ", err, exc_info=True)
            raise
        except asyncio.TimeoutError:
            return False

        for meter in result.get("meters", {}):
            params = {
                "meterId": meter["meterNumber"]
            }
            try:
                with async_timeout.timeout(self._timeout):
                    resp = await self._session.get(
                        "https://api.services.oss.no/api/Device/health",
                        headers=headers,
                        params=params,
                    )
                if resp.status != 200:
                    _LOGGER.error(
                        "Error connecting to OSS, resp code: %s %s",
                        resp.status,
                        resp.reason,
                    )
                    return False
                meter_health_result = await resp.json()
            except aiohttp.ClientError as err:
                _LOGGER.error("Error connecting to OSS: %s ", err, exc_info=True)
                raise
            except asyncio.TimeoutError:
                return False
            from_time = (now - datetime.timedelta(minutes=self._poll_period*20)).strftime("%Y-%m-%dT%H:%M:%SZ")
            to_time = (now + datetime.timedelta(minutes=self._poll_period*20)).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                with async_timeout.timeout(self._timeout):
                    resp = await self._session.get(
                        f"https://api.services.oss.no/api/Telemetry/{meter['meterNumber']}/{from_time}/{to_time}/{OSSData.RESOLUTION['minute']}",
                        headers=headers,
                    )
                if resp.status != 200:
                    _LOGGER.error(
                        "Error connecting to OSS, resp code: %s %s",
                        resp.status,
                        resp.reason,
                    )
                    return False
                meter_readings_result= await resp.json()
            except aiohttp.ClientError as err:
                _LOGGER.error("Error connecting to OSS: %s ", err, exc_info=True)
                raise
            except asyncio.TimeoutError:
                return False

            latest_meter_reading = meter_readings_result[-1]
            for sensor in latest_meter_reading:
                sensor_type = sensor.lower()
                if sensor_type in SENSOR_TYPES:
                    self.sensors[f'{meter["meterNumber"]}_{sensor_type}'] = (
                        meter,
                        sensor_type,
                        latest_meter_reading.get(sensor, {}),
                        latest_meter_reading.get("timestamp", "")
                    )
        return True
