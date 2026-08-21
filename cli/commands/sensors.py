"""Schema-driven BLE sensor commands.

This module implements subscription management, CSV recording, sensor
configuration, firmware transfer, provisioning, and IMU calibration.

Sensor-specific behavior is supplied by modules registered in
``SENSOR_REGISTRY``. A sensor module may expose:

* ``NAME``, ``SERVICE_UUID``, and ``CONFIG_UUID``
* ``PARAMS``, ``encode_config()``, and ``decode_config()``
* ``DATA_CHANNELS`` channel definitions
* ``CSV_HEADER`` and ``csv_rows()`` for optional recording
* ``DATA_UUID`` and ``FB_UUID`` for FOTA
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bleak.exc import BleakError

from ...config import MEASUREMENTS_DIR
from ...plotting import LiveBarPlot, LivePlot
from ...sensors.registry import SENSOR_REGISTRY
from ...sinks.csv_sink import CsvSink
from ...state import ctx
from .base import require_connected

# ---------------------------------------------------------------------------
# Shared types and exceptions
# ---------------------------------------------------------------------------

DecodeFn = Callable[[bytes], Any]
NotificationCallback = Callable[[Any, bytearray], None]

BLE_ERRORS = (BleakError, OSError, ValueError)
DECODE_ERRORS = (ValueError, struct.error, TypeError)

# ---------------------------------------------------------------------------
# FOTA protocol constants
# ---------------------------------------------------------------------------

FOTA_FLASH_PAGE_SIZE = 8192
FOTA_PAYLOAD_LEN = 224
FOTA_RESERVED_LEN = 8
FOTA_PACKET_LEN = 240
FOTA_CRC_OFFSET = FOTA_PACKET_LEN - 4
FOTA_PACKETS_PER_PAGE = math.ceil(FOTA_FLASH_PAGE_SIZE / FOTA_PAYLOAD_LEN)
FOTA_SEQUENCE_MODULUS = FOTA_PACKETS_PER_PAGE  # 37 sequence IDs: 0..36
FOTA_FEEDBACK_LEN = 8
FOTA_PACE_S = 0.0075
FOTA_PAGE_PAUSE_S = 0.05
FOTA_ACK_DRAIN_S = 2.0
FOTA_START_TIMEOUT_S = 0.5
FOTA_START_SETTLE_S = 1.0
FOTA_PROTOCOL_VERSION = 1
FOTA_UUID = 0x12345678
FOTA_VERSION = (1, 2, 3)

# Actual transport packet layout used by the existing firmware protocol:
#   [0:2]   sequence_id, uint16 LE, repeats 0..36 for every page
#   [2:4]   page_id, uint16 LE
#   [4:12]  reserved, 8 bytes
#   [12:236] payload, 224 bytes
#   [236:240] CRC32 over bytes [0:236]
FOTA_BODY_STRUCT = struct.Struct("<HH8s224s")
FOTA_CRC_STRUCT = struct.Struct("<I")
FOTA_START_STRUCT = struct.Struct("<IBBBII3sBB")

# ---------------------------------------------------------------------------
# Provisioning and calibration protocol constants
# ---------------------------------------------------------------------------

PROVISION_UUID = "0000fe44-8e22-4541-9d4c-21edae82ed19"
CALIBRATION_UUID = "0000fe41-8e22-4541-9d4c-21edae82ed19"
CALIBRATION_TIMEOUT_S = 8.0

WUP_OPTIONS = {
    "RTC_DISABLED": 0,  # Kept for protocol/backward compatibility.
    "RTC_30_SECS": 1,
    "RTC_1_MIN": 2,
    "RTC_15_MIN": 3,
    "RTC_30_MIN": 4,
    "RTC_1_HOUR": 5,
    "RTC_4_HOURS": 6,
    "RTC_8_HOURS": 7,
    "RTC_12_HOURS": 8,
    "RTC_1_DAY": 9,
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _get_sensor(value: str, argument_name: str = "module") -> Any | None:
    """Return a registered sensor module or print a consistent error."""
    mod = SENSOR_REGISTRY.get(value)
    if mod is None:
        print(
            f"⚠️  Unknown -{argument_name} '{value}'. "
            f"Available: {list(SENSOR_REGISTRY)}"
        )
    return mod


def _format_cfg(mod: Any, cfg: dict) -> str:
    """Format a configuration for display using the module hook if present."""
    formatter = getattr(mod, "format_config_title", None)
    return formatter(cfg) if formatter else str(cfg)


def _format_cfg_for(mod: Any, channel_name: str, cfg: dict) -> str:
    """Format a channel title suffix, falling back to module formatting."""
    formatter = getattr(mod, "channel_title_suffix", None)
    if formatter is not None:
        return formatter(channel_name, cfg)
    return _format_cfg(mod, cfg)


def _compact_ranges(values: Sequence[int]) -> str:
    """Collapse sorted integers into a compact form such as ``1-3, 7, 9``."""
    if not values:
        return ""

    parts: list[str] = []
    start = previous = values[0]

    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue

        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value

    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(parts)


def _close_plots(plots: Mapping[str, Any]) -> None:
    """Close every plot in a mapping."""
    for plot in plots.values():
        plot.close()


def _maybe_write_csv(module_name: str, channel_name: str, decoded: Any) -> None:
    """Write decoded notification rows when recording is active."""
    subscription = ctx.subscriptions.get(module_name)
    sink = subscription.get("sink") if subscription is not None else None
    if sink is None:
        return

    mod = SENSOR_REGISTRY[module_name]
    rows_fn = getattr(mod, "csv_rows", None)
    if rows_fn is None:
        return

    try:
        timestamp_iso = datetime.now(timezone.utc).isoformat()
        timestamp_ms = int(time.time() * 1000)
        rows = rows_fn(channel_name, decoded, timestamp_iso, timestamp_ms)
        sink.write_rows(rows)
    except (OSError, ValueError) as exc:
        print(f"[{channel_name}] csv write error: {exc}")


def _push_single_plot_value(plot: Any, decoded: Any, series_count: int) -> None:
    """Normalize decoded scalar/vector data and push it to one plot."""
    if (
        series_count > 1
        and isinstance(decoded, (tuple, list))
        and len(decoded) >= series_count
    ):
        plot.push(tuple(float(item) for item in decoded[:series_count]))
    elif isinstance(decoded, (int, float)):
        plot.push(float(decoded))
    elif isinstance(decoded, (tuple, list)) and decoded:
        plot.push(float(max(abs(item) for item in decoded)))


def _make_single_cb(
    decode_fn: DecodeFn,
    plot: Any,
    channel_name: str,
    series_count: int,
    module_name: str,
) -> NotificationCallback:
    """Create a notification callback for a channel with one plot."""

    def callback(_handle: Any, data: bytearray) -> None:
        try:
            decoded = decode_fn(data)
            _maybe_write_csv(module_name, channel_name, decoded)
            _push_single_plot_value(plot, decoded, series_count)
        except DECODE_ERRORS as exc:
            print(f"[{channel_name}] decode error: {exc}")

    return callback


def _make_fanout_cb(
    decode_fn: DecodeFn,
    plots_by_output: Mapping[str, Any],
    channel_name: str,
    module_name: str,
) -> NotificationCallback:
    """Create a callback that fans one decoded dictionary into many plots."""

    def callback(_handle: Any, data: bytearray) -> None:
        try:
            decoded = decode_fn(data)
            if not isinstance(decoded, dict):
                return

            _maybe_write_csv(module_name, channel_name, decoded)

            for output_name, plot in plots_by_output.items():
                if output_name not in decoded:
                    continue

                value = decoded[output_name]
                if isinstance(plot, LiveBarPlot):
                    if isinstance(value, (tuple, list)) and len(value) == 2:
                        plot.push(value[0], value[1])
                elif isinstance(value, (int, float)):
                    plot.push(float(value))
        except DECODE_ERRORS as exc:
            print(f"[{channel_name}] decode error: {exc}")

    return callback


# ---------------------------------------------------------------------------
# CSV recording commands
# ---------------------------------------------------------------------------

async def cmd_record(value: str, out: str | None) -> None:
    """Attach a CSV sink to an active sensor subscription."""
    if not require_connected():
        return

    mod = _get_sensor(value)
    if mod is None:
        return

    if not hasattr(mod, "CSV_HEADER") or not hasattr(mod, "csv_rows"):
        print(
            f"⚠️  '{value}' does not support recording "
            "(missing CSV_HEADER / csv_rows)."
        )
        return

    subscription = ctx.subscriptions.get(value)
    if subscription is None:
        print(
            f"⚠️  '{value}' is not subscribed. "
            f"Run `subscribe -module {value}` first."
        )
        return

    existing_sink = subscription.get("sink")
    if existing_sink is not None:
        print(f"⚠️  Already recording '{value}' to {existing_sink.path}")
        return

    if not out:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = f"ess_{value}_{stamp}.csv"

    output_path = Path(out)
    if not output_path.is_absolute():
        output_path = MEASUREMENTS_DIR / output_path

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sink = CsvSink(output_path, mod.CSV_HEADER)
    except OSError as exc:
        print(f"❌ Could not create/open {output_path}: {exc}")
        return

    subscription["sink"] = sink
    print(f"🔴 Recording '{value}' → {output_path.resolve()}")


async def cmd_stop_record(value: str) -> None:
    """Detach and close the CSV sink associated with a subscription."""
    subscription = ctx.subscriptions.get(value)
    if subscription is None:
        print(f"⚠️  '{value}' is not subscribed.")
        return

    sink = subscription.pop("sink", None)
    if sink is None:
        print(f"Not recording '{value}'.")
        return

    stats = sink.stats()
    sink.close()
    print(
        f"⏹  Stopped recording '{value}':\n"
        f"     File    : {stats['path']}\n"
        f"     Frames  : {stats['frames']}\n"
        f"     Rows    : {stats['rows']}\n"
        f"     Duration: {stats['duration_s']:.1f} s"
    )


# ---------------------------------------------------------------------------
# Sensor configuration
# ---------------------------------------------------------------------------

def _coerce_config_value(name: str, value: Any, spec: dict) -> Any:
    """Validate and normalize one schema-driven configuration value."""
    value_type = spec["type"]

    if value_type == "int":
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"-{name} must be an integer") from exc

        if not spec["min"] <= normalized <= spec["max"]:
            raise ValueError(
                f"-{name} must be in [{spec['min']}, {spec['max']}]"
            )
        return normalized

    if value_type == "choice":
        if value not in spec["choices"]:
            raise ValueError(f"-{name} must be one of {spec['choices']}")
        return value

    # Preserve prior behavior for schema types not explicitly interpreted here.
    return value


def _refresh_subscription_titles(configured_mod: Any, actual: dict) -> None:
    """Refresh plots for subscriptions sharing the configured characteristic."""
    try:
        for subscription_name, subscription in list(ctx.subscriptions.items()):
            subscription_mod = SENSOR_REGISTRY[subscription_name]
            if subscription_mod.CONFIG_UUID != configured_mod.CONFIG_UUID:
                continue

            subscription["config"] = actual
            for plot_key, plot in subscription["plots"].items():
                if not hasattr(plot, "set_title"):
                    continue

                if "." in plot_key:
                    channel_name, output_name = plot_key.split(".", 1)
                    base_title = subscription_mod.DATA_CHANNELS[channel_name][
                        "outputs"
                    ][output_name]["title"]
                else:
                    channel_name = plot_key
                    base_title = subscription_mod.DATA_CHANNELS[channel_name]["title"]

                suffix = _format_cfg_for(subscription_mod, channel_name, actual)
                plot.set_title(f"{base_title}  [{suffix}]")

            print(
                f"ℹ️  Subscription '{subscription_name}' refreshed; "
                "plot titles updated."
            )
    except (KeyError, NameError, AttributeError, TypeError) as exc:
        print(
            "⚠️  Title refresh skipped (non-fatal): "
            f"{type(exc).__name__}: {exc}"
        )


async def cmd_configure(value: str, kv: dict) -> None:
    """Merge, validate, write, and read back a sensor configuration."""
    if not require_connected():
        return

    mod = _get_sensor(value, "value")
    if mod is None:
        return

    if not kv:
        flags = " ".join(f"-{name} <val>" for name in mod.PARAMS)
        print(f"Usage: configure -value {value} {flags}")
        return

    try:
        raw = await ctx.client.read_gatt_char(mod.CONFIG_UUID)
        current = mod.decode_config(raw)
    except BLE_ERRORS as exc:
        print(f"❌ read config failed: {exc}")
        return

    params = dict(current)
    for name, supplied_value in kv.items():
        if name not in mod.PARAMS:
            print(
                f"⚠️  Unknown param '-{name}' for {value}. "
                f"Valid: {list(mod.PARAMS)}"
            )
            return

        try:
            params[name] = _coerce_config_value(
                name, supplied_value, mod.PARAMS[name]
            )
        except ValueError as exc:
            print(f"❌ {exc}")
            return

    try:
        payload = mod.encode_config(params)
        await ctx.client.write_gatt_char(mod.CONFIG_UUID, payload, response=False)
        await asyncio.sleep(0.8)
        raw_readback = await ctx.client.read_gatt_char(mod.CONFIG_UUID)
        actual = mod.decode_config(raw_readback)
    except BLE_ERRORS as exc:
        print(f"❌ write/readback failed: {exc}")
        return

    status = "✅" if actual == params else "⚠️"
    print(
        f"{status} Configured {mod.NAME}:\n"
        f"     UUID    : {mod.CONFIG_UUID}\n"
        f"     Service : {mod.SERVICE_UUID}\n"
        f"     Wrote   : {params}   (bytes={payload.hex()})\n"
        f"     Readback: {actual}"
    )
    _refresh_subscription_titles(mod, actual)


# ---------------------------------------------------------------------------
# FOTA helpers and command
# ---------------------------------------------------------------------------

def _resolve_firmware_path(file_arg: Any) -> Path | None:
    """Resolve a firmware path using the command's existing search policy."""
    if not file_arg:
        print("❌ Missing -file argument.")
        print("Usage: update -module fota -file <firmware.bin>")
        return None

    file_path = Path(file_arg)
    if not file_path.is_absolute():
        candidates = (Path.cwd() / file_path, Path.cwd().parent / file_path)
        file_path = next((path for path in candidates if path.exists()), None)
        if file_path is None:
            print(f"❌ File not found: {file_arg}")
            for candidate in candidates:
                print(f"   Tried: {candidate}")
            return None

    if not file_path.is_file():
        print(f"❌ Invalid file: {file_path}")
        return None
    if file_path.suffix.lower() != ".bin":
        print(f"❌ Expected a .bin file, got: {file_path.name}")
        return None
    return file_path


