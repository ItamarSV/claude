import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

HISTORIES_DIR = Path(__file__).parent / "group_histories"
HISTORIES_DIR.mkdir(exist_ok=True)

# One asyncio Lock per group file to prevent concurrent write corruption
_locks: dict[str, asyncio.Lock] = {}


def _safe_filename(group_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", group_id)


def _group_file(group_id: str) -> Path:
    return HISTORIES_DIR / f"{_safe_filename(group_id)}.txt"


def _get_lock(group_id: str) -> asyncio.Lock:
    if group_id not in _locks:
        _locks[group_id] = asyncio.Lock()
    return _locks[group_id]


async def append_message(group_id: str, sender: str, text: str, timestamp: str) -> None:
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    formatted = dt.strftime("%Y-%m-%d %H:%M")
    line = f"[{formatted}] {sender}: {text}\n"

    async with _get_lock(group_id):
        with open(_group_file(group_id), "a", encoding="utf-8") as f:
            f.write(line)


def read_history(group_id: str) -> str:
    path = _group_file(group_id)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_recent_history(group_id: str, hours: int = 2) -> str:
    path = _group_file(group_id)
    if not path.exists():
        return ""
    cutoff = datetime.now() - timedelta(hours=hours)
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ts = datetime.strptime(line[1:17], "%Y-%m-%d %H:%M")
            if ts >= cutoff:
                lines.append(line)
        except (ValueError, IndexError):
            continue
    return "\n".join(lines)
