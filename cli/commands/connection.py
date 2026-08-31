"""connect / disconnect commands."""

import asyncio

from typing import Any
from bleak import BleakClient

from ...state import ctx, State
from .sensors import cmd_unsubscribe

from .base import require_connected

from winrt.windows.devices.bluetooth import BluetoothLEDevice
from winrt.windows.devices.enumeration import (
    DevicePairingKinds,
    DevicePairingResultStatus,
)

FIXED_PIN = "111111"


def _on_pairing_requested(sender, args):
    kind = args.pairing_kind
    if kind == DevicePairingKinds.PROVIDE_PIN:
        deferral = args.get_deferral()
        print(f"🔑 Providing fixed PIN: {FIXED_PIN}")
        if hasattr(args, "accept_with_pin"):
            args.accept_with_pin(FIXED_PIN)
        else:
            args.accept(FIXED_PIN)        # older/some builds project it here
        deferral.complete()
    elif kind in (DevicePairingKinds.CONFIRM_ONLY,
                  DevicePairingKinds.CONFIRM_PIN_MATCH):
        args.accept()


async def _pair_fixed_pin(mac: str) -> bool:
    """Resolve the WinRT device and bond with the fixed PIN.
    Assumes Windows already knows the device (i.e. after connecting)."""
    addr = int(mac.replace(":", "").replace("-", ""), 16)
    device = await BluetoothLEDevice.from_bluetooth_address_async(addr)
    if device is None:
        print("❌ Could not resolve device for pairing.")
        return False

    pairing = device.device_information.pairing
    if pairing.is_paired:
        device.close()
        print("🔐 Already bonded.")
        return True

    custom = pairing.custom
    token = custom.add_pairing_requested(_on_pairing_requested)
    try:
        result = await custom.pair_async(DevicePairingKinds.PROVIDE_PIN)
    finally:
        custom.remove_pairing_requested(token)
        device.close()

    ok = result.status in (
        DevicePairingResultStatus.PAIRED,
        DevicePairingResultStatus.ALREADY_PAIRED,
    )
    print("🔐 Paired." if ok else f"❌ Pairing failed: {result.status.name}")
    return ok

async def cmd_connect(mac: str):
    """Connect to a known device, then bond with fixed PIN."""

    if ctx.state == State.CONNECTED:
        print(f"Already connected to {ctx.connected_addr}.")
        return

    if mac not in ctx.devices:
        print(f"⚠️  {mac} not in known devices. Run `scan` first.")
        return

    prev_state = ctx.state
    ctx.state = State.CONNECTING

    print(f"🔗 Connecting to {mac} ...")
    await asyncio.sleep(0.6)

    def _on_unexpected_disconnect(client: BleakClient) -> None:
        if ctx.state != State.CONNECTED:
            return
        print(f"\n⚠️  Lost connection to {ctx.connected_addr} (timeout/reset).")
        ctx.client = None
        ctx.connected_addr = None
        ctx.subscriptions.clear()
        ctx.state = State.IDLE
        print("Renewed state → IDLE. Reconnect with `connect <mac>`.")

    try:
        # --- Step 1: connect (this makes Windows cache the device) ---
        ctx.client = BleakClient(
            mac, disconnected_callback=_on_unexpected_disconnect)
        await ctx.client.connect()

        if not ctx.client.is_connected:
            ctx.state = prev_state
            print("❌ Connection failed.")
            return

        # # # --- Step 2: now bond with the fixed PIN (device is resolvable now) ---
        # print(f"🔐 Pairing with fixed PIN ({FIXED_PIN})...")
        # if not await _pair_fixed_pin(mac):
        #     print("⚠️  Pairing failed — link is connected but not bonded.")
        #     # keep the connection or drop it, your call:
        #     # await ctx.client.disconnect(); ctx.state = prev_state; return

        ctx.connected_addr = mac
        ctx.state = State.CONNECTED
        print(f"✅ Connected to {mac}")

    # pylint: disable=broad-exception-caught
    except Exception as e:
        ctx.state = prev_state
        print(f"❌ Connection error: {e}")
        print("Consider Triple tap the Device to activate it...")


async def cmd_disconnect() -> Any:
    """
        Disconnect current device + close all subscriptions.
    """

    if not require_connected():
        return

    if ctx.state != State.CONNECTED:
        print("Not connected.")

        return

    for value in list(ctx.subscriptions.keys()):
        await cmd_unsubscribe(value)

    try:
        await ctx.client.disconnect()

    # pylint: disable=broad-exception-caught
    except Exception as e:
        print(f"Disconnect error: {e}")

    ctx.client = None
    ctx.connected_addr = None
    ctx.state = State.IDLE

    print("👋 Disconnected.")
