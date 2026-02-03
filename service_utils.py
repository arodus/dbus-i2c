from script_utils import VERSION
from vedbus import VeDbusService
from settingsdevice import SettingsDevice
from settableservice import SettableService
from collections import namedtuple
import logging
from datetime import datetime

BASE_DEVICE_INSTANCE_ID = 1024
PRODUCT_ID = 0
FIRMWARE_VERSION = 0
HARDWARE_VERSION = 0
CONNECTED = 1


PowerSample = namedtuple('PowerSample', ['power', 'timestamp'])


def _safe_min(newValue, currentValue):
    return min(newValue, currentValue) if currentValue else newValue


def _safe_max(newValue, currentValue):
    return max(newValue, currentValue) if currentValue else newValue


def toKWh(joules):
    return joules/3600/1000


VOLTAGE_TEXT = lambda path,value: "{:.2f}V".format(value)
CURRENT_TEXT = lambda path,value: "{:.3f}A".format(value)
POWER_TEXT = lambda path,value: "{:.2f}W".format(value)
ENERGY_TEXT = lambda path,value: "{:.6f}kWh".format(value)


def getServiceName(serviceType, i2cBusNum, i2cAddr):
    return f"com.victronenergy.{serviceType}.{getDeviceAddress(i2cBusNum, i2cAddr)}"


def getDeviceAddress(i2cBusNum, i2cAddr):
    return f"i2c_bus{i2cBusNum}_addr{i2cAddr}"


def getDeviceInstance(i2cBusNum, i2cAddr):
    return BASE_DEVICE_INSTANCE_ID + i2cBusNum * 128 + i2cAddr


