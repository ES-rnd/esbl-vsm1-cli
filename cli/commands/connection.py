"""connect / disconnect commands."""

import asyncio

from typing import Any
from bleak import BleakClient

from ...state import ctx, State
from .sensors import cmd_unsubscribe

from .base import require_connected


async def cmd_connect(mac: str):
    """
        Connect to a known device by MAC address.
    """

    if ctx.state == State.CONNECTED:
        print(f"Already connected to {ctx.connected_addr}.")

        return

    if mac not in ctx.devices:
        print(f"⚠️  {mac} not in known devices. Run `scan` first.")

        return

    # Mutual exclusion: stop scanning before connecting
    prev_state = ctx.state
    ctx.state = State.CONNECTING

    print(f"🔗 Connecting to {mac} ...")

    await asyncio.sleep(0.6)   # let scanner_loop tear down

    try:
        ctx.client = BleakClient(mac)

        await ctx.client.connect()

        if ctx.client.is_connected:
            ctx.connected_addr = mac
            ctx.state = State.CONNECTED
            print(f"✅ Connected to {mac}")
        else:
            ctx.state = prev_state
            print("❌ Connection failed.")

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
