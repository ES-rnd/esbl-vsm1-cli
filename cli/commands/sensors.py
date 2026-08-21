"""subscribe / unsubscribe / configure commands (schema-driven)."""

import asyncio
import struct
from typing import Any
import time
from datetime import datetime, timezone
from pathlib import Path
import zlib
import math

from bleak.exc import BleakError

from ...state import ctx
from ...sensors.registry import SENSOR_REGISTRY
from ...plotting import LivePlot, LiveBarPlot
from .base import require_connected
from ...config import MEASUREMENTS_DIR
from ...sinks.csv_sink import CsvSink


def _make_fanout_cb(decode_fn, plots_by_output, label, value) -> Any:
    """
        Closure for channels that decode into a dict → multiple plots.
        Writes CSV through the module's csv_rows() if a sink is attached.
    """

    def cb(_handle, data) -> Any:
        try:
            decoded = decode_fn(data)
            if not isinstance(decoded, dict):
                return

            _maybe_write_csv(value, label, decoded)

            for out_name, plot in plots_by_output.items():
                if out_name not in decoded:
                    continue
                v = decoded[out_name]

                if isinstance(plot, LiveBarPlot):
                    if isinstance(v, (tuple, list)) and len(v) == 2:
                        plot.push(v[0], v[1])
                else:
                    if isinstance(v, (int, float)):
                        plot.push(float(v))

        except (ValueError, struct.error) as e:
            print(f"[{label}] decode error: {e}")
    return cb


def _format_cfg(mod, cfg: dict) -> str:
    """
        Use the sensor's own formatter if present, else fall back to dict repr.   # noqa
    """

    formatter = getattr(mod, "format_config_title", None)

    return formatter(cfg) if formatter else str(cfg)


def _format_cfg_for(mod, ch_name: str, cfg: dict) -> str:
    """
        Use the sensor's per-channel title formatter if present, otherwise
        fall back to the module-level one.
    """

    per_ch = getattr(mod, "channel_title_suffix", None)
    if per_ch is not None:
        return per_ch(ch_name, cfg)

    return _format_cfg(mod, cfg)


# ═════════════════════════════════════════════
# record / stop_record  (FFT only for now)
# ═════════════════════════════════════════════
async def cmd_record(value: str, out: str | None) -> Any:
    """
        Attach a CSV sink to an active subscription. Schema comes from the
        sensor module (CSV_HEADER + csv_rows). Works for any module that
        declares them: temp, imu, fft, ...
    """

    if not require_connected():
        return

    if value not in SENSOR_REGISTRY:
        print(f"⚠️  Unknown -module '{value}'.")
        return

    mod = SENSOR_REGISTRY[value]
    if not hasattr(mod, "CSV_HEADER") or not hasattr(mod, "csv_rows"):
        print(f"⚠️  '{value}' does not support recording "
              f"(missing CSV_HEADER / csv_rows).")
        return

    sub = ctx.subscriptions.get(value)
    if sub is None:
        print(f"⚠️  '{value}' is not subscribed. Run `subscribe -module {value}` first.")  # noqa
        return

    if sub.get("sink") is not None:
        print(f"⚠️  Already recording '{value}' to {sub['sink'].path}")
        return

    # Auto-name if -out missing: ess_<module>_<UTC timestamp>.csv
    if not out:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = f"ess_{value}_{stamp}.csv"

    # Resolve into the measurements folder (relative names only).
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = MEASUREMENTS_DIR / out_path

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"❌ Could not create {out_path.parent}: {e}")
        return

    try:
        sink = CsvSink(out_path, mod.CSV_HEADER)
    except OSError as e:
        print(f"❌ Could not open {out_path}: {e}")
        return

    sub["sink"] = sink
    print(f"🔴 Recording '{value}' → {out_path.resolve()}")


async def cmd_stop_record(value: str) -> Any:
    """
        Detach + close the CSV sink for a subscription.
    """

    if value not in ctx.subscriptions:
        print(f"⚠️  '{value}' is not subscribed.")
        return

    sub = ctx.subscriptions[value]
    sink = sub.pop("sink", None)

    if sink is None:
        print(f"Not recording '{value}'.")
        return

    s = sink.stats()
    sink.close()

    print(
        f"⏹  Stopped recording '{value}':\n"
        f"     File    : {s['path']}\n"
        f"     Frames  : {s['frames']}\n"
        f"     Rows    : {s['rows']}\n"
        f"     Duration: {s['duration_s']:.1f} s"
    )


