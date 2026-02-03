# dbus-i2c Copilot Instructions

## Project Overview
Python-based DBus services for I2C sensor devices on Victron Energy GX systems (Venus OS). Exposes I2C sensors (temperature, DC power) as DBus services for integration with Victron's monitoring ecosystem.

## Architecture

### Core Components
- **`dbus-i2c.py`**: Main entry point. Reads device configs from `/data/setupOptions/dbus-i2c/device-*` JSON files, creates service instances via `device_utils.createDevice()`, and schedules periodic updates using GLib timers
- **`device_utils.py`**: Dynamic device instantiation - uses `importlib.import_module()` to load service modules and `inspect.signature()` to introspect constructors, passing extra kwargs only if constructor accepts them (>3 parameters)
- **`service_utils.py`**: Service base classes and mixins defining the service hierarchy:
  - `SimpleI2CService` → base for all I2C devices, extends `SettableService` for settings persistence
  - `TemperatureService` → extends SimpleI2CService for temp/humidity/pressure sensors
  - `DCI2CService` → extends SimpleI2CService for DC power monitoring
  - `DCLoadServiceMixin` / `DCSourceServiceMixin` → add load vs source-specific paths
  - `PVChargerServiceMixin` → adds solarcharger paths for PV/wind integration (yield, PV voltage, state)
- **`*_service.py`**: Individual sensor implementations (BME280, INA219, INA226, SHT3x, DPS310, Lynx distributors)
- **`script_utils.py`**: Defines `SCRIPT_HOME` (/data/dbus-i2c) and reads VERSION from version file

### DBus Integration Pattern
Services use `vedbus.VeDbusService` from the external `velib_python` library. Each service:
1. Registers with a unique name: `com.victronenergy.{serviceType}.i2c_bus{N}_addr{0xXX}`
2. Publishes standard Victron paths like `/Dc/0/Voltage`, `/Temperature`, `/History/*`
3. Uses device instance IDs calculated as `1024 + bus*128 + addr` to avoid conflicts

### Update vs Publish Pattern (Dual-Timer Architecture)
- **`update()`**: Reads sensor data, stores in `self._local_values` dict (for DC/PV services) or directly publishes (for temperature services)
- **`publish()`**: (DC/PV services only) Batch-writes `_local_values` to DBus at lower frequency to reduce DBus traffic
- **Timing**: `dbus-i2c.py` creates separate GLib timers for each - `updateInterval` (sensor polling) and `publishInterval` (DBus updates)
- **Exception handling**: Each timer gets wrapped in try/except that sets `/Connected` = 0 on failure
- **Timer setup**: Uses `GLib.timeout_add()` for intervals ≤1000ms or `GLib.timeout_add_seconds()` for longer intervals

## Device Configuration
JSON files in `/data/setupOptions/dbus-i2c/device-*` format:
```json
{
  "module": "ina219_service",
  "class": "INA219DCLoadService", 
  "bus": 1,
  "address": 64,
  "updateInterval": 1000,
  "publishInterval": 5000,
  "shuntResistance": 0.1,
  "maxExpectedCurrent": 2
}
```
- Required: `module`, `class`, `bus`, `address`, `updateInterval`
- Extra kwargs passed directly to constructor if signature accepts them (see `device_utils.py`)

## Installation & Deployment
- **Target platform**: Victron Venus OS (Raspberry Pi)
- **Installer**: `setup` bash script uses SetupHelper framework (requires `/data/SetupHelper/HelperResources/IncludeHelpers`)
- **Service management**: daemontools via `/service/dbus-i2c` symlink → `service/run` script executes `dbus-i2c.py`
- **Dependencies**: Installed from GitHub into `$scriptDir/ext` by `setup` script's `extInstall()` function
- **External deps**: `velib_python`, sensor-specific libraries (smbus2, pi_ina219, Sensirion drivers, ina226)
- **I2C kernel setup**: `setup` script modifies `/u-boot/config.txt` (adds `dtparam=i2c_arm=on`) and `/etc/modules` (adds `i2c-dev`)

## Development Workflow
- **Testing locally**: Not designed for local dev - requires Venus OS DBus and I2C hardware
- **Unit tests**: Run `pytest tests/` - currently only validates `device_utils.createDevice()` logic, no hardware mocking
- **Deployment**: Copy to `/data/dbus-i2c` on Venus OS, run `./setup` to install dependencies and configure
- **Service control**: `svc -d /service/dbus-i2c` (stop), `svc -u /service/dbus-i2c` (start)
- **Logs**: `tail -f /var/log/dbus-i2c/current` (via daemontools multilog in `service/log/run`)
- **DBus inspection**: Use `dbus -y` on Venus OS to see registered services

## Adding New Sensors
1. Create `newsensor_service.py` with class extending `TemperatureService` or `DCI2CService`
2. Implement `_configure_service(**kwargs)` to initialize hardware and add DBus paths
3. Implement `update()` to read sensor and call `self._update(...)`
4. Add device configuration prompts to `setup` script (see `createDeviceFileINA226()` for param examples)
5. Add library installation in `setup` script's install section using `extInstall(repo_url, project_name, branch, repo_path)`
6. For DC services, ensure `update()` passes timestamp via `time.perf_counter()` for energy integration

## Testing
- Single test file: `tests/test_device.py` validates `device_utils.createDevice()` dynamic instantiation
- Run with pytest: `pytest tests/` (tests validate JSON config parsing and constructor signature introspection)
- No hardware mocking - tests focus on configuration parsing logic
- Test pattern: Define mock device classes inline with 3-param vs 4+-param constructors to verify kwargs handling

## Key Conventions
- **I2C bus access**: Use `with SMBus(self.i2cBus) as bus:` pattern - open/close for each read to avoid holding bus
- **Device identification**: Combine `i2cBus` + `i2cAddr` for unique service names and device instances
- **Logging**: Each service gets logger named `dbus-i2c.{bus}.{addr:#04x}.{deviceName}`
- **Error handling**: Services catch exceptions in update/publish wrappers and set `/Connected` = 0 on failure
- **Energy tracking**: DC services integrate power over time using trapezoid rule (`PowerSample` namedtuple stores previous measurement)

## Critical Paths
- `/data/setupOptions/dbus-i2c/` - device configs read at startup
- `/data/dbus-i2c/ext/` - external Python libraries
- `/data/dbus-i2c/version` - read by `script_utils.py` for VERSION constant
- `/u-boot/config.txt` and `/etc/modules` - modified by setup script to enable I2C kernel support

## Common Patterns
- **Mixin composition**: `class INA219DCLoadService(DCLoadServiceMixin, INA219Service)` - mixins must come first
- **Settable paths**: Use `self.add_settable_path()` for user-configurable values (CustomName, TemperatureType)
- **History paths**: Min/Max values tracked using `_safe_min()` / `_safe_max()` helpers
- **Rounding**: Consistently round sensor values (voltage: 3 decimals, temp: 1 decimal, power: 3 decimals)
- **PV charger services**: Inherit from `PVChargerServiceMixin` + `SimpleI2CService` (not `DCI2CService`) and implement `_update_pv()`. Measures only battery side (after charge controller) using INA226's `supply_voltage()` for battery voltage and `current()` for charging current
