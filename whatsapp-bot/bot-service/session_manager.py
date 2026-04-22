import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SESSION_TIMEOUT = 300   # 5 minutes
GHOST_WINDOW = 120      # 2 minutes


@dataclass
class DialogSession:
    session_id: str
    group_id: str
    user_jid: str
    user_name: str
    type: str           # "web_search" | "reminder_repeat"
    question: str       # Shown to user once on open
    data: dict          # Type-specific payload
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timeout_task: Any = field(default=None, repr=False)


class SessionManager:
    def __init__(self):
        # Active sessions: (group_id, user_jid) -> DialogSession
        self._sessions: dict[tuple, DialogSession] = {}
        # Recently timed-out sessions: (group_id, user_jid) -> (session, closed_at)
        self._ghosts: dict[tuple, tuple[DialogSession, datetime]] = {}
        # Per-(group, user) locks for concurrent request safety
        self._locks: dict[tuple, asyncio.Lock] = {}

    def _key(self, group_id: str, user_jid: str) -> tuple:
        return (group_id, user_jid)

    def lock(self, group_id: str, user_jid: str) -> asyncio.Lock:
        key = self._key(group_id, user_jid)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def get(self, group_id: str, user_jid: str) -> DialogSession | None:
        return self._sessions.get(self._key(group_id, user_jid))

    def open(self, session: DialogSession) -> bool:
        """Open a new session. Returns False if user already has an active session."""
        key = self._key(session.group_id, session.user_jid)
        if key in self._sessions:
            return False
        self._sessions[key] = session
        self._ghosts.pop(key, None)     # Clear ghost when new session opens
        return True

    def close(self, group_id: str, user_jid: str) -> DialogSession | None:
        """Close session cleanly (fulfilled or cancelled by user). Cancels timeout task."""
        key = self._key(group_id, user_jid)
        session = self._sessions.pop(key, None)
        if session and session.timeout_task:
            session.timeout_task.cancel()
        return session

    def close_to_ghost(self, group_id: str, user_jid: str) -> DialogSession | None:
        """Called by the timeout task — closes and stores as ghost for GHOST_WINDOW seconds."""
        key = self._key(group_id, user_jid)
        session = self._sessions.pop(key, None)
        if session:
            self._ghosts[key] = (session, datetime.now(timezone.utc))
        return session

    def get_ghost(self, group_id: str, user_jid: str) -> DialogSession | None:
        """Returns the ghost session if it exists and hasn't expired."""
        key = self._key(group_id, user_jid)
        entry = self._ghosts.get(key)
        if not entry:
            return None
        session, closed_at = entry
        if (datetime.now(timezone.utc) - closed_at).total_seconds() > GHOST_WINDOW:
            self._ghosts.pop(key, None)
            return None
        return session

    def revive_ghost(self, group_id: str, user_jid: str) -> DialogSession | None:
        """Consume ghost and re-open as a fresh session with the same data."""
        key = self._key(group_id, user_jid)
        entry = self._ghosts.pop(key, None)
        if not entry:
            return None
        old, _ = entry
        new_session = DialogSession(
            session_id=str(uuid.uuid4()),
            group_id=old.group_id,
            user_jid=old.user_jid,
            user_name=old.user_name,
            type=old.type,
            question=old.question,
            data=dict(old.data),
        )
        self._sessions[key] = new_session
        return new_session


session_manager = SessionManager()
