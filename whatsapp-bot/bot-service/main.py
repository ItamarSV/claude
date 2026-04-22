import asyncio
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv(Path(__file__).parent.parent / ".env")

from gemini_client import (
    process_message, summarize_text, resolve_timezone, transcribe_audio,
    resolve_repeat_interval, handle_session_message, generate_timeout_message,
    generate_action_message, web_search_call,
)
from cost_tracker import get_monthly_summary, COST_LOGS_DIR
from history_manager import append_message, read_history_since, read_recent_history, HISTORIES_DIR
from policy_manager import (
    is_main_group, get_status, set_pending, get_pending,
    activate, is_mention_only, is_listener, reset_to_new,
    get_group_name, set_group_name, get_all_active_groups,
    new_group_message, MAIN_GROUP_ID,
)
from reminders import scheduler, add_reminder, list_reminders, cancel_reminder as _cancel_reminder_job
from session_manager import session_manager, DialogSession, SESSION_TIMEOUT
from timezone_manager import (
    get_user_timezone, set_user_timezone, is_valid_tz,
    local_to_utc, utc_to_local, compute_reminder_jobs,
)

WHATSAPP_SERVICE_URL = os.environ.get("WHATSAPP_SERVICE_URL", "http://whatsapp-service:3000")

_latest_seq: dict[str, int] = {}
_seq_lock = asyncio.Lock()


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


class GroupLeft(BaseModel):
    group_id: str


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _start_typing(group_id: str):
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            await client.post(f"{WHATSAPP_SERVICE_URL}/typing", json={"group_id": group_id})
        except Exception:
            pass


async def _send(group_id: str, text: str, mention_jids: list | None = None):
    payload = {"group_id": group_id, "text": text}
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
    YES = {"yes", "yeah", "sure", "yep", "כן", "אוקי", "ok", "y", "nah", "yep"}
    return t in YES or any(t.startswith(w + " ") for w in YES)


def _format_fire_time(fire_at_utc: datetime, tz: str) -> str:
    local = utc_to_local(fire_at_utc, tz)
    return local.strftime("%a %b %d %H:%M")


# ── Session infrastructure ────────────────────────────────────────────────────

async def _session_timeout(group_id: str, user_jid: str):
    """Fires after SESSION_TIMEOUT seconds — closes session and notifies user."""
    await asyncio.sleep(SESSION_TIMEOUT)
    async with session_manager.lock(group_id, user_jid):
        session = session_manager.close_to_ghost(group_id, user_jid)
    if session:
        msg_text = await generate_timeout_message(session.type, session.data, session.user_name)
        await _send(group_id, msg_text)
        await append_message(group_id, "Bot", msg_text, datetime.now(timezone.utc).isoformat())


async def _open_session(session: DialogSession) -> bool:
    """Open a session and start its timeout task. Returns False if user already has one."""
    opened = session_manager.open(session)
    if opened:
        task = asyncio.create_task(_session_timeout(session.group_id, session.user_jid))
        session.timeout_task = task
    return opened


async def _execute_session(session: DialogSession, user_text: str, interval: str | None = None) -> str:
    """Execute the action a session was waiting to perform."""
    if session.type == "web_search":
        search_context = session.data.get("search_context") or session.data.get("original_message", user_text)
        return await web_search_call(session.group_id, search_context)

    if session.type == "reminder_repeat":
        if not interval:
            return None     # Gemini's "cancel" reply is already natural — use that
        repeat_spec = None
        try:
            repeat_spec = await resolve_repeat_interval(interval)
        except Exception:
            pass
        for job_id in session.data.get("scheduled_jobs", []):
            _cancel_reminder_job(job_id[:8], allowed_group_id=None)
        fire_at_naive = datetime.fromisoformat(session.data["iso_time"])
        setter_jid = session.data["created_by_jid"]
        participants = await _fetch_participants(session.group_id)
        if participants:
            jobs = compute_reminder_jobs(participants, fire_at_naive, setter_jid)
        else:
            tz = get_user_timezone(setter_jid)
            jobs = [{"fire_at_utc": local_to_utc(fire_at_naive, tz), "mention_jids": [], "display_tz": tz}]
        for job in jobs:
            add_reminder(
                group_id=session.group_id,
                message=session.data["message"],
                fire_at_utc=job["fire_at_utc"],
                mention_jids=job["mention_jids"],
                display_tz=job["display_tz"],
                repeat_interval=interval,
                repeat_spec=repeat_spec,
            )
        return None     # Gemini's "proceed" reply is already natural — use that

    return None


