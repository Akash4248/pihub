from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any

from .models import ConversationSession, utc_now

logger = logging.getLogger("inference.session")


class SessionManager:
    """In-memory conversation session manager with LRU eviction and TTL cleanup."""

    def __init__(self, max_sessions: int = 10000, ttl_seconds: int = 86400) -> None:
        self._sessions: OrderedDict[str, ConversationSession] = OrderedDict()
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds

    def _evict_expired(self) -> None:
        now = datetime.now(tz=timezone.utc)
        expired_keys: list[str] = []
        for sid, sess in self._sessions.items():
            try:
                dt = datetime.fromisoformat(sess.updated_at.replace("Z", "+00:00"))
                if (now - dt).total_seconds() > self.ttl_seconds:
                    expired_keys.append(sid)
            except Exception:
                pass
        for sid in expired_keys:
            self._sessions.pop(sid, None)

        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)


    def get_or_create(self, request: Any) -> ConversationSession:
        self._evict_expired()
        state = self._merged_state(request)
        session_id = str(state.get("session_id") or state.get("sessionId") or self._stable_session_id(request))
        history = self._history(request)
        session = self._sessions.get(session_id)
        if session is None:
            session = ConversationSession(
                session_id=session_id,
                student_id=self._optional_str(state.get("student_id") or state.get("studentId")),
                language=self._optional_str(getattr(request, "language", None)),
                grade=getattr(request, "grade", None),
                subject=self._optional_str(getattr(request, "subject", None)),
                chapter_id=self._optional_str(state.get("chapter_id") or state.get("chapterId") or getattr(request, "chapter", None)),
                experiment_id=self._optional_str(state.get("experiment_id") or state.get("experimentId")),
                chat_history=history,
            )
            self._sessions[session_id] = session
            self._sessions.move_to_end(session_id)
            self._evict_expired()
            return session


        session.language = self._optional_str(getattr(request, "language", None)) or session.language
        session.grade = getattr(request, "grade", None) or session.grade
        session.subject = self._optional_str(getattr(request, "subject", None)) or session.subject
        session.chapter_id = self._optional_str(state.get("chapter_id") or state.get("chapterId") or getattr(request, "chapter", None)) or session.chapter_id
        session.experiment_id = self._optional_str(state.get("experiment_id") or state.get("experimentId")) or session.experiment_id
        session.chat_history = history or session.chat_history
        session.updated_at = utc_now()
        self._sessions.move_to_end(session_id)
        return session

    def append_turn(self, session_id: str, question: str, answer: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.chat_history.append({"role": "student", "content": question})
        session.chat_history.append({"role": "tutor", "content": answer})
        session.updated_at = utc_now()
        self._sessions.move_to_end(session_id)

    def get(self, session_id: str) -> ConversationSession | None:
        session = self._sessions.get(session_id)
        if session:
            self._sessions.move_to_end(session_id)
        return session

    @staticmethod
    def _merged_state(request: Any) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for attr in ("sessionState", "session_state"):
            value = getattr(request, attr, None)
            if isinstance(value, dict):
                output.update(value)
        return output

    @staticmethod
    def _history(request: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for attr in ("conversationHistory", "conversation_history"):
            value = getattr(request, attr, None)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        output.append(item)
                    else:
                        output.append({"role": "unknown", "content": str(item)})
        return output

    @staticmethod
    def _stable_session_id(request: Any) -> str:
        payload = {
            "student_id": SessionManager._merged_state(request).get("student_id"),
            "grade": getattr(request, "grade", None),
            "subject": getattr(request, "subject", None),
            "chapter": getattr(request, "chapter", None),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