def _maybe_write_csv(value: str, ch_name: str, decoded) -> None:
    """
        If a sink is attached to this subscription, write the module's
        per-notification CSV rows.
    """
    sub = ctx.subscriptions.get(value)
    sink = sub.get("sink") if sub is not None else None
    if sink is None:
        return

    mod = SENSOR_REGISTRY[value]
    rows_fn = getattr(mod, "csv_rows", None)
    if rows_fn is None:
        return

    try:
        ts_iso = datetime.now(timezone.utc).isoformat()
        ts_ms = int(time.time() * 1000)
        sink.write_rows(rows_fn(ch_name, decoded, ts_iso, ts_ms))
    except (OSError, ValueError) as e:
        print(f"[{ch_name}] csv write error: {e}")


def _make_single_cb(decode_fn, plot_ref, label, series_count, value) -> Any:
    """
        Closure for channels with a single plot (temp / imu accel).
    """

    def cb(_handle, data) -> Any:
        try:
            v = decode_fn(data)

            # CSV logging — only if user called `record`
            _maybe_write_csv(value, label, v)

            if series_count > 1 and isinstance(v, (tuple, list)) \
                    and len(v) >= series_count:
                plot_ref.push(tuple(float(x) for x in v[:series_count]))

            elif isinstance(v, (int, float)):
                plot_ref.push(float(v))

            elif isinstance(v, (tuple, list)) and v:
                plot_ref.push(float(max(abs(x) for x in v)))

        except (ValueError, struct.error) as e:
            print(f"[{label}] decode error: {e}")
    return cb



# ═════════════════════════════════════════════
# configure  (schema-driven, works for any sensor)
# ═════════════════════════════════════════════
async def cmd_configure(value: str, kv: dict):
    """
        Write configuration to a sensor using its declared PARAMS schema.
        Reads current config first, merges in user-supplied kv, validates,
        writes, and reads back to confirm.
    """

    if not require_connected():
        return

    if value not in SENSOR_REGISTRY:
        print(f"⚠️  Unknown -value '{value}'. Available: {list(SENSOR_REGISTRY)}")  # noqa
        return

    mod = SENSOR_REGISTRY[value]

    if not kv:
        flags = " ".join(f"-{k} <val>" for k in mod.PARAMS)
        print(f"Usage: configure -value {value} {flags}")
        return

    # 1) Read current config (so partial updates work)
    try:
        raw = await ctx.client.read_gatt_char(mod.CONFIG_UUID)

        current = mod.decode_config(raw)

    except (BleakError, OSError, ValueError) as e:
        print(f"❌ read config failed: {e}")
        return

    # 2) Validate + merge
    params = dict(current)
    for k, v in kv.items():
        if k not in mod.PARAMS:
            print(f"⚠️  Unknown param '-{k}' for {value}. Valid: {list(mod.PARAMS)}")   # noqa
            return

        spec = mod.PARAMS[k]

        if spec["type"] == "int":
            try:
                v = int(v)
            except ValueError:
                print(f"❌ -{k} must be an integer")

                return

            if not spec["min"] <= v <= spec["max"]:
                print(f"❌ -{k} must be in [{spec['min']}, {spec['max']}]")
                return

        elif spec["type"] == "choice":
            if v not in spec["choices"]:
                print(f"❌ -{k} must be one of {spec['choices']}")

                return

        params[k] = v

    # 3) Encode + write + read-back
    try:
        payload = mod.encode_config(params)
        await ctx.client.write_gatt_char(mod.CONFIG_UUID, payload, response=False)  # noqa

        await asyncio.sleep(0.8)

        raw_rb = await ctx.client.read_gatt_char(mod.CONFIG_UUID)
        actual = mod.decode_config(raw_rb)

    except (BleakError, OSError, ValueError) as e:
        print(f"❌ write/readback failed: {e}")
        return

    ok = "✅" if actual == params else "⚠️"

    print(
        f"{ok} Configured {mod.NAME}:\n"
        f"     UUID    : {mod.CONFIG_UUID}\n"
        f"     Service : {mod.SERVICE_UUID}\n"
        f"     Wrote   : {params}   (bytes={payload.hex()})\n"
        f"     Readback: {actual}"
    )

    # 4) If currently subscribed, refresh plots' titles for THIS module
    #    and for any sibling subscription sharing the same CONFIG_UUID
    #    (e.g. fft reuses the imu service/config).
    try:
        for sub_key, sub in list(ctx.subscriptions.items()):
            sub_mod = SENSOR_REGISTRY[sub_key]

            if sub_mod.CONFIG_UUID != mod.CONFIG_UUID:
                continue

            sub["config"] = actual

            for plot_key, plot in sub["plots"].items():
                if not hasattr(plot, "set_title"):     # ← skip non-plot entries # noqa
                    continue

                # plot_key is either "ch_name" or "ch_name.out_name" (fan-out)
                if "." in plot_key:
                    ch_name, out_name = plot_key.split(".", 1)
                    base_title = sub_mod.DATA_CHANNELS[ch_name]["outputs"][out_name]["title"]   # noqa
                else:
                    ch_name = plot_key
                    base_title = sub_mod.DATA_CHANNELS[plot_key]["title"]

                label = _format_cfg_for(sub_mod, ch_name, actual)
                plot.set_title(f"{base_title}  [{label}]")

            print(f"ℹ️  Subscription '{sub_key}' refreshed — plot titles updated.")  # noqa

    except (KeyError, NameError, AttributeError, TypeError) as e:
        print(f"⚠️  Title refresh skipped (non-fatal): {type(e).__name__}: {e}")     # noqa

