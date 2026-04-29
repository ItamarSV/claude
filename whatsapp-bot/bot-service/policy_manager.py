import json
import os
from pathlib import Path

POLICIES_FILE = Path(__file__).parent / "group_policies.json"
MAIN_GROUP_ID = os.environ.get("MAIN_GROUP_ID", "")


def _load() -> dict:
    if POLICIES_FILE.exists():
        return json.loads(POLICIES_FILE.read_text())
    return {}


def _save(data: dict):
    POLICIES_FILE.write_text(json.dumps(data, indent=2))


def is_main_group(group_id: str) -> bool:
    return bool(MAIN_GROUP_ID) and group_id == MAIN_GROUP_ID


def get_status(group_id: str) -> str:
    """Returns 'active', 'pending', or 'new'."""
    if is_main_group(group_id):
        return "active"
    return _load().get(group_id, {}).get("status", "new")


def set_pending(group_id: str, group_name: str):
    data = _load()
    data[group_id] = {"status": "pending"}
    data["_pending"] = {"group_id": group_id, "group_name": group_name}
    _save(data)


def get_pending() -> dict | None:
    """Returns {group_id, group_name} of the group awaiting policy, or None."""
    return _load().get("_pending")


def activate(group_id: str, mention_only: bool, listener: bool = False):
    data = _load()
    pending = data.get("_pending", {})
    group_name = pending.get("group_name", group_id) if pending.get("group_id") == group_id else group_id
    data[group_id] = {"status": "active", "mention_only": mention_only, "listener": listener, "name": group_name}
    if pending.get("group_id") == group_id:
        del data["_pending"]
    _save(data)


def is_listener(group_id: str) -> bool:
    return _load().get(group_id, {}).get("listener", False)


def get_group_name(group_id: str) -> str:
    return _load().get(group_id, {}).get("name", group_id)


def set_group_name(group_id: str, name: str):
    data = _load()
    if group_id in data:
        data[group_id]["name"] = name
        _save(data)


def get_all_active_groups() -> list[tuple[str, str]]:
    """Returns list of (group_id, display_name) for all active non-main groups."""
    data = _load()
    return [
        (gid, entry.get("name", gid))
        for gid, entry in data.items()
        if not gid.startswith("_") and entry.get("status") == "active"
    ]


def update_participant_name(group_id: str, jid: str, name: str):
    if not jid or not name:
        return
    data = _load()
    participants = data.get(group_id, {}).get("participants", [])
    for p in participants:
        if p["jid"] == jid:
            if p["name"] != name:
                p["name"] = name
                data[group_id]["participants"] = participants
                _save(data)
            return


def set_participants(group_id: str, participants: list[dict]):
    data = _load()
    if group_id in data:
        data[group_id]["participants"] = participants
        _save(data)


def get_participants(group_id: str) -> list[dict]:
    return _load().get(group_id, {}).get("participants", [])


def reset_to_new(group_id: str):
    """Called when bot is removed from a group — resets so policy is re-asked on rejoin."""
    data = _load()
    if group_id in data:
        name = data[group_id].get("name", group_id)
        data[group_id] = {"status": "new", "name": name}
        _save(data)


def is_mention_only(group_id: str) -> bool:
    return _load().get(group_id, {}).get("mention_only", False)


def new_group_message(group_name: str) -> str:
    return (
        f"I was invited to a new group: *{group_name}*\n\n"
        "What policy should I use?\n"
        "*1.* Reply only when @mentioned\n"
        "*2.* Reply to all messages\n"
        "*3.* Listener only (read silently, never reply)\n\n"
        "Reply *1*, *2*, or *3*."
    )
