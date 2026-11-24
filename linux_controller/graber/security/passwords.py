"""Recovery and owner password verifiers."""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import re
import secrets
import threading
from pathlib import Path

from ..util.encoding import decode_base64url, encode_base64url
from .storage import AuthError, read_auth_text, write_auth_text


SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 128 * 1024 * 1024
MINIMUM_PASSWORD_LENGTH = 12
MAXIMUM_PASSWORD_LENGTH = 256


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
    def _validate_username(username: str) -> str:
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", username):
            raise AuthError("username must use letters, numbers, dots, dashes, or underscores")
        if username.casefold() == "admin":
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
            "salt": encode_base64url(salt), "verifier": encode_base64url(verifier),
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
            salt = decode_base64url(str(record["salt"]))
            expected = decode_base64url(str(record["verifier"]))
            if len(salt) < 16 or len(expected) != length:
                raise ValueError
        except (TypeError, ValueError, binascii.Error) as exc:
            raise AuthError("invalid credential record") from exc
        return record

    @staticmethod
    def _verify_credential(record: dict[str, object], username: str, password: str) -> bool:
        record = PasswordStore._validate_credential(record)
        actual = _derive(
            password, decode_base64url(str(record["salt"])), n=int(record["n"]),
            r=int(record["r"]), p=int(record["p"]), length=int(record["dklen"]),
        )
        expected = decode_base64url(str(record["verifier"]))
        username_ok = hmac.compare_digest(
            username.casefold().encode("utf-8"), str(record["username"]).casefold().encode("utf-8")
        )
        return username_ok and hmac.compare_digest(actual, expected)

    @staticmethod
    def _serialize(record: dict[str, object]) -> str:
        return json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"

    def _write(self, record: dict[str, object]) -> str:
        serialized = self._serialize(record)
        write_auth_text(self.path, serialized)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def initialize(self, username: str, initial_password_file: str | Path) -> bool:
        """Create recovery credentials once, preserving any existing account file."""
        if username.casefold() != "admin":
            raise AuthError("the recovery username must be admin")
        password = read_auth_text(Path(initial_password_file)).strip()
        validate_password(password)
        with self._lock:
            if self.configured:
                self._record()
                return False
            record = {
                "version": 2, "algorithm": "scrypt",
                "recovery": self._credential("admin", password), "owner": None,
            }
            if write_auth_text(self.path, self._serialize(record), replace=False):
                return True
            self._record()
            return False

    def _raw_record(self) -> dict[str, object]:
        try:
            record = json.loads(read_auth_text(self.path))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthError(f"invalid password file: {self.path}") from exc
        if not isinstance(record, dict):
            raise AuthError(f"invalid password file: {self.path}")
        return record

    def _record(self) -> dict[str, object]:
        record = self._raw_record()
        if record.get("version") != 2 or record.get("algorithm") != "scrypt":
            raise AuthError("unsupported authentication file format (expected version 2 with scrypt)")
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
