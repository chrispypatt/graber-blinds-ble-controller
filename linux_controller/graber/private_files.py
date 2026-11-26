"""Read owner-private text files and replace or create them atomically."""

import errno
import os
import stat
import tempfile
from pathlib import Path


class PrivateFileError(ValueError):
    """A file failed its required type, permissions or ownership check."""


def read_private_text(path: Path, *, require_owner: bool = False) -> str:
    # Validate the opened descriptor so path replacement cannot change what we read.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PrivateFileError(f"private file must not be a symlink: {path}") from exc
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PrivateFileError(f"expected a regular file: {path}")
        if info.st_mode & 0o077:
            raise PrivateFileError(f"private file must use mode 0600 or stricter: {path}")
        if require_owner and info.st_uid != os.geteuid():
            raise PrivateFileError(f"private file must belong to the current user: {path}")
        with os.fdopen(descriptor, encoding="utf-8", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def write_private_text(path: Path, value: str, *, replace: bool = True) -> bool:
    """Publish a complete 0600 file. Return False if exclusive creation loses a race."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise PrivateFileError(f"private file must not be a symlink: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)
