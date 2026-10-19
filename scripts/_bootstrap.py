"""Shared setup for scripts: run them from the repo root, e.g. `python scripts/check_setup.py`."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank.airwallex import AirwallexAPI, AirwallexClient  # noqa: E402
from agentbank.config import Config  # noqa: E402


def connect() -> tuple[AirwallexClient, AirwallexAPI]:
    client = AirwallexClient(Config.from_env(ROOT / ".env"))
    return client, AirwallexAPI(client)
