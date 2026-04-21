import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIMEZONE_FILE = Path(__file__).parent / "user_timezones.json"
DEFAULT_TZ = "Asia/Jerusalem"


def _load() -> dict:
    if TIMEZONE_FILE.exists():
        return json.loads(TIMEZONE_FILE.read_text())
    return {}


def _save(data: dict):
    TIMEZONE_FILE.write_text(json.dumps(data, indent=2))


def get_user_timezone(jid: str) -> str:
    return _load().get(jid, DEFAULT_TZ)


def set_user_timezone(jid: str, tz: str):
    data = _load()
    data[jid] = tz
    _save(data)


def is_valid_tz(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, KeyError):
        return False


def local_to_utc(naive_dt: datetime, tz: str) -> datetime:
    return naive_dt.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))


def utc_to_local(utc_dt: datetime, tz: str) -> datetime:
    return utc_dt.astimezone(ZoneInfo(tz))


def compute_reminder_jobs(
    participants: list[dict],
    fire_at_naive: datetime,
    setter_jid: str,
) -> list[dict]:
    """
    Returns list of jobs: [{fire_at_utc, mention_jids, display_tz}]
    - Participants sharing setter's timezone → one group message (no mentions)
    - Participants with different timezone → @mention at their local clock time
    """
    setter_tz = get_user_timezone(setter_jid)

    tz_groups: dict[str, list] = {}
    for p in participants:
        tz = get_user_timezone(p["jid"])
        tz_groups.setdefault(tz, []).append(p)

    jobs = []
    for tz, members in tz_groups.items():
        fire_utc = local_to_utc(fire_at_naive, tz)
        jids = [m["jid"] for m in members]
        jobs.append({
            "fire_at_utc": fire_utc,
            "mention_jids": [] if tz == setter_tz else jids,
            "display_tz": tz,
        })

    return jobs
