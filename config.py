"""Project-wide constants."""

from pathlib import Path

# Advertisment packet filtering
TARGET_INDEX = 16       # Byte 17 (1-based)
TARGET_VALUE = 0x69     # Identification Value
TARGET_LENGTH = 25      # Reconstructed ADV length for ESS devices

# Persistence in Scanning
OUTPUT_FILE = Path("ess_devices.json")

# GATT Characteristic prefix search
ESS_SERVICE_PREFIX = "0000fe"

# ── Measurements output folder ───────────────
# Located inside the project (next to the ess_cli package), NOT the cwd.
# Resolves to: <project_root>/measurements/
MEASUREMENTS_DIR = Path(__file__).resolve().parent.parent / "measurements"
