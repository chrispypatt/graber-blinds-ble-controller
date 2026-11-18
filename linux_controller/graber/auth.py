"""Password and session primitives for the local web portal."""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
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
    """Atomic versioned storage for recovery and everyday credentials."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return self.path.is_file() and not self.path.is_symlink()

    @staticmethod
    def _validate_username(username: str, *, allow_admin: bool = False) -> str:
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", username):
            raise AuthError("username must use letters, numbers, dots, dashes, or underscores")
        if not allow_admin and username.casefold() == "admin":
            raise AuthError("username admin is reserved for recovery")
        return username

    @staticmethod
    def _credential(username: str, password: str) -> dict[str, object]:
        validate_password(password)
        salt = secrets.token_bytes(16)
        verifier = _derive(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, length=SCRYPT_DKLEN)
        return {
            "username": username,
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN,
            "salt": _b64encode(salt), "verifier": _b64encode(verifier),
        }

    @staticmethod
    def _validate_credential(record: object) -> dict[str, object]:
        required = {"username", "n", "r", "p", "dklen", "salt", "verifier"}
        if not isinstance(record, dict) or not required <= record.keys():
            raise AuthError("invalid credential record")
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
            raise AuthError("invalid credential record") from exc
        return record

    @staticmethod
    def _verify_credential(record: dict[str, object], username: str, password: str) -> bool:
        record = PasswordStore._validate_credential(record)
        actual = _derive(
            password, _b64decode(str(record["salt"])), n=int(record["n"]),
            r=int(record["r"]), p=int(record["p"]), length=int(record["dklen"]),
        )
        expected = _b64decode(str(record["verifier"]))
        username_ok = hmac.compare_digest(
            username.casefold().encode("utf-8"), str(record["username"]).casefold().encode("utf-8")
        )
        return username_ok and hmac.compare_digest(actual, expected)

    def _write(self, record: dict[str, object]) -> str:
        serialized = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        _atomic_private_write(self.path, serialized)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def initialize(self, username: str, initial_password_file: str | Path) -> bool:
        """Ensure v2 recovery auth, migrating v1 without storing plaintext."""
        if username.casefold() != "admin":
            raise AuthError("the recovery username must be admin")
        source = Path(initial_password_file)
        _validate_private_file(source)
        password = source.read_text(encoding="utf-8").strip()
        validate_password(password)
        with self._lock:
            if not self.configured:
                self._write({
                    "version": 2, "algorithm": "scrypt",
                    "recovery": self._credential("admin", password), "owner": None,
                })
                return True
            raw = self._raw_record()
            if raw.get("version") == 2:
                self._record()
                return False
            legacy = self._legacy_record(raw)
            owner = None
            legacy_is_setup_token = self._verify_credential(
                legacy, str(legacy["username"]), password
            )
            if not legacy_is_setup_token and not bool(legacy["must_change_password"]):
                owner = {key: legacy[key] for key in (
                    "username", "n", "r", "p", "dklen", "salt", "verifier"
                )}
                if str(owner["username"]).casefold() == "admin":
                    owner["username"] = "owner"
            self._write({
                "version": 2, "algorithm": "scrypt",
                "recovery": self._credential("admin", password), "owner": owner,
            })
            return True

    def _raw_record(self) -> dict[str, object]:
        _validate_private_file(self.path)
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthError(f"invalid password file: {self.path}") from exc
        if not isinstance(record, dict):
            raise AuthError(f"invalid password file: {self.path}")
        return record

    def _legacy_record(self, record: dict[str, object]) -> dict[str, object]:
        required = {
            "version", "algorithm", "username", "n", "r", "p", "dklen",
            "salt", "verifier", "must_change_password",
        }
        if not required <= record.keys() or record.get("version") != 1 or record.get("algorithm") != "scrypt":
            raise AuthError("unsupported password verifier")
        self._validate_credential(record)
        return record

    def _record(self) -> dict[str, object]:
        record = self._raw_record()
        if record.get("version") != 2 or record.get("algorithm") != "scrypt":
            raise AuthError("authentication file requires migration with auth init")
        if not {"recovery", "owner"} <= record.keys():
            raise AuthError(f"invalid password file: {self.path}")
        recovery = self._validate_credential(record["recovery"])
        if str(recovery["username"]).casefold() != "admin":
            raise AuthError("recovery username must be admin")
        if record["owner"] is not None:
            owner = self._validate_credential(record["owner"])
            self._validate_username(str(owner["username"]))
        return record

    @property
    def setup_required(self) -> bool:
        with self._lock:
            return self._record()["owner"] is None

    @property
    def owner_username(self) -> str | None:
        with self._lock:
            owner = self._record()["owner"]
            return None if owner is None else str(owner["username"])

    def authenticate(self, username: str, password: str) -> tuple[str, str] | None:
        if not 1 <= len(username) <= 64 or not 1 <= len(password) <= MAXIMUM_PASSWORD_LENGTH:
            return None
        with self._lock:
            record = self._record()
            recovery = record["recovery"]
            owner = record["owner"]
        selected = recovery if username.casefold() == "admin" else owner
        if selected is None or not self._verify_credential(selected, username, password):
            return None
        role = "recovery" if selected is recovery else "owner"
        return role, str(selected["username"])

    def verify(self, username: str, password: str) -> bool:
        return self.authenticate(username, password) is not None

    def set_owner(self, username: str, password: str) -> str:
        username = self._validate_username(username)
        credential = self._credential(username, password)
        with self._lock:
            record = self._record()
            record["owner"] = credential
            return self._write(record)

    def change_owner_password(self, username: str, current_password: str, new_password: str) -> str:
        with self._lock:
            record = self._record()
            owner = record["owner"]
            if owner is None or not self._verify_credential(owner, username, current_password):
                raise AuthError("current password is incorrect")
            record["owner"] = self._credential(str(owner["username"]), new_password)
            return self._write(record)

    def set_recovery_password(self, password: str) -> None:
        with self._lock:
            record = self._record()
            record["recovery"] = self._credential("admin", password)
            self._write(record)

    def reset_owner(self) -> None:
        with self._lock:
            record = self._record()
            record["owner"] = None
            self._write(record)


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
            _validate_private_file(path)
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise AuthError("session file must be a regular file owned by the portal user")
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
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
            _atomic_private_write(self._path, json.dumps({"version": 1, "sessions": sessions}))


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
            if (session.credential_generation != self._credential_generation
                    or session.credential_revision != self._credential_revision()):
                raise AuthError("credentials changed; sign in again")
            revision = self.password_store.change_owner_password(
                session.username, current_password or "", new_password
            )
            self._credential_generation += 1
            self.sessions.clear()
            return self.sessions.create(
                session.username, role="owner",
                credential_generation=self._credential_generation,
                credential_revision=revision if self._persistent_sessions else None,
            )

    def configure_owner(self, session: Session, username: str,
                        password: str) -> tuple[str, Session]:
        if session.role != "recovery":
            raise AuthError("only the recovery administrator can replace the owner account")
        with self._credential_lock:
            if (session.credential_generation != self._credential_generation
                    or session.credential_revision != self._credential_revision()):
                raise AuthError("credentials changed; sign in again")
            revision = self.password_store.set_owner(username, password)
            self._credential_generation += 1
            self.sessions.clear()
            return self.sessions.create(
                self.password_store.owner_username or username, role="owner",
                credential_generation=self._credential_generation,
                credential_revision=revision if self._persistent_sessions else None,
            )


def _prompt_password() -> str:
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("passwords do not match")
    return first


def _set_recovery_password(args: argparse.Namespace) -> int:
    if getattr(args, "username", "admin").casefold() != "admin":
        raise SystemExit("the recovery username must be admin")
    PasswordStore(args.file).set_recovery_password(_prompt_password())
    print(f"recovery password updated: {args.file}")
    return 0


def _initialize(args: argparse.Namespace) -> int:
    changed = PasswordStore(args.file).initialize(args.username, args.initial_password_file)
    print(f"authentication {'initialized or migrated' if changed else 'already initialized'}: {args.file}")
    return 0


def _reset_owner(args: argparse.Namespace) -> int:
    PasswordStore(args.file).reset_owner()
    print(f"owner account reset: {args.file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage local Graber portal authentication")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="initialize from an existing private setup key")
    initialize.add_argument("--file", required=True, type=Path)
    initialize.add_argument("--initial-password-file", required=True, type=Path)
    initialize.add_argument("--username", default="admin")
    initialize.set_defaults(handler=_initialize)
    recovery = commands.add_parser(
        "set-recovery-password", help="rotate the recovery administrator password"
    )
    recovery.add_argument("--file", required=True, type=Path)
    recovery.set_defaults(handler=_set_recovery_password)
    password = commands.add_parser(
        "set-password", help="compatibility alias for set-recovery-password"
    )
    password.add_argument("--file", required=True, type=Path)
    password.add_argument("--username", default="admin")
    password.set_defaults(handler=_set_recovery_password)
    reset = commands.add_parser("reset-owner", help="delete the everyday owner account")
    reset.add_argument("--file", required=True, type=Path)
    reset.set_defaults(handler=_reset_owner)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
