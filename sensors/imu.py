"""LSM6DSV IMU + FFT sensor binding."""

import struct
from typing import Any


NAME = "LSM6DSV IMU/FFT"
SERVICE_UUID = "0000fe50-cc7a-482a-984a-7f2ed5b3e58f"
CONFIG_UUID = "0000fe51-8e22-4541-9d4c-21edae82ed19"

# Notification characteristics
DATA0_UUID = "0000fe52-8e22-4541-9d4c-21edae82ed19"   # low-g accel  (12B = 3 float32 LE) # noqa
DATA1_UUID = "0000fe53-8e22-4541-9d4c-21edae82ed19"   # high-g accel (12B = 3 float32 LE) # noqa
DATA2_UUID = "0000fe54-8e22-4541-9d4c-21edae82ed19"   # FFT chunk  (TODO)
DATA3_UUID = "0000fe55-8e22-4541-9d4c-21edae82ed19"   # FFT chunk  (TODO)


# ── Enum tables (firmware <-> human) ─────────
AXIS = {"x": 1, "y": 2, "z": 3}
LOW_SCALE = {"2g": 0, "4g": 1, "8g": 2, "16g": 3}
HIGH_SCALE = {"32g": 0, "64g": 1, "128g": 2, "256g": 3}
MODE = {"low": 0, "high": 1}

AXIS_INV = {v: k for k, v in AXIS.items()}
LOW_SCALE_INV = {v: k for k, v in LOW_SCALE.items()}
HIGH_SCALE_INV = {v: k for k, v in HIGH_SCALE.items()}
MODE_INV = {v: k for k, v in MODE.items()}


# ── Configure schema ─────────────────────────
PARAMS = {
    "low_scale":  {"type": "choice", "choices": list(LOW_SCALE.keys()),
                   "help": "Low-g accel full scale"},
    "high_scale": {"type": "choice", "choices": list(HIGH_SCALE.keys()),
                   "help": "High-g accel full scale"},
    "mode": {"type": "choice", "choices": list(MODE.keys()),
             "help": "Which accel feeds the FFT (low/high)"}
}

# ── Sample rates per channel × mode ──────────
# Driven by firmware policy:
#   mode == high : high-g streams @ 7680 sps,  low-g streams @ 480 sps
#   mode == low  : high-g streams @ 480  sps,  low-g streams @ 8000 sps
SAMPLE_RATES = {
    "accel_low": {
        "high": 480,
        "low":  8000,
    },
    "accel_high": {
        "high": 7680,
        "low":  480,
    },
}


# ── CSV schema ──
CSV_HEADER = ["t_host_iso", "t_host_ns", "channel", "x", "y", "z"]


def csv_rows(ch_name, decoded, ts_iso, ts_ms) -> Any:
    """
        One row per notification carrying xyz floats.
        `channel` distinguishes accel_low vs accel_high in a single file.
    """
    if isinstance(decoded, (tuple, list)) and len(decoded) >= 3:
        yield (ts_iso, ts_ms, ch_name,
               float(decoded[0]), float(decoded[1]), float(decoded[2]))


def channel_title_suffix(ch_name: str, cfg: dict) -> str:
    """
        Return the [...] suffix shown after the channel's base title.
        Includes the accel ranges (always) and the per-channel sample rate
        derived from the active mode.
    """
    base = f"low={cfg['low_scale']}  high={cfg['high_scale']}"

    rate = SAMPLE_RATES.get(ch_name, {}).get(cfg["mode"])
    if rate is not None:
        return f"{base}  @{rate}sps"

    return base


# ── Config codec ─────────────────────────────
def encode_config(params: dict) -> bytes:
    """
        Pack the 4-byte CONFIG payload:
            byte 0 : axis        (1=x, 2=y, 3=z)
            byte 1 : low_scale   (0=2g, 1=4g, 2=8g, 3=16g)
            byte 2 : high_scale  (0=32g, 1=64g, 2=128g, 3=256g)
            byte 3 : mode        (0=low, 1=high)
    """
    return bytes([
        AXIS[params["axis"]],
        LOW_SCALE[params["low_scale"]],
        HIGH_SCALE[params["high_scale"]],
        MODE[params["mode"]],
    ])


def decode_config(data: bytes) -> dict:
    """Unpack the 4-byte CONFIG payload back into a params dict."""
    if len(data) < 4:
        raise ValueError(f"Expected ≥4 bytes, got {len(data)}")

    return {
        "axis":       AXIS_INV[data[0]],
        "low_scale":  LOW_SCALE_INV[data[1]],
        "high_scale": HIGH_SCALE_INV[data[2]],
        "mode":       MODE_INV[data[3]],
    }


# ── Data decoders ────────────────────────────
def _decode_accel_xyz(data: bytes) -> Any:
    """
        Decode a 12-byte payload as 3x float32 LE = (x, y, z) accelerometer
        reading in g.
    """
    if len(data) < 12:
        raise ValueError(f"Expected ≥12 bytes, got {len(data)}")

    return struct.unpack("<fff", bytes(data[:12]))


def _decode_fft_chunk(data: bytes) -> Any:
    """
        Placeholder decoder for FFT magnitude chunks.
        Assumes: little-endian float32 array, N bins per chunk.
        Replace with the actual payload format once confirmed.
    """
    n = len(data) // 4
    if n == 0:
        return ()
    return struct.unpack(f"<{n}f", bytes(data[: n * 4]))


def format_config_title(cfg: dict) -> str:
    """
        Compact one-liner of the config to embed in plot titles.
        Only the accelerometer ranges are shown; axis and mode are hidden
        because they are FFT-related and not relevant to the accel plots.
    """
    return f"low={cfg['low_scale']}  high={cfg['high_scale']}"


# ── Data channel registry (one per notification UUID) ──
DATA_CHANNELS = {
    "accel_low": {
        "uuid":    DATA0_UUID,
        "decode":  _decode_accel_xyz,
        "title":   "IMU Low-g Accel  (x, y, z)",
        "ylabel":  "mg",
        "series":  ["x", "y", "z"],
        "enabled": True,
    },
    "accel_high": {
        "uuid":    DATA1_UUID,
        "decode":  _decode_accel_xyz,
        "title":   "IMU High-g Accel  (x, y, z)",
        "ylabel":  "mg",
        "series":  ["x", "y", "z"],
        "enabled": True,
    }
}
