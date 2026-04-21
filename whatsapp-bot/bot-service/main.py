import os
import re
from asyncio import Lock
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv(Path(__file__).parent.parent / ".env")

from gemini_client import process_message, summarize_text, resolve_timezone, transcribe_audio, resolve_repeat_interval
from cost_tracker import get_monthly_summary, COST_LOGS_DIR
from history_manager import append_message, read_history_since, HISTORIES_DIR
from policy_manager import (
    is_main_group, get_status, set_pending, get_pending,
    activate, is_mention_only, is_listener, reset_to_new,
    get_group_name, set_group_name, get_all_active_groups,
    new_group_message, MAIN_GROUP_ID,
)
from reminders import scheduler, add_reminder, list_reminders, cancel_reminder as _cancel_reminder_job
from timezone_manager import (
    get_user_timezone, set_user_timezone, is_valid_tz,
    local_to_utc, utc_to_local, compute_reminder_jobs,
)

WHATSAPP_SERVICE_URL = os.environ.get("WHATSAPP_SERVICE_URL", "http://whatsapp-service:3000")

_latest_seq: dict[str, int] = {}
_seq_lock = Lock()
_awaiting_reply: set[str] = set()

# Reminder session: group_id -> session dict
_pending_reminder: dict[str, dict] = {}

YES_WORDS = {"yes", "yeah", "sure", "yep", "כן", "אוקי", "ok", "y", "yep", "sure"}
NO_WORDS  = {"no", "nope", "לא", "n", "nah", "not"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    HISTORIES_DIR.mkdir(exist_ok=True)
    COST_LOGS_DIR.mkdir(exist_ok=True)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


class IncomingMessage(BaseModel):
    group_id: str
    sender: str
    sender_jid: str = ""
    text: str = ""
    timestamp: str
    is_bot_mentioned: bool = False
    is_reply_to_bot: bool = False
    audio_data: str | None = None
    audio_mime: str | None = None


class GroupJoined(BaseModel):
    group_id: str
    group_name: str


async def _send(group_id: str, text: str, buttons: list | None = None, mention_jids: list | None = None):
    payload = {"group_id": group_id, "text": text}
    if buttons:
        payload["buttons"] = buttons
    if mention_jids:
        payload["mention_jids"] = mention_jids
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f"{WHATSAPP_SERVICE_URL}/send", json=payload)
            r.raise_for_status()
        except Exception as e:
            print(f"Failed to send message: {e}")


