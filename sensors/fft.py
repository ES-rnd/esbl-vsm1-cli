"""Vibration FFT analysis binding (shares IMU service)."""

import struct
from typing import Any

from . import imu


NAME = "Vibration FFT"
SERVICE_UUID = imu.SERVICE_UUID
CONFIG_UUID = imu.CONFIG_UUID            # same fe51 as imu
DATA_UUID = imu.DATA3_UUID             # fe55 streaming packet

NUM_BINS = 50
PACKET_LEN = 1 + 4 + 4 + 4 + NUM_BINS * 2 + NUM_BINS * 2 + 4 + 4   # 221


# ── Configure schema ─────────────────────────
# Only the FFT-related fields are user-editable here.
# `low_scale` / `high_scale` are preserved from the readback
# (set via `configure -module imu ...`).
PARAMS = {
    "axis": {"type": "choice", "choices": list(imu.AXIS.keys()),
             "help": "FFT input axis"},
    "mode": {"type": "choice", "choices": list(imu.MODE.keys()),
             "help": "Which accel feeds the FFT (low/high)"},
}


# ── Reuse imu's codec verbatim ───────────────
encode_config = imu.encode_config
decode_config = imu.decode_config


# ── CSV schema ──
CSV_HEADER = [
    "t_host_iso", "t_host_ns", "channel", "axis", "bin_index",
    "freq_hz", "magnitude",
    "rms", "crest", "kurtosis", "vel_rms", "disp_rms",
]


def csv_rows(ch_name, decoded, ts_iso, ts_ns) -> Any:
    """50 rows per notification (long format). Frame scalars repeated."""
    if not isinstance(decoded, dict):
        return

    axis = decoded["axis"]

    # correct axis
    if axis > 3:
        axis = axis - 4

    freqs, mags = decoded["fft_spectrum"]
    rms = decoded["rms"]
    crest = decoded["crest"]
    kurt = decoded["kurtosis"]
    vel_rms = decoded["velocity_rms"]
    disp_rms = decoded["displacement_rms"]

    n = min(len(freqs), len(mags))

    for i in range(n):
        yield (ts_iso, ts_ns, ch_name, axis, i,
               freqs[i], mags[i],
               rms, crest, kurt, vel_rms, disp_rms)


# ── Title formatters ─────────────────────────
def format_config_title(cfg: dict) -> str:
    """
        Compact one-liner of the config to embed in plot titles.
        Shows only the FFT-relevant fields (axis + mode).
    """
    return f"axis={cfg['axis']}  mode={cfg['mode']}"


def channel_title_suffix(_ch_name: str, cfg: dict) -> str:
    """
        Per-channel title suffix. Adds the effective FFT input sample rate
        (taken from imu.SAMPLE_RATES of the channel that feeds the FFT).

        `_ch_name` is part of the framework signature but unused here:
        all FFT outputs share the same suffix.
    """
    base = format_config_title(cfg)

    feeder = "accel_high" if cfg["mode"] == "high" else "accel_low"
    rate = imu.SAMPLE_RATES.get(feeder, {}).get(cfg["mode"])

    if rate is not None:
        return f"{base}  @{rate}sps"

    return base


# ── Data decoder ─────────────────────────────
def _decode_fft_packet(data: bytes) -> Any:
    """
        Decode the fe55 streaming packet (221 bytes total):
            byte    0       : axis             (uint8: 1=x, 2=y, 3=z)
            bytes   1.. 4   : rms              (float32 LE)
            bytes   5.. 8   : crest            (float32 LE)
            bytes   9.. 12  : kurtosis         (float32 LE)
            bytes  13..112  : magnitudes       (50 × uint16 LE)
            bytes 113..212  : frequencies      (50 × uint16 LE)
            bytes 213..216  : velocity_rms     (float32 LE)
            bytes 217..220  : displacement_rms (float32 LE)
    """
    if len(data) < PACKET_LEN:
        raise ValueError(f"Expected ≥{PACKET_LEN} bytes, got {len(data)}")

    b = bytes(data)

    axis = b[0]
    rms = struct.unpack("<f", b[1:5])[0]
    crest = struct.unpack("<f", b[5:9])[0]
    kurtosis = struct.unpack("<f", b[9:13])[0]

    mags_off = 13
    mags = struct.unpack(f"<{NUM_BINS}H", b[mags_off:mags_off + NUM_BINS * 2])

    freqs_off = mags_off + NUM_BINS * 2
    freqs = struct.unpack(f"<{NUM_BINS}H", b[freqs_off:freqs_off + NUM_BINS * 2])   # noqa

    tail_off = freqs_off + NUM_BINS * 2
    vel_rms = struct.unpack("<f", b[tail_off:tail_off + 4])[0]
    disp_rms = struct.unpack("<f", b[tail_off + 4:tail_off + 8])[0]

    return {
        "axis":             axis,
        "fft_spectrum":     (list(freqs), list(mags)),
        "rms":              rms,
        "crest":            crest,
        "kurtosis":         kurtosis,
        "velocity_rms":     vel_rms,
        "displacement_rms": disp_rms,
    }


# ── Channel registry: one notify, multiple plot outputs ──
DATA_CHANNELS = {
    "fft": {
        "uuid":    DATA_UUID,
        "decode":  _decode_fft_packet,
        "enabled": True,
        "outputs": {
            "fft_spectrum": {
                "type":   "bar",
                "title":  "FFT Spectrum",
                "xlabel": "Frequency [Hz]",
                "ylabel": "Magnitude",
            },
            "rms": {
                "type":   "line",
                "title":  "RMS",
                "ylabel": "mg",
            },
            "crest": {
                "type":   "line",
                "title":  "Crest Factor",
                "ylabel": "ratio",
            },
            "kurtosis": {
                "type":   "line",
                "title":  "Kurtosis",
                "ylabel": "ratio",
            },
            "velocity_rms": {
                "type":   "line",
                "title":  "Velocity RMS",
                "ylabel": "mm/s",
            },
            "displacement_rms": {
                "type":   "line",
                "title":  "Displacement RMS",
                "ylabel": "mm",
            },
        },
    },
}
