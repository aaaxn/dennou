#!/usr/bin/env python3
"""
dennou (電脳) — SSH-based GPU + tmux monitoring dashboard.

Run on your local machine. Connects to remote machines via SSH.
Zero installation on remote hosts.

Usage:
    uv run python app.py
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

from core.config import load_config
from core.collectors import collect_machine, close_all

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logger = logging.getLogger("dennou")

# Silence verbose asyncssh per-channel logging
logging.getLogger("asyncssh").setLevel(logging.WARNING)

# ── Config ───────────────────────────────────────────────────────────────────

cfg = load_config()
logger.info(f"Monitoring {len(cfg['machines'])} machine(s): {list(cfg['machines'].keys())}")

# ── WebSocket real-time loop ─────────────────────────────────────────────────

clients: set[WebSocket] = set()
_poll_task: asyncio.Task | None = None
_has_clients = asyncio.Event()


async def poll_loop():
    """Background loop: SSH-poll every machine, broadcast to WS clients."""
    interval = cfg["poll_interval"]
    tmux_lines = cfg["tmux_capture_lines"]

    while True:
        await _has_clients.wait()

        # Collect from all machines concurrently
        machines = cfg["machines"]
        tasks = {
            name: collect_machine(name, mcfg, tmux_lines)
            for name, mcfg in machines.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        payload = {}
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                payload[name] = {"status": "error", "error": str(result)}
            else:
                payload[name] = result

        message = json.dumps({"machines": payload})

        # Broadcast concurrently, collect dead clients
        async def _send(ws: WebSocket) -> WebSocket | None:
            try:
                await ws.send_text(message)
                return None
            except Exception:
                return ws

        dead_results = await asyncio.gather(*[_send(ws) for ws in clients])
        dead = {ws for ws in dead_results if ws is not None}
        clients.difference_update(dead)

        await asyncio.sleep(interval)


def _ensure_poll_loop():
    """Start poll_loop if not already running."""
    global _poll_task
    if _poll_task is not None and not _poll_task.done():
        return
    _poll_task = asyncio.create_task(poll_loop())
    _poll_task.add_done_callback(_on_poll_done)


def _on_poll_done(task: asyncio.Task):
    """Log if poll_loop crashes, reset so it can be restarted."""
    global _poll_task
    _poll_task = None
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"poll_loop crashed: {exc}", exc_info=exc)


# ── FastAPI ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app):
    yield
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
    await close_all()


app = FastAPI(title="dennou", lifespan=lifespan)

TEMPLATE = Path(__file__).parent / "index.html"


@app.get("/")
async def index():
    return HTMLResponse(TEMPLATE.read_text())


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    _has_clients.set()
    _ensure_poll_loop()

    try:
        while True:
            await websocket.receive_text()  # keep-alive
    except Exception:
        pass
    finally:
        clients.discard(websocket)
        if not clients:
            _has_clients.clear()


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="info")
