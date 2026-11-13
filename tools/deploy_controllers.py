#!/usr/bin/env python3
"""Safely synchronize, validate, compile, and optionally OTA-upload controllers.

The private inventory and ESPHome secrets stay outside this repository.  The
default invocation is a plan only.  ``--apply`` is required before this tool
can modify the live deployment, and ``--flash`` is an additional explicit gate
for OTA upload.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = os.environ.get("GRABER_INVENTORY")
DEFAULT_CONTROLLER_ROOT = os.environ.get("GRABER_CONTROLLER_ROOT")
DEFAULT_ESPHOME_ROOT = os.environ.get("GRABER_ESPHOME_ROOT")
DEFAULT_BACKUP_ROOT = os.environ.get("GRABER_BACKUP_ROOT")
DEFAULT_DEPLOY_CONFIG = os.environ.get("GRABER_DEPLOY_CONFIG")


def die(message: str) -> "NoReturn":
    raise SystemExit(f"error: {message}")


def load_inventory(path: Path) -> list[dict]:
    if not path.exists():
        die(f"private inventory not found: {path}")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read inventory {path}: {exc}")
    controllers = data.get("controllers")
    if not isinstance(controllers, list) or not controllers:
        die("inventory must contain a non-empty controllers list")
    for controller in controllers:
        for key in ("file", "name", "friendly_name", "blinds"):
            if key not in controller:
                die(f"inventory controller missing {key!r}")
    return controllers


def load_deploy_config(path: Path | None) -> dict:
    if path is None:
        return {}
    if not path.exists():
        die(f"private deploy config not found: {path}")
    try:
        config = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read deploy config {path}: {exc}")
    if not isinstance(config, dict):
        die("deploy config must be a JSON object")
    for key in ("aliases", "devices"):
        if key in config and not isinstance(config[key], dict):
            die(f"deploy config {key!r} must be an object")
    return config


def choose(controllers: list[dict], target: str, aliases: dict[str, str]) -> list[dict]:
    if target == "all":
        return controllers
    wanted = aliases.get(target, target)
    matches = [c for c in controllers if c["name"] == wanted or c["file"] == target]
    if not matches:
        valid = ", ".join(sorted({"all", *aliases, *(c["name"] for c in controllers)}))
        die(f"unknown controller {target!r}; choose one of: {valid}")
    return matches


def run(command: list[str], *, cwd: Path, dry_run: bool) -> None:
    print("+", " ".join(str(part) for part in command))
    if not dry_run:
        subprocess.run(command, cwd=cwd, check=True)


def backup_live(controller_root: Path, esphome_root: Path, backup_root: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_root / f"graber-deploy-backup-{stamp}"
    destination.mkdir(parents=True, exist_ok=False)
    for source in (controller_root / "packages", controller_root / "components"):
        if source.exists():
            shutil.copytree(source, destination / source.name)
    wrappers = destination / "esphome"
    wrappers.mkdir()
    for source in esphome_root.glob("*.yaml"):
        if source.name.startswith(("graber-", "esphome-web-")):
            shutil.copy2(source, wrappers / source.name)
    print(f"backup: {destination}")
    return destination


def sync_sources(controller_root: Path) -> None:
    for name in ("packages", "components"):
        source = REPO_ROOT / name
        destination = controller_root / name
        if not source.is_dir():
            die(f"repository source directory missing: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # copytree dirs_exist_ok preserves unrelated deployment files while
        # ensuring every reviewed repository file is updated.
        shutil.copytree(source, destination, dirs_exist_ok=True)
        print(f"synced: {source} -> {destination}")


def generate_wrappers(
    inventory: Path, controllers: list[dict], output_dir: Path, generator: Path
) -> None:
    """Render selected private-inventory wrappers into output_dir."""
    with tempfile.TemporaryDirectory(prefix="graber-generated-") as temp:
        generated = Path(temp)
        subprocess.run(
            [sys.executable, str(generator), str(inventory), "--output-dir", str(generated)],
            cwd=REPO_ROOT,
            check=True,
        )
        for controller in controllers:
            source = generated / controller["file"]
            destination = output_dir / controller["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            print(f"generated: {destination}")


def make_build_workspace(
    inventory: Path,
    controllers: list[dict],
    esphome_root: Path,
    generator: Path,
) -> tempfile.TemporaryDirectory[str]:
    """Stage a self-contained, writable ESPHome tree for config/compile/upload."""
    workspace = tempfile.TemporaryDirectory(prefix="graber-build-")
    root = Path(workspace.name)
    staged_esphome = root / "esphome"
    staged_controller = root / "graber-controller"
    staged_esphome.mkdir()
    shutil.copytree(REPO_ROOT / "packages", staged_controller / "packages")
    shutil.copytree(REPO_ROOT / "components", staged_controller / "components")
    secrets = esphome_root / "secrets.yaml"
    if not secrets.is_file():
        workspace.cleanup()
        die(f"ESPHome secrets file not found: {secrets}")
    shutil.copy2(secrets, staged_esphome / "secrets.yaml")
    generate_wrappers(inventory, controllers, staged_esphome, generator)
    print(f"build workspace: {staged_esphome}")
    return workspace


def parse_devices(values: list[str], selected: list[dict], configured: dict[str, str]) -> dict[str, str]:
    if not values:
        result = {c["name"]: configured[c["name"]] for c in selected if c["name"] in configured}
        result.update({c["name"]: f"{c['name']}.local" for c in selected if c["name"] not in result})
        return result
    result: dict[str, str] = {}
    for value in values:
        if "=" in value:
            key, address = value.split("=", 1)
        elif len(selected) == 1:
            key, address = selected[0]["name"], value
        else:
            die("--device must be NAME=ADDRESS when targeting multiple controllers")
        if not key or not address:
            die(f"invalid --device value: {value!r}")
        result[key] = address
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy reviewed Graber controller sources (dry-run by default)."
    )
    parser.add_argument("target", help="inventory target name, alias, or all")
    parser.add_argument("--deploy-config", type=Path, default=DEFAULT_DEPLOY_CONFIG,
                        help="ignored private JSON file with paths, aliases, and devices")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--controller-root", type=Path, default=DEFAULT_CONTROLLER_ROOT)
    parser.add_argument("--esphome-root", type=Path, default=DEFAULT_ESPHOME_ROOT)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--esphome", default=os.environ.get("ESPHOME", "esphome"))
    parser.add_argument("--device", action="append", default=[], metavar="NAME=ADDRESS")
    parser.add_argument("--apply", action="store_true", help="modify the live deployment")
    parser.add_argument("--config", action="store_true", help="run esphome config")
    parser.add_argument("--compile", action="store_true", help="run esphome compile")
    parser.add_argument("--flash", action="store_true", help="OTA-upload after compiling")
    parser.add_argument("--skip-backup", action="store_true", help="not recommended")
    args = parser.parse_args()

    config = load_deploy_config(args.deploy_config)
    args.inventory = args.inventory or config.get("inventory")
    args.controller_root = args.controller_root or config.get("controller_root")
    args.esphome_root = args.esphome_root or config.get("esphome_root")
    args.backup_root = args.backup_root or config.get("backup_root")
    for name in ("inventory", "controller_root", "esphome_root", "backup_root"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, Path(value))

    if args.inventory is None:
        die("--inventory is required (or set GRABER_INVENTORY in a private environment)")
    if args.controller_root is None:
        die("--controller-root is required (or set GRABER_CONTROLLER_ROOT in a private environment)")
    if args.esphome_root is None:
        die("--esphome-root is required (or set GRABER_ESPHOME_ROOT in a private environment)")

    if args.flash and not args.apply:
        die("--flash requires --apply")
    if args.compile and not args.apply:
        die("--compile requires --apply")
    if args.config and not args.apply:
        die("--config requires --apply")
    if args.skip_backup and not args.apply:
        die("--skip-backup only applies with --apply")
    if args.apply and not args.skip_backup and args.backup_root is None:
        die("--backup-root is required for --apply (or set it in the private deploy config)")

    controllers = load_inventory(args.inventory)
    selected = choose(controllers, args.target, config.get("aliases", {}))
    devices = parse_devices(args.device, selected, config.get("devices", {}))
    print(f"repository: {REPO_ROOT}")
    print(f"target: {', '.join(c['name'] for c in selected)}")
    print(f"mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print("private inventory and secrets are not copied into the repository")

    if not args.apply:
        print("would back up live packages/components and controller wrappers")
        print(f"would sync {REPO_ROOT / 'packages'} and {REPO_ROOT / 'components'}")
        print("would generate selected wrappers from the private inventory")
    else:
        if not args.skip_backup:
            backup_live(args.controller_root, args.esphome_root, args.backup_root)
        sync_sources(args.controller_root)
        generator = REPO_ROOT / "tools" / "generate_controllers.py"
        # Generate only selected wrappers into the live ESPHome directory.
        # Build operations below use a separate writable workspace.
        generate_wrappers(args.inventory, selected, args.esphome_root, generator)

    if args.config or args.compile or args.flash:
        generator = REPO_ROOT / "tools" / "generate_controllers.py"
        workspace = make_build_workspace(
            args.inventory, selected, args.esphome_root, generator
        )
        try:
            staged_esphome = Path(workspace.name) / "esphome"
            for controller in selected:
                wrapper = staged_esphome / controller["file"]
                if args.config:
                    run([args.esphome, "config", str(wrapper)], cwd=staged_esphome, dry_run=False)
                if args.compile or args.flash:
                    run(
                        [args.esphome, "compile", str(wrapper), "--skip-bootloader"],
                        cwd=staged_esphome,
                        dry_run=False,
                    )
                if args.flash:
                    run(
                        [args.esphome, "upload", str(wrapper), "--device", devices[controller["name"]]],
                        cwd=staged_esphome,
                        dry_run=False,
                    )
        finally:
            workspace.cleanup()
    elif not args.apply:
        print("no live files changed; add --apply, then optionally --config/--compile/--flash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