async def _do_schedule_reminder(
    group_id: str,
    message: str,
    iso_time: str,
    created_by_jid: str,
    repeat_interval: str | None,
) -> list[dict]:
    """Schedule reminder jobs (possibly with repeat). Returns list of scheduled job dicts."""
    fire_at_naive = datetime.fromisoformat(iso_time)
    participants = await _fetch_participants(group_id)
    if participants:
        jobs = compute_reminder_jobs(participants, fire_at_naive, created_by_jid)
    else:
        tz = get_user_timezone(created_by_jid)
        jobs = [{"fire_at_utc": local_to_utc(fire_at_naive, tz), "mention_jids": [], "display_tz": tz}]

    repeat = None if (not repeat_interval or repeat_interval == "ask") else repeat_interval
    repeat_spec = None
    if repeat:
        try:
            repeat_spec = await resolve_repeat_interval(repeat)
        except Exception:
            pass

    scheduled = []
    for job in jobs:
        job_id = add_reminder(
            group_id=group_id,
            message=message,
            fire_at_utc=job["fire_at_utc"],
            mention_jids=job["mention_jids"],
            display_tz=job["display_tz"],
            repeat_interval=repeat,
            repeat_spec=repeat_spec,
        )
        scheduled.append({**job, "job_id": job_id})
    return scheduled


# ── Lifecycle endpoints ───────────────────────────────────────────────────────

@app.post("/bot-online")
async def bot_online():
    return {"ok": True}


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