def _compact_ranges(ids: list[int]) -> str:
    """Collapse a sorted list of ints into 'a, b-c, d' style ranges."""
    if not ids:
        return ""
    parts = []
    start = prev = ids[0]
    for x in ids[1:]:
        if x == prev + 1:
            prev = x
            continue
        parts.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = x
    parts.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(parts)

async def cmd_update(value: str, kv: dict):
    """
    FOTA file streamer.

    Reads a .bin file from -file, splits it first into 8192-byte flash pages,
    then splits each page into fixed 224-byte BLE payload packets.

    Packet format, total 240 bytes:

        [0:2]     packet_id, uint16 LE, global packet id
        [2:4]     page_id, uint16 LE
        [4:228]   payload, 224 bytes
        [228:236] reserved, 8 bytes
        [236:240] crc32 over bytes [0:236]

    The original firmware size and original image CRC32 are sent in the
    start/config packet. Page padding is only for transport/flash-page alignment.
    """

    if not require_connected():
        return

    if value not in SENSOR_REGISTRY:
        print(f"⚠️  Unknown -value '{value}'. Available: {list(SENSOR_REGISTRY)}")
        return

    mod = SENSOR_REGISTRY[value]

    # ── Resolve file argument ────────────────────────────────────
    file_arg = kv.get("file")

    if not file_arg:
        print("❌ Missing -file argument.")
        print("Usage: update -module fota -file <firmware.bin>")
        return

    file_path = Path(file_arg)

    if not file_path.is_absolute():
        cwd_path = Path.cwd() / file_path
        parent_path = Path.cwd().parent / file_path

        if cwd_path.exists():
            file_path = cwd_path
        elif parent_path.exists():
            file_path = parent_path
        else:
            print(f"❌ File not found: {file_arg}")
            print(f"   Tried: {cwd_path}")
            print(f"   Tried: {parent_path}")
            return

    if not file_path.exists() or not file_path.is_file():
        print(f"❌ Invalid file: {file_path}")
        return

    if file_path.suffix.lower() != ".bin":
        print(f"❌ Expected a .bin file, got: {file_path.name}")
        return

    try:
        fw = file_path.read_bytes()
    except OSError as e:
        print(f"❌ Could not read file '{file_path}': {e}")
        return

    if not fw:
        print(f"❌ File is empty: {file_path}")
        return

    # ── FOTA geometry ────────────────────────────────────────────
    FLASH_PAGE_SIZE = 8192

    PAYLOAD_LEN = 14 * 16          # 224 bytes
    RESERVED_LEN = 8               # [228:236]
    PACKET_TOTAL_LEN = 240

    PACKET_ID_OFF = 0
    PAGE_ID_OFF = 2
    PAYLOAD_OFF = 4
    RESERVED_OFF = PAYLOAD_OFF + PAYLOAD_LEN      # 228
    CRC_OFF = PACKET_TOTAL_LEN - 4                # 236

    PACKETS_PER_PAGE = math.ceil(FLASH_PAGE_SIZE / PAYLOAD_LEN)

    N_PAGES = math.ceil(len(fw) / FLASH_PAGE_SIZE)
    N_PACKETS = N_PAGES * PACKETS_PER_PAGE

    padded_fw_size = N_PAGES * FLASH_PAGE_SIZE
    image_padding = padded_fw_size - len(fw)

    if N_PAGES > 0x10000:
        print(
            f"❌ Firmware too large for uint16 page IDs: "
            f"{N_PAGES} pages required."
        )
        return

    if N_PACKETS > 0x10000:
        print(
            f"❌ Firmware too large for uint16 global packet IDs: "
            f"{N_PACKETS} packets required."
        )
        return

    # ── Streaming / feedback params ──────────────────────────────
    FB_LEN = 8
    PACE_S = 0.0075
    DRAIN_S = 2.0

    # ── FOTA start header values ─────────────────────────────────
    fw_uuid = 0x12345678

    version_major = 1
    version_minor = 2
    version_patch = 3

    total_size = len(fw)
    image_crc32 = zlib.crc32(fw) & 0xFFFFFFFF

    reserved = b"\x00" * 3
    flags = 0x00
    protocol_ver = 1

    # ── ACK bookkeeping ──────────────────────────────────────────
    acked: set[int] = set()
    ack_order: list[int] = []
    bad_len = 0

    response_data = None
    response_event = asyncio.Event()

    def _compact_ranges(values: list[int]) -> str:
        if not values:
            return ""

        ranges = []
        start = prev = values[0]

        for v in values[1:]:
            if v == prev + 1:
                prev = v
                continue

            ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = v

        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")

        return ", ".join(ranges)

    def _fb_cb(_handle, data) -> None:
        nonlocal response_data
        nonlocal bad_len

        data = bytes(data)

        if len(data) < 5:
            bad_len += 1
            return

        try:
            rx_uuid = struct.unpack("<I", data[0:4])[0]
        except struct.error:
            bad_len += 1
            return

        if rx_uuid != fw_uuid:
            print("Wrong Response UUID. Skipping...")
            return

        resp_type = data[4]

        # Start/config response
        if resp_type == 0:
            if len(data) < 6:
                bad_len += 1
                return

            response_data = data[5]
            response_event.set()
            return

        # Packet ACK response
        if resp_type == 1:
            if len(data) != FB_LEN:
                bad_len += 1
                return

            ack_id = data[6] | (data[7] << 8)

            ack_order.append(ack_id)
            acked.add(ack_id)
            return

        bad_len += 1

    # ── Resolve + validate feedback characteristic ───────────────
    fb_uuid = getattr(mod, "FB_UUID", None)

    if fb_uuid is None:
        print(f"❌ '{value}' module has no FB_UUID defined.")
        return

    fb_char = ctx.client.services.get_characteristic(fb_uuid)

    if fb_char is None:
        print(f"❌ Feedback char {fb_uuid} not found in the GATT table.")
        return

    props = fb_char.properties

    if "notify" not in props and "indicate" not in props:
        print(f"❌ {fb_uuid} is not notifiable (props={props}).")
        return

    print(
        "\n📦 FOTA file selected:\n"
        f"     File              : {file_path.name}\n"
        f"     Path              : {file_path}\n"
        f"     Version           : @v{version_major}.{version_minor}.{version_patch}\n"
        f"     Original size     : {len(fw)} B\n"
        f"     Image CRC32       : 0x{image_crc32:08X}\n"
        f"     Flash page size   : {FLASH_PAGE_SIZE} B\n"
        f"     Pages             : {N_PAGES}\n"
        f"     Page padding      : {image_padding} B\n"
        f"     Payload           : {PAYLOAD_LEN} B/packet\n"
        f"     Packets/page      : {PACKETS_PER_PAGE}\n"
        f"     Total packets     : {N_PACKETS}\n"
        f"     Packet size       : {PACKET_TOTAL_LEN} B\n"
    )

    # 1) Subscribe to feedback FIRST so no early ACK is missed
    try:
        await ctx.client.start_notify(fb_uuid, _fb_cb)
    except (BleakError, OSError) as e:
        print(f"❌ Could not subscribe to feedback {fb_uuid}: {e}")
        return

    try:
        # ── Build and send start/config packet ────────────────────
        start_packet = (
            struct.pack(
                "<IBBBII",
                fw_uuid,
                version_major,
                version_minor,
                version_patch,
                total_size,
                image_crc32,
            )
            + reserved
            + bytes([flags])
            + bytes([protocol_ver])
        )

        if len(start_packet) != 20:
            print(f"❌ Internal start packet size error: {len(start_packet)} != 20")
            return

        response_event.clear()
        response_data = None

        try:
            await ctx.client.write_gatt_char(
                mod.CONFIG_UUID,
                start_packet,
                response=False,
            )
        except (BleakError, OSError, ValueError) as e:
            print(f"❌ start/config write failed: {e}")
            return

        try:
            await asyncio.wait_for(response_event.wait(), timeout=0.5)

            if response_data != 1:
                print("FOTA Rejected. Exiting...")
                return

        except asyncio.TimeoutError:
            print("Fatal: No response in 500 ms. Exiting FOTA...")
            return

        print("FOTA Accepted. Proceeding...")

        await asyncio.sleep(1)

        sent = 0
        sent_ids: set[int] = set()

        t_start = time.perf_counter()

        # ── Stream page-by-page, packet-by-packet ─────────────────
        global_packet_id = 0

        for page_id in range(N_PAGES):
            page_start = page_id * FLASH_PAGE_SIZE
            page_end = page_start + FLASH_PAGE_SIZE

            page = fw[page_start:page_end]

            # Pad final firmware page to full flash page size.
            if len(page) < FLASH_PAGE_SIZE:
                page = page.ljust(FLASH_PAGE_SIZE, b"\x00")

            if len(page) != FLASH_PAGE_SIZE:
                print(
                    f"❌ Internal page size error: "
                    f"page={page_id}, len={len(page)}"
                )
                return

            for pkt_in_page in range(PACKETS_PER_PAGE):
                payload_start = pkt_in_page * PAYLOAD_LEN
                payload_end = payload_start + PAYLOAD_LEN

                payload = page[payload_start:payload_end]

                # Last packet of every 8192-byte page is partially padded,
                # because 8192 is not divisible by 224.
                if len(payload) < PAYLOAD_LEN:
                    payload = payload.ljust(PAYLOAD_LEN, b"\x00")

                body = (
                    struct.pack("<H", global_packet_id) +  # [0:2]
                    struct.pack("<H", page_id) +           # [2:4]
                    (b"\x00" * RESERVED_LEN) +             # [4:12]
                    payload                                # [12:236]
                )

                if len(body) != CRC_OFF:
                    print(
                        f"❌ Internal body size error: "
                        f"{len(body)} != {CRC_OFF}"
                    )
                    return

                crc32 = zlib.crc32(body) & 0xFFFFFFFF
                packet = body + struct.pack("<I", crc32)

                if len(packet) != PACKET_TOTAL_LEN:
                    print(
                        f"❌ Internal packet size error: "
                        f"{len(packet)} != {PACKET_TOTAL_LEN}"
                    )
                    return

                try:
                    await ctx.client.write_gatt_char(
                        mod.DATA_UUID,
                        packet,
                        response=False,
                    )
                except (BleakError, OSError, ValueError) as e:
                    print(
                        f"❌ write failed on packet {global_packet_id} "
                        f"(page={page_id}, pkt_in_page={pkt_in_page}): {e}"
                    )
                    return

                sent_ids.add(global_packet_id)
                sent += 1
                global_packet_id += 1

                global_packet_id = global_packet_id % 37

                progress = (sent / N_PACKETS) * 100.0
                print(
                    f"\r🚀 Sending FOTA: {sent}/{N_PACKETS} packets "
                    f"({progress:5.1f}%)",
                    end="",
                    flush=True,
                )

                if PACE_S:
                    await asyncio.sleep(PACE_S)

                if pkt_in_page == 36:
                    await asyncio.sleep(0.05)

        print()

        t_sent = time.perf_counter()

        # ── Drain: give the device time to finish ACKing ──────────
        print(f"… streamed {sent} packets, draining ACKs for {DRAIN_S:.1f}s …")
        await asyncio.sleep(DRAIN_S)

        # ── Analysis ──────────────────────────────────────────────
        total_s = t_sent - t_start

        air_bytes = sent * PACKET_TOTAL_LEN
        firmware_bytes = len(fw)

        thru_kbs_air = (air_bytes / total_s) / 1024.0 if total_s else 0.0
        thru_kbs_fw = (firmware_bytes / total_s) / 1024.0 if total_s else 0.0

        missing = sorted(sent_ids - acked)
        unexpected = sorted(acked - sent_ids)
        dup_count = len(ack_order) - len(acked)

        if total_s:
            est_500kb = 500 * 1024 / (firmware_bytes / total_s)
            est_500kb_str = f"{est_500kb:.1f} s"
        else:
            est_500kb_str = "n/a"

        print(
            "\n✅ FOTA file streaming complete:\n"
            f"     File              : {file_path.name}\n"
            f"     FW size           : {firmware_bytes} B\n"
            f"     Image CRC32       : 0x{image_crc32:08X}\n"
            f"     Flash pages       : {N_PAGES}\n"
            f"     Page padding      : {image_padding} B\n"
            f"     Packets/page      : {PACKETS_PER_PAGE}\n"
            f"     Sent              : {sent}/{N_PACKETS}\n"
            f"     ACK'd unique      : {len(acked)}\n"
            f"     Duplicates        : {dup_count}\n"
            f"     Bad-length        : {bad_len}\n"
            f"     Air bytes         : {air_bytes} B\n"
            f"     Send time         : {total_s:.3f} s\n"
            f"     Throughput        : {thru_kbs_fw:.2f} KB/s firmware\n"
            f"                         {thru_kbs_air:.2f} KB/s over BLE payload\n"
            f"     Est. 500 KB       : {est_500kb_str}"
        )

        if not missing:
            print(f"     Missing           : 0  🎉 all {sent} packets ACK'd")
        else:
            print(f"     Missing           : {len(missing)} packet id(s) never ACK'd:")
            print("       " + _compact_ranges(missing))

        if unexpected:
            print(
                f"     ⚠️ Unexpected ACKs (id not sent): {unexpected[:20]}"
                f"{' …' if len(unexpected) > 20 else ''}"
            )

    finally:
        try:
            if ctx.client and ctx.client.is_connected:
                await ctx.client.stop_notify(fb_uuid)
        except (BleakError, OSError) as e:
            print(f"stop_notify warning ({fb_uuid}): {e}")

