"""Browser session persistence and per-client login throttling."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from .storage import read_auth_text, write_auth_text


@dataclass(frozen=True)
class Session:
    username: str
    role: str
    csrf_token: str
    expires_at: float
    setup_required: bool = False
    credential_generation: int = 0
    credential_revision: str | None = None


class SessionStore:
    """Bounded opaque browser sessions with optional private disk storage."""

    def __init__(self, ttl_s: float = 12 * 60 * 60, maximum: int = 64,
                 clock: Callable[[], float] | None = None, *,
                 path: Path | None = None) -> None:
        if not math.isfinite(ttl_s) or ttl_s < 60 or maximum < 1:
            raise ValueError("session limits are invalid")
        self.ttl_s, self.maximum = float(ttl_s), int(maximum)
        self._clock = clock or (time.time if path is not None else time.monotonic)
        self._path = path
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.RLock()
        if path is not None and (path.exists() or path.is_symlink()):
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            text = read_auth_text(path, require_owner=True)
        except UnicodeError:
            return
        try:
            stored = json.loads(text)
            if stored["version"] != 1:
                return
            now = self._clock()
            for key, raw in stored["sessions"].items():
                session = replace(Session(**raw), credential_generation=0)
                if (re.fullmatch(r"[a-f0-9]{64}", key)
                        and isinstance(session.username, str)
                        and session.role in ("owner", "recovery")
                        and isinstance(session.csrf_token, str)
                        and isinstance(session.setup_required, bool)
                        and isinstance(session.credential_revision, str)
                        and math.isfinite(session.expires_at)
                        and now < session.expires_at <= now + self.ttl_s):
                    self._sessions[key] = session
        except (TypeError, ValueError, KeyError, AttributeError):
            self._sessions.clear()
        self._prune()

    def _save(self) -> None:
        if self._path is not None:
            sessions = {key: asdict(value) for key, value in self._sessions.items()}
            write_auth_text(self._path, json.dumps({"version": 1, "sessions": sessions}))

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune(self) -> None:
        now = self._clock()
        for key, session in list(self._sessions.items()):
            if session.expires_at <= now:
                self._sessions.pop(key, None)
        while len(self._sessions) > self.maximum:
            self._sessions.popitem(last=False)

    def create(self, username: str, *, role: str = "owner",
               setup_required: bool = False,
               credential_generation: int = 0,
               credential_revision: str | None = None) -> tuple[str, Session]:
        with self._lock:
            self._prune()
            token = secrets.token_urlsafe(32)
            session = Session(
                username, role, secrets.token_urlsafe(24), self._clock() + self.ttl_s,
                setup_required, credential_generation, credential_revision,
            )
            self._sessions[self._key(token)] = session
            self._prune()
            self._save()
            return token, session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            self._prune()
            key = self._key(token)
            session = self._sessions.get(key)
            if session is not None:
                self._sessions.move_to_end(key)
            return session

    def remove(self, token: str | None) -> None:
        if token:
            with self._lock:
                if self._sessions.pop(self._key(token), None) is not None:
                    self._save()

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._save()


class LoginLimiter:
    """Bounded per-client sliding-window failed-login limiter."""

    def __init__(self, attempts: int = 5, window_s: float = 300,
                 maximum_clients: int = 1024,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.attempts, self.window_s = attempts, float(window_s)
        self.maximum_clients, self._clock = maximum_clients, clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.RLock()

    def _events(self, client: str) -> deque[float]:
        now = self._clock()
        events = self._failures.setdefault(client, deque())
        while events and events[0] <= now - self.window_s:
            events.popleft()
        self._failures.move_to_end(client)
        while len(self._failures) > self.maximum_clients:
            self._failures.popitem(last=False)
        return events

    def retry_after(self, client: str) -> int:
        with self._lock:
            events = self._events(client)
            if len(events) < self.attempts:
                return 0
            return max(1, int(self.window_s - (self._clock() - events[0]) + 0.999))

    def failed(self, client: str) -> None:
        with self._lock:
            self._events(client).append(self._clock())

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
