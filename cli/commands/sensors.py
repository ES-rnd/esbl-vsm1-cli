"""subscribe / unsubscribe / configure commands (schema-driven)."""

import asyncio
import struct
from typing import Any
import time
from datetime import datetime, timezone
from pathlib import Path
import struct, zlib

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

async def cmd_update(value: str, kv: dict):
    """
        One-way FOTA benchmark: stream N packets on DATA_UUID back-to-back
        WITHOUT waiting per packet. A notification callback records every
        ACK'd packet id. After streaming (plus a drain window) it reports
        total time/throughput and prints any packet ids that never ACK'd.
    """

    if not require_connected():
        return

    if value not in SENSOR_REGISTRY:
        print(f"⚠️  Unknown -value '{value}'. Available: {list(SENSOR_REGISTRY)}")  # noqa
        return

    mod = SENSOR_REGISTRY[value]

    # ── Packet geometry ──────────────────────────────────────────
    RESERVED    = b"\x00" * 10     # 10 reserved zero bytes
    PAYLOAD_LEN = 14 * 16          # 224 bytes
    N_PACKETS   = 1700

    # ── Streaming / feedback params ──────────────────────────────
    FB_LEN        = 8              # expected feedback length (bytes)
    PACE_S        = 0.0075         # inter-packet pacing (set 0.0 for max rate)
    DRAIN_S       = 2.0            # wait after last send for stragglers
    loop          = asyncio.get_running_loop()

    # ── ACK bookkeeping (populated by the notification callback) ──
    acked: set[int]      = set()   # unique ids that ACK'd
    ack_order: list[int] = []      # every ACK, in arrival order (to spot dupes)
    bad_len              = 0       # ACKs with unexpected length

    def _fb_cb(_handle, data) -> None:
        nonlocal bad_len
        if len(data) < 3:
            bad_len += 1
            return
        if len(data) != FB_LEN:
            bad_len += 1
            # still parse the id — length mismatch is informational
        ack_id = data[1] + (data[2] << 8)
        ack_order.append(ack_id)
        acked.add(ack_id)

    # ── Resolve + validate feedback characteristic BEFORE subscribing ──
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

    # 1) Subscribe to feedback FIRST so no early ACK is missed
    try:
        await ctx.client.start_notify(fb_uuid, _fb_cb)
    except (BleakError, OSError) as e:
        print(f"❌ Could not subscribe to feedback {fb_uuid}: {e}")
        return

    sent      = 0
    sent_ids  = set()
    t_start   = time.perf_counter()

    try:
        # ── Stream all packets, no per-packet wait ──
        for i in range(N_PACKETS):
            payload = bytes((1 + j) & 0xFF for j in range(PAYLOAD_LEN))
            body = (
                struct.pack("<H", i) +      # [0:2]  packet id (LE)
                RESERVED +                  # [2:12] 10 reserved zeros
                payload                     # [12:236] 224-byte payload
            )
            crc32  = zlib.crc32(body) & 0xFFFFFFFF
            packet = body + struct.pack("<I", crc32)

            try:
                await ctx.client.write_gatt_char(
                    mod.DATA_UUID, packet, response=False)
            except (BleakError, OSError, ValueError) as e:
                print(f"❌ write failed on packet {i}: {e}")
                return

            sent_ids.add(i)
            sent += 1

            if PACE_S:
                await asyncio.sleep(PACE_S)

        t_sent = time.perf_counter()

        # ── Drain: give the device time to finish ACKing the last packets ──
        print(f"… streamed {sent} packets, draining ACKs for {DRAIN_S:.1f}s …")
        await asyncio.sleep(DRAIN_S)

        # ── Analysis ──
        total_s     = t_sent - t_start
        total_bytes = sent * (PAYLOAD_LEN + 16)          # +16 = 2 id +10 rsvd +4 crc
        thru_kbs    = (total_bytes / total_s) / 1024.0 if total_s else 0.0

        missing   = sorted(sent_ids - acked)             # sent but never ACK'd
        unexpected = sorted(acked - sent_ids)            # ACK'd but never sent (!)
        dup_count = len(ack_order) - len(acked)          # repeated ACKs

        print(
            "\n✅ FOTA one-way streaming complete:\n"
            f"     Sent        : {sent}/{N_PACKETS}\n"
            f"     ACK'd unique: {len(acked)}\n"
            f"     Duplicates  : {dup_count}\n"
            f"     Bad-length  : {bad_len}\n"
            f"     Bytes total : {total_bytes} B\n"
            f"     Send time   : {total_s:.3f} s\n"
            f"     Throughput  : {thru_kbs:.2f} KB/s\n"
            f"     Est. 500 KB : "
            f"{500 * 1024 / (total_bytes / total_s):.1f} s" if total_s else "n/a"
        )

        # ── Missing report ──
        if not missing:
            print(f"     Missing     : 0  🎉 all {sent} packets ACK'd")
        else:
            print(f"     Missing     : {len(missing)} packet id(s) never ACK'd:")
            # print compactly as ranges (e.g. 12, 40-43, 2195)
            print("       " + _compact_ranges(missing))

        if unexpected:
            print(f"     ⚠️ Unexpected ACKs (id not sent): {unexpected[:20]}"
                  f"{' …' if len(unexpected) > 20 else ''}")

    finally:
        try:
            if ctx.client and ctx.client.is_connected:
                await ctx.client.stop_notify(fb_uuid)
        except (BleakError, OSError) as e:
            print(f"stop_notify warning ({fb_uuid}): {e}")


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

# subscribe  (multi-channel)
# ═════════════════════════════════════════════
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
