"""Shared application state (singleton)."""

import json
from enum import Enum
from typing import Any
from bleak import BleakClient, BleakScanner

from .config import OUTPUT_FILE


class State(Enum):
    """
        High-level CLI state machine.
    """

    IDLE = "idle"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class AppCtx:
    """
        Shared state across scanner, REPL, and BLE client.
    """

    def __init__(self) -> None:
        """
            Constructor of App Context Class
        """

        self.state: State = State.IDLE
        self.devices: dict[str, dict] = {}
        self.scanner: BleakScanner | None = None
        self.client:  BleakClient | None = None
        self.connected_addr: str | None = None

        # value_key -> {char_uuid, plot, freq_hz}
        self.subscriptions: dict[str, dict] = {}

        self._load_devices()

    def _load_devices(self: Any) -> Any:
        """
            Load registered Device from <json> file.
        """

        if OUTPUT_FILE.exists():
            try:
                self.devices = json.loads(
                    OUTPUT_FILE.read_text(encoding="utf-8"))

            # pylint: disable=broad-exception-caught
            except Exception:
                self.devices = {}

    def save(self: Any) -> Any:
        """
            Saves current Devices in the <json> file.
        """

        OUTPUT_FILE.write_text(
            json.dumps(self.devices, indent=2), encoding="utf-8")

    # ── known MAC list (for autocomplete) ────
    def known_addrs(self) -> list[tuple[str, str]]:
        """
            Return [(address, display_label), ...] for autocomplete.
        """

        return [
            (addr, f"{info.get('name', '?')}  RSSI={info.get('rssi', '?')}")
            for addr, info in self.devices.items()
        ]


# Singleton imported by everything else
ctx = AppCtx()
