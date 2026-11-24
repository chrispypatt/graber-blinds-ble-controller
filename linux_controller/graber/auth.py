"""Compatibility entry point for the authentication CLI and portal API."""

from .security import (
    MAXIMUM_PASSWORD_LENGTH, MINIMUM_PASSWORD_LENGTH, AuthError, AuthManager,
    LoginLimiter, PasswordStore, Session, SessionStore, validate_password,
)
from .security.cli import main
from .security.passwords import SCRYPT_DKLEN, SCRYPT_MAXMEM, SCRYPT_N, SCRYPT_P, SCRYPT_R

__all__ = [
    "AuthError", "AuthManager", "LoginLimiter", "MAXIMUM_PASSWORD_LENGTH",
    "MINIMUM_PASSWORD_LENGTH", "PasswordStore", "Session", "SessionStore",
    "SCRYPT_DKLEN", "SCRYPT_MAXMEM", "SCRYPT_N", "SCRYPT_P", "SCRYPT_R",
    "main", "validate_password",
]

if __name__ == "__main__":
    raise SystemExit(main())
