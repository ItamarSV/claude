import os
from asyncio import Lock
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv(Path(__file__).parent.parent / ".env")

from gemini_client import process_message, summarize_text
from cost_tracker import get_monthly_summary
from history_manager import append_message, read_history_since, HISTORIES_DIR
from cost_tracker import COST_LOGS_DIR
from policy_manager import (
    is_main_group, get_status, set_pending, get_pending,
    activate, is_mention_only, get_group_name, set_group_name, get_all_active_groups,
    new_group_message, MAIN_GROUP_ID,
)

WHATSAPP_SERVICE_URL = os.environ.get("WHATSAPP_SERVICE_URL", "http://whatsapp-service:3000")

# Policy 2: per-group sequence counter — skip reply if a newer message arrived
_latest_seq: dict[str, int] = {}
_seq_lock = Lock()

# Session: groups where the bot is awaiting a follow-up reply (bypass mention filter)
_awaiting_reply: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    HISTORIES_DIR.mkdir(exist_ok=True)
    COST_LOGS_DIR.mkdir(exist_ok=True)
    yield


app = FastAPI(lifespan=lifespan)


class IncomingMessage(BaseModel):
    group_id: str
    sender: str
    text: str
    timestamp: str
    is_bot_mentioned: bool = False
    is_reply_to_bot: bool = False


class GroupJoined(BaseModel):
    group_id: str
    group_name: str


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


async def _send(group_id: str, text: str, buttons: list | None = None):
    payload = {"group_id": group_id, "text": text}
    if buttons:
        payload["buttons"] = buttons
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f"{WHATSAPP_SERVICE_URL}/send", json=payload)
            r.raise_for_status()
        except Exception as e:
            print(f"Failed to send message: {e}")


@app.post("/group-joined")
async def group_joined(body: GroupJoined):
    if is_main_group(body.group_id) or not MAIN_GROUP_ID:
        return {"ok": True}
    set_pending(body.group_id, body.group_name)
    await _send(MAIN_GROUP_ID, new_group_message(body.group_name))
    return {"ok": True}


@app.post("/webhook")
async def webhook(msg: IncomingMessage):
    # Policy 2: assign sequence number
    async with _seq_lock:
        seq = _latest_seq.get(msg.group_id, 0) + 1
        _latest_seq[msg.group_id] = seq

    # Save to history always
    await append_message(msg.group_id, msg.sender, msg.text, msg.timestamp)

    # Main group: check if this is a pending policy reply, then process normally
    if is_main_group(msg.group_id):
        pending = get_pending()
        if pending and msg.text.strip() in ("1", "2"):
            mention_only = msg.text.strip() == "1"
            activate(pending["group_id"], mention_only)
            label = "@mention only" if mention_only else "all messages"
            await _send(MAIN_GROUP_ID, f"Policy set for *{pending['group_name']}*: {label} ✅")
            return {"ok": True}
        # Fall through to normal AI processing for Main group

    else:
        status = get_status(msg.group_id)

        # New group — notify Main and wait
        if status == "new":
            if MAIN_GROUP_ID:
                # Fetch group name not available here; use group_id as fallback
                set_pending(msg.group_id, msg.group_id)
                await _send(MAIN_GROUP_ID, new_group_message(msg.group_id))
            return {"ok": True}

        # Pending — ignore all messages until policy set via Main
        if status == "pending":
            return {"ok": True}

        # Backfill group name if missing
        if get_group_name(msg.group_id) == msg.group_id:
            await _fetch_and_cache_group_name(msg.group_id)

        # Active — apply policy 1 (mention-only)
        awaiting = msg.group_id in _awaiting_reply
        if awaiting:
            _awaiting_reply.discard(msg.group_id)
        if is_mention_only(msg.group_id) and not msg.is_bot_mentioned and not msg.is_reply_to_bot and not awaiting:
            return {"ok": True}

    # Usage command: "/usage" (Main group only)
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
        reply = "\n".join(lines)
        await _send(msg.group_id, reply)
        return {"ok": True}

    # Summarize command: "/summarize"
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
                reply = await summarize_text(
                    msg.group_id,
                    f"Summarize today's activity across all groups:\n\n{combined}",
                )
        else:
            chunk = read_history_since(msg.group_id, today_start)
            if not chunk:
                reply = "No conversation recorded today yet."
            else:
                reply = await summarize_text(
                    msg.group_id,
                    f"Summarize today's conversation in this group:\n\n{chunk}",
                )
        if _latest_seq.get(msg.group_id) != seq:
            return {"ok": True}
        await _send(msg.group_id, reply)
        await append_message(msg.group_id, "Bot", reply, datetime.now(timezone.utc).isoformat())
        return {"ok": True}

    # Generate AI response
    try:
        reply = await process_message(msg.group_id, msg.sender, msg.text)
    except Exception as e:
        print(f"Gemini error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Policy 2: skip if a newer message arrived while processing
    if _latest_seq.get(msg.group_id) != seq:
        return {"ok": True}

    reply_text = reply["text"] if isinstance(reply, dict) else reply

    if isinstance(reply, dict):
        _awaiting_reply.add(msg.group_id)
        await _send(msg.group_id, reply["text"], reply.get("buttons"))
    else:
        await _send(msg.group_id, reply)

    await append_message(msg.group_id, "Bot", reply_text, datetime.now(timezone.utc).isoformat())

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