async def cmd_subscribe(value: str):
    """
        Subscribe to ALL data channels of a sensor; open one plot per channel.
    """

    if not require_connected():
        return

    if value not in SENSOR_REGISTRY:
        print(f"⚠️  Unknown -module '{value}'. Available: {list(SENSOR_REGISTRY)}") # noqa
        return

    if value in ctx.subscriptions:
        print(f"Already subscribed to '{value}'.")
        return

    mod = SENSOR_REGISTRY[value]

    # Read current config (for display + title)
    try:
        raw = await ctx.client.read_gatt_char(mod.CONFIG_UUID)
        cfg = mod.decode_config(raw)
    except (BleakError, OSError, ValueError) as e:
        print(f"❌ Failed to read config {mod.CONFIG_UUID}: {e}")
        return

    print(
        f"📥 Subscribing to {mod.NAME}:\n"
        f"     Service : {mod.SERVICE_UUID}\n"
        f"     Config  : {cfg}\n"
        f"     Channels: {list(mod.DATA_CHANNELS.keys())}"
    )

    plots: dict = {}
    subscribed_uuids: list = []

    def _make_cb(decode_fn, plot_ref, label, series_count) -> Any:
        """
            Closure factory — captures per-channel decoder/plot/series.
        """

        def cb(_handle, data) -> Any:
            """
                Private callback function helper
            """
            try:
                v = decode_fn(data)

                # Multi-series (e.g. accel xyz → 3 floats per sample)
                if series_count > 1 and isinstance(v, (tuple, list)) \
                        and len(v) >= series_count:
                    plot_ref.push(tuple(float(x) for x in v[:series_count]))

                # Single scalar (e.g. temperature)
                elif isinstance(v, (int, float)):
                    plot_ref.push(float(v))

                # Array (FFT chunk) → push max magnitude as quick viz
                elif isinstance(v, (tuple, list)) and v:
                    plot_ref.push(float(max(abs(x) for x in v)))

            except (ValueError, struct.error) as e:
                print(f"[{label}] decode error: {e}")

        return cb

    for ch_name, ch in mod.DATA_CHANNELS.items():
        if not ch.get("enabled", True):
            continue

        # ── Fan-out channel: one notify → multiple plots ──
        if "outputs" in ch:
            ch_plots: dict = {}

            for out_name, out_spec in ch["outputs"].items():
                full_title = (
                    f"{out_spec['title']}  "
                    f"[{_format_cfg_for(mod, ch_name, cfg)}]"
                )

                if out_spec["type"] == "bar":
                    p = LiveBarPlot(
                        title=full_title,
                        xlabel=out_spec.get("xlabel", ""),
                        ylabel=out_spec["ylabel"],
                    )
                else:
                    p = LivePlot(
                        title=full_title,
                        ylabel=out_spec["ylabel"],
                        window_s=30.0,
                    )

                ch_plots[out_name] = p
                plots[f"{ch_name}.{out_name}"] = p

            try:
                await ctx.client.start_notify(
                    ch["uuid"],
                    _make_fanout_cb(ch["decode"], ch_plots, ch_name, value),
                )
                subscribed_uuids.append(ch["uuid"])

            except (BleakError, OSError) as e:
                for p in ch_plots.values():
                    p.close()
                print(f"❌ start_notify failed for {ch_name}: {e}")

            continue   # done with this channel

        # ── Single-plot channel (temp / imu accel) ──
        series = ch.get("series")
        series_count = len(series) if series else 1

        plot = LivePlot(
            title=f"{ch['title']} [{_format_cfg_for(mod, ch_name, cfg)}]",
            ylabel=ch["ylabel"],
            window_s=30.0,
            series=series,
        )

        try:
            await ctx.client.start_notify(
                ch["uuid"],
                _make_single_cb(ch["decode"], plot, ch_name,
                                series_count, value),
            )
            plots[ch_name] = plot
            subscribed_uuids.append(ch["uuid"])

        except (BleakError, OSError) as e:
            plot.close()
            print(f"❌ start_notify failed for {ch_name}: {e}")

    if not subscribed_uuids:
        for p in plots.values():
            p.close()
        return

    watchdog = asyncio.create_task(_watch_plots_closure(value))

    ctx.subscriptions[value] = {
        "char_uuids": subscribed_uuids,
        "plots":      plots,
        "config":     cfg,
        "watchdog":   watchdog,
    }
    print(f"✅ Subscribed to '{value}' — {len(plots)} live plot(s) opened.")


