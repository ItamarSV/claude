import os
from asyncio import Lock
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv(Path(__file__).parent.parent / ".env")

from gemini_client import process_message
from history_manager import append_message, HISTORIES_DIR
from cost_tracker import COST_LOGS_DIR
from policy_manager import (
    is_main_group, get_status, set_pending, get_pending,
    activate, is_mention_only, new_group_message, MAIN_GROUP_ID,
)

WHATSAPP_SERVICE_URL = os.environ.get("WHATSAPP_SERVICE_URL", "http://whatsapp-service:3000")

# Policy 2: per-group sequence counter — skip reply if a newer message arrived
_latest_seq: dict[str, int] = {}
_seq_lock = Lock()


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


class GroupJoined(BaseModel):
    group_id: str
    group_name: str


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
    if is_main_group(body.group_id):
        return {"ok": True}
    if get_status(body.group_id) == "new" and MAIN_GROUP_ID:
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

        # Active — apply policy 1 (mention-only)
        if is_mention_only(msg.group_id) and not msg.is_bot_mentioned:
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

    if isinstance(reply, dict):
        await _send(msg.group_id, reply["text"], reply.get("buttons"))
    else:
        await _send(msg.group_id, reply)

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
