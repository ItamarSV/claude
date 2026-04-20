import json
from pathlib import Path

POLICIES_FILE = Path(__file__).parent / "group_policies.json"

SETUP_MESSAGE = (
    "I've been added to this group!\n\n"
    "Please set my response policy:\n"
    "*1.* Reply only when @mentioned\n"
    "*2.* Reply to all messages\n\n"
    "Reply *1* or *2*. I won't respond to other messages until this is set."
)


def _load() -> dict:
    if POLICIES_FILE.exists():
        return json.loads(POLICIES_FILE.read_text())
    return {}


def _save(data: dict):
    POLICIES_FILE.write_text(json.dumps(data, indent=2))


def get_status(group_id: str) -> str:
    """Returns 'active', 'pending', or 'new'."""
    return _load().get(group_id, {}).get("status", "new")


def set_pending(group_id: str):
    data = _load()
    data[group_id] = {"status": "pending"}
    _save(data)


def activate(group_id: str, mention_only: bool):
    data = _load()
    data[group_id] = {"status": "active", "mention_only": mention_only}
    _save(data)


def is_mention_only(group_id: str) -> bool:
    return _load().get(group_id, {}).get("mention_only", False)
