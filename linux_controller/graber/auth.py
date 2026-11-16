"""Password and in-memory session primitives for the local web portal."""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 128 * 1024 * 1024
MINIMUM_PASSWORD_LENGTH = 12
MAXIMUM_PASSWORD_LENGTH = 256


class AuthError(ValueError):
    """Invalid authentication configuration or credential input."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _validate_private_file(path: Path) -> None:
    if path.is_symlink():
        raise AuthError(f"authentication file must not be a symlink: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise AuthError(f"authentication file must use mode 0600 or stricter: {path}")


def _atomic_private_write(path: Path, value: str, *, replace: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and path.is_symlink():
        raise AuthError(f"authentication file must not be a symlink: {path}")
    if path.exists() and not replace:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise AuthError("password must be text")
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MINIMUM_PASSWORD_LENGTH} characters")
    if len(password) > MAXIMUM_PASSWORD_LENGTH:
        raise AuthError(f"password must be no more than {MAXIMUM_PASSWORD_LENGTH} characters")


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=length, maxmem=SCRYPT_MAXMEM,
    )


class PasswordStore:
    """Atomic mode-0600 scrypt verifier storage for one administrator."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def configured(self) -> bool:
        return self.path.is_file() and not self.path.is_symlink()

    def set_password(self, username: str, password: str, *, must_change_password: bool = False) -> None:
        username = username.strip()
        if not username or len(username) > 64:
            raise AuthError("username must contain from 1 through 64 characters")
        validate_password(password)
        salt = secrets.token_bytes(16)
        verifier = _derive(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, length=SCRYPT_DKLEN)
        record = {
            "version": 1, "algorithm": "scrypt", "username": username,
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN,
            "salt": _b64encode(salt), "verifier": _b64encode(verifier),
            "must_change_password": bool(must_change_password),
        }
        _atomic_private_write(
            self.path, json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )

    def initialize(self, username: str, initial_password_file: str | Path) -> bool:
        """Hash an existing private setup key once; never print or copy it."""
        if self.configured:
            _validate_private_file(self.path)
            self._record()
            return False
        source = Path(initial_password_file)
        _validate_private_file(source)
        password = source.read_text(encoding="utf-8").strip()
        self.set_password(username, password, must_change_password=True)
        return True

    def _record(self) -> dict[str, object]:
        _validate_private_file(self.path)
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthError(f"invalid password file: {self.path}") from exc
        required = {
            "version", "algorithm", "username", "n", "r", "p", "dklen",
            "salt", "verifier", "must_change_password",
        }
        if not isinstance(record, dict) or not required <= record.keys():
            raise AuthError(f"invalid password file: {self.path}")
        if record["version"] != 1 or record["algorithm"] != "scrypt":
            raise AuthError("unsupported password verifier")
        return record

    @property
    def must_change_password(self) -> bool:
        return bool(self._record()["must_change_password"])

    def verify(self, username: str, password: str) -> bool:
        if not self.configured:
            return False
        if not 1 <= len(username) <= 64 or not 1 <= len(password) <= MAXIMUM_PASSWORD_LENGTH:
            return False
        record = self._record()
        try:
            n, r, p, length = (int(record[key]) for key in ("n", "r", "p", "dklen"))
            memory_bytes = 128 * n * r
            cpu_units = n * r * p
            if (
                n < (1 << 14) or n > (1 << 18) or n & (n - 1)
                or r < 1 or r > 16 or p < 1 or p > 8
                or memory_bytes > 64 * 1024 * 1024
                or cpu_units > SCRYPT_N * SCRYPT_R * 4
                or length < 16 or length > 64
            ):
                raise ValueError
            salt = _b64decode(str(record["salt"]))
            expected = _b64decode(str(record["verifier"]))
            if len(salt) < 16 or len(expected) != length:
                raise ValueError
        except (TypeError, ValueError, binascii.Error) as exc:
            raise AuthError(f"invalid password file: {self.path}") from exc
        actual = _derive(password, salt, n=n, r=r, p=p, length=length)
        username_ok = hmac.compare_digest(
            username.encode("utf-8"), str(record["username"]).encode("utf-8")
        )
        return username_ok and hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class Session:
    username: str
    csrf_token: str
    expires_at: float
    must_change_password: bool = False