def _build_fota_packet(sequence_id: int, page_id: int, payload: bytes) -> bytes:
    """Build one 240-byte FOTA packet with its body CRC32."""
    if len(payload) != FOTA_PAYLOAD_LEN:
        raise ValueError(
            f"payload must be {FOTA_PAYLOAD_LEN} bytes, got {len(payload)}"
        )

    body = FOTA_BODY_STRUCT.pack(
        sequence_id,
        page_id,
        b"\x00" * FOTA_RESERVED_LEN,
        payload,
    )
    if len(body) != FOTA_CRC_OFFSET:
        raise ValueError(
            f"internal body size error: {len(body)} != {FOTA_CRC_OFFSET}"
        )

    packet = body + FOTA_CRC_STRUCT.pack(zlib.crc32(body) & 0xFFFFFFFF)
    if len(packet) != FOTA_PACKET_LEN:
        raise ValueError(
            f"internal packet size error: {len(packet)} != {FOTA_PACKET_LEN}"
        )
    return packet


def _print_fota_selection(
    file_path: Path,
    firmware: bytes,
    image_crc32: int,
    page_count: int,
    packet_count: int,
    image_padding: int,
) -> None:
    """Print the pre-transfer firmware summary."""
    major, minor, patch = FOTA_VERSION
    print(
        "\n📦 FOTA file selected:\n"
        f"     File              : {file_path.name}\n"
        f"     Path              : {file_path}\n"
        f"     Version           : @v{major}.{minor}.{patch}\n"
        f"     Original size     : {len(firmware)} B\n"
        f"     Image CRC32       : 0x{image_crc32:08X}\n"
        f"     Flash page size   : {FOTA_FLASH_PAGE_SIZE} B\n"
        f"     Pages             : {page_count}\n"
        f"     Page padding      : {image_padding} B\n"
        f"     Payload           : {FOTA_PAYLOAD_LEN} B/packet\n"
        f"     Packets/page      : {FOTA_PACKETS_PER_PAGE}\n"
        f"     Total packets     : {packet_count}\n"
        f"     Packet size       : {FOTA_PACKET_LEN} B\n"
    )


