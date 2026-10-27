#!/usr/bin/env python3
"""Inspect or map Bosto input devices in the current KDE Wayland session.

Run as the desktop user, without sudo. Default/--show is read-only. Changes
are limited to known Bosto pen/tablet and RacerTech touch devices; a config
copy and typed D-Bus property backup are saved before any change.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


SERVICE = "org.kde.KWin"
IFACE = "org.kde.KWin.InputDevice"
ROOT = "/org/kde/KWin/InputDevice"
PEN_IDS = (0x0ED1, 0x7821)
TOUCH_IDS = (0x34C7, 0x2700)
PROPERTIES = ("name", "vendor", "product", "touch", "tabletTool", "enabled",
              "outputName", "outputArea", "mapToWorkspace", "inputArea", "size",
              "calibrationMatrix")
CONFIG_KEYS = {"outputName": b"OutputUuid", "outputArea": b"OutputArea"}


class MappingError(Exception):
    pass


def command(args):
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MappingError("{}: {}".format(args[0], exc)) from exc
    if result.returncode:
        raise MappingError("{} failed: {}".format(args[0], result.stderr.strip() or result.stdout.strip()))
    return result.stdout


def get_property(path, name):
    try:
        return json.loads(command(["busctl", "--user", "--json=short", "get-property",
                                   SERVICE, path, IFACE, name]))
    except json.JSONDecodeError as exc:
        raise MappingError("busctl did not return valid JSON") from exc


def discover():
    listing = command(["busctl", "--user", "--list", "tree", SERVICE])
    paths = sorted(set(re.findall(r"^" + re.escape(ROOT) + r"/event\d+$", listing, re.MULTILINE)))
    devices = []
    for path in paths:
        vendor = get_property(path, "vendor")
        product = get_property(path, "product")
        if (vendor["data"], product["data"]) not in (PEN_IDS, TOUCH_IDS):
            continue
        props = {"vendor": vendor, "product": product}
        for name in PROPERTIES:
            if name not in props:
                props[name] = get_property(path, name)
        devices.append({"path": path, "properties": props})
    return devices


def identity(device):
    props = device["properties"]
    return {"vendor": props["vendor"]["data"], "product": props["product"]["data"],
            "name": props["name"]["data"], "path": device["path"]}


def known_kind(device):
    props = device["properties"]
    ids = (props["vendor"]["data"], props["product"]["data"])
    if ids == TOUCH_IDS and props["touch"]["data"]:
        return "touch"
    if ids == PEN_IDS and props["tabletTool"]["data"]:
        return "pen"
    return None


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "kcminputrc"


def config_group(device_id):
    name = device_id["name"]
    if any(char in name for char in "[]\r\n"):
        raise MappingError("Unsupported characters in device config group name")
    return "[Libinput][{}][{}][{}]".format(device_id["vendor"], device_id["product"], name).encode()


def validate_property(name, value):
    if name == "outputName" and value.get("type") == "s" and isinstance(value.get("data"), str):
        if "\x00" not in value["data"]:
            return
    if name == "outputArea" and value.get("type") == "(dddd)":
        data = value.get("data")
        if isinstance(data, list) and len(data) == 4 and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in data
        ):
            return
    raise MappingError("Invalid or unsupported backup property: " + name)


def set_property(path, name, value):
    validate_property(name, value)
    data = value["data"]
    args = [str(x) for x in data] if isinstance(data, list) else [data]
    command(["busctl", "--user", "set-property", SERVICE, path, IFACE,
             name, value["type"]] + args)
    actual = get_property(path, name)
    if actual != value:
        raise MappingError("{} {} did not retain requested value".format(path, name))


def ensure_output(name):
    if not shutil.which("kscreen-doctor"):
        raise MappingError("kscreen-doctor is required to verify the target output")
    try:
        outputs = json.loads(command(["kscreen-doctor", "-j"]))["outputs"]
    except (KeyError, json.JSONDecodeError) as exc:
        raise MappingError("Cannot parse kscreen-doctor output") from exc
    if not any(output.get("name") == name and output.get("connected") and output.get("enabled")
               for output in outputs):
        raise MappingError("Target output is not connected and enabled: " + name)


def save_backup(changes, devices, base):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = base.expanduser().resolve() / stamp
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    config = config_path().expanduser().resolve()
    exists = config.exists()
    saved_config = directory / "kcminputrc"
    if exists:
        shutil.copy2(config, saved_config)
        os.chmod(saved_config, 0o600)
        content = saved_config.read_bytes()
    else:
        content = b""
        saved_config.write_bytes(content)
        os.chmod(saved_config, 0o600)
    old_changes = []
    by_path = {device["path"]: device for device in devices}
    for change in changes:
        device = by_path[change["device"]["path"]]
        old_changes.append({"device": identity(device), "properties": {
            name: device["properties"][name] for name in change["properties"]
        }})
    backup = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "uid": os.getuid(),
        "config": {"path": str(config), "backup_path": str(saved_config),
                   "sha256": hashlib.sha256(content).hexdigest(), "existed": exists},
        "changes": old_changes,
    }
    path = directory / "backup.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(backup, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)
    return path


def match_backup(backup, devices):
    if backup.get("schema_version") != 1 or backup.get("uid") != os.getuid():
        raise MappingError("Backup version or desktop user does not match")
    if not isinstance(backup.get("changes"), list) or not backup["changes"]:
        raise MappingError("Backup has no device changes")
    matched = []
    seen = set()
    for saved in backup["changes"]:
        old = saved["device"]
        candidates = [device for device in devices if known_kind(device) and all(
            identity(device)[key] == old[key] for key in ("vendor", "product", "name")
        )]
        if len(candidates) != 1:
            raise MappingError("Cannot uniquely match saved device: " + old["name"])
        device = candidates[0]
        if device["path"] in seen:
            raise MappingError("Backup contains duplicate device entries")
        seen.add(device["path"])
        if not isinstance(saved["properties"], dict) or not saved["properties"]:
            raise MappingError("Backup device has no properties")
        for name, value in saved["properties"].items():
            validate_property(name, value)
        matched.append({"device": identity(device), "properties": saved["properties"]})
    return matched


def config_sections(content):
    sections = []
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(b"[") and stripped.endswith(b"]"):
            sections.append((stripped, [line]))
        else:
            if not sections:
                sections.append((None, []))
            sections[-1][1].append(line)
    return sections


def entry_key(line):
    if line.lstrip().startswith((b"#", b";")) or b"=" not in line:
        return None
    return line.split(b"=", 1)[0].strip().split(b"[", 1)[0]


def restored_config(current, original, changes):
    """Restore only affected keys; preserve all unrelated current settings."""
    targets = {config_group(change["device"]): {CONFIG_KEYS[key] for key in change["properties"]}
               for change in changes}
    original_entries = {group: [] for group in targets}
    for group, lines in config_sections(original):
        if group in targets:
            original_entries[group].extend(
                line if line.endswith(b"\n") else line + b"\n"
                for line in lines[1:] if entry_key(line) in targets[group]
            )
    result = []
    emitted = set()
    for group, lines in config_sections(current):
        if group not in targets:
            result.extend(lines)
            continue
        kept = [line for line in lines[1:] if entry_key(line) not in targets[group]]
        entries = original_entries[group] if group not in emitted else []
        if kept or entries:
            result.append(lines[0])
            result.extend(kept)
            if entries and result and not result[-1].endswith(b"\n"):
                result[-1] += b"\n"
            result.extend(entries)
        emitted.add(group)
    for group, entries in original_entries.items():
        if group not in emitted and entries:
            if result and not result[-1].endswith(b"\n"):
                result[-1] += b"\n"
            result.append(group + b"\n")
            result.extend(entries)
    return b"".join(result)


def restore_config_keys(saved_config, changes):
    path = config_path().expanduser().resolve()
    if Path(saved_config["path"]).expanduser().resolve() != path:
        raise MappingError("Backup config path differs from this desktop user's config")
    original = Path(saved_config["backup_path"]).read_bytes()
    if hashlib.sha256(original).hexdigest() != saved_config["sha256"]:
        raise MappingError("Config backup checksum mismatch")
    current = path.read_bytes() if path.exists() else b""
    updated = restored_config(current, original, changes)
    if updated == current:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".kcminputrc-bosto-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def show(devices):
    if not devices:
        print("No matching Bosto/RacerTech devices are exposed by KWin.")
    for device in devices:
        ident = identity(device)
        print("{} {:04x}:{:04x} {} ({})".format(
            device["path"], ident["vendor"], ident["product"], ident["name"], known_kind(device) or "other"))
        for name in ("enabled", "outputName", "outputArea", "mapToWorkspace", "inputArea", "size", "calibrationMatrix"):
            print("  {}: {}".format(name, json.dumps(device["properties"][name]["data"], ensure_ascii=False)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="read current mappings (default)")
    parser.add_argument("--apply-touch", action="store_true", help="map known RacerTech touchscreen to the output")
    parser.add_argument("--reset-pen-area", action="store_true", help="map known Bosto pen devices and reset output area to 0,0,1,1")
    parser.add_argument("--output", default="DVI-I-1", help="enabled target output connector (default: DVI-I-1)")
    parser.add_argument("--backup-dir", type=Path, default=Path(__file__).resolve().parents[1] / "build/input-backups",
                        help="parent directory for timestamped backups")
    parser.add_argument("--restore", type=Path, metavar="BACKUP_JSON", help="restore saved mappings and affected config keys")
    args = parser.parse_args()
    if args.show and (args.apply_touch or args.reset_pen_area or args.restore):
        parser.error("--show cannot be combined with a change action")
    if args.restore and (args.apply_touch or args.reset_pen_area):
        parser.error("--restore cannot be combined with other change actions")
    if os.geteuid() == 0:
        raise MappingError("Run as the logged-in desktop user, without sudo")
    devices = discover()
    if not (args.apply_touch or args.reset_pen_area or args.restore):
        show(devices)
        return 0
    original_backup = None
    if args.restore:
        original_backup = json.loads(args.restore.read_text(encoding="utf-8"))
        changes = match_backup(original_backup, devices)
        # Validate the config and fingerprint before changing any live property.
        saved = original_backup["config"]
        if Path(saved["path"]).expanduser().resolve() != config_path().expanduser().resolve():
            raise MappingError("Backup belongs to a different config location")
        if hashlib.sha256(Path(saved["backup_path"]).read_bytes()).hexdigest() != saved["sha256"]:
            raise MappingError("Config backup checksum mismatch")
        for change in changes:
            for name, value in change["properties"].items():
                if name == "outputName" and value["data"]:
                    ensure_output(value["data"])
    else:
        ensure_output(args.output)
        changes = []
        found = {"touch": 0, "pen": 0}
        for device in devices:
            kind = known_kind(device)
            if not ((kind == "touch" and args.apply_touch) or (kind == "pen" and args.reset_pen_area)):
                continue
            found[kind] += 1
            values = {"outputName": {"type": "s", "data": args.output}}
            if kind == "pen":
                values["outputArea"] = {"type": "(dddd)", "data": [0.0, 0.0, 1.0, 1.0]}
            changed = {key: value for key, value in values.items() if device["properties"][key] != value}
            if changed:
                changes.append({"device": identity(device), "properties": changed})
        if args.apply_touch and not found["touch"]:
            raise MappingError("No known 34c7:2700 touchscreen found")
        if args.reset_pen_area and not found["pen"]:
            raise MappingError("No known 0ed1:7821 tablet-tool device found")
        if not changes:
            print("Requested mappings are already active; no settings changed.")
            return 0
    for change in changes:
        config_group(change["device"])
    backup_path = save_backup(changes, devices, args.backup_dir)
    print("Backup: " + str(backup_path), flush=True)
    try:
        for change in changes:
            for name, value in change["properties"].items():
                set_property(change["device"]["path"], name, value)
        if original_backup is not None:
            restore_config_keys(original_backup["config"], changes)
    except Exception:
        print("A change failed; restore saved state with:\n  python3 {} --restore {}".format(
            Path(__file__).resolve(), backup_path), file=sys.stderr)
        raise
    print("Restored saved mappings." if original_backup else "Input mappings updated.")
    show(discover())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MappingError, OSError, ValueError, KeyError, TypeError) as exc:
        print("Mapping error: {}".format(exc), file=sys.stderr)
        sys.exit(1)
