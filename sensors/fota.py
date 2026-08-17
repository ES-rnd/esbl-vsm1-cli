"""STS4x temperature sensor binding."""

import struct
from typing import Any


NAME = "Over-the-Air Update"
SERVICE_UUID = "0000fe70-cc7a-482a-984a-7f2ed5b3e58f"
CONFIG_UUID = "0000fe71-8e22-4541-9d4c-21edae82ed19"
DATA_UUID = "0000fe72-8e22-4541-9d4c-21edae82ed19"
FB_UUID = "0000fe73-8e22-4541-9d4c-21edae82ed19"


PARAMS = {
    "file": {"type": "str"}
}

# ── CSV schema ──
CSV_HEADER = ["t_host_iso", "t_host_ns", "channel", "temperature_c"]


def csv_rows(ch_name, decoded, ts_iso, ts_ms) -> Any:
    """
        One row per notification: (iso, ns, channel, temperature).
    """

    if isinstance(decoded, (int, float)):
        yield (ts_iso, ts_ms, ch_name, float(decoded))


def encode_config(params: dict) -> bytes:
    """Pack uint32 LE sampling frequency [Hz]."""

    data = struct.pack("<I", int(params["file"]))

    print(f"Packing {data}")

    return data
