"""GPU metrics collector via nvidia-smi."""

from dennou.ssh import run

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


async def collect(conn) -> list[dict]:
    """Query nvidia-smi and return a list of GPU metric dicts."""
    raw = await run(
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
