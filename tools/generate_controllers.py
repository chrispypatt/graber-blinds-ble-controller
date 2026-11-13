#!/usr/bin/env python3
"""Generate minimal ESPHome controller YAML wrappers from private JSON inventory."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
SECRET_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def quoted(value: object) -> str:
    """Return a JSON string, which is also a safe YAML double-quoted scalar."""
    return json.dumps(str(value), ensure_ascii=False)


def validate(config: dict) -> None:
    if not isinstance(config.get("controllers"), list) or not config["controllers"]:
        raise ValueError("inventory must contain a non-empty controllers list")
    seen_devices: set[str] = set()
    seen_controllers: set[str] = set()
    seen_files: set[str] = set()
    for controller in config.get("controllers", []):
        for key in ("file", "name", "friendly_name", "blinds"):
            if key not in controller:
                raise ValueError(f"controller missing {key!r}")
        if not NAME_RE.fullmatch(controller["name"]):
            raise ValueError(f"invalid ESPHome name: {controller['name']!r}")
        if controller["name"] in seen_controllers:
            raise ValueError(f"duplicate controller name: {controller['name']}")
        seen_controllers.add(controller["name"])
        output = Path(controller["file"])
        if output.is_absolute() or ".." in output.parts or output.suffix not in (".yaml", ".yml"):
            raise ValueError(f"unsafe controller output file: {controller['file']!r}")
        if controller["file"] in seen_files:
            raise ValueError(f"duplicate controller output file: {controller['file']}")
        seen_files.add(controller["file"])
        if not isinstance(controller["friendly_name"], str) or not controller["friendly_name"].strip():
            raise ValueError(f"invalid friendly_name for {controller['name']}")
        for path_key in ("package_dir", "component_path"):
            value = controller.get(path_key, "packages" if path_key == "package_dir" else "components")
            if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
                raise ValueError(f"invalid {path_key} for {controller['name']}")
        if len(controller.get("fallback_ap_ssid", "")) > 32:
            raise ValueError(f"fallback AP SSID is longer than 32 characters: {controller['name']}")
        if "fallback_ap_secret" in controller and not SECRET_RE.fullmatch(controller["fallback_ap_secret"]):
            raise ValueError(f"invalid fallback_ap_secret for {controller['name']}")
        local_ids: set[str] = set()
        for blind in controller["blinds"]:
            for key in ("id", "name", "mac_secret", "token_secret", "dual_rail"):
                if key not in blind:
                    raise ValueError(f"blind in {controller['name']} missing {key!r}")
            if not ID_RE.fullmatch(blind["id"]):
                raise ValueError(f"invalid blind id: {blind['id']!r}")
            if blind["id"] in local_ids or blind["id"] in seen_devices:
                raise ValueError(f"duplicate blind id: {blind['id']}")
            if not isinstance(blind["dual_rail"], bool):
                raise ValueError(f"dual_rail must be boolean for {blind['id']}")
            for name_key in ("name", "cover_name", "position_name"):
                if name_key in blind and (not isinstance(blind[name_key], str) or not blind[name_key].strip()):
                    raise ValueError(f"invalid {name_key} for {blind['id']}")
            local_ids.add(blind["id"])
            seen_devices.add(blind["id"])
            for secret_key in ("mac_secret", "token_secret"):
                if not SECRET_RE.fullmatch(blind[secret_key]):
                    raise ValueError(f"invalid secret name: {blind[secret_key]!r}")
        group_ids: set[str] = set()
        for group in controller.get("groups", []):
            for key in ("id", "name", "blinds"):
                if key not in group:
                    raise ValueError(f"group in {controller['name']} missing {key!r}")
            if not ID_RE.fullmatch(group["id"]):
                raise ValueError(f"invalid group id: {group['id']!r}")
            if group["id"] in group_ids:
                raise ValueError(f"duplicate group id: {group['id']}")
            group_ids.add(group["id"])
            if not isinstance(group["name"], str) or not group["name"].strip():
                raise ValueError(f"invalid group name: {group['id']}")
            if not isinstance(group["blinds"], list) or not group["blinds"]:
                raise ValueError(f"group {group['id']} must contain at least one blind")
            if len(group["blinds"]) != len(set(group["blinds"])):
                raise ValueError(f"group {group['id']} contains duplicate blinds")
            unknown = set(group["blinds"]) - local_ids
            if unknown:
                raise ValueError(f"group {group['id']} has unknown blinds: {sorted(unknown)}")
            tdbu_blinds = group.get("tdbu_blinds", [])
            if not isinstance(tdbu_blinds, list) or len(tdbu_blinds) != len(set(tdbu_blinds)):
                raise ValueError(f"group {group['id']} tdbu_blinds must be a unique list")
            if not set(tdbu_blinds).issubset(group["blinds"]):
                raise ValueError(f"group {group['id']} tdbu_blinds must be members of blinds")
            dual_ids = {blind["id"] for blind in controller["blinds"] if blind["dual_rail"]}
            if not set(tdbu_blinds).issubset(dual_ids):
                raise ValueError(f"group {group['id']} tdbu_blinds must contain only TDBU motors")


def render(controller: dict) -> str:
    package_dir = controller.get("package_dir", "packages")
    component_path = controller.get("component_path", "components")
    lines = [
        "# Generated by tools/generate_controllers.py. Edit the private inventory, not this file.",
        "esphome:",
        f"  name: {controller['name']}",
        f"  friendly_name: {quoted(controller['friendly_name'])}",
        '  comment: "Graber BLE controller: serialized setup with 30s warm reuse"',
        "",
        "substitutions:",
        f"  graber_component_path: {quoted(component_path)}",
        "",
        "packages:",
        f"  controller_base: !include {quoted(package_dir + '/graber-common.yaml')}",
    ]
    for blind in controller["blinds"]:
        dual = "true" if blind["dual_rail"] else "false"
        dual_cpp = "1" if blind["dual_rail"] else "0"
        top_internal = "false" if blind["dual_rail"] else "true"
        lines.extend([
            f"  {blind['id']}: !include",
            f"    file: {quoted(package_dir + '/graber-blind.yaml')}",
            "    vars:",
            f"      blind_id: {blind['id']}",
            f"      blind_name: {quoted(blind['name'])}",
            f"      blind_mac: !secret {blind['mac_secret']}",
            f"      blind_token: !secret {blind['token_secret']}",
            f"      blind_dual_rail: {dual}",
            f"      blind_dual_rail_cpp: {dual_cpp}",
            f"      blind_top_internal: {top_internal}",
            f"      blind_bottom_cover_name: {quoted(blind.get('cover_name', blind['name'] + ' Bottom'))}",
            f"      blind_bottom_reported_name: {quoted(blind.get('position_name', blind['name'] + ' Bottom Reported'))}",
        ])
    if controller.get("fallback_ap_secret"):
        lines.extend([
            "",
            "wifi:",
            "  ap:",
            f"    ssid: {quoted(controller.get('fallback_ap_ssid', controller['name'] + '-fallback'))}",
            f"    password: !secret {controller['fallback_ap_secret']}",
            "",
            "captive_portal:",
        ])
    groups = controller.get("groups", [])
    if groups:
        lines.extend(["", "button:"])
    for group in groups:
        dispatchers = ", ".join(f"id({item}_dispatcher)" for item in group["blinds"])
        for label, mode in (("Open", 4), ("Closed", 5), ("Favorite", 3)):
            lines.extend([
                "  - platform: template",
                f"    name: {quoted(group['name'] + ' Bulk ' + label)}",
                f"    id: {group['id']}_bulk_{label.lower()}",
                "    on_press:",
                "      - lambda: |-",
                "          esphome::graber_dispatcher::GraberDispatcher::enqueue_bulk(",
                f"            {mode}, {{{dispatchers}}});",
            ])
        lines.extend([
            "  - platform: template",
            f"    name: {quoted(group['name'] + ' Bulk Stop')}",
            f"    id: {group['id']}_bulk_stop",
            "    on_press:",
            "      - lambda: |-",
            *[f"          id({item}_dispatcher).enqueue(1);" for item in group["blinds"]],
        ])
        tdbu_dispatchers = ", ".join(f"id({item}_dispatcher)" for item in group.get("tdbu_blinds", []))
        if tdbu_dispatchers:
            for label, mode in (("Stack Up", 9), ("Stack Down", 10)):
                lines.extend([
                    "  - platform: template",
                    f"    name: {quoted(group['name'] + ' Bulk ' + label)}",
                    f"    id: {group['id']}_bulk_{label.lower().replace(' ', '_')}",
                    "    on_press:",
                    "      - lambda: |-",
                    "          esphome::graber_dispatcher::GraberDispatcher::enqueue_bulk(",
                    f"            {mode}, {{{tdbu_dispatchers}}});",
                ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.inventory.read_text())
    validate(config)
    for controller in config["controllers"]:
        destination = args.output_dir / controller["file"]
        content = render(controller)
        if args.check:
            if not destination.exists() or destination.read_text() != content:
                raise SystemExit(f"generated file is stale: {destination}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
            print(destination)


if __name__ == "__main__":
    main()
