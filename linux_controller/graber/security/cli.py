"""Command-line administration for the recovery and everyday accounts."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from .passwords import PasswordStore
from .storage import AuthError


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
    print(f"authentication {'initialized' if changed else 'already initialized'}: {args.file}")
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
    try:
        return args.handler(args)
    except AuthError as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