async def _fetch_participants(group_id: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{WHATSAPP_SERVICE_URL}/group-participants", params={"group_id": group_id})
            if r.status_code == 200:
                return r.json().get("participants", [])
    except Exception:
        pass
    return []


async def _fetch_and_cache_group_name(group_id: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{WHATSAPP_SERVICE_URL}/group-name", params={"group_id": group_id})
            if r.status_code == 200:
                name = r.json().get("name")
                if name and name != group_id:
                    set_group_name(group_id, name)
    except Exception:
        pass


def _is_yes(text: str) -> bool:
    t = text.strip().lower()
    return t in YES_WORDS or any(t.startswith(w) for w in YES_WORDS)


def _is_no(text: str) -> bool:
    t = text.strip().lower()
    return t in NO_WORDS or any(t.startswith(w) for w in NO_WORDS)


_DAY_WORDS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
              "mon", "tue", "wed", "thu", "fri", "sat", "sun",
              "weekday", "weekdays", "weekend", "weekends"}

def _extract_repeat_interval(text: str) -> str | None:
    t = text.lower()
    m = re.search(r"every\s+(\d+)\s+minute", t)
    if m:
        return f"every {m.group(1)} minutes"
    m = re.search(r"every\s+(\d+)\s+hour", t)
    if m:
        return f"every {m.group(1)} hours"
    if re.search(r"every\s+day|daily|כל יום", t):
        return "daily"
    if re.search(r"every\s+week|weekly|כל שבוע|שבועי", t):
        return "weekly"
    if re.search(r"every\s+month|monthly|כל חודש|חודשי", t):
        return "monthly"
    if re.search(r"every\s+year|yearly|annually|כל שנה|שנתי", t):
        return "yearly"
    # Pass through day-of-week phrases to Gemini resolver
    words = set(re.split(r'[\s,/&]+', t))
    if words & _DAY_WORDS:
        return text.strip()
    return None


async def _schedule_reminder_jobs(group_id: str, session: dict) -> list[dict]:
    fire_at_naive = datetime.fromisoformat(session["iso_time"])
    setter_jid = session["created_by_jid"]
    participants = await _fetch_participants(group_id)

    if participants:
        jobs = compute_reminder_jobs(participants, fire_at_naive, setter_jid)
    else:
        tz = get_user_timezone(setter_jid)
        jobs = [{"fire_at_utc": local_to_utc(fire_at_naive, tz), "mention_jids": [], "display_tz": tz}]

    scheduled = []
    repeat = session.get("repeat_interval")
    if repeat == "ask":
        repeat = None

    repeat_spec = None
    if repeat:
        try:
            repeat_spec = await resolve_repeat_interval(repeat)
        except Exception:
            pass

    for job in jobs:
        job_id = add_reminder(
            group_id=group_id,
            message=session["message"],
            fire_at_utc=job["fire_at_utc"],
            mention_jids=job["mention_jids"],
            display_tz=job["display_tz"],
            repeat_interval=repeat,
            repeat_spec=repeat_spec,
        )
        scheduled.append({**job, "job_id": job_id})

    return scheduled


def _format_fire_time(fire_at_utc: datetime, tz: str) -> str:
    local = utc_to_local(fire_at_utc, tz)
    return local.strftime("%a %b %d %H:%M")


class GroupLeft(BaseModel):
    group_id: str


@app.post("/group-left")
async def group_left(body: GroupLeft):
    name = get_group_name(body.group_id)
    reset_to_new(body.group_id)
    if MAIN_GROUP_ID:
        await _send(MAIN_GROUP_ID, f"⚠️ I was removed from *{name}*.")
    return {"ok": True}


@app.post("/group-joined")
async def group_joined(body: GroupJoined):
    if is_main_group(body.group_id) or not MAIN_GROUP_ID:
        return {"ok": True}
    if get_status(body.group_id) != "new":
        return {"ok": True}
    set_pending(body.group_id, body.group_name)
    await _send(MAIN_GROUP_ID, new_group_message(body.group_name))
    return {"ok": True}


@app.post("/webhook")
async def webhook(msg: IncomingMessage):
    async with _seq_lock:
        seq = _latest_seq.get(msg.group_id, 0) + 1
        _latest_seq[msg.group_id] = seq

    # Transcribe audio before anything else so history and processing use the text
    if msg.audio_data and not msg.text:
        try:
            transcription = await transcribe_audio(
                msg.group_id, msg.audio_data, msg.audio_mime or "audio/ogg; codecs=opus"
            )
            msg = msg.model_copy(update={"text": f"[voice] {transcription}"})
        except Exception as e:
            print(f"Audio transcription failed: {e}")
            msg = msg.model_copy(update={"text": "[voice message]"})

    await append_message(msg.group_id, msg.sender, msg.text, msg.timestamp)

    if is_main_group(msg.group_id):
        pending = get_pending()
        if pending and msg.text.strip() in ("1", "2", "3"):
            choice = msg.text.strip()
            mention_only = choice == "1"
            listener = choice == "3"
            activate(pending["group_id"], mention_only=mention_only, listener=listener)
            label = "@mention only" if mention_only else ("listener only" if listener else "all messages")
            await _send(MAIN_GROUP_ID, f"Policy set for *{pending['group_name']}*: {label} ✅")
            return {"ok": True}
    else:
        status = get_status(msg.group_id)

        if status == "new":
            if MAIN_GROUP_ID:
                name = get_group_name(msg.group_id)
                set_pending(msg.group_id, name)
                await _send(MAIN_GROUP_ID, new_group_message(name))
            return {"ok": True}

        if status == "pending":
            return {"ok": True}

        if get_group_name(msg.group_id) == msg.group_id:
            await _fetch_and_cache_group_name(msg.group_id)

        # Listener mode — save history but never reply
        if is_listener(msg.group_id):
            return {"ok": True}

        awaiting = msg.group_id in _awaiting_reply
        if awaiting:
            _awaiting_reply.discard(msg.group_id)
        if is_mention_only(msg.group_id) and not msg.is_bot_mentioned and not msg.is_reply_to_bot and not awaiting:
            return {"ok": True}

    # ── Reminder session handler ──────────────────────────────────────────────
    if msg.group_id in _pending_reminder:
        session = _pending_reminder[msg.group_id]
        reply = None

        if session["state"] == "awaiting_confirm":
            if _is_yes(msg.text):
                scheduled = await _schedule_reminder_jobs(msg.group_id, session)
                session["scheduled_jobs"] = scheduled
                display_tz = get_user_timezone(session["created_by_jid"])
                fire_str = _format_fire_time(scheduled[0]["fire_at_utc"], display_tz)

                if session.get("repeat_interval") == "ask":
                    session["state"] = "awaiting_repeat"
                    _awaiting_reply.add(msg.group_id)
                    reply = f"✅ Reminder set for {fire_str}. Should this repeat? If yes, say how often (e.g. weekly, daily, every Monday)"
                elif session.get("repeat_interval"):
                    _pending_reminder.pop(msg.group_id, None)
                    reply = f"✅ Reminder set for {fire_str}, repeating {session['repeat_interval']}"
                else:
                    _pending_reminder.pop(msg.group_id, None)
                    reply = f"✅ Reminder set for {fire_str}"

            elif _is_no(msg.text):
                _pending_reminder.pop(msg.group_id, None)
                reply = "Got it, no reminder."
            else:
                _pending_reminder.pop(msg.group_id, None)

        elif session["state"] == "awaiting_repeat":
            if _is_no(msg.text):
                _pending_reminder.pop(msg.group_id, None)
                reply = "Got it, reminder without repeat."
            else:
                interval = _extract_repeat_interval(msg.text)
                if interval:
                    # Cancel one-time jobs and re-add with repeat
                    for j in session.get("scheduled_jobs", []):
                        _cancel_reminder_job(j["job_id"][:8], allowed_group_id=None)
                    repeat_spec = None
                    try:
                        repeat_spec = await resolve_repeat_interval(interval)
                    except Exception:
                        pass
                    fire_at_naive = datetime.fromisoformat(session["iso_time"])
                    setter_jid = session["created_by_jid"]
                    participants = await _fetch_participants(msg.group_id)
                    if participants:
                        jobs = compute_reminder_jobs(participants, fire_at_naive, setter_jid)
                    else:
                        tz = get_user_timezone(setter_jid)
                        jobs = [{"fire_at_utc": local_to_utc(fire_at_naive, tz), "mention_jids": [], "display_tz": tz}]
                    for job in jobs:
                        add_reminder(
                            group_id=msg.group_id,
                            message=session["message"],
                            fire_at_utc=job["fire_at_utc"],
                            mention_jids=job["mention_jids"],
                            display_tz=job["display_tz"],
                            repeat_interval=interval,
                            repeat_spec=repeat_spec,
                        )
                    _pending_reminder.pop(msg.group_id, None)
                    reply = f"✅ Repeating {interval}"
                elif _is_yes(msg.text):
                    _awaiting_reply.add(msg.group_id)
                    reply = "How often? (e.g. daily, weekly, every Monday, monthly)"
                else:
                    _pending_reminder.pop(msg.group_id, None)
                    reply = "Got it, reminder without repeat."

        if reply:
            if _latest_seq.get(msg.group_id) != seq:
                return {"ok": True}
            await _send(msg.group_id, reply)
            await append_message(msg.group_id, "Bot", reply, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

    # ── /usage command (Main group only) ──────────────────────────────────────
    if is_main_group(msg.group_id) and msg.text.strip().lower().startswith("/usage"):
        now = datetime.now(timezone.utc)
        s = get_monthly_summary(now.year, now.month)
        lines = [f"📊 *Gemini usage — {now.strftime('%B %Y')}*\n"]
        lines.append(f"Total calls: {s['total_calls']}  |  Tokens: {s['total_tokens']:,}  |  Cost: ${s['total_cost']:.4f}\n")
        if s["by_group"]:
            lines.append("*Per group:*")
            for gid, g in sorted(s["by_group"].items(), key=lambda x: -x[1]["cost"]):
                if is_main_group(gid):
                    name = "Main"
                else:
                    name = get_group_name(gid)
                    if name == gid:
                        await _fetch_and_cache_group_name(gid)
                        name = get_group_name(gid)
                lines.append(f"• {name}: {g['calls']} calls, {g['tokens']:,} tokens, ${g['cost']:.4f}")
        await _send(msg.group_id, "\n".join(lines))
        return {"ok": True}

    # ── /summarize command ────────────────────────────────────────────────────
    if msg.text.strip().lower().startswith("/summarize"):
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
        if is_main_group(msg.group_id):
            parts = []
            for gid, name in get_all_active_groups():
                chunk = read_history_since(gid, today_start)
                if chunk:
                    parts.append(f"=== {name} ===\n{chunk}")
            combined = "\n\n".join(parts) if parts else None
            if not combined:
                reply = "No activity in any group today."
            else:
                reply = await summarize_text(msg.group_id, f"Summarize today's activity across all groups:\n\n{combined}")
        else:
            chunk = read_history_since(msg.group_id, today_start)
            if not chunk:
                reply = "No conversation recorded today yet."
            else:
                reply = await summarize_text(msg.group_id, f"Summarize today's conversation in this group:\n\n{chunk}")
        if _latest_seq.get(msg.group_id) != seq:
            return {"ok": True}
        await _send(msg.group_id, reply)
        await append_message(msg.group_id, "Bot", reply, datetime.now(timezone.utc).isoformat())
        return {"ok": True}

    # ── /reminders command ────────────────────────────────────────────────────
    if msg.text.strip().lower().startswith("/reminders"):
        parts = msg.text.strip().split()
        subcommand = parts[1].lower() if len(parts) > 1 else ""

        if subcommand == "cancel" and len(parts) > 2:
            short_id = parts[2].lstrip("#")
            allowed = None if is_main_group(msg.group_id) else msg.group_id
            if _cancel_reminder_job(short_id, allowed_group_id=allowed):
                reply = f"✅ Reminder #{short_id} cancelled."
            else:
                reply = f"Couldn't find reminder #{short_id} for this group."
        else:
            # List reminders
            group_filter = None if is_main_group(msg.group_id) else msg.group_id
            jobs = list_reminders(group_filter)
            if not jobs:
                reply = "No pending reminders."
            else:
                lines = ["⏰ *Pending reminders*\n"]
                current_group = None
                for j in jobs:
                    gid = j["group_id"]
                    if is_main_group(msg.group_id) and gid != current_group:
                        current_group = gid
                        display_name = "Main" if is_main_group(gid) else get_group_name(gid)
                        lines.append(f"\n_*{display_name}*_")
                    tz = get_user_timezone(msg.sender_jid) or "Asia/Jerusalem"
                    fire_str = _format_fire_time(j["next_run"], tz) if j["next_run"] else "unknown"
                    mention_str = f" (@{', @'.join(jid.split('@')[0] for jid in j['mention_jids'])})" if j["mention_jids"] else ""
                    repeat_str = f" 🔁 {j['repeat_interval']}" if j.get("repeat_interval") else ""
                    lines.append(f"• #{j['id']} | {fire_str}{mention_str}{repeat_str} — {j['message']}")
                reply = "\n".join(lines)

        if _latest_seq.get(msg.group_id) != seq:
            return {"ok": True}
        await _send(msg.group_id, reply)
        return {"ok": True}

    # ── Gemini ────────────────────────────────────────────────────────────────
    pending = list_reminders(msg.group_id)
    reminders_context = ""
    if pending:
        lines = []
        for j in pending:
            repeat = f" 🔁 {j['repeat_interval']}" if j.get("repeat_interval") else ""
            fire_str = _format_fire_time(j["next_run"], get_user_timezone(msg.sender_jid)) if j["next_run"] else "unknown"
            lines.append(f"#{j['id']} | {fire_str}{repeat} — {j['message']}")
        reminders_context = "\n".join(lines)

    try:
        reply = await process_message(msg.group_id, msg.sender, msg.text, msg.sender_jid, reminders_context)
    except Exception as e:
        print(f"Gemini error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if _latest_seq.get(msg.group_id) != seq:
        return {"ok": True}

    # Handle special reply types from Gemini
    if isinstance(reply, dict):
        rtype = reply.get("type")

        if rtype == "set_reminder":
            _pending_reminder[msg.group_id] = {
                "message": reply["message"],
                "iso_time": reply["iso_time"],
                "repeat_interval": reply.get("repeat_interval"),
                "created_by": msg.sender,
                "created_by_jid": msg.sender_jid,
                "state": "awaiting_confirm",
            }
            _awaiting_reply.add(msg.group_id)
            tz = get_user_timezone(msg.sender_jid)
            try:
                fire_naive = datetime.fromisoformat(reply["iso_time"])
                fire_str = fire_naive.strftime("%a %b %d at %H:%M")
            except Exception:
                fire_str = reply["iso_time"]
            confirm_text = f"Should I set a reminder for {fire_str} — {reply['message']}? (yes/no)"
            await _send(msg.group_id, confirm_text)
            await append_message(msg.group_id, "Bot", confirm_text, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

        if rtype == "update_timezone":
            raw_tz = reply.get("timezone", "")
            resolved = await resolve_timezone(raw_tz)
            if resolved and is_valid_tz(resolved):
                set_user_timezone(msg.sender_jid, resolved)
                confirm_text = f"✅ Your timezone is now {resolved}. You'll be @mentioned individually in groups where others have a different timezone."
            else:
                confirm_text = f"Sorry, I couldn't recognize '{raw_tz}' as a timezone. Try something like 'London', 'Tel Aviv', or 'New York'."
            await _send(msg.group_id, confirm_text)
            await append_message(msg.group_id, "Bot", confirm_text, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

        if rtype == "cancel_reminder":
            rid = reply.get("reminder_id", "").lstrip("#")
            allowed = None if is_main_group(msg.group_id) else msg.group_id
            if rid and _cancel_reminder_job(rid, allowed_group_id=allowed):
                confirm_text = f"✅ Reminder #{rid} cancelled."
            else:
                confirm_text = f"I couldn't find that reminder. Use /reminders to see what's active."
            await _send(msg.group_id, confirm_text)
            await append_message(msg.group_id, "Bot", confirm_text, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

        # Web search / other dicts
        _awaiting_reply.add(msg.group_id)
        await _send(msg.group_id, reply["text"], reply.get("buttons"))
        await append_message(msg.group_id, "Bot", reply["text"], datetime.now(timezone.utc).isoformat())
    else:
        await _send(msg.group_id, reply)
        await append_message(msg.group_id, "Bot", reply, datetime.now(timezone.utc).isoformat())

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
