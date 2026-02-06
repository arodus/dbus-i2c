import logging
from ina226 import INA226
from service_utils import DCI2CService, DCLoadServiceMixin, DCSourceServiceMixin, PVChargerServiceMixin, SimpleI2CService
import time


class INA226HardwareMixin:
    """Shared INA226 hardware initialization and reading logic"""
    
    def _configure_service(self, maxExpectedCurrent, shuntResistance, **kwargs):
        """Configure hardware then delegate to service-specific path setup"""
        self._configure_device(maxExpectedCurrent, shuntResistance)
        super()._configure_service(**kwargs)
    
    def _configure_device(self, maxExpectedCurrent, shuntResistance):
        """Initialize and configure the INA226 hardware"""
        self.device = INA226(busnum=self.i2cBus, address=self.i2cAddr, max_expected_amps=maxExpectedCurrent, shunt_ohms=shuntResistance, log_level=logging.INFO)
        self.device.configure(avg_mode=INA226.AVG_4BIT, bus_ct=INA226.VCT_2116us_BIT, shunt_ct=INA226.VCT_2116us_BIT)
        self.device.sleep()

    def _voltage(self):
        """All INA226 services use supply voltage (source-side measurement)"""
        return self.device.supply_voltage()

    def _read_sensor(self):
        """Read sensor with proper wake/wait/sleep timing and error handling"""
        try:
            self.device.wake()
            # With the parameters 4 samples and 2.116ms conversion time the conversion needs around 8.5ms per channel
            # As there are two channels (supply voltage and current) the overall time is around 17ms
            # Add timeout to prevent infinite loop on I2C errors
            max_attempts = 100  # ~1 second timeout (100 * 10ms)
            attempts = 0
            while attempts < max_attempts:
                try:
                    if self.device.is_conversion_ready() != 0:
                        break
                except OSError as e:
                    self.logger.debug(f"I2C error checking conversion (attempt {attempts + 1}): {e}")
                time.sleep(0.01)
                attempts += 1
            
            if attempts >= max_attempts:
                self.logger.warning("INA226 conversion timeout - returning zero values")
                return 0.0, 0.0, 0.0, time.perf_counter()
            
            voltage = round(self._voltage(), 3)
            current = round(self.device.current()/1000, 3)
            power = round(self.device.power()/1000, 3)
            now = time.perf_counter()
            return voltage, current, power, now
        except OSError as e:
            self.logger.warning(f"I2C communication error: {e} - returning zero values")
            return 0.0, 0.0, 0.0, time.perf_counter()
        finally:
            # Always try to put device back to sleep, even on error
            try:
                self.device.sleep()
            except OSError:
                pass  # Ignore errors during sleep


class INA226Service(INA226HardwareMixin, DCI2CService):
    """Base class for INA226 DC monitoring services"""
    
    def __init__(self, conn, i2cBus, i2cAddr, serviceType, maxExpectedCurrent, shuntResistance, **kwargs):
        super().__init__(conn, i2cBus, i2cAddr, serviceType, 'INA226', maxExpectedCurrent=maxExpectedCurrent, shuntResistance=shuntResistance, **kwargs)

    def update(self):
        voltage, current, power, now = self._read_sensor()
        super()._update(voltage, current, power, now)


class INA226DCService(INA226Service):
    """INA226 service for DC power monitoring"""
    pass


class INA226DCLoadService(DCLoadServiceMixin, INA226DCService):
    def __init__(self, conn, i2cBus, i2cAddr, **kwargs):
        super().__init__(conn, i2cBus, i2cAddr, 'dcload', **kwargs)


class INA226AlternatorService(DCSourceServiceMixin, INA226DCService):
    def __init__(self, conn, i2cBus, i2cAddr, **kwargs):
        super().__init__(conn, i2cBus, i2cAddr, 'alternator', **kwargs)


class INA226DCSourceService(DCSourceServiceMixin, INA226DCService):
    def __init__(self, conn, i2cBus, i2cAddr, **kwargs):
        super().__init__(conn, i2cBus, i2cAddr, 'dcsource', **kwargs)


class INA226PVChargerService(INA226HardwareMixin, PVChargerServiceMixin, SimpleI2CService):
    """
    INA226 configured as a PV/wind charger monitor.
    Shunt connected between charge controller output and battery (battery side only).
    - supply_voltage() reads battery voltage
    - current() reads charging current (positive = charging)
    """
    def __init__(self, conn, i2cBus, i2cAddr, maxExpectedCurrent, shuntResistance, **kwargs):
        # Call SimpleI2CService.__init__ directly to avoid DCI2CService paths
        SimpleI2CService.__init__(self, conn, i2cBus, i2cAddr, 'solarcharger', 'INA226-PVCharger',
                                  maxExpectedCurrent=maxExpectedCurrent, shuntResistance=shuntResistance, **kwargs)

    def update(self):
        voltage, current, power, now = self._read_sensor()
        self._update_pv(voltage, current, power, now)

    def publish(self):
        PVChargerServiceMixin.publish(self)
