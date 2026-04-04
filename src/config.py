"""Load and validate dennou configuration from config.yaml."""

import os
from pathlib import Path

import yaml

_DEFAULT_SETTINGS = {
    "poll_interval": 3,
    "tmux_capture_lines": 25,
    "port": 1312,
    "host": "127.0.0.1",
}


def load_config(path: str | Path | None = None) -> dict:
    """Load config.yaml and return a normalised dict."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config.yaml"
    else:
        path = Path(path)

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    machines = {}
    for name, mcfg in (raw.get("machines") or {}).items():
        mcfg = mcfg or {}
        machines[name] = {
            "host": mcfg.get("host", name),
            "user": mcfg.get("user", os.environ.get("USER", "")),
            "port": int(mcfg.get("port", 22)),
        }

    settings = {**_DEFAULT_SETTINGS, **(raw.get("settings") or {})}
    settings["port"] = int(settings["port"])
    settings["poll_interval"] = int(settings["poll_interval"])
    settings["tmux_capture_lines"] = int(settings["tmux_capture_lines"])

    return {
        "machines": machines,
        **settings,
    }
