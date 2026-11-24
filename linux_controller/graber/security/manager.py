"""Portal authentication and session invalidation on credential changes."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from .passwords import PasswordStore
from .sessions import LoginLimiter, Session, SessionStore
from .storage import AuthError


class AuthManager:
    """Authenticate recovery/owner accounts and issue protected sessions."""

    cookie_name = "graber_session"

    def __init__(self, password_store: PasswordStore, *, secure_cookie: bool = True,
                 session_ttl_s: float = 12 * 60 * 60,
                 limiter: LoginLimiter | None = None,
                 session_path: Path | None = None) -> None:
        if not password_store.configured:
            raise AuthError(f"authentication is not initialized: {password_store.path}")
        self.password_store = password_store
        self.secure_cookie = secure_cookie
        self.sessions = SessionStore(session_ttl_s, path=session_path)
        self._persistent_sessions = session_path is not None
        self.limiter = limiter or LoginLimiter()
        self._credential_lock = threading.RLock()
        self._credential_generation = 0

    @property
    def session_ttl_s(self) -> float:
        return self.sessions.ttl_s

    def _credential_revision(self) -> str | None:
        if not self._persistent_sessions:
            return None
        return hashlib.sha256(self.password_store.path.read_bytes()).hexdigest()

    def get_session(self, token: str | None) -> Session | None:
        with self._credential_lock:
            session = self.sessions.get(token)
            if session is not None:
                try:
                    if session.credential_revision == self._credential_revision():
                        return session
                except OSError:
                    return None
            return None

    def login(self, client: str, username: str, password: str) -> tuple[str, Session] | None:
        if self.limiter.retry_after(client):
            return None
        with self._credential_lock:
            generation = self._credential_generation
            revision = self._credential_revision()
        authenticated = self.password_store.authenticate(username, password)
        if authenticated is None:
            self.limiter.failed(client)
            return None
        role, canonical_username = authenticated
        with self._credential_lock:
            setup_required = self.password_store.setup_required
            if generation != self._credential_generation or revision != self._credential_revision():
                return None
            self.limiter.succeeded(client)
            return self.sessions.create(
                canonical_username, role=role,
                setup_required=setup_required,
                credential_generation=self._credential_generation,
                credential_revision=revision,
            )

    def change_password(self, session: Session, current_password: str | None,
                        new_password: str) -> tuple[str, Session]:
        if session.role != "owner":
            raise AuthError("recovery password can be changed only from the command line")
        with self._credential_lock:
            self._require_current_credentials(session)
            revision = self.password_store.change_owner_password(
                session.username, current_password or "", new_password
            )
            return self._replace_owner_session(session.username, revision)

    def configure_owner(self, session: Session, username: str,
                        password: str) -> tuple[str, Session]:
        if session.role != "recovery":
            raise AuthError("only the recovery administrator can replace the owner account")
        with self._credential_lock:
            self._require_current_credentials(session)
            revision = self.password_store.set_owner(username, password)
            return self._replace_owner_session(self.password_store.owner_username or username, revision)

    def _require_current_credentials(self, session: Session) -> None:
        if (session.credential_generation != self._credential_generation
                or session.credential_revision != self._credential_revision()):
            raise AuthError("credentials changed; sign in again")

    def _replace_owner_session(self, username: str, revision: str) -> tuple[str, Session]:
        self._credential_generation += 1
        self.sessions.clear()
        return self.sessions.create(
            username, role="owner",
            credential_generation=self._credential_generation,
            credential_revision=revision if self._persistent_sessions else None,
        )
