"""Tmux session collector — sessions, windows, and pane content."""

import secrets

from dennou.ssh import run

_TMUX_BATCH_CMD = r"""
tmux list-sessions -F '#{{session_name}}	#{{session_attached}}	#{{session_windows}}' 2>/dev/null || exit 0
echo '{win_sep}'
tmux list-windows -a -F '#{{session_name}}	#{{window_index}}	#{{window_name}}	#{{window_active}}	#{{pane_current_command}}	#{{pane_current_path}}' 2>/dev/null || true
echo '{pane_sep}'
for target in $(tmux list-windows -a -F '#{{session_name}}:#{{window_index}}' 2>/dev/null); do
  echo "{pane_prefix}${{target}}{pane_suffix}"
  tmux capture-pane -t "$target" -p -S -__LINES__ 2>/dev/null || true
done
"""


async def collect(conn, capture_lines: int = 25) -> list[dict]:
    """Collect all tmux sessions, windows, and pane content."""
    token = secrets.token_hex(8)
    win_sep = f"---TMUX_WINDOWS_{token}---"
    pane_sep = f"---TMUX_PANES_{token}---"
    pane_prefix = f"===PANE_{token}:"
    pane_suffix = f"==="
    cmd = _TMUX_BATCH_CMD.format(
        win_sep=win_sep,
        pane_sep=pane_sep,
        pane_prefix=pane_prefix,
        pane_suffix=pane_suffix,
    ).replace("__LINES__", str(capture_lines))
    raw = await run(conn, cmd, timeout=15)
    if not raw:
        return []

    # Split into sections
    sections = raw.split(win_sep)
    if len(sections) < 2:
        return []
    session_block = sections[0]
    rest = sections[1].split(pane_sep)
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
    window_keys: dict[str, dict] = {}
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

    # Parse pane captures
    for chunk in pane_block.split(pane_prefix):
        if not chunk.strip():
            continue
        header_end = chunk.find(pane_suffix)
        if header_end == -1:
            continue
        target = chunk[:header_end]
        content_start = header_end + len(pane_suffix)
        if content_start < len(chunk) and chunk[content_start] == "\n":
            content_start += 1
        content = chunk[content_start:]
        clines = content.rstrip("\n").split("\n")
        while clines and not clines[0].strip():
            clines.pop(0)
        content = "\n".join(clines)
        if target in window_keys:
            window_keys[target]["content"] = content

    return list(session_map.values())
