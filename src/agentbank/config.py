"""Load and sanity-check credentials from the environment or a .env file."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SANDBOX_API = "https://api.sandbox.airwallex.com/api/v1"
SANDBOX_FILES = "https://files.sandbox.airwallex.com/api/v1"


class ConfigError(RuntimeError):
    pass


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader. Existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _check_credential(name: str, value: str) -> str:
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    # A stray quote, space or label is the classic cause of an HTML 403 from the API edge.
    if re.search(r"[\s'\"]", value) or value.lower().startswith("client"):
        raise ConfigError(
            f"{name} contains whitespace, quotes or a label ({len(value)} chars). "
            "Paste the bare value only."
        )
    # Client IDs are 22 characters; a leftover piece of the "Client ID" label ("ID…") makes 24, and the API
    # edge answers any wrong-length ID with an HTML 403 instead of a JSON 401 (verified 2026-10-23).
    if name == "AWX_CLIENT_ID" and len(value) != 22:
        raise ConfigError(
            f"AWX_CLIENT_ID is {len(value)} characters, expected 22"
            f"{' (it starts with ID: a leftover of the label?)' if value.startswith('ID') else ''}. "
            "Paste the bare value only."
        )
    return value


@dataclass(frozen=True)
class Config:
    client_id: str
    api_key: str
    api_base: str = SANDBOX_API
    files_base: str = SANDBOX_FILES
    on_behalf_of: str | None = None

    @classmethod
    def from_env(cls, dotenv: str | Path = ".env") -> "Config":
        load_dotenv(dotenv)
        key = _check_credential("AWX_API_KEY", os.environ.get("AWX_API_KEY", ""))
        if not re.fullmatch(r"[0-9a-f]+", key):
            raise ConfigError("AWX_API_KEY should be lowercase hex. Did you paste the client ID instead?")
        return cls(
            client_id=_check_credential("AWX_CLIENT_ID", os.environ.get("AWX_CLIENT_ID", "")),
            api_key=key,
            on_behalf_of=os.environ.get("AWX_ON_BEHALF_OF") or None,
        )
