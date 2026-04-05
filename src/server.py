"""dennou (電脳) — FastAPI server + WebSocket real-time loop."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

from .ssh import get_conn, drop_conn, close_all, ConnectionDead
from . import gpu, system, tmux

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logger = logging.getLogger("dennou")

# Silence verbose asyncssh per-channel logging
logging.getLogger("asyncssh").setLevel(logging.WARNING)

cfg: dict = {}


def init(config: dict):
    """Initialise module with loaded config."""
    global cfg
    cfg = config
    _load_template()
    logger.info(
        f"Monitoring {len(cfg['machines'])} machine(s): {list(cfg['machines'].keys())}"
    )


async def collect_machine(
    machine_name: str, machine_cfg: dict, tmux_lines: int = 25
) -> dict:
    """Collect all metrics from a single machine."""
    conn = await get_conn(machine_name, machine_cfg)
    if conn is None:
        return {"status": "offline"}

    try:
        gpus, sys_info, tmux_sessions = await asyncio.gather(
            gpu.collect(conn),
            system.collect(conn),
            tmux.collect(conn, tmux_lines),
        )

        cpu_pct = system.compute_cpu_percent(machine_name, sys_info)
        if cpu_pct is not None:
            sys_info["cpu_percent"] = cpu_pct

        return {
            "status": "online",
            "gpus": gpus,
            "system": sys_info,
            "tmux": tmux_sessions,
            "timestamp": time.time(),
        }

    except ConnectionDead:
        logger.warning(f"[{machine_name}] connection dead, will reconnect next cycle")
        await drop_conn(machine_name)
        return {"status": "offline"}

    except Exception as e:
        logger.error(f"[{machine_name}] collection error: {e}")
        await drop_conn(machine_name)
        return {"status": "error"}


clients: set[WebSocket] = set()
_poll_task: asyncio.Task | None = None
_has_clients = asyncio.Event()
_last_message: str = ""
_last_snapshot: str = ""


async def poll_loop():
    """Background loop: SSH-poll every machine, broadcast to WS clients."""
    global _last_message, _last_snapshot
    interval = cfg["poll_interval"]
    tmux_lines = cfg["tmux_capture_lines"]

    while True:
        await _has_clients.wait()

        machines = cfg["machines"]
        tasks = {
            name: collect_machine(name, mcfg, tmux_lines)
            for name, mcfg in machines.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        payload = {}
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                payload[name] = {"status": "error"}
            else:
                payload[name] = result

        snapshot_payload = {}
        for name, machine in payload.items():
            if "timestamp" not in machine:
                snapshot_payload[name] = machine
                continue
            snapshot_payload[name] = {
                key: value for key, value in machine.items() if key != "timestamp"
            }

        message = json.dumps({"machines": payload})
        snapshot = json.dumps({"machines": snapshot_payload})

        if snapshot == _last_snapshot:
            await asyncio.sleep(interval)
            continue

        async def _send(ws: WebSocket) -> WebSocket | None:
            try:
                await ws.send_text(message)
                return None
            except Exception:
                return ws

        _last_message = message
        _last_snapshot = snapshot

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


@asynccontextmanager
async def lifespan(app):
    yield
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
    await close_all()
    system.clear_state()
    global _last_message, _last_snapshot
    _last_message = ""
    _last_snapshot = ""


app = FastAPI(title="dennou", lifespan=lifespan)

_TEMPLATE_PATH = Path(__file__).parent.parent / "web" / "index.html"
_template_html: str = ""


def _load_template():
    global _template_html
    _template_html = _TEMPLATE_PATH.read_text()


@app.get("/")
async def index():
    if not _template_html:
        _load_template()
    return HTMLResponse(_template_html)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    _has_clients.set()
    _ensure_poll_loop()

    try:
        if _last_message:
            await websocket.send_text(_last_message)
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        clients.discard(websocket)
        if not clients:
            _has_clients.clear()