class SessionStore:
    """Bounded, memory-only opaque browser sessions."""

    def __init__(self, ttl_s: float = 12 * 60 * 60, maximum: int = 64,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if ttl_s < 60 or maximum < 1:
            raise ValueError("session limits are invalid")
        self.ttl_s, self.maximum, self._clock = float(ttl_s), int(maximum), clock
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.RLock()

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

    def create(self, username: str, *, must_change_password: bool = False) -> tuple[str, Session]:
        with self._lock:
            self._prune()
            token = secrets.token_urlsafe(32)
            session = Session(
                username, secrets.token_urlsafe(24), self._clock() + self.ttl_s,
                must_change_password,
            )
            self._sessions[self._key(token)] = session
            self._prune()
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
                self._sessions.pop(self._key(token), None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


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


class AuthManager:
    """Authenticate one administrator and issue protected browser sessions."""

    cookie_name = "graber_session"

    def __init__(self, password_store: PasswordStore, *, secure_cookie: bool = True,
                 session_ttl_s: float = 12 * 60 * 60,
                 limiter: LoginLimiter | None = None) -> None:
        if not password_store.configured:
            raise AuthError(f"authentication is not initialized: {password_store.path}")
        self.password_store = password_store
        self.secure_cookie = secure_cookie
        self.sessions = SessionStore(session_ttl_s)
        self.limiter = limiter or LoginLimiter()
        self._credential_lock = threading.RLock()
        self._credential_generation = 0

    @property
    def session_ttl_s(self) -> float:
        return self.sessions.ttl_s

    def login(self, client: str, username: str, password: str) -> tuple[str, Session] | None:
        if self.limiter.retry_after(client):
            return None
        with self._credential_lock:
            generation = self._credential_generation
        if not self.password_store.verify(username, password):
            self.limiter.failed(client)
            return None
        with self._credential_lock:
            if generation != self._credential_generation:
                return None
            self.limiter.succeeded(client)
            return self.sessions.create(
                username, must_change_password=self.password_store.must_change_password
            )

    def change_password(self, session: Session, current_password: str | None,
                        new_password: str) -> tuple[str, Session]:
        with self._credential_lock:
            generation = self._credential_generation
        if not session.must_change_password and not self.password_store.verify(
            session.username, current_password or ""
        ):
            raise AuthError("current password is incorrect")
        with self._credential_lock:
            if generation != self._credential_generation:
                raise AuthError("credentials changed; sign in again")
            self.password_store.set_password(session.username, new_password)
            self._credential_generation += 1
            self.sessions.clear()
            return self.sessions.create(session.username)


def _set_password(args: argparse.Namespace) -> int:
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("passwords do not match")
    PasswordStore(args.file).set_password(args.username, first)
    print(f"password updated: {args.file}")
    return 0


def _initialize(args: argparse.Namespace) -> int:
    created = PasswordStore(args.file).initialize(args.username, args.initial_password_file)
    print(f"authentication {'initialized' if created else 'already initialized'}: {args.file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage local Graber portal authentication")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="initialize from an existing private setup key")
    initialize.add_argument("--file", required=True, type=Path)
    initialize.add_argument("--initial-password-file", required=True, type=Path)
    initialize.add_argument("--username", default="admin")
    initialize.set_defaults(handler=_initialize)
    password = commands.add_parser("set-password", help="set the administrator password")
    password.add_argument("--file", required=True, type=Path)
    password.add_argument("--username", default="admin")
    password.set_defaults(handler=_set_password)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
