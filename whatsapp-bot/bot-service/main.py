import os
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

WHATSAPP_SERVICE_URL = os.environ.get("WHATSAPP_SERVICE_URL", "http://whatsapp-service:3000")


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


@app.post("/webhook")
async def webhook(msg: IncomingMessage):
    # Save to history first (always, regardless of bot response)
    await append_message(msg.group_id, msg.sender, msg.text, msg.timestamp)

    # Generate response
    try:
        reply = await process_message(msg.group_id, msg.sender, msg.text)
    except Exception as e:
        print(f"Gemini error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Send reply back via whatsapp-service
    payload = {"group_id": msg.group_id}
    if isinstance(reply, dict):
        payload.update(reply)
    else:
        payload["text"] = reply

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{WHATSAPP_SERVICE_URL}/send",
                json=payload,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"Failed to send reply: {e}")
            raise HTTPException(status_code=502, detail="Failed to deliver reply")

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
