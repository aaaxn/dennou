"""Load and validate dennou configuration from config.yaml."""

import os
from pathlib import Path

import yaml

_DEFAULT_SETTINGS = {
    "poll_interval": 3,
    "tmux_capture_lines": 25,
    "port": 1312,
    "host": "0.0.0.0",
}


def load_config(path: str | Path | None = None) -> dict:
    """Load config.yaml and return a normalised dict."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config.yaml"
    else:
        path = Path(path)

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # --- machines ---
    machines = {}
    for name, mcfg in (raw.get("machines") or {}).items():
        mcfg = mcfg or {}
        machines[name] = {
            "host": mcfg.get("host", name),
            "user": mcfg.get("user", os.environ.get("USER", "")),
            "port": int(mcfg.get("port", 22)),
        }

    # --- settings (merged with defaults) ---
    settings = {**_DEFAULT_SETTINGS, **(raw.get("settings") or {})}

    return {
        "machines": machines,
        **settings,
    }
