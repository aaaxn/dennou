"""SSH-based collector — gathers GPU, system, and tmux metrics from remote machines."""

import asyncio
import asyncssh
import logging
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSH connection pool
# ---------------------------------------------------------------------------

_connections: dict[str, asyncssh.SSHClientConnection] = {}
_conn_locks: dict[str, asyncio.Lock] = {}


async def _get_conn(machine_name: str, machine_cfg: dict) -> asyncssh.SSHClientConnection | None:
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
                    known_hosts=None,
                ),
                timeout=15,
            )
            _connections[machine_name] = conn
            logger.info(f"[{machine_name}] SSH connected")
            return conn
        except Exception as e:
            logger.warning(f"[{machine_name}] SSH connection failed: {e}")
            return None


def _drop_conn(machine_name: str):
    """Drop a dead connection so the next poll reconnects."""
    conn = _connections.pop(machine_name, None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass


class _ConnectionDead(Exception):
    """Raised when a command fails due to a broken connection."""


async def _run(conn: asyncssh.SSHClientConnection, cmd: str, timeout: float = 8) -> str | None:
    """Run a command over an existing connection. Raises _ConnectionDead on transport errors."""
    try:
        result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        if result.exit_status == 0:
            return result.stdout.strip()
        return None
    except (asyncssh.ConnectionLost, asyncssh.DisconnectError, BrokenPipeError, ConnectionResetError):
        raise _ConnectionDead()
    except (asyncio.TimeoutError, TimeoutError):
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GPU metrics  (nvidia-smi)
# ---------------------------------------------------------------------------

_GPU_QUERY = (
    "index,name,temperature.gpu,utilization.gpu,utilization.memory,"
    "memory.used,memory.total,power.draw,power.limit,fan.speed,"
    "clocks.current.graphics,clocks.current.memory,"
    "pcie.link.gen.current,pcie.link.width.current"
)


def _parse_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


async def _collect_gpus(conn) -> list[dict]:
    raw = await _run(
        conn,
        f"nvidia-smi --query-gpu={_GPU_QUERY} --format=csv,noheader,nounits 2>/dev/null",
    )
    if not raw:
        return []

    gpus = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 14:
            continue

        idx = _parse_int(parts[0])
        gpus.append({
            "index": idx if idx is not None else 0,
            "name": parts[1],
            "temperature": _parse_float(parts[2]),
            "utilization": _parse_float(parts[3]),
            "memory_util": _parse_float(parts[4]),
            "memory_used": _parse_float(parts[5]),
            "memory_total": _parse_float(parts[6]),
            "power_draw": _parse_float(parts[7]),
            "power_limit": _parse_float(parts[8]),
            "fan_speed": _parse_float(parts[9]),
            "clock_graphics": _parse_float(parts[10]),
            "clock_memory": _parse_float(parts[11]),
            "pcie_gen": parts[12].strip(),
            "pcie_width": parts[13].strip(),
        })

    return gpus


# ---------------------------------------------------------------------------
# System metrics  (cpu, ram, load, uptime)
# ---------------------------------------------------------------------------

# We grab everything in ONE ssh command to reduce round-trips.
_SYS_CMD = r"""
echo "---LOADAVG---"; cat /proc/loadavg 2>/dev/null;
echo "---MEMINFO---"; free -b 2>/dev/null | head -3;
echo "---CPU---"; nproc 2>/dev/null;
echo "---UPTIME---"; uptime -s 2>/dev/null;
echo "---CPUPCT---"; grep 'cpu ' /proc/stat 2>/dev/null;
"""


async def _collect_system(conn) -> dict:
    raw = await _run(conn, _SYS_CMD)
    if not raw:
        return {}

    info: dict = {}
    section = None
    lines_by_section: dict[str, list[str]] = {}

    for line in raw.splitlines():
        if line.startswith("---") and line.endswith("---"):
            section = line.strip("-")
            lines_by_section[section] = []
        elif section:
            lines_by_section.setdefault(section, []).append(line)

    # Load average
    la = lines_by_section.get("LOADAVG", [])
    if la:
        parts = la[0].split()
        try:
            info["load_1"] = float(parts[0])
            info["load_5"] = float(parts[1])
            info["load_15"] = float(parts[2])
        except (IndexError, ValueError):
            pass

    # Memory (from 'free -b') — use 'available' (parts[6]) when present
    mem_lines = lines_by_section.get("MEMINFO", [])
    for ml in mem_lines:
        if ml.startswith("Mem:"):
            parts = ml.split()
            try:
                total = int(parts[1])
                used = int(parts[2])
                available = int(parts[6]) if len(parts) > 6 else None
                info["mem_total"] = total
                info["mem_used"] = total - available if available else used
                info["mem_free"] = int(parts[3])
                if total > 0:
                    info["mem_percent"] = round(info["mem_used"] / total * 100, 1)
            except (IndexError, ValueError):
                pass
        elif ml.startswith("Swap:"):
            parts = ml.split()
            try:
                info["swap_total"] = int(parts[1])
                info["swap_used"] = int(parts[2])
            except (IndexError, ValueError):
                pass

    # CPU count
    cpu_lines = lines_by_section.get("CPU", [])
    if cpu_lines:
        try:
            info["cpu_count"] = int(cpu_lines[0].strip())
        except ValueError:
            pass

    # Uptime
    up_lines = lines_by_section.get("UPTIME", [])
    if up_lines:
        info["uptime_since"] = up_lines[0].strip()

    # CPU usage — raw /proc/stat (we compute % from delta on next poll)
    cpu_stat = lines_by_section.get("CPUPCT", [])
    if cpu_stat:
        parts = cpu_stat[0].split()
        if len(parts) >= 8:
            try:
                vals = [int(p) for p in parts[1:8]]
                info["_cpu_stat"] = vals  # user, nice, system, idle, iowait, irq, softirq
            except ValueError:
                pass

    return info


# Keep previous cpu_stat per machine for delta calculation
_prev_cpu_stat: dict[str, list[int]] = {}


def _compute_cpu_percent(machine_name: str, sys_info: dict) -> float | None:
    """Compute CPU usage % from /proc/stat deltas."""
    cur = sys_info.pop("_cpu_stat", None)
    if cur is None:
        return None

    prev = _prev_cpu_stat.get(machine_name)
    _prev_cpu_stat[machine_name] = cur

    if prev is None:
        return None

    delta = [c - p for c, p in zip(cur, prev)]
    total = sum(delta)
    if total == 0:
        return 0.0

    idle = delta[3]  # idle is the 4th field
    return round((1 - idle / total) * 100, 1)


# ---------------------------------------------------------------------------
# Tmux sessions
# ---------------------------------------------------------------------------

_TMUX_BATCH_CMD = r"""
tmux list-sessions -F '#{session_name}	#{session_attached}	#{session_windows}' 2>/dev/null || exit 0
echo '---TMUX_WINDOWS---'
tmux list-windows -a -F '#{session_name}	#{window_index}	#{window_name}	#{window_active}	#{pane_current_command}	#{pane_current_path}' 2>/dev/null || true
echo '---TMUX_PANES---'
for target in $(tmux list-windows -a -F '#{session_name}:#{window_index}' 2>/dev/null); do
  echo "===PANE:${target}==="
  tmux capture-pane -t "$target" -p -S -__LINES__ 2>/dev/null || true
done
"""


async def _collect_tmux(conn, capture_lines: int = 25) -> list[dict]:
    cmd = _TMUX_BATCH_CMD.replace("__LINES__", str(capture_lines))
    raw = await _run(conn, cmd, timeout=15)
    if not raw:
        return []

    # Split into sections
    sections = raw.split("---TMUX_WINDOWS---")
    if len(sections) < 2:
        return []
    session_block = sections[0]
    rest = sections[1].split("---TMUX_PANES---")
    window_block = rest[0] if rest else ""
    pane_block = rest[1] if len(rest) > 1 else ""

    # Parse sessions
    session_map: dict[str, dict] = {}
    for line in session_block.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        session_map[parts[0]] = {
            "name": parts[0],
            "attached": parts[1] == "1",
            "window_count": int(parts[2]) if parts[2].isdigit() else 0,
            "windows": [],
        }

    # Parse windows
    window_keys: dict[str, dict] = {}  # "session:idx" -> window dict
    for line in window_block.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        sess_name, w_idx, w_name, w_active, w_cmd, w_path = parts[:6]
        win = {
            "index": w_idx,
            "name": w_name,
            "active": w_active == "1",
            "command": w_cmd,
            "path": w_path,
            "content": "",
        }
        window_keys[f"{sess_name}:{w_idx}"] = win
        if sess_name in session_map:
            session_map[sess_name]["windows"].append(win)

    # Parse pane captures — handle both "===\n" and "===" at end of output
    for chunk in pane_block.split("===PANE:"):
        if not chunk.strip():
            continue
        header_end = chunk.find("===")
        if header_end == -1:
            continue
        target = chunk[:header_end]
        # Skip past "===" and optional newline
        content_start = header_end + 3
        if content_start < len(chunk) and chunk[content_start] == "\n":
            content_start += 1
        content = chunk[content_start:]
        # Strip leading/trailing blank lines
        clines = content.rstrip("\n").split("\n")
        while clines and not clines[0].strip():
            clines.pop(0)
        content = "\n".join(clines)
        if target in window_keys:
            window_keys[target]["content"] = content

    return list(session_map.values())


# ---------------------------------------------------------------------------
# Public API — collect everything for one machine
# ---------------------------------------------------------------------------

async def collect_machine(machine_name: str, machine_cfg: dict, tmux_lines: int = 25) -> dict:
    """Collect all metrics from a single machine. Returns dict or offline marker."""
    conn = await _get_conn(machine_name, machine_cfg)
    if conn is None:
        return {"status": "offline"}

    try:
        # Run GPU + system + tmux concurrently
        gpus, sys_info, tmux_sessions = await asyncio.gather(
            _collect_gpus(conn),
            _collect_system(conn),
            _collect_tmux(conn, tmux_lines),
        )

        # Compute CPU %
        cpu_pct = _compute_cpu_percent(machine_name, sys_info)
        if cpu_pct is not None:
            sys_info["cpu_percent"] = cpu_pct

        return {
            "status": "online",
            "gpus": gpus,
            "system": sys_info,
            "tmux": tmux_sessions,
            "timestamp": time.time(),
        }

    except _ConnectionDead:
        logger.warning(f"[{machine_name}] connection dead, will reconnect next cycle")
        _drop_conn(machine_name)
        return {"status": "offline"}

    except Exception as e:
        logger.error(f"[{machine_name}] collection error: {e}")
        _drop_conn(machine_name)
        return {"status": "error", "error": str(e)}


async def close_all():
    """Gracefully close all SSH connections."""
    for name, conn in list(_connections.items()):
        try:
            conn.close()
        except Exception:
            pass
    _connections.clear()
    _conn_locks.clear()
    _prev_cpu_stat.clear()
