"""SSH connection pool — persistent connections with auto-reconnect."""

import asyncio
import logging

import asyncssh

logger = logging.getLogger(__name__)

_connections: dict[str, asyncssh.SSHClientConnection] = {}
_conn_locks: dict[str, asyncio.Lock] = {}


class ConnectionDead(Exception):
    """Raised when a command fails due to a broken connection."""


async def get_conn(
    machine_name: str, machine_cfg: dict
) -> asyncssh.SSHClientConnection | None:
    """Return a cached SSH connection, reconnecting if needed."""
    lock = _conn_locks.setdefault(machine_name, asyncio.Lock())

    async with lock:
        conn = _connections.get(machine_name)
        if conn is not None:
            return conn

        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    machine_cfg["host"],
                    port=machine_cfg["port"],
                    username=machine_cfg["user"],
                ),
                timeout=15,
            )
            _connections[machine_name] = conn
            logger.info(f"[{machine_name}] SSH connected")
            return conn
        except Exception as e:
            logger.warning(f"[{machine_name}] SSH connection failed: {e}")
            return None


async def _close_conn(conn: asyncssh.SSHClientConnection):
    """Close a single SSH connection, ignoring errors."""
    try:
        conn.close()
        await conn.wait_closed()
    except Exception:
        pass


async def drop_conn(machine_name: str):
    """Drop a dead connection so the next poll reconnects."""
    conn = _connections.pop(machine_name, None)
    if conn:
        await _close_conn(conn)


async def run(
    conn: asyncssh.SSHClientConnection, cmd: str, timeout: float = 8
) -> str | None:
    """Run a command over an existing connection. Raises ConnectionDead on transport errors."""
    try:
        result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        if result.exit_status == 0 and isinstance(result.stdout, str):
            return result.stdout.strip()
        return None
    except (
        asyncssh.ConnectionLost,
        asyncssh.DisconnectError,
        BrokenPipeError,
        ConnectionResetError,
    ):
        raise ConnectionDead()
    except (asyncio.TimeoutError, TimeoutError):
        return None
    except Exception:
        return None


async def close_all():
    """Gracefully close all SSH connections."""
    for conn in list(_connections.values()):
        await _close_conn(conn)
    _connections.clear()
    _conn_locks.clear()
