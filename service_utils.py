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


def toWh(watt_seconds):
    """Convert watt-seconds to watt-hours"""
    return watt_seconds / 3600


VOLTAGE_TEXT = lambda path,value: "{:.2f}V".format(value)
CURRENT_TEXT = lambda path,value: "{:.3f}A".format(value)
POWER_TEXT = lambda path,value: "{:.2f}W".format(value)
ENERGY_TEXT = lambda path,value: "{:.6f}kWh".format(value)
ENERGY_WH_TEXT = lambda path,value: "{:.1f}Wh".format(value)


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
        self.productId = kwargs.pop('productId', PRODUCT_ID)  # Allow productId from config, default to 0
        self.logger.info(f"Using product ID: {self.productId:#06x} ({self.productId})")
        self.service = VeDbusService(getServiceName(serviceType, i2cBus, i2cAddr), conn, register=False)
        self.add_settable_path("/CustomName", "", 0, 0)
        self._configure_service(**kwargs)
        self._init_settings(conn)
        di = self.register_device_instance(serviceType, getDeviceAddress(i2cBus, i2cAddr), getDeviceInstance(i2cBus, i2cAddr))
        self.service.add_mandatory_paths(__file__, VERSION, 'I2C',
                di, self.productId, deviceName, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
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
        self.add_settable_path("/History/MinimumTemperature", 1000, -100, 1000, silent=True)  # High initial value for min tracking
        self.add_settable_path("/History/MaximumTemperature", -1000, -100, 1000, silent=True)  # Low initial value for max tracking

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

    def _configure_service(self, **kwargs):
        self.service.add_path("/Dc/0/Voltage", None, gettextcallback=VOLTAGE_TEXT)
        self.service.add_path("/Dc/0/Current", None, gettextcallback=CURRENT_TEXT)
        self.service.add_path("/Alarms/LowVoltage", 0)
        self.service.add_path("/Alarms/HighVoltage", 0)
        self.service.add_path("/Alarms/LowTemperature", 0)
        self.service.add_path("/Alarms/HighTemperature", 0)
        self.service.add_path("/Dc/0/Power", None, gettextcallback=POWER_TEXT)
        self.add_settable_path("/History/MaximumVoltage", 0, 0, 1000, silent=True, gettextcallback=VOLTAGE_TEXT)
        self.add_settable_path("/History/MaximumCurrent", 0, 0, 10000, silent=True, gettextcallback=CURRENT_TEXT)
        self.add_settable_path("/History/MaximumPower", 0, 0, 100000, silent=True, gettextcallback=POWER_TEXT)
        self._local_values = {}
        for path, dbusobj in self.service._dbusobjects.items():
            if not dbusobj._writeable:
                self._local_values[path] = self.service[path]
        # Load history settable paths into local values for batch updates
        self._local_values['/History/MaximumVoltage'] = self.service['/History/MaximumVoltage']
        self._local_values['/History/MaximumCurrent'] = self.service['/History/MaximumCurrent']
        self._local_values['/History/MaximumPower'] = self.service['/History/MaximumPower']
        self._configure_energy_history(**kwargs)
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

    def _configure_energy_history(self, **kwargs):
        # Total energy consumed - persists across restarts
        self.add_settable_path('/History/EnergyIn', 0, 0, 1000000, gettextcallback=ENERGY_TEXT)
        # Load settable energy path into local values for batch updates
        self._local_values['/History/EnergyIn'] = self.service['/History/EnergyIn']
        # Internal Wh accumulator for precision
        self._energy_in_wh = self._local_values['/History/EnergyIn'] * 1000  # Convert from kWh to Wh

    def _increment_energy_usage(self, change):
        # Accumulate in Wh for precision (change is in kWh from toKWh)
        self._energy_in_wh += change * 1000
        # Update kWh value for publishing
        self._local_values['/History/EnergyIn'] = self._energy_in_wh / 1000


class DCSourceServiceMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _configure_energy_history(self, **kwargs):
        # Total energy generated - persists across restarts
        self.add_settable_path('/History/EnergyOut', 0, 0, 1000000, gettextcallback=ENERGY_TEXT)
        # MonitorMode: -1=Generic, -2=AC Charger, -3=DC/DC Charger, -4=Water Gen, -7=Shaft Gen, -8=Wind Charger
        monitorMode = kwargs.get('monitorMode', -1)  # Default to Generic Source
        self.add_settable_path("/Settings/MonitorMode", monitorMode, -8, -1)
        # Load settable energy path into local values for batch updates
        self._local_values['/History/EnergyOut'] = self.service['/History/EnergyOut']
        # Internal Wh accumulator for precision
        self._energy_out_wh = self._local_values['/History/EnergyOut'] * 1000  # Convert from kWh to Wh

    def _increment_energy_usage(self, change):
        # Accumulate in Wh for precision (change is in kWh from toKWh)
        self._energy_out_wh += change * 1000
        # Update kWh value for publishing
        self._local_values['/History/EnergyOut'] = self._energy_out_wh / 1000


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
        
        # PV side measurements (mirror battery side since we only measure after charge controller)
        self.service.add_path("/Pv/V", None, gettextcallback=VOLTAGE_TEXT)
        
        # Number of trackers (1 for single INA226 monitor)
        self.service.add_path("/NrOfTrackers", 1)
        
        # Power measurements
        self.service.add_path("/Yield/Power", None, gettextcallback=POWER_TEXT)
        
        # Energy yield - stored in kWh as per Victron standard, UI will format as Wh/kWh automatically
        self.add_settable_path('/Yield/User', 0, 0, 1000000)  # Total yield - visible in UI
        self.add_settable_path('/Yield/System', 0, 0, 1000000)  # Total yield - visible in UI
        
        # Charger state and mode
        self.service.add_path("/State", self.STATE_BULK)
        self.service.add_path("/Mode", 1)  # 1 = On, 4 = Off
        self.service.add_path("/ErrorCode", self.ERROR_NONE)
        
        # MPPT operating mode (solarcharger only, not for plain PV inverters)
        self.service.add_path("/MppOperationMode", 2)  # 0 = Off, 1 = Voltage/current limited, 2 = MPPT active
        
        # History - daily values also persist to survive restarts, stored in kWh
        self.add_settable_path("/History/Daily/0/Yield", 0, 0, 1000000)  # Today's yield - visible in UI
        self.add_settable_path("/History/Daily/0/MaxPower", 0, 0, 1000000, gettextcallback=POWER_TEXT)
        self.add_settable_path("/History/Daily/1/Yield", 0, 0, 1000000)  # Yesterday's yield
        self.add_settable_path("/History/Daily/1/MaxPower", 0, 0, 1000000, gettextcallback=POWER_TEXT)
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
        # Internal Wh accumulators for precision (not published directly)
        self._yield_user_wh = self._local_values['/Yield/User'] * 1000  # Convert from kWh to Wh
        self._yield_system_wh = self._local_values['/Yield/System'] * 1000
        self._daily_yield_wh = self._local_values['/History/Daily/0/Yield'] * 1000
        self.lastPower = None
        self._first_update = True  # Skip energy integration on first update after restart

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
            self._daily_yield_wh = 0  # Reset internal Wh accumulator
        
        self._local_values["/Dc/0/Voltage"] = voltage
        self._local_values["/Dc/0/Current"] = current
        self._local_values["/Pv/V"] = voltage  # Mirror battery voltage to PV voltage
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
        
        # Integrate energy (accumulate in Wh for precision)
        if self.lastPower is not None and not self._first_update:
            energy_delta_wh = toWh((self.lastPower.power + power)/2 * (now - self.lastPower.timestamp))
            self._yield_user_wh += energy_delta_wh
            self._yield_system_wh += energy_delta_wh
            self._daily_yield_wh += energy_delta_wh
            # Update kWh values for publishing
            self._local_values['/Yield/User'] = self._yield_user_wh / 1000
            self._local_values['/Yield/System'] = self._yield_system_wh / 1000
            self._local_values['/History/Daily/0/Yield'] = self._daily_yield_wh / 1000
        
        self._first_update = False
        self.lastPower = PowerSample(power, now)

    def publish(self):
        for k,v in self._local_values.items():
            self.service[k] = v

