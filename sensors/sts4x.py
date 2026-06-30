"""STS4x temperature sensor binding."""

import struct
from typing import Any


NAME = "STS4x Temperature"
SERVICE_UUID = "0000fe60-cc7a-482a-984a-7f2ed5b3e58f"
CONFIG_UUID = "0000fe61-8e22-4541-9d4c-21edae82ed19"
DATA_UUID = "0000fe62-8e22-4541-9d4c-21edae82ed19"


PARAMS = {
    "freq": {"type": "int", "min": 1, "max": 10, "unit": "Hz",
             "help": "Sampling frequency"},
}


# ── CSV schema ──
CSV_HEADER = ["t_host_iso", "t_host_ns", "channel", "temperature_c"]


def csv_rows(ch_name, decoded, ts_iso, ts_ns) -> Any:
    """
        One row per notification: (iso, ns, channel, temperature).
    """

    if isinstance(decoded, (int, float)):
        yield (ts_iso, ts_ns, ch_name, float(decoded))


def encode_config(params: dict) -> bytes:
    """Pack uint32 LE sampling frequency [Hz]."""
    return struct.pack("<I", int(params["freq"]))


def decode_config(data: bytes) -> dict:
    """Unpack uint32 LE sampling frequency."""
    if len(data) < 4:
        raise ValueError(f"Expected ≥4 bytes, got {len(data)}")
    return {"freq": struct.unpack("<I", bytes(data[:4]))[0]}


def _decode_temperature(data: bytes) -> Any:
    """Decode float32 LE temperature [°C]."""
    if len(data) < 4:
        raise ValueError(f"Expected ≥4 bytes, got {len(data)}")
    return struct.unpack("<f", bytes(data[:4]))[0]


def format_config_title(cfg: dict) -> str:
    """Compact one-liner of the config to embed in plot titles."""
    return f"{cfg['freq']} Hz"


DATA_CHANNELS = {
    "temperature": {
        "uuid":   DATA_UUID,
        "decode": _decode_temperature,
        "title":  "STS4x Temperature",
        "ylabel": "°C",
    },
}
