"""scan / stop / list / clear commands."""

from typing import Any, Optional

from bleak import BleakScanner

from ...state import ctx, State

my_mac = "d0:39:57:37:a9:6e"                # THIS gateway's MAC
mac_bytes = bytes(int(b, 16) for b in my_mac.split(":"))[::-1]  # LSB-first


# ---------------------------------------------------------------------------
# Advertising layout (manufacturer-specific data, AFTER the 2-byte company id
# which Bleak strips into the dict key). Must match fill_advData() on the MCU:
#
#   [0] ES_ID
#   [1] FW version
#   [2] Battery %
#   [3] User data 0
#   [4] User data 1
#   [5] Device Type
#   [6] Owner tag hi   \  2-byte identity hash (FNV-1a of owner MAC)
#   [7] Owner tag lo   /
#   [8..11] measurements (live data)
# ---------------------------------------------------------------------------
ST_COMPANY_ID = 0x0030

OFF_ES_ID       = 0
OFF_FW          = 1
OFF_BATTERY     = 2
OFF_USER_0      = 3
OFF_USER_1      = 4
OFF_DEV_TYPE    = 5
OFF_TAG_HI      = 6
OFF_TAG_LO      = 7
OFF_MEAS        = 8            # 4 bytes: [8..11]
MANUF_MIN_LEN   = 8           # need at least through the owner tag

# This gateway's own identity tag. Set it to the FNV-1a hash of THIS gateway's
# MAC (same algorithm as the MCU), so we can recognise sensors we provisioned.
# You can compute it once at startup and assign ctx.my_owner_tag.
def fnv1a_16(data: bytes) -> int:
    """16-bit FNV-1a, folded — identical to the MCU's mac_hash16()."""
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return (h ^ (h >> 16)) & 0xFFFF

ctx.my_owner_tag = fnv1a_16(mac_bytes)
print(f"Gateway owner tag: 0x{ctx.my_owner_tag:04X}")

def _decode_manuf(md: dict) -> Optional[dict]:
    """Decode our manufacturer-specific payload. Returns None if not ours/format."""
    raw = md.get(ST_COMPANY_ID)
    if not raw or len(raw) < MANUF_MIN_LEN:
        return None

    payload = bytes(raw)
    tag = (payload[OFF_TAG_HI] << 8) | payload[OFF_TAG_LO]

    meas = list(payload[OFF_MEAS:OFF_MEAS + 4]) if len(payload) >= OFF_MEAS + 4 else []

    return {
        "es_id":      payload[OFF_ES_ID],
        "fw":         payload[OFF_FW],
        "battery":    payload[OFF_BATTERY],
        "user":       (payload[OFF_USER_0], payload[OFF_USER_1]),
        "dev_type":   payload[OFF_DEV_TYPE],
        "owner_tag":  tag,
        "meas":       meas,
    }


def _on_detection(device, adv):
    """BleakScanner detection callback: decode + store our sensors."""
    info = _decode_manuf(adv.manufacturer_data)
    if info is None:
        return  # not one of our ESS beacons

    my_tag = getattr(ctx, "my_owner_tag", None)
    ours = (my_tag is not None) and (info["owner_tag"] == my_tag)

    entry = ctx.devices.get(device.address, {})
    entry.update({
        "name":       adv.local_name or device.name or entry.get("name", "?"),
        "rssi":       adv.rssi,
        "seen_count": entry.get("seen_count", 0) + 1,
        # decoded advertising fields
        "es_id":      info["es_id"],
        "fw":         info["fw"],
        "battery":    info["battery"],
        "user":       info["user"],
        "dev_type":   info["dev_type"],
        "owner_tag":  info["owner_tag"],
        "meas":       info["meas"],
        "ours":       ours,
    })
    ctx.devices[device.address] = entry


async def cmd_scan() -> Any:
    """Start background scanning (mutually exclusive with CONNECTED)."""

    if ctx.state == State.CONNECTED:
        print("⚠️  Cannot scan while connected. Disconnect first.")
        return

    if ctx.state == State.SCANNING:
        print("Already scanning.")
        return

    ctx.state = State.SCANNING

    # Create + start a scanner with our detection callback.
    ctx.scanner = BleakScanner(detection_callback=_on_detection)
    await ctx.scanner.start()

    print("🔍 Scanning started in the background.")


async def cmd_stop() -> Any:
    """Stop background scanning."""

    if ctx.state == State.SCANNING:
        scanner = getattr(ctx, "scanner", None)
        if scanner is not None:
            try:
                await scanner.stop()
            except Exception as e:  # pylint: disable=broad-except
                print(f"Scanner stop error: {e}")
            ctx.scanner = None

        ctx.state = State.IDLE
        ctx.save()
        print("🛑 Scanning stopped.")
    else:
        print("Not scanning.")


async def cmd_list_devices() -> Any:
    """List known (saved) ESS devices with decoded advertising data."""

    if not ctx.devices:
        print("No known devices yet. Run `scan` first.")
        return

    # Header
    print(
        f"{'MAC':<18} {'NAME':<12} {'RSSI':>5} "
        f"{'ES':>3} {'FW':>3} {'BAT%':>5} {'TYPE':>5} "
        f"{'TAG':>6} {'OURS':>5}  {'MEAS':<12} seen"
    )

    for addr, info in ctx.devices.items():
        tag = info.get("owner_tag")
        tag_str = f"0x{tag:04X}" if isinstance(tag, int) else "?"
        ours = info.get("ours")
        ours_str = "Y" if ours else ("N" if ours is not None else "?")
        meas = info.get("meas", [])
        meas_str = " ".join(f"{b:02X}" for b in meas) if meas else "-"

        print(
            f"{addr:<18} {str(info.get('name', '?')):<12} "
            f"{info.get('rssi', '?'):>5} "
            f"{info.get('es_id', '?'):>3} "
            f"{info.get('fw', '?'):>3} "
            f"{info.get('battery', '?'):>5} "
            f"{info.get('dev_type', '?'):>5} "
            f"{tag_str:>6} {ours_str:>5}  {meas_str:<12} "
            f"{info.get('seen_count', 0)}"
        )


async def cmd_clear() -> Any:
    """Clear known devices JSON."""

    ctx.devices.clear()
    ctx.save()

    print("🧹 Cleared known devices.")
