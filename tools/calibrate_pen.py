#!/usr/bin/env python3
"""Guarded native axis metadata correction for the inspected Bosto BT-101A.

--show (default) only reads. --apply explicitly updates the active legacy Stylus
ABS_X maximum and X/Y resolution with EVIOCSABS, after an exclusive backup.
--restore BACKUP restores that saved metadata. No HID feature reports, input
grabs, pressure changes, kernel modules, or persistent system files are used.
Existing libinput clients may cache metadata until the device is reopened.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import sys


USB_ID = (0x0ED1, 0x7821)
NAME = "Pen Tablet  Pen Tablet  Stylus"
DESCRIPTOR_SHA256 = "6f04ee782e6f0a79f3ab349bf4839c4ac1602795fa7534f73e4ce23c98ce23b5"
FIELDS = ("value", "minimum", "maximum", "fuzz", "flat", "resolution")
EXPECTED_MAX = {"x": 8191, "y": 10240}
CORRECTED_MAX = {"x": 17920, "y": 10240}
MODEL_MM = (217, 135)


def ioc(direction, kind, command, size):
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | command


def read_ioctl(fd, command, size):
    data = bytearray(size)
    fcntl.ioctl(fd, ioc(2, "E", command, size), data, True)
    return bytes(data)


def read_abs(fd, code):
    return dict(zip(FIELDS, struct.unpack("=iiiiii", read_ioctl(fd, 0x40 + code, 24))))


def write_abs(fd, code, metadata):
    # Preserve the current position; only metadata is changed.
    metadata = dict(metadata, value=read_abs(fd, code)["value"])
    data = struct.pack("=iiiiii", *(metadata[field] for field in FIELDS))
    fcntl.ioctl(fd, ioc(1, "E", 0xC0 + code, len(data)), data)


def text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def find_device():
    matches = []
    for event in Path("/sys/class/input").glob("event*"):
        device = event / "device"
        try:
            ids = (int(text(device / "id/vendor"), 16), int(text(device / "id/product"), 16))
        except ValueError:
            continue
        if ids != USB_ID or text(device / "name") != NAME:
            continue
        hid = None
        for parent in device.resolve().parents:
            if (parent / "report_descriptor").is_file():
                hid = parent
                break
        if hid is None:
            raise RuntimeError("Cannot locate the Stylus HID report descriptor")
        digest = hashlib.sha256((hid / "report_descriptor").read_bytes()).hexdigest()
        if digest != DESCRIPTOR_SHA256:
            raise RuntimeError("HID descriptor differs from the inspected BT-101A; refusing correction")
        matches.append({"node": str(Path("/dev/input") / event.name), "name": NAME,
                        "usb_id": "0ed1:7821", "descriptor_sha256": digest})
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one inspected BT-101A Stylus, found %d" % len(matches))
    return matches[0]


def physical_size():
    """Prefer a precise, model-plausible native-mode EDID DTD image size.

    The supplied device advertises 384x240 mm in its DTD despite being 10.1in;
    reject that copied display geometry. Base EDID cm fields are too coarse.
    """
    rejected = []
    candidates = []
    for path in Path("/sys/class/drm").glob("card*-*/edid"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
            continue
        if len(data) % 128 or any(sum(data[i:i + 128]) % 256 for i in range(0, len(data), 128)):
            continue
        for offset in (54, 72, 90, 108):
            dtd = data[offset:offset + 18]
            if not dtd[0] and not dtd[1]:
                continue
            pixels = (dtd[2] | ((dtd[4] & 0xF0) << 4),
                      dtd[5] | ((dtd[7] & 0xF0) << 4))
            if pixels != (1280, 800):
                continue
            mm = (dtd[12] | ((dtd[14] & 0xF0) << 4),
                  dtd[13] | ((dtd[14] & 0x0F) << 8))
            if all(abs(value - expected) <= expected * 0.05 for value, expected in zip(mm, MODEL_MM)):
                candidates.append((mm, str(path)))
            else:
                rejected.append({"edid": str(path), "dtd_size_mm": list(mm),
                                 "reason": "outside 5% of the BT-101A physical dimensions"})
    if len(candidates) == 1:
        size, source = candidates[0]
        return {"size_mm": list(size), "source": source, "rejected_edid_sizes": rejected}
    return {"size_mm": list(MODEL_MM), "source": "BT-101A manufacturer specification",
            "rejected_edid_sizes": rejected}


def target_axes(axes, physical):
    result = {}
    for axis, mm in zip(("x", "y"), physical["size_mm"]):
        result[axis] = dict(axes[axis], maximum=CORRECTED_MAX[axis],
                            resolution=round(CORRECTED_MAX[axis] / mm))
    return result


def metadata_equal(left, right):
    return all(left[key] == right[key] for key in FIELDS if key != "value")


def save_backup(path, device, axes):
    backup = {"schema_version": 1, "device": device, "original_axes": axes,
              "created_utc": datetime.now(timezone.utc).isoformat()}
    # Do not overwrite a previous original calibration on repeated runs.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(backup, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_backup(path, device):
    backup = json.loads(path.read_text())
    if not isinstance(backup, dict) or not isinstance(backup.get("device"), dict):
        raise RuntimeError("Invalid calibration backup structure")
    if backup.get("schema_version") != 1 or any(
        backup.get("device", {}).get(field) != device[field]
        for field in ("name", "usb_id", "descriptor_sha256")
    ):
        raise RuntimeError("Backup does not match the inspected BT-101A Stylus")
    axes = backup.get("original_axes", {})
    if not isinstance(axes, dict):
        raise RuntimeError("Invalid axis metadata in backup")
    for axis in ("x", "y"):
        info = axes.get(axis, {})
        if not isinstance(info, dict) or set(info) != set(FIELDS) or not all(type(info[key]) is int for key in FIELDS):
            raise RuntimeError("Invalid axis metadata in backup")
        if info["minimum"] != 0 or info["maximum"] != EXPECTED_MAX[axis]:
            raise RuntimeError("Backup does not contain the original inspected axis ranges")
        if any(not -(2**31) <= value < 2**31 for value in info.values()):
            raise RuntimeError("Backup axis metadata is outside signed 32-bit range")
        if info["resolution"] < 0 or info["fuzz"] < 0 or info["flat"] < 0:
            raise RuntimeError("Backup axis metadata is invalid")
    return axes


def replace_metadata(fd, before, after):
    changed = []
    try:
        for code, axis in enumerate(("x", "y")):
            if metadata_equal(before[axis], after[axis]):
                continue
            write_abs(fd, code, after[axis])
            changed.append((code, axis))
            if not metadata_equal(read_abs(fd, code), after[axis]):
                raise RuntimeError("Kernel did not retain requested %s metadata" % axis)
    except (OSError, RuntimeError) as original:
        rollback_errors = []
        for code, axis in reversed(changed):
            try:
                write_abs(fd, code, before[axis])
            except OSError as exc:
                rollback_errors.append(str(exc))
        if rollback_errors:
            raise RuntimeError("Correction failed (%s); rollback errors: %s" %
                               (original, "; ".join(rollback_errors))) from original
        raise
    return bool(changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--show", action="store_true", help="read metadata only (default)")
    action.add_argument("--apply", action="store_true", help="explicitly correct native axis metadata")
    action.add_argument("--restore", type=Path, metavar="BACKUP", help="restore saved original metadata")
    parser.add_argument("--backup", type=Path,
                        help="new backup path for --apply (default: timestamped file beside this package)")
    args = parser.parse_args()
    if args.backup and not args.apply:
        parser.error("--backup is only valid with --apply")
    try:
        device = find_device()
        flags = os.O_RDWR if args.apply or args.restore else os.O_RDONLY
        fd = os.open(device["node"], flags | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            _, vendor, product, _ = struct.unpack("=HHHH", read_ioctl(fd, 0x02, 8))
            if (vendor, product) != USB_ID:
                raise RuntimeError("Device identity changed during enumeration")
            actual_name = read_ioctl(fd, 0x06, 256).split(b"\0", 1)[0].decode("utf-8")
            if actual_name != NAME:
                raise RuntimeError("Input name changed during enumeration")
            before = {axis: read_abs(fd, code) for code, axis in enumerate(("x", "y"))}
            physical = physical_size()
            target = target_axes(before, physical)
            result = {"device": device, "before": before, "proposed": target,
                      "physical_geometry": physical, "changed": False}
            if args.apply:
                already_correct = all(metadata_equal(before[axis], target[axis]) for axis in ("x", "y"))
                if not already_correct:
                    if any(before[axis]["minimum"] != 0 or before[axis]["maximum"] != EXPECTED_MAX[axis]
                           for axis in ("x", "y")):
                        raise RuntimeError("Axis ranges differ from the confirmed 8191x10240 legacy Stylus")
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                    backup = args.backup or Path(__file__).resolve().parent.parent / ("pen-calibration-%s.json" % stamp)
                    save_backup(backup, device, before)
                    result["backup"] = str(backup.resolve())
                    result["changed"] = replace_metadata(fd, before, target)
            elif args.restore:
                original = load_backup(args.restore, device)
                # Accept only the original or our corrected ranges, so an
                # unrelated later calibration cannot be silently overwritten.
                if any(before[axis]["minimum"] != 0 or before[axis]["maximum"] not in
                       (EXPECTED_MAX[axis], CORRECTED_MAX[axis]) for axis in ("x", "y")):
                    raise RuntimeError("Current axis ranges are outside this correction's scope")
                result["changed"] = replace_metadata(fd, before, original)
                result["restored_from"] = str(args.restore.resolve())
            result["after"] = {axis: read_abs(fd, code) for code, axis in enumerate(("x", "y"))}
            print(json.dumps(result, indent=2))
        finally:
            os.close(fd)
    except (OSError, ValueError, RuntimeError) as exc:
        print("Calibration: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