# ═════════════════════════════════════════════
# unsubscribe
# ═════════════════════════════════════════════
async def cmd_unsubscribe(value: str):
    """
        Stop ALL notifications + close ALL plots for a sensor.
    """

    if value not in ctx.subscriptions:
        print(f"Not subscribed to '{value}'.")
        return

    sub = ctx.subscriptions.pop(value)

    wd = sub.get("watchdog")
    if wd is not None and not wd.done():
        wd.cancel()

    # ── Auto-close sink if recording was on ──
    sink = sub.pop("sink", None)
    if sink is not None:
        s = sink.stats()
        sink.close()
        print(f"⏹  Auto-stopped recording → {s['path']} "
              f"({s['rows']} rows, {s['frames']} frames)")

    for uuid in sub.get("char_uuids", []):
        try:
            if ctx.client and ctx.client.is_connected:
                await ctx.client.stop_notify(uuid)

        except (BleakError, OSError) as e:
            print(f"stop_notify warning ({uuid}): {e}")

    for plot in sub.get("plots", {}).values():
        plot.close()

    print(f"👋 Unsubscribed from '{value}'.")


async def cmd_provision(wup: str):
    if not require_connected():
        return

    PROVISION_UUID = "0000fe44-8e22-4541-9d4c-21edae82ed19"

    WUP_OPTIONS = {
        "RTC_DISABLRD": 0,
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

    packet = [1, WUP_OPTIONS[wup], 0, 0, 0, 0, 0, 0, 0, 0]

    payload = bytes(packet)

    def _cb(_handle, data):
        result = bytes(data)

        if payload == result:
            print("\n✅ Sucessfull")

    # ── Subscribe FIRST so no early frame is missed ──────────────
    try:
        await ctx.client.start_notify(PROVISION_UUID, _cb)
    except (BleakError, OSError) as e:
        print(f"❌ Could not subscribe to {PROVISION_UUID}: {e}")
        return

    await asyncio.sleep(0.5)

    print("Sensor Provisioning Requested")

    try:
        await ctx.client.write_gatt_char(
            PROVISION_UUID, payload, response=False)
    except (BleakError, OSError, ValueError) as e:
        print(f"❌ calibration write failed: {e}")
        return


async def cmd_calibrate(value: str, kv: dict):
    """
        Calibrate a sensor module.

        Subscribes to the calibration characteristic, issues the start
        write, then decodes/prints every feedback frame the firmware sends
        and reports the final result (theta on success, or the reason on
        failure).
    """

    if not require_connected():
        return

    CALIBRATION_UUID = "0000fe41-8e22-4541-9d4c-21edae82ed19"

    # ── Echo back the arguments we received ──────────────────────
    print("🛠  Calibration requested:")
    print(f"     Module  : {value}")

    packet = [0, 0, 0, 0, 0]
    if kv:
        for k, v in kv.items():
            if k == "z_offset":
                packet[1] = int(v)
            elif k == "mag_xy":
                packet[2] = int(v)
            elif k == "jitter":
                packet[3] = int(v)
            else:
                print(f"     ⚠️ ignoring unknown option -{k}")

    for k in ("z_offset", "mag_xy", "jitter"):
        if k in kv:
            print(f"     -{k:<9}: {int(kv[k])} mg")

    packet = bytes(packet)

    # ── Result bookkeeping ───────────────────────────────────────
    done = asyncio.Event()
    result = {"ok": False, "theta_deg": None, "reason": None}

    def _u16(lo, hi):
        return lo | (hi << 8)

    def _s16(lo, hi):
        u = lo | (hi << 8)
        return u - 0x10000 if u & 0x8000 else u

    def _decode(data: bytes) -> None:
        if len(data) < 5:
            print(f"[calibrate] short frame ({len(data)} B): {data.hex()}")
            return

        state = data[0]
        sub = data[1]

        # ── 0x00: start / accepted (params echoed in [1:4]) ──
        if state == 0x00:
            print(f"✅ Accepted — z_offset={data[1]} mg, "
                  f"mag_xy={data[2]} mg, jitter={data[3]} mg")

        # ── 0x01: transient condition failure (NOT terminal) ──
        elif state == 0x01:
            if sub == 0x01:
                print(f"   ⚠️ Z-axis misalignment: {_u16(data[2], data[3])} mg "
                      f"exceeds threshold")
            elif sub == 0x02:
                print(f"   ⚠️ XY-plane misalignment: {_u16(data[2], data[3])} mg "
                      f"off from 1000 mg")
            elif sub == 0x03:
                print("   ⚠️ Jitter / movement detected — hold the sensor still")
            else:
                print(f"   ⚠️ Condition error (sub=0x{sub:02X})")

        # ── 0x02: SUCCESS — theta*10 (rad) lives in bytes [1:3] ──
        elif state == 0x02:
            raw = _s16(data[1], data[2])
            theta_rad = raw / 10.0
            theta_deg = theta_rad * 57.29578
            result["ok"] = True
            result["theta_deg"] = theta_deg
            print(f"🎉 Calibration SUCCESS — theta ≈ {theta_deg:+.1f}° "
                  f"({theta_rad:+.3f} rad, raw={raw})")
            done.set()

        # ── 0x03: timeout (terminal) ──
        elif state == 0x03:
            result["reason"] = "timeout — sensor never stabilized"
            print("⏱  Calibration TIMED OUT.")
            done.set()

        else:
            print(f"[calibrate] unknown state 0x{state:02X}: {data.hex()}")

    def _cb(_handle, data):
        _decode(bytes(data))

    # ── Subscribe FIRST so no early frame is missed ──────────────
    try:
        await ctx.client.start_notify(CALIBRATION_UUID, _cb)
    except (BleakError, OSError) as e:
        print(f"❌ Could not subscribe to {CALIBRATION_UUID}: {e}")
        return

    try:
        # ── Kick off calibration ──
        try:
            await ctx.client.write_gatt_char(
                CALIBRATION_UUID, packet, response=False)
        except (BleakError, OSError, ValueError) as e:
            print(f"❌ calibration write failed: {e}")
            return

        print("👋 Sensor Calibration Requested — waiting for result…\n")

        # ── Wait for a terminal frame. Firmware timeout is 5 s
        #    (CALIBRATION_TIMEOUT), give a little headroom. ──
        try:
            await asyncio.wait_for(done.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            print("\n❌ No terminal result within 8 s "
                  "(no success/timeout frame received).")
            return

        # ── Final summary ──
        if result["ok"]:
            print(f"\n✅ Done — rotation theta = {result['theta_deg']:+.2f}°")
        else:
            print(f"\n❌ Calibration failed — reason: {result['reason']}")

    finally:
        try:
            if ctx.client and ctx.client.is_connected:
                await ctx.client.stop_notify(CALIBRATION_UUID)
        except (BleakError, OSError) as e:
            print(f"stop_notify warning ({CALIBRATION_UUID}): {e}")

# ═════════════════════════════════════════════
# Watchdog: tear down subscription when ALL plots are closed
# ═════════════════════════════════════════════
async def _watch_plots_closure(value: str):
    """
        If the user closes every plot window, auto-unsubscribe BLE notifications.   # noqa
    """

    sub = ctx.subscriptions.get(value)
    if sub is None:
        return

    plots = sub.get("plots", {})

    while any(getattr(p, "proc", None) and p.proc.is_alive()
              for p in plots.values()):
        await asyncio.sleep(0.5)
        if value not in ctx.subscriptions:
            return

    if value in ctx.subscriptions:
        print(f"\nℹ️  All plots for '{value}' closed — unsubscribing.")
        await cmd_unsubscribe(value)
