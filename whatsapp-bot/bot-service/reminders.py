import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

REMINDERS_DB = Path(__file__).parent / "reminders.db"
_WHATSAPP_URL = os.environ.get("WHATSAPP_SERVICE_URL", "http://localhost:3000")

scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{REMINDERS_DB}")},
    timezone="UTC",
)


async def fire_reminder(group_id: str, message: str, mention_jids: list, repeat_interval: str | None = None):
    if mention_jids:
        mentions_text = " ".join(f"@{jid.split('@')[0]}" for jid in mention_jids)
        text = f"⏰ {mentions_text} Reminder: {message}"
    else:
        text = f"⏰ Reminder: {message}"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            await client.post(
                f"{_WHATSAPP_URL}/send",
                json={"group_id": group_id, "text": text, "mention_jids": mention_jids},
            )
        except Exception as e:
            print(f"Failed to fire reminder: {e}")


def _repeat_trigger(repeat_interval: str):
    r = repeat_interval.lower()
    m = re.search(r"(\d+)", r)
    n = int(m.group(1)) if m else 1
    if "minute" in r:
        return IntervalTrigger(minutes=n)
    if "hour" in r:
        return IntervalTrigger(hours=n)
    if "day" in r or "daily" in r:
        return IntervalTrigger(days=n if m else 1)
    if "week" in r or "weekly" in r:
        return IntervalTrigger(weeks=n if m else 1)
    if "month" in r or "monthly" in r:
        return IntervalTrigger(days=30 * n)
    if "year" in r or "annual" in r or "yearly" in r:
        return IntervalTrigger(days=365 * n)
    return None


def add_reminder(
    group_id: str,
    message: str,
    fire_at_utc: datetime,
    mention_jids: list,
    display_tz: str,
    repeat_interval: str | None = None,
) -> str:
    trigger = _repeat_trigger(repeat_interval) if repeat_interval else DateTrigger(run_date=fire_at_utc)
    job = scheduler.add_job(
        fire_reminder,
        trigger=trigger,
        kwargs={"group_id": group_id, "message": message, "mention_jids": mention_jids, "repeat_interval": repeat_interval},
        misfire_grace_time=300,
    )
    return job.id


def list_reminders(group_id: str | None = None) -> list[dict]:
    result = []
    for job in scheduler.get_jobs():
        kw = job.kwargs
        if group_id and kw.get("group_id") != group_id:
            continue
        result.append({
            "id": job.id[:8],
            "full_id": job.id,
            "group_id": kw.get("group_id"),
            "message": kw.get("message"),
            "next_run": job.next_run_time,
            "mention_jids": kw.get("mention_jids", []),
            "repeat_interval": kw.get("repeat_interval"),
        })
    return sorted(result, key=lambda x: x["next_run"] or datetime.max.replace(tzinfo=ZoneInfo("UTC")))


def cancel_reminder(short_id: str, allowed_group_id: str | None = None) -> bool:
    for job in scheduler.get_jobs():
        if job.id.startswith(short_id):
            if allowed_group_id and job.kwargs.get("group_id") != allowed_group_id:
                return False
            job.remove()
            return True
    return False