# ── Main webhook ──────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(msg: IncomingMessage):
    async with _seq_lock:
        seq = _latest_seq.get(msg.group_id, 0) + 1
        _latest_seq[msg.group_id] = seq

    # Transcribe audio before anything else
    if msg.audio_data and not msg.text:
        await _start_typing(msg.group_id)
        try:
            transcription = await transcribe_audio(
                msg.group_id, msg.audio_data, msg.audio_mime or "audio/ogg; codecs=opus"
            )
            msg = msg.model_copy(update={"text": f"[voice] {transcription}"})
        except Exception as e:
            print(f"Audio transcription failed: {e}")
            msg = msg.model_copy(update={"text": "[voice message]"})

    await append_message(msg.group_id, msg.sender, msg.text, msg.timestamp)

    # ── Policy checks ─────────────────────────────────────────────────────────
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
        if is_listener(msg.group_id):
            return {"ok": True}

        # Session owners bypass mention-only filter; others must mention the bot
        has_session = bool(msg.sender_jid and session_manager.get(msg.group_id, msg.sender_jid))
        if is_mention_only(msg.group_id) and not msg.is_bot_mentioned and not msg.is_reply_to_bot and not has_session:
            return {"ok": True}

    # ── Session routing ───────────────────────────────────────────────────────
    if msg.sender_jid:
        # Snapshot session state without holding lock during async work
        async with session_manager.lock(msg.group_id, msg.sender_jid):
            session = session_manager.get(msg.group_id, msg.sender_jid)
            ghost = None if session else session_manager.get_ghost(msg.group_id, msg.sender_jid)

        # Active session — slash commands bypass it, everything else goes through
        if session and not msg.text.strip().startswith("/"):
            recent = read_recent_history(msg.group_id, hours=2)
            await _start_typing(msg.group_id)
            try:
                result = await handle_session_message(
                    session.type, session.question, session.data, msg.text, recent
                )
            except Exception as e:
                print(f"Session handler error: {e}")
                result = {"action": "ignore", "reply": "Sorry, something went wrong."}

            action = result.get("action", "ignore")
            reply = result.get("reply", "")

            # Lock to check session is still valid (could have timed out during Gemini call)
            async with session_manager.lock(msg.group_id, msg.sender_jid):
                current = session_manager.get(msg.group_id, msg.sender_jid)
                if not current or current.session_id != session.session_id:
                    action = "ignore"   # Session expired while we were waiting
                elif action in ("proceed", "cancel"):
                    session_manager.close(msg.group_id, msg.sender_jid)

            if action == "proceed":
                execute_result = await _execute_session(session, msg.text, result.get("interval"))
                # web_search returns the actual search content — use it
                # reminder_repeat returns None — keep Gemini's natural reply
                if execute_result is not None:
                    reply = execute_result

            if reply and _latest_seq.get(msg.group_id) == seq:
                await _send(msg.group_id, reply)
                await append_message(msg.group_id, "Bot", reply, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

        # Ghost revival — user approved within 2 minutes of timeout
        if ghost and _is_yes(msg.text):
            async with session_manager.lock(msg.group_id, msg.sender_jid):
                new_session = session_manager.revive_ghost(msg.group_id, msg.sender_jid)
            if new_session:
                task = asyncio.create_task(_session_timeout(msg.group_id, msg.sender_jid))
                new_session.timeout_task = task
                if _latest_seq.get(msg.group_id) == seq:
                    execute_result = await _execute_session(new_session, msg.text)
                    if execute_result is not None:
                        await _send(msg.group_id, execute_result)
                        await append_message(msg.group_id, "Bot", execute_result, datetime.now(timezone.utc).isoformat())
                return {"ok": True}

    # ── /usage command ────────────────────────────────────────────────────────
    if is_main_group(msg.group_id) and msg.text.strip().lower().startswith("/usage"):
        now = datetime.now(timezone.utc)
        s = get_monthly_summary(now.year, now.month)
        lines = [f"📊 *Gemini usage — {now.strftime('%B %Y')}*\n"]
        lines.append(f"Total calls: {s['total_calls']}  |  Tokens: {s['total_tokens']:,}  |  Cost: ${s['total_cost']:.4f}\n")
        if s["by_group"]:
            lines.append("*Per group:*")
            for gid, g in sorted(s["by_group"].items(), key=lambda x: -x[1]["cost"]):
                name = "Main" if is_main_group(gid) else get_group_name(gid)
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
                await _start_typing(msg.group_id)
                reply = await summarize_text(msg.group_id, f"Summarize today's activity across all groups:\n\n{combined}")
        else:
            chunk = read_history_since(msg.group_id, today_start)
            if not chunk:
                reply = "No conversation recorded today yet."
            else:
                await _start_typing(msg.group_id)
                reply = await summarize_text(msg.group_id, f"Summarize today's conversation in this group:\n\n{chunk}")
        if _latest_seq.get(msg.group_id) == seq:
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

        if _latest_seq.get(msg.group_id) == seq:
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

    await _start_typing(msg.group_id)
    try:
        reply = await process_message(msg.group_id, msg.sender, msg.text, msg.sender_jid, reminders_context)
    except Exception as e:
        print(f"Gemini error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if _latest_seq.get(msg.group_id) != seq:
        return {"ok": True}

    # Handle structured replies from Gemini
    if isinstance(reply, dict):
        rtype = reply.get("type")

        # ── Web search → open session ─────────────────────────────────────────
        if rtype == "web_search":
            question = reply["question"]
            session = DialogSession(
                session_id=str(uuid.uuid4()),
                group_id=msg.group_id,
                user_jid=msg.sender_jid,
                user_name=msg.sender,
                type="web_search",
                question=question,
                data={"original_message": reply["original_message"]},
            )
            opened = await _open_session(session)
            if not opened:
                question = await generate_action_message("session_already_open", {
                    "action_description": f"search the web for: {reply.get('original_message', 'something')}"
                })
            await _send(msg.group_id, question)
            await append_message(msg.group_id, "Bot", question, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

        # ── Set reminder → schedule directly ─────────────────────────────────
        if rtype == "set_reminder":
            print(f"[set_reminder] confirmation_message={reply.get('confirmation_message')!r} repeat_question={reply.get('repeat_question')!r}")
            scheduled = await _do_schedule_reminder(
                group_id=msg.group_id,
                message=reply["message"],
                iso_time=reply["iso_time"],
                created_by_jid=msg.sender_jid,
                repeat_interval=reply.get("repeat_interval"),
            )
            display_tz = get_user_timezone(msg.sender_jid)
            fire_str = _format_fire_time(scheduled[0]["fire_at_utc"], display_tz) if scheduled else reply["iso_time"]
            confirm_text = reply.get("confirmation_message") or f"✅ Reminder set for {fire_str} — {reply['message']}"
            await _send(msg.group_id, confirm_text)
            await append_message(msg.group_id, "Bot", confirm_text, datetime.now(timezone.utc).isoformat())

            # If repeat was ambiguous, open a session to ask
            if reply.get("repeat_interval") == "ask" and scheduled:
                repeat_q = reply.get("repeat_question") or f"Should '{reply['message']}' repeat? If so, how often? (e.g. daily, every Monday, weekly)"
                session = DialogSession(
                    session_id=str(uuid.uuid4()),
                    group_id=msg.group_id,
                    user_jid=msg.sender_jid,
                    user_name=msg.sender,
                    type="reminder_repeat",
                    question=repeat_q,
                    data={
                        "message": reply["message"],
                        "iso_time": reply["iso_time"],
                        "created_by_jid": msg.sender_jid,
                        "scheduled_jobs": [j["job_id"] for j in scheduled],
                    },
                )
                opened = await _open_session(session)
                if opened:
                    await _send(msg.group_id, session.question)
                    await append_message(msg.group_id, "Bot", session.question, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

        # ── Update timezone ───────────────────────────────────────────────────
        if rtype == "update_timezone":
            raw_tz = reply.get("timezone", "")
            resolved = await resolve_timezone(raw_tz)
            if resolved and is_valid_tz(resolved):
                set_user_timezone(msg.sender_jid, resolved)
                confirm_text = reply.get("confirmation_message") or f"Done! Your timezone is now set to {resolved}."
            else:
                confirm_text = f"I couldn't recognise '{raw_tz}' as a timezone — try something like 'London', 'New York', or 'Asia/Jerusalem'."
            await _send(msg.group_id, confirm_text)
            await append_message(msg.group_id, "Bot", confirm_text, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

        # ── Cancel reminder → direct ──────────────────────────────────────────
        if rtype == "cancel_reminder":
            rid = reply.get("reminder_id", "").lstrip("#")
            allowed = None if is_main_group(msg.group_id) else msg.group_id
            if rid and _cancel_reminder_job(rid, allowed_group_id=allowed):
                confirm_text = reply.get("cancellation_message") or "Done, the reminder has been cancelled."
            else:
                confirm_text = "I couldn't find that reminder — use /reminders to see what's still active."
            await _send(msg.group_id, confirm_text)
            await append_message(msg.group_id, "Bot", confirm_text, datetime.now(timezone.utc).isoformat())
            return {"ok": True}

    # Plain text reply
    await _send(msg.group_id, reply)
    await append_message(msg.group_id, "Bot", reply, datetime.now(timezone.utc).isoformat())
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
