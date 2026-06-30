"""Background continuous BLE scanner."""

import asyncio
from datetime import datetime, timezone
from typing import Any

from bleak import BleakScanner

from ..state import ctx, State
from .adv import build_raw_adv, is_ess_device


async def scanner_loop(stop: asyncio.Event):
    """
        Run continuous BLE scanning while ctx.state == SCANNING.
        Pauses automatically when state changes (e.g. CONNECTED).
    """

    def cb(device: Any, adv: Any) -> Any:
        """
            Private Callback Function registering Devices
        """

        try:
            if ctx.state != State.SCANNING:
                return
            if not is_ess_device(adv):
                return

            raw = build_raw_adv(adv)
            now = datetime.now(timezone.utc).isoformat()

            entry = ctx.devices.get(
                device.address, {"first_seen": now, "seen_count": 0})

            entry.update({
                "name":       device.name or adv.local_name,
                "address":    device.address,
                "rssi":       adv.rssi,
                "raw_adv":    raw.hex(),
                "last_seen":  now,
                "seen_count": entry.get("seen_count", 0) + 1,
            })

            ctx.devices[device.address] = entry

        except (IndexError, ValueError, AttributeError, TypeError) as e:
            # Never let scan-callback errors escape into asyncio's loop
            # (they trigger prompt_toolkit's "Press ENTER" loop)
            print(f"[scanner cb] ignored {type(e).__name__}: {e}")

    while not stop.is_set():
        if ctx.state == State.SCANNING:
            try:
                if ctx.scanner is None:
                    ctx.scanner = BleakScanner(cb)
                    await ctx.scanner.start()

            # pylint: disable=broad-exception-caught
            except Exception as e:
                print(f"[scanner] error: {e}")
                await asyncio.sleep(1)
        else:
            if ctx.scanner is not None:
                try:
                    await ctx.scanner.stop()

                # pylint: disable=broad-exception-caught
                except Exception:
                    pass
                ctx.scanner = None

        await asyncio.sleep(0.5)
        ctx.save()
