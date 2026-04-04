"""Entry point: python -m dennou"""

import uvicorn

from dennou.config import load_config
from dennou.server import app


def main():
    cfg = load_config()
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="info")


if __name__ == "__main__":
    main()
