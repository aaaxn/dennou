"""Entry point: python -m dennou"""

import sys
import uvicorn

from .config import load_config


def main():
    try:
        cfg = load_config()
    except FileNotFoundError:
        print("Error: config.yaml not found. Copy config.yaml.example to config.yaml and edit it.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config.yaml: {e}", file=sys.stderr)
        sys.exit(1)

    from .server import app, init
    init(cfg)
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="info")


if __name__ == "__main__":
    main()
