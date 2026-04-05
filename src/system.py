"""System metrics collector — CPU, RAM, load average, uptime."""

from .ssh import run

_SYS_CMD = r"""
echo "---LOADAVG---"; cat /proc/loadavg 2>/dev/null;
echo "---MEMINFO---"; free -b 2>/dev/null | head -3;
echo "---CPU---"; nproc 2>/dev/null;
echo "---UPTIME---"; uptime -s 2>/dev/null;
echo "---CPUPCT---"; grep 'cpu ' /proc/stat 2>/dev/null;
"""

# Previous cpu_stat per machine for delta calculation
_prev_cpu_stat: dict[str, list[int]] = {}


async def collect(conn) -> dict:
    """Collect system metrics in a single SSH command."""
    raw = await run(conn, _SYS_CMD)
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

    # Memory (from 'free -b')
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

    # CPU usage — raw /proc/stat
    cpu_stat = lines_by_section.get("CPUPCT", [])
    if cpu_stat:
        parts = cpu_stat[0].split()
        if len(parts) >= 8:
            try:
                vals = [int(p) for p in parts[1:8]]
                info["_cpu_stat"] = vals
            except ValueError:
                pass

    return info


def compute_cpu_percent(machine_name: str, sys_info: dict) -> float | None:
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

    idle = delta[3]
    return round((1 - idle / total) * 100, 1)


def clear_state():
    """Clear cached CPU stat deltas."""
    _prev_cpu_stat.clear()
