"""Registry mapping logical sensor keys to their modules."""

from . import sts4x, imu, fft

SENSOR_REGISTRY = {
    "temp": sts4x,
    "imu":  imu,
    "fft":  fft
}

VALUE_KEYS = list(SENSOR_REGISTRY.keys())
