"""Private storage shared by password verifiers and persisted sessions."""

from pathlib import Path

from ..util.private_files import PrivateFileError, read_private_text, write_private_text


class AuthError(ValueError):
    """Invalid authentication configuration or credential input."""


def read_auth_text(path: Path, *, require_owner: bool = False) -> str:
    try:
        return read_private_text(path, require_owner=require_owner)
    except PrivateFileError as exc:
        raise AuthError(str(exc)) from exc


def write_auth_text(path: Path, text: str, *, replace: bool = True) -> bool:
    try:
        return write_private_text(path, text, replace=replace)
    except PrivateFileError as exc:
        raise AuthError(str(exc)) from exc