class SimpleI2CService(SettableService):
    def __init__(self, conn, i2cBus, i2cAddr, serviceType, deviceName, **kwargs):
        super().__init__()
        self.logger = logging.getLogger(f"dbus-i2c.{i2cBus}.{i2cAddr:#04x}.{deviceName}")
        self.serviceType = serviceType
        self.i2cBus = i2cBus
        self.i2cAddr = i2cAddr
        self.deviceName = deviceName
        self.service = VeDbusService(getServiceName(serviceType, i2cBus, i2cAddr), conn, register=False)
        self.add_settable_path("/CustomName", "", 0, 0)
        self._configure_service(**kwargs)
        self._init_settings(conn)
        di = self.register_device_instance(serviceType, getDeviceAddress(i2cBus, i2cAddr), getDeviceInstance(i2cBus, i2cAddr))
        self.service.add_mandatory_paths(__file__, VERSION, 'I2C',
                di, PRODUCT_ID, deviceName, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.service.add_path("/I2C/Bus", i2cBus)
        self.service.add_path("/I2C/Address", "{:#04x}".format(i2cAddr))
        self.service.register()

    def add_settable_path(self, subPath, initialValue, minValue=0, maxValue=0, silent=False, **kwargs):
        """Override to handle silent parameter separately from DBus path kwargs"""
        settingName = subPath[1:].lower()
        self.service.add_path(subPath, initialValue, writeable=True, 
                             onchangecallback=lambda path, newValue: self._value_changed(settingName, newValue), **kwargs)
        self.supportedSettings[settingName] = [
            self._get_settings_path(subPath),
            initialValue,
            minValue,
            maxValue,
            silent
        ]
        self.settablePaths[settingName] = subPath

    def error(self, msg):
        self.logger.exception(msg)
        self.service["/Connected"] = 0

    def __str__(self):
        return "{}@{}/{:#04x}".format(self.deviceName, self.i2cBus, self.i2cAddr)


class TemperatureService(SimpleI2CService):
    TYPE_BATTERY = 0
    TYPE_FRIDGE = 1
    TYPE_GENERIC = 2
    TYPE_ROOM = 3
    TYPE_OUTDOOR = 4
    TYPE_WATER_HEATER = 5
    TYPE_FREEZER = 6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _configure_service(self):
        self.service.add_path("/Temperature", None)
        # default type is battery
        self.add_settable_path("/TemperatureType", TemperatureService.TYPE_BATTERY, 0, 6)
        self.service.add_path("/History/MinimumTemperature", None)
        self.service.add_path("/History/MaximumTemperature", None)

    def _update(self, temp, humidity, pressure):
        temp = round(temp, 1)
        self.service["/Temperature"] = temp
        if pressure is not None:
            self.service["/Pressure"] = round(pressure, 1)
        if humidity is not None:
            self.service["/Humidity"] = round(humidity, 1)
        self.service["/History/MinimumTemperature"] = _safe_min(temp, self.service["/History/MinimumTemperature"])
        self.service["/History/MaximumTemperature"] = _safe_max(temp, self.service["/History/MaximumTemperature"])


class DCI2CService(SimpleI2CService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _configure_service(self):
        self.service.add_path("/Dc/0/Voltage", None, gettextcallback=VOLTAGE_TEXT)
        self.service.add_path("/Dc/0/Current", None, gettextcallback=CURRENT_TEXT)
        self._configure_energy_history()
        self.service.add_path("/Alarms/LowVoltage", 0)
        self.service.add_path("/Alarms/HighVoltage", 0)
        self.service.add_path("/Alarms/LowTemperature", 0)
        self.service.add_path("/Alarms/HighTemperature", 0)
        self.service.add_path("/Dc/0/Power", None, gettextcallback=POWER_TEXT)
        self.service.add_path("/History/MaximumVoltage", 0, gettextcallback=VOLTAGE_TEXT)
        self.service.add_path("/History/MaximumCurrent", 0, gettextcallback=CURRENT_TEXT)
        self.service.add_path("/History/MaximumPower", 0, gettextcallback=POWER_TEXT)
        self._local_values = {}
        for path, dbusobj in self.service._dbusobjects.items():
            if not dbusobj._writeable:
                self._local_values[path] = self.service[path]
        self.lastPower = None

    def _update(self, voltage, current, power, now):
        self._local_values["/Dc/0/Voltage"] = voltage
        self._local_values["/Dc/0/Current"] = current
        self._local_values["/Dc/0/Power"] = power
        self._local_values["/History/MaximumVoltage"] = max(voltage, self._local_values["/History/MaximumVoltage"])
        self._local_values["/History/MaximumCurrent"] = max(current, self._local_values["/History/MaximumCurrent"])
        self._local_values["/History/MaximumPower"] = max(power, self._local_values["/History/MaximumPower"])

        if self.lastPower is not None:
            # trapezium integration
            self._increment_energy_usage(toKWh((self.lastPower.power + power)/2 * (now - self.lastPower.timestamp)))
        self.lastPower = PowerSample(power, now)

    def publish(self):
        for k,v in self._local_values.items():
            self.service[k] = v


class DCLoadServiceMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _configure_energy_history(self):
        self.service.add_path('/History/EnergyIn', 0, gettextcallback=ENERGY_TEXT)

    def _increment_energy_usage(self, change):
        self._local_values['/History/EnergyIn'] += change


class DCSourceServiceMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _configure_energy_history(self):
        self.service.add_path('/History/EnergyOut', 0, gettextcallback=ENERGY_TEXT)

    def _increment_energy_usage(self, change):
        self._local_values['/History/EnergyOut'] += change


class PVChargerServiceMixin:
    """
    Mixin for INA226 devices acting as solar/wind charger monitors.
    Measures battery voltage and charging current on the output side of an external charge controller.
    Shunt is placed between charge controller output and battery.
    """
    # Charger states from Victron documentation
    STATE_OFF = 0
    STATE_BULK = 3

    # Error codes
    ERROR_NONE = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _configure_service(self):
        # Battery side measurements
        self.service.add_path("/Dc/0/Voltage", None, gettextcallback=VOLTAGE_TEXT)
        self.service.add_path("/Dc/0/Current", None, gettextcallback=CURRENT_TEXT)
        
        # Power measurements
        self.service.add_path("/Yield/Power", None, gettextcallback=POWER_TEXT)
        
        # Energy yield (kWh) - stored in settings for persistence across restarts
        self.add_settable_path('/Yield/User', 0, 0, 1000000, silent=True, gettextcallback=ENERGY_TEXT)
        self.add_settable_path('/Yield/System', 0, 0, 1000000, silent=True, gettextcallback=ENERGY_TEXT)
        
        # Charger state and mode
        self.service.add_path("/State", self.STATE_BULK)
        self.service.add_path("/Mode", 1)  # 1 = On, 4 = Off
        self.service.add_path("/ErrorCode", self.ERROR_NONE)
        
        # MPPT operating mode (solarcharger only, not for plain PV inverters)
        self.service.add_path("/MppOperationMode", 2)  # 0 = Off, 1 = Voltage/current limited, 2 = MPPT active
        
        # History - daily values also persist to survive restarts
        self.add_settable_path("/History/Daily/0/Yield", 0, 0, 1000000, silent=True, gettextcallback=ENERGY_TEXT)
        self.add_settable_path("/History/Daily/0/MaxPower", 0, 0, 1000000, silent=True, gettextcallback=POWER_TEXT)
        self.add_settable_path("/History/Daily/1/Yield", 0, 0, 1000000, silent=True, gettextcallback=ENERGY_TEXT)
        self.add_settable_path("/History/Daily/1/MaxPower", 0, 0, 1000000, silent=True, gettextcallback=POWER_TEXT)
        self.add_settable_path("/History/LastDay", datetime.now().day, 1, 31, silent=True)  # Track day for rollover detection
        
        # Initialize local values for batch updates
        self._local_values = {}
        for path, dbusobj in self.service._dbusobjects.items():
            if not dbusobj._writeable:
                self._local_values[path] = self.service[path]
        # Manually add ALL settable paths to local values for batch updates and persistence
        self._local_values['/Yield/User'] = self.service['/Yield/User']
        self._local_values['/Yield/System'] = self.service['/Yield/System']
        self._local_values['/History/Daily/0/Yield'] = self.service['/History/Daily/0/Yield']
        self._local_values['/History/Daily/0/MaxPower'] = self.service['/History/Daily/0/MaxPower']
        self._local_values['/History/Daily/1/Yield'] = self.service['/History/Daily/1/Yield']
        self._local_values['/History/Daily/1/MaxPower'] = self.service['/History/Daily/1/MaxPower']
        self._local_values['/History/LastDay'] = self.service['/History/LastDay']
        self.lastPower = None

    def _update_pv(self, voltage, current, power, now):
        """
        Update PV charger values.
        
        Args:
            voltage: Battery voltage (V)
            current: Charging current (A)
            power: Charging power (W)
            now: Timestamp
        """
        # Check if day has changed and reset daily statistics
        current_day = datetime.now().day
        if current_day != self._local_values['/History/LastDay']:
            # Roll over to yesterday's stats
            self._local_values['/History/Daily/1/Yield'] = self._local_values['/History/Daily/0/Yield']
            self._local_values['/History/Daily/1/MaxPower'] = self._local_values['/History/Daily/0/MaxPower']
            # Reset today's stats
            self._local_values['/History/Daily/0/Yield'] = 0
            self._local_values['/History/Daily/0/MaxPower'] = 0
            self._local_values['/History/LastDay'] = current_day
        
        self._local_values["/Dc/0/Voltage"] = voltage
        self._local_values["/Dc/0/Current"] = current
        self._local_values["/Yield/Power"] = power
        
        # Update daily max power
        self._local_values["/History/Daily/0/MaxPower"] = max(power, self._local_values["/History/Daily/0/MaxPower"])
        
        # Determine charger state based on power
        if power < 1:
            self._local_values["/State"] = self.STATE_OFF
            self._local_values["/MppOperationMode"] = 0
        else:
            self._local_values["/State"] = self.STATE_BULK
            self._local_values["/MppOperationMode"] = 2
        
        # Integrate energy (kWh)
        if self.lastPower is not None:
            energy_delta = toKWh((self.lastPower.power + power)/2 * (now - self.lastPower.timestamp))
            self._local_values['/Yield/User'] += energy_delta
            self._local_values['/Yield/System'] += energy_delta
            self._local_values['/History/Daily/0/Yield'] += energy_delta
        
        self.lastPower = PowerSample(power, now)

    def publish(self):
        for k,v in self._local_values.items():
            self.service[k] = v