async def cmd_update(value: str, kv: dict) -> None:
    """Stream a binary firmware image using the existing page-based FOTA protocol.

    The image is divided into 8192-byte flash pages. Each page is transported
    in 37 packets containing 224 payload bytes each. The final image page and
    the final packet in every page are zero-padded.

    Sequence IDs intentionally repeat from 0 through 36 for every flash page.
    Page identity is carried separately in the packet's ``page_id`` field.
    """
    if not require_connected():
        return

    mod = _get_sensor(value, "value")
    if mod is None:
        return

    file_path = _resolve_firmware_path(kv.get("file"))
    if file_path is None:
        return

    try:
        firmware = file_path.read_bytes()
    except OSError as exc:
        print(f"❌ Could not read file '{file_path}': {exc}")
        return

    if not firmware:
        print(f"❌ File is empty: {file_path}")
        return

    page_count = math.ceil(len(firmware) / FOTA_FLASH_PAGE_SIZE)
    packet_count = page_count * FOTA_PACKETS_PER_PAGE
    image_padding = page_count * FOTA_FLASH_PAGE_SIZE - len(firmware)

    if page_count > 0x10000:
        print(
            "❌ Firmware too large for uint16 page IDs: "
            f"{page_count} pages required."
        )
        return

    # Preserve the original protocol constraint/check, although packet sequence
    # IDs repeat per page rather than increasing globally.
    if packet_count > 0x10000:
        print(
            "❌ Firmware too large for uint16 packet accounting: "
            f"{packet_count} packets required."
        )
        return

    image_crc32 = zlib.crc32(firmware) & 0xFFFFFFFF
    feedback_uuid = getattr(mod, "FB_UUID", None)
    if feedback_uuid is None:
        print(f"❌ '{value}' module has no FB_UUID defined.")
        return

    feedback_char = ctx.client.services.get_characteristic(feedback_uuid)
    if feedback_char is None:
        print(f"❌ Feedback char {feedback_uuid} not found in the GATT table.")
        return

    properties = feedback_char.properties
    if "notify" not in properties and "indicate" not in properties:
        print(f"❌ {feedback_uuid} is not notifiable (props={properties}).")
        return

    _print_fota_selection(
        file_path,
        firmware,
        image_crc32,
        page_count,
        packet_count,
        image_padding,
    )

    acked: set[int] = set()
    ack_order: list[int] = []
    bad_length_count = 0
    response_data: int | None = None
    response_event = asyncio.Event()

    def feedback_callback(_handle: Any, data: bytearray) -> None:
        nonlocal bad_length_count, response_data

        frame = bytes(data)
        if len(frame) < 5:
            bad_length_count += 1
            return

        try:
            received_uuid = struct.unpack_from("<I", frame, 0)[0]
        except struct.error:
            bad_length_count += 1
            return

        if received_uuid != FOTA_UUID:
            print("Wrong Response UUID. Skipping...")
            return

        response_type = frame[4]
        if response_type == 0:
            if len(frame) < 6:
                bad_length_count += 1
                return
            response_data = frame[5]
            response_event.set()
            return

        if response_type == 1:
            if len(frame) != FOTA_FEEDBACK_LEN:
                bad_length_count += 1
                return
            ack_id = struct.unpack_from("<H", frame, 6)[0]
            ack_order.append(ack_id)
            acked.add(ack_id)
            return

        bad_length_count += 1

    try:
        await ctx.client.start_notify(feedback_uuid, feedback_callback)
    except (BleakError, OSError) as exc:
        print(f"❌ Could not subscribe to feedback {feedback_uuid}: {exc}")
        return

    try:
        major, minor, patch = FOTA_VERSION
        start_packet = FOTA_START_STRUCT.pack(
            FOTA_UUID,
            major,
            minor,
            patch,
            len(firmware),
            image_crc32,
            b"\x00" * 3,
            0,
            FOTA_PROTOCOL_VERSION,
        )
        if len(start_packet) != 20:
            print(
                f"❌ Internal start packet size error: "
                f"{len(start_packet)} != 20"
            )
            return

        response_event.clear()
        response_data = None
        try:
            await ctx.client.write_gatt_char(
                mod.CONFIG_UUID, start_packet, response=False
            )
        except BLE_ERRORS as exc:
            print(f"❌ start/config write failed: {exc}")
            return

        try:
            await asyncio.wait_for(
                response_event.wait(), timeout=FOTA_START_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            print("Fatal: No response in 500 ms. Exiting FOTA...")
            return

        if response_data != 1:
            print("FOTA Rejected. Exiting...")
            return

        print("FOTA Accepted. Proceeding...")
        await asyncio.sleep(FOTA_START_SETTLE_S)

        sent = 0
        sent_ids: set[int] = set()
        started_at = time.perf_counter()

        for page_id in range(page_count):
            page_start = page_id * FOTA_FLASH_PAGE_SIZE
            page = firmware[page_start : page_start + FOTA_FLASH_PAGE_SIZE]
            page = page.ljust(FOTA_FLASH_PAGE_SIZE, b"\x00")

            for packet_in_page in range(FOTA_PACKETS_PER_PAGE):
                payload_start = packet_in_page * FOTA_PAYLOAD_LEN
                payload = page[payload_start : payload_start + FOTA_PAYLOAD_LEN]
                payload = payload.ljust(FOTA_PAYLOAD_LEN, b"\x00")

                sequence_id = packet_in_page % FOTA_SEQUENCE_MODULUS
                try:
                    packet = _build_fota_packet(sequence_id, page_id, payload)
                except ValueError as exc:
                    print(f"❌ {exc}")
                    return

                try:
                    await ctx.client.write_gatt_char(
                        mod.DATA_UUID, packet, response=False
                    )
                except BLE_ERRORS as exc:
                    print(
                        f"❌ write failed on packet {sequence_id} "
                        f"(page={page_id}, pkt_in_page={packet_in_page}): {exc}"
                    )
                    return

                sent_ids.add(sequence_id)
                sent += 1
                progress = sent / packet_count * 100.0
                print(
                    f"\r🚀 Sending FOTA: {sent}/{packet_count} packets "
                    f"({progress:5.1f}%)",
                    end="",
                    flush=True,
                )

                if FOTA_PACE_S:
                    await asyncio.sleep(FOTA_PACE_S)
                if packet_in_page == FOTA_PACKETS_PER_PAGE - 1:
                    await asyncio.sleep(FOTA_PAGE_PAUSE_S)

        print()
        sent_at = time.perf_counter()
        print(
            f"… streamed {sent} packets, draining ACKs for "
            f"{FOTA_ACK_DRAIN_S:.1f}s …"
        )
        await asyncio.sleep(FOTA_ACK_DRAIN_S)

        elapsed_s = sent_at - started_at
        air_bytes = sent * FOTA_PACKET_LEN
        firmware_bytes = len(firmware)
        air_kib_s = air_bytes / elapsed_s / 1024.0 if elapsed_s else 0.0
        firmware_kib_s = (
            firmware_bytes / elapsed_s / 1024.0 if elapsed_s else 0.0
        )
        missing = sorted(sent_ids - acked)
        unexpected = sorted(acked - sent_ids)
        duplicate_count = len(ack_order) - len(acked)
        estimated_500_kib = (
            f"{500 * 1024 / (firmware_bytes / elapsed_s):.1f} s"
            if elapsed_s
            else "n/a"
        )

        print(
            "\n✅ FOTA file streaming complete:\n"
            f"     File              : {file_path.name}\n"
            f"     FW size           : {firmware_bytes} B\n"
            f"     Image CRC32       : 0x{image_crc32:08X}\n"
            f"     Flash pages       : {page_count}\n"
            f"     Page padding      : {image_padding} B\n"
            f"     Packets/page      : {FOTA_PACKETS_PER_PAGE}\n"
            f"     Sent              : {sent}/{packet_count}\n"
            f"     ACK'd unique      : {len(acked)}\n"
            f"     Duplicates        : {duplicate_count}\n"
            f"     Bad-length        : {bad_length_count}\n"
            f"     Air bytes         : {air_bytes} B\n"
            f"     Send time         : {elapsed_s:.3f} s\n"
            f"     Throughput        : {firmware_kib_s:.2f} KB/s firmware\n"
            f"                         {air_kib_s:.2f} KB/s over BLE payload\n"
            f"     Est. 500 KB       : {estimated_500_kib}"
        )

        if not missing:
            print(f"     Missing           : 0  🎉 all {sent} packets ACK'd")
        else:
            print(
                f"     Missing           : {len(missing)} "
                "sequence id(s) never ACK'd:"
            )
            print("       " + _compact_ranges(missing))

        if unexpected:
            suffix = " …" if len(unexpected) > 20 else ""
            print(
                f"     ⚠️ Unexpected ACKs (id not sent): "
                f"{unexpected[:20]}{suffix}"
            )
    finally:
        try:
            if ctx.client and ctx.client.is_connected:
                await ctx.client.stop_notify(feedback_uuid)
        except (BleakError, OSError) as exc:
            print(f"stop_notify warning ({feedback_uuid}): {exc}")


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------

def _create_fanout_plots(mod: Any, channel_name: str, channel: dict, cfg: dict):
    """Create all plots declared by a fan-out channel."""
    channel_plots: dict[str, Any] = {}

    for output_name, output_spec in channel["outputs"].items():
        title = (
            f"{output_spec['title']}  "
            f"[{_format_cfg_for(mod, channel_name, cfg)}]"
        )
        if output_spec["type"] == "bar":
            plot = LiveBarPlot(
                title=title,
                xlabel=output_spec.get("xlabel", ""),
                ylabel=output_spec["ylabel"],
            )
        else:
            plot = LivePlot(
                title=title,
                ylabel=output_spec["ylabel"],
                window_s=30.0,
            )
        channel_plots[output_name] = plot

    return channel_plots


async def cmd_subscribe(value: str) -> None:
    """Subscribe to every enabled data channel and open its live plots."""
    if not require_connected():
        return

    mod = _get_sensor(value)
    if mod is None:
        return

    if value in ctx.subscriptions:
        print(f"Already subscribed to '{value}'.")
        return

    try:
        raw = await ctx.client.read_gatt_char(mod.CONFIG_UUID)
        cfg = mod.decode_config(raw)
    except BLE_ERRORS as exc:
        print(f"❌ Failed to read config {mod.CONFIG_UUID}: {exc}")
        return

    print(
        f"📥 Subscribing to {mod.NAME}:\n"
        f"     Service : {mod.SERVICE_UUID}\n"
        f"     Config  : {cfg}\n"
        f"     Channels: {list(mod.DATA_CHANNELS.keys())}"
    )

    plots: dict[str, Any] = {}
    subscribed_uuids: list[str] = []

    for channel_name, channel in mod.DATA_CHANNELS.items():
        if not channel.get("enabled", True):
            continue

        if "outputs" in channel:
            channel_plots = _create_fanout_plots(
                mod, channel_name, channel, cfg
            )
            plots.update(
                {
                    f"{channel_name}.{output_name}": plot
                    for output_name, plot in channel_plots.items()
                }
            )

            try:
                callback = _make_fanout_cb(
                    channel["decode"], channel_plots, channel_name, value
                )
                await ctx.client.start_notify(channel["uuid"], callback)
                subscribed_uuids.append(channel["uuid"])
            except (BleakError, OSError) as exc:
                _close_plots(channel_plots)
                print(f"❌ start_notify failed for {channel_name}: {exc}")
            continue

        series = channel.get("series")
        series_count = len(series) if series else 1
        plot = LivePlot(
            title=(
                f"{channel['title']} "
                f"[{_format_cfg_for(mod, channel_name, cfg)}]"
            ),
            ylabel=channel["ylabel"],
            window_s=30.0,
            series=series,
        )

        try:
            callback = _make_single_cb(
                channel["decode"], plot, channel_name, series_count, value
            )
            await ctx.client.start_notify(channel["uuid"], callback)
            plots[channel_name] = plot
            subscribed_uuids.append(channel["uuid"])
        except (BleakError, OSError) as exc:
            plot.close()
            print(f"❌ start_notify failed for {channel_name}: {exc}")

    if not subscribed_uuids:
        _close_plots(plots)
        return

    # Store the subscription before starting the watchdog so the task cannot
    # observe a missing subscription if it runs immediately.
    ctx.subscriptions[value] = {
        "char_uuids": subscribed_uuids,
        "plots": plots,
        "config": cfg,
        "watchdog": None,
    }
    ctx.subscriptions[value]["watchdog"] = asyncio.create_task(
        _watch_plots_closure(value)
    )
    print(f"✅ Subscribed to '{value}'; {len(plots)} live plot(s) opened.")


async def cmd_unsubscribe(value: str) -> None:
    """Stop all notifications, recording, plots, and watchdogs for a sensor."""
    subscription = ctx.subscriptions.pop(value, None)
    if subscription is None:
        print(f"Not subscribed to '{value}'.")
        return

    watchdog = subscription.get("watchdog")
    if watchdog is not None and not watchdog.done():
        watchdog.cancel()

    sink = subscription.pop("sink", None)
    if sink is not None:
        stats = sink.stats()
        sink.close()
        print(
            f"⏹  Auto-stopped recording → {stats['path']} "
            f"({stats['rows']} rows, {stats['frames']} frames)"
        )

    for characteristic_uuid in subscription.get("char_uuids", []):
        try:
            if ctx.client and ctx.client.is_connected:
                await ctx.client.stop_notify(characteristic_uuid)
        except (BleakError, OSError) as exc:
            print(f"stop_notify warning ({characteristic_uuid}): {exc}")

    _close_plots(subscription.get("plots", {}))
    print(f"👋 Unsubscribed from '{value}'.")


async def _watch_plots_closure(value: str) -> None:
    """Auto-unsubscribe when every plot belonging to a sensor is closed."""
    subscription = ctx.subscriptions.get(value)
    if subscription is None:
        return

    plots = subscription.get("plots", {})
    while any(
        getattr(plot, "proc", None) and plot.proc.is_alive()
        for plot in plots.values()
    ):
        await asyncio.sleep(0.5)
        if value not in ctx.subscriptions:
            return

    if value in ctx.subscriptions:
        print(f"\nℹ️  All plots for '{value}' closed; unsubscribing.")
        await cmd_unsubscribe(value)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

async def cmd_provision(wup: str) -> None:
    """Send the selected RTC wake-up provisioning option to the sensor."""
    if not require_connected():
        return

    if wup not in WUP_OPTIONS:
        print(f"❌ Unknown wake-up option '{wup}'. Valid: {list(WUP_OPTIONS)}")
        return

    payload = bytes([1, WUP_OPTIONS[wup], 0, 0, 0, 0, 0, 0, 0, 0])

    def callback(_handle: Any, data: bytearray) -> None:
        if payload == bytes(data):
            print("\n✅ Successful")

    try:
        await ctx.client.start_notify(PROVISION_UUID, callback)
    except (BleakError, OSError) as exc:
        print(f"❌ Could not subscribe to {PROVISION_UUID}: {exc}")
        return

    await asyncio.sleep(0.5)
    print("Sensor Provisioning Requested")

    try:
        await ctx.client.write_gatt_char(
            PROVISION_UUID, payload, response=False
        )
    except BLE_ERRORS as exc:
        print(f"❌ provisioning write failed: {exc}")

    # Intentionally do not stop notifications here. This preserves the original
    # command's lifetime behavior, in which provisioning responses may arrive
    # after the write coroutine returns.


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def _u16_le(low: int, high: int) -> int:
    """Decode an unsigned 16-bit little-endian value from two octets."""
    return low | (high << 8)


def _s16_le(low: int, high: int) -> int:
    """Decode a signed 16-bit little-endian value from two octets."""
    unsigned = _u16_le(low, high)
    return unsigned - 0x10000 if unsigned & 0x8000 else unsigned


def _build_calibration_packet(kv: dict) -> bytes | None:
    """Build the five-byte calibration command while preserving its schema."""
    packet = [0, 0, 0, 0, 0]
    field_offsets = {"z_offset": 1, "mag_xy": 2, "jitter": 3}

    for name, value in kv.items():
        offset = field_offsets.get(name)
        if offset is None:
            print(f"     ⚠️ ignoring unknown option -{name}")
            continue
        try:
            packet[offset] = int(value)
        except (TypeError, ValueError):
            print(f"❌ -{name} must be an integer")
            return None

    try:
        return bytes(packet)
    except ValueError as exc:
        print(f"❌ Calibration parameters must fit in one byte: {exc}")
        return None


async def cmd_calibrate(value: str, kv: dict) -> None:
    """Start calibration and report transient feedback and terminal status."""
    if not require_connected():
        return

    print("🛠  Calibration requested:")
    print(f"     Module  : {value}")

    packet = _build_calibration_packet(kv)
    if packet is None:
        return

    for name in ("z_offset", "mag_xy", "jitter"):
        if name in kv:
            print(f"     -{name:<9}: {int(kv[name])} mg")

    done = asyncio.Event()
    result: dict[str, Any] = {
        "ok": False,
        "theta_deg": None,
        "reason": None,
    }

    def decode_feedback(data: bytes) -> None:
        if len(data) < 5:
            print(f"[calibrate] short frame ({len(data)} B): {data.hex()}")
            return

        state = data[0]
        substate = data[1]

        if state == 0x00:
            print(
                f"✅ Accepted; z_offset={data[1]} mg, "
                f"mag_xy={data[2]} mg, jitter={data[3]} mg"
            )
        elif state == 0x01:
            if substate == 0x01:
                measured = _u16_le(data[2], data[3])
                print(
                    f"   ⚠️ Z-axis misalignment: {measured} mg exceeds threshold"
                )
            elif substate == 0x02:
                measured = _u16_le(data[2], data[3])
                print(
                    f"   ⚠️ XY-plane misalignment: {measured} mg off from 1000 mg"
                )
            elif substate == 0x03:
                print("   ⚠️ Jitter / movement detected; hold the sensor still")
            else:
                print(f"   ⚠️ Condition error (sub=0x{substate:02X})")
        elif state == 0x02:
            raw = _s16_le(data[1], data[2])
            theta_rad = raw / 10.0
            theta_deg = math.degrees(theta_rad)
            result["ok"] = True
            result["theta_deg"] = theta_deg
            print(
                f"🎉 Calibration SUCCESS; theta ≈ {theta_deg:+.1f}° "
                f"({theta_rad:+.3f} rad, raw={raw})"
            )
            done.set()
        elif state == 0x03:
            result["reason"] = "timeout; sensor never stabilized"
            print("⏱  Calibration TIMED OUT.")
            done.set()
        else:
            print(f"[calibrate] unknown state 0x{state:02X}: {data.hex()}")

    def callback(_handle: Any, data: bytearray) -> None:
        decode_feedback(bytes(data))

    try:
        await ctx.client.start_notify(CALIBRATION_UUID, callback)
    except (BleakError, OSError) as exc:
        print(f"❌ Could not subscribe to {CALIBRATION_UUID}: {exc}")
        return

    try:
        try:
            await ctx.client.write_gatt_char(
                CALIBRATION_UUID, packet, response=False
            )
        except BLE_ERRORS as exc:
            print(f"❌ calibration write failed: {exc}")
            return

        print("👋 Sensor Calibration Requested; waiting for result…\n")
        try:
            await asyncio.wait_for(done.wait(), timeout=CALIBRATION_TIMEOUT_S)
        except asyncio.TimeoutError:
            print(
                "\n❌ No terminal result within 8 s "
                "(no success/timeout frame received)."
            )
            return

        if result["ok"]:
            print(f"\n✅ Done; rotation theta = {result['theta_deg']:+.2f}°")
        else:
            print(f"\n❌ Calibration failed; reason: {result['reason']}")
    finally:
        try:
            if ctx.client and ctx.client.is_connected:
                await ctx.client.stop_notify(CALIBRATION_UUID)
        except (BleakError, OSError) as exc:
            print(f"stop_notify warning ({CALIBRATION_UUID}): {exc}")
