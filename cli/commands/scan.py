"""scan / stop / list / clear commands."""

from typing import Any
from ...state import ctx, State


async def cmd_scan() -> Any:
    """
        Start background scanning (mutually exclusive with CONNECTED).
    """

    if ctx.state == State.CONNECTED:
        print("⚠️  Cannot scan while connected. Disconnect first.")

        return

    ctx.state = State.SCANNING

    print("🔍 Scanning started in the background.")


async def cmd_stop() -> Any:
    """
        Stop background scanning.
    """

    if ctx.state == State.SCANNING:
        ctx.state = State.IDLE

        print("🛑 Scanning stopped.")

    else:
        print("Not scanning.")


async def cmd_list_devices() -> Any:
    """
        List known (saved) ESS devices.
    """

    if not ctx.devices:
        print("No known devices yet. Run `scan` first.")

        return

    print(f"{'MAC':<20} {'NAME':<20} {'RSSI':>6}  seen")

    for addr, info in ctx.devices.items():
        print(
            f"{addr:<20} {str(info.get('name', '?')):<20} "
            f"{info.get('rssi', '?'):>6}  {info.get('seen_count', 0)}"
        )


async def cmd_clear() -> Any:
    """
        Clear known devices JSON.
    """

    ctx.devices.clear()
    ctx.save()

    print("🧹 Cleared known devices.")
