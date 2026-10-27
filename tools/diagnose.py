#!/usr/bin/env python3
"""Read-only Bosto USB, HID, input and display diagnostics (Python 3.9+).

No input event streams are opened and no hardware settings are changed. Device
serial numbers and raw EDID data are deliberately excluded from the report.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


SUPPORTED_IDS = {("0ed1", "7821"), ("34c7", "2700")}


class Reader:
    def __init__(self):
        self.warnings = []

    def bytes(self, path):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            self.warnings.append("{}: {}".format(path, exc.strerror or str(exc)))
            return None

    def text(self, path):
        value = self.bytes(path)
        return value.decode("utf-8", errors="replace").strip() if value is not None else None

    def children(self, path, pattern="*"):
        try:
            return sorted(path.glob(pattern), key=lambda p: p.name)
        except OSError as exc:
            self.warnings.append("{}: {}".format(path, exc.strerror or str(exc)))
            return []


def properties(text):
    result = {}
    for line in (text or "").splitlines():
        if line.startswith("E:"):
            line = line[2:]
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def input_properties(text):
    """Keep classification and calibration only; never serials or unique IDs."""
    return {
        key: value for key, value in properties(text).items()
        if key == "ID_INPUT" or key.startswith("ID_INPUT_")
        or key.startswith("EVDEV_ABS_")
        or key in ("LIBINPUT_IGNORE_DEVICE", "LIBINPUT_CALIBRATION_MATRIX")
    }


def driver(path):
    try:
        return Path(os.readlink(path / "driver")).name
    except OSError:
        return None


def node_info(path):
    info = {"path": str(path), "exists": False, "readable": False, "writable": False}
    try:
        mode = path.stat().st_mode
        info.update(exists=True, readable=os.access(path, os.R_OK),
                    writable=os.access(path, os.W_OK), character_device=stat.S_ISCHR(mode))
    except FileNotFoundError:
        pass
    except OSError as exc:
        info["error"] = exc.strerror or str(exc)
    return info


def read_id(reader, path):
    vendor = reader.text(path / "idVendor")
    product = reader.text(path / "idProduct")
    return ((vendor or "").lower(), (product or "").lower())


def collect_usb(reader, sys_root, dev_root):
    devices = []
    for path in reader.children(sys_root / "bus/usb/devices"):
        vendor, product = read_id(reader, path)
        if (vendor, product) not in SUPPORTED_IDS:
            continue
        uevent = properties(reader.text(path / "uevent"))
        entry = {
            "path": str(path), "name": path.name, "vendor_id": vendor,
            "product_id": product, "manufacturer": reader.text(path / "manufacturer"),
            "product": reader.text(path / "product"),
            "speed_mbps": reader.text(path / "speed"), "interfaces": [],
        }
        devname = uevent.get("DEVNAME")
        if devname and devname.startswith("bus/usb/") and ".." not in Path(devname).parts:
            entry["device_node"] = node_info(dev_root / devname)
        for interface in reader.children(path.parent, path.name + ":*"):
            endpoints = []
            for endpoint in reader.children(interface, "ep_*"):
                endpoints.append({
                    "address": reader.text(endpoint / "bEndpointAddress"),
                    "attributes": reader.text(endpoint / "bmAttributes"),
                    "max_packet_size_hex": reader.text(endpoint / "wMaxPacketSize"),
                })
            entry["interfaces"].append({
                "path": str(interface), "name": interface.name,
                "number_hex": reader.text(interface / "bInterfaceNumber"),
                "class_hex": reader.text(interface / "bInterfaceClass"),
                "subclass_hex": reader.text(interface / "bInterfaceSubClass"),
                "protocol_hex": reader.text(interface / "bInterfaceProtocol"),
                "driver": driver(interface), "endpoints": endpoints,
            })
        devices.append(entry)
    return devices


def collect_hid(reader, sys_root, dev_root):
    devices = []
    for path in reader.children(sys_root / "bus/hid/devices"):
        props = properties(reader.text(path / "uevent"))
        try:
            _, vendor, product = props.get("HID_ID", "").split(":")
            ids = ("{:04x}".format(int(vendor, 16)), "{:04x}".format(int(product, 16)))
        except ValueError:
            continue
        if ids not in SUPPORTED_IDS:
            continue
        descriptor_path = path / "report_descriptor"
        descriptor = reader.bytes(descriptor_path)
        devices.append({
            "path": str(path), "name": props.get("HID_NAME"),
            "vendor_id": ids[0], "product_id": ids[1], "driver": driver(path),
            "report_descriptor": {
                "path": str(descriptor_path), "available": descriptor is not None,
                "size_bytes": len(descriptor) if descriptor is not None else None,
                "sha256": hashlib.sha256(descriptor).hexdigest() if descriptor is not None else None,
                "hex": descriptor.hex(" ") if descriptor is not None else None,
            },
            "hidraw_nodes": [node_info(dev_root / p.name)
                             for p in reader.children(path / "hidraw", "hidraw*")],
        })
    return devices


def collect_inputs(reader, sys_root, udev_root, dev_root):
    devices = []
    for path in reader.children(sys_root / "class/input", "input*"):
        ids = ((reader.text(path / "id/vendor") or "").lower(),
               (reader.text(path / "id/product") or "").lower())
        if ids not in SUPPORTED_IDS:
            continue
        parent_udev_path = udev_root / "data" / ("+input:" + path.name)
        parent_props = input_properties(reader.text(parent_udev_path))
        entry = {
            "path": str(path), "name": reader.text(path / "name"),
            "vendor_id": ids[0], "product_id": ids[1],
            "input_properties_hex": reader.text(path / "properties"),
            "udev_path": str(parent_udev_path), "udev_properties": parent_props,
            "events": [],
        }
        combined = dict(parent_props)
        for event_path in reader.children(path, "event*"):
            device_number = reader.text(event_path / "dev")
            udev_path = udev_root / "data" / ("c" + device_number) if device_number else None
            event_props = input_properties(reader.text(udev_path)) if udev_path else {}
            combined.update(event_props)
            entry["events"].append({
                "name": event_path.name, "node": node_info(dev_root / "input" / event_path.name),
                "udev_path": str(udev_path) if udev_path else None,
                "udev_properties": event_props,
            })
        entry["classifications"] = [key.removeprefix("ID_INPUT_").lower()
                                    for key, value in sorted(combined.items())
                                    if key.startswith("ID_INPUT_") and value == "1"
                                    and not key.endswith("_MM")]
        devices.append(entry)
    return devices


def edid_summary(data):
    """Extract monitor name/size; do not include serial numbers or raw EDID."""
    if not data or len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {}
    manufacturer_code = int.from_bytes(data[8:10], "big")
    manufacturer = "".join(chr(64 + ((manufacturer_code >> shift) & 31))
                           for shift in (10, 5, 0))
    summary = {"manufacturer_code": manufacturer,
               "width_mm": data[21] * 10, "height_mm": data[22] * 10}
    for offset in (54, 72, 90, 108):
        block = data[offset:offset + 18]
        if block[:5] == b"\x00\x00\x00\xfc\x00":
            summary["monitor_name"] = block[5:18].decode("ascii", errors="replace").strip(" \n\x00")
    return summary


def collect_drm(reader, sys_root):
    monitors = []
    for path in reader.children(sys_root / "class/drm", "card*-*"):
        if reader.text(path / "status") != "connected":
            continue
        monitors.append({
            "path": str(path), "connector": path.name, "status": "connected",
            "modes": list(dict.fromkeys((reader.text(path / "modes") or "").splitlines())),
            "enabled": reader.text(path / "enabled"),
            "edid": edid_summary(reader.bytes(path / "edid")),
        })
    return monitors


def collect(args):
    reader = Reader()
    sys_root = Path(args.sys_root)
    udev_root = Path(args.udev_root)
    dev_root = Path(args.dev_root)
    modules_root = Path(args.modules_root)
    uname = os.uname()
    kernel_dir = modules_root / uname.release
    build = kernel_dir / "build"
    for root in (sys_root, udev_root, dev_root):
        if not root.is_dir():
            reader.warnings.append("{}: directory unavailable".format(root))
    try:
        build_target = os.readlink(build)
    except OSError:
        build_target = None
    result = {
        "schema_version": 1,
        "system": {"kernel_release": uname.release, "kernel_version": uname.version,
                   "machine": uname.machine},
        "roots": {"sys": str(sys_root), "udev": str(udev_root),
                  "dev": str(dev_root), "modules": str(modules_root)},
        "usb_devices": collect_usb(reader, sys_root, dev_root),
        "hid_devices": collect_hid(reader, sys_root, dev_root),
        "input_devices": collect_inputs(reader, sys_root, udev_root, dev_root),
        "connected_monitors": collect_drm(reader, sys_root),
        "device_access": {
            "input_directory": node_info(dev_root / "input"),
            "usb_directory": node_info(dev_root / "bus/usb"),
            "dri_directory": node_info(dev_root / "dri"),
            "uinput": node_info(dev_root / "uinput"),
        },
        "evdi": {
            "loaded": (sys_root / "module/evdi").is_dir(),
            "kernel_module_directory": str(kernel_dir),
            "kernel_build_directory": str(build), "kernel_build_available": build.is_dir(),
            "kernel_build_symlink_target": build_target,
            "installed_modules": [str(p) for p in reader.children(kernel_dir, "**/evdi.ko*")],
        },
        "warnings": reader.warnings,
        "notes": [
            "Read-only: no input events are read and no hardware settings are changed.",
            "USB/HID serial properties, HID_UNIQ and raw EDID are excluded.",
            "Reported permissions describe this process; a sandbox may hide host device nodes.",
            "Descriptor capabilities do not prove that live pen, touch or display data works.",
        ],
    }
    return result


def show_node(info):
    if not info["exists"]:
        return "{}: unavailable{}".format(info["path"], " (" + info["error"] + ")" if info.get("error") else "")
    return "{}: present, read={}, write={}".format(info["path"], info["readable"], info["writable"])


def human_report(report):
    lines = ["Bosto Linux read-only diagnostics", "Kernel: {} ({})".format(
        report["system"]["kernel_release"], report["system"]["machine"]), "", "USB devices:"]
    for device in report["usb_devices"]:
        lines.append("  {} {}:{} {}".format(device["path"], device["vendor_id"],
                     device["product_id"], device["product"] or ""))
        if "device_node" in device:
            lines.append("    " + show_node(device["device_node"]))
        for interface in device["interfaces"]:
            lines.append("    {} class={} driver={}".format(interface["name"],
                         interface["class_hex"], interface["driver"] or "unbound"))
    if not report["usb_devices"]:
        lines.append("  No matching 0ed1:7821 or 34c7:2700 devices found.")
    lines.extend(["", "HID descriptors:"])
    for device in report["hid_devices"]:
        desc = device["report_descriptor"]
        lines.append("  {} driver={}".format(device["path"], device["driver"] or "unbound"))
        lines.append("    Descriptor: " + desc["path"])
        lines.append("    {} bytes, SHA256 {}".format(desc["size_bytes"], desc["sha256"]) if desc["available"]
                     else "    Descriptor unavailable: " + desc["path"])
        if desc["hex"]:
            octets = desc["hex"].split()
            lines.extend("    " + " ".join(octets[i:i + 24]) for i in range(0, len(octets), 24))
        lines.extend("    " + show_node(node) for node in device["hidraw_nodes"])
    lines.extend(["", "Input devices (events are not read):"])
    for device in report["input_devices"]:
        lines.append("  {}: {} [{}]".format(device["path"], device["name"],
                     ", ".join(device["classifications"]) or "unclassified"))
        for key, value in sorted(device["udev_properties"].items()):
            lines.append("    {}={}".format(key, value))
        for event in device["events"]:
            lines.append("    " + show_node(event["node"]))
            lines.append("      udev: {}".format(event["udev_path"] or "unavailable"))
            for key, value in sorted(event["udev_properties"].items()):
                lines.append("      {}={}".format(key, value))
    lines.extend(["", "Connected DRM monitors:"])
    for monitor in report["connected_monitors"]:
        lines.append("  {} {} modes={}".format(monitor["connector"],
                     monitor["edid"].get("monitor_name", ""), ", ".join(monitor["modes"]) or "unavailable"))
    if not report["connected_monitors"]:
        lines.append("  No connected monitors visible in DRM sysfs.")
    lines.extend(["", "Device access:"])
    lines.extend("  " + show_node(info) for info in report["device_access"].values())
    evdi = report["evdi"]
    lines.extend(["", "EVDI:", "  Loaded: {}".format(evdi["loaded"]),
                  "  Kernel module build directory: {} ({})".format(
                      evdi["kernel_build_directory"], "available" if evdi["kernel_build_available"] else "unavailable"),
                  "  Installed modules: " + (", ".join(evdi["installed_modules"]) or "none found")])
    if report["warnings"]:
        lines.extend(["", "Warnings:"] + ["  " + warning for warning in report["warnings"]])
    lines.extend([""] + report["notes"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sys-root", default="/sys", help="sysfs root (default: /sys)")
    parser.add_argument("--udev-root", default="/run/udev", help="udev runtime root (default: /run/udev)")
    parser.add_argument("--dev-root", default="/dev", help="device root (default: /dev)")
    parser.add_argument("--modules-root", default="/lib/modules", help="kernel modules root (default: /lib/modules)")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args()
    report = collect(args)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else human_report(report))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
