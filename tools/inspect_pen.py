#!/usr/bin/env python3
"""Read-only calibration diagnostics for Bosto 0ed1:7821 and touch 34c7:2700.

Default: query only pen/touch evdev axis ranges. --seconds 1..30 additionally
summarizes axis minima/maxima while you move the pen/touch to screen corners.
--raw also summarizes raw pen report IDs 01/0a, to distinguish firmware modes.
Only the pen's tip, proximity, and first barrel-button states are summarized;
all keyboard/other button reports are discarded. No typing or raw bytes are
printed or saved. No grabs, feature reports, settings writes, or uinput devices.
"""

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import selectors
import struct
import sys
import time


IDS = {(0x0ED1, 0x7821), (0x34C7, 0x2700)}
AXES = {0: "x", 1: "y", 24: "pressure", 26: "tilt_x", 27: "tilt_y",
        53: "touch_x", 54: "touch_y"}
EVENT = struct.Struct("@llHHi")
PEN_STATES = {320: "in_range", 330: "tip", 331: "barrel"}


def ioc_read(kind, command, size):
    # Linux asm-generic _IOR, used by the target Fedora x86_64/aarch64 systems.
    return (2 << 30) | (size << 16) | (ord(kind) << 8) | command


def ioctl_bytes(fd, kind, command, size):
    value = bytearray(size)
    fcntl.ioctl(fd, ioc_read(kind, command, size), value, True)
    return bytes(value)


def text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def parse_hex(value):
    try:
        return int(value, 16)
    except ValueError:
        return -1


def open_readonly(path):
    return os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)


def summary_add(summary, name, value):
    if name not in summary:
        summary[name] = {"minimum": value, "maximum": value, "last": value, "events": 1}
    else:
        axis = summary[name]
        axis["minimum"] = min(axis["minimum"], value)
        axis["maximum"] = max(axis["maximum"], value)
        axis["last"] = value
        axis["events"] += 1


def init_pen_states(entry, keys=0):
    entry["pen_states"] = {
        name: {"value": int(bool(keys & (1 << code))), "presses": 0, "releases": 0}
        for code, name in PEN_STATES.items()
    }
    entry["contact"] = {"tip_down_frames": 0, "tip_up_frames": 0,
                        "max_pressure_tip_down": 0, "max_pressure_tip_up": 0}


def process_evdev_event(entry, event_type, code, value):
    if event_type == 3 and code in AXES:
        summary_add(entry["observed"], AXES[code], value)
    pen = entry.get("role") == "pen" and entry.get("usb_id") == "0ed1:7821"
    if not pen:
        return
    if event_type == 1 and code in PEN_STATES and value in (0, 1):
        state = entry["pen_states"][PEN_STATES[code]]
        if state["value"] != value:
            state["presses" if value else "releases"] += 1
        state["value"] = value
    elif event_type == 0 and code == 0:  # SYN_REPORT: complete pen frame.
        observed = entry["observed"].get("pressure", {})
        pressure = observed.get("last", entry.get("axis_info", {}).get("pressure", {}).get("value", 0))
        suffix = "down" if entry["pen_states"]["tip"]["value"] else "up"
        contact = entry["contact"]
        contact["tip_%s_frames" % suffix] += 1
        contact["max_pressure_tip_%s" % suffix] = max(contact["max_pressure_tip_%s" % suffix], pressure)
    elif event_type == 0 and code == 3:  # SYN_DROPPED: state is incomplete.
        entry["sampling_error"] = "Input events dropped; repeat this short diagnostic"


def collect_evdev(report, opened):
    for event in sorted(Path("/sys/class/input").glob("event*")):
        device = event / "device"
        ids = (parse_hex(text(device / "id/vendor")),
               parse_hex(text(device / "id/product")))
        if ids not in IDS:
            continue
        name = text(device / "name")
        # The shared HID also contains keyboard/media keys. Do not open them.
        if ids == (0x0ED1, 0x7821) and "keyboard" in name.lower():
            continue
        node = Path("/dev/input") / event.name
        entry = {"node": str(node), "name": name,
                 "usb_id": "%04x:%04x" % ids, "axis_info": {}, "observed": {}}
        fd = None
        try:
            fd = open_readonly(node)
            _, vendor, product, _ = struct.unpack("=HHHH", ioctl_bytes(fd, "E", 0x02, 8))
            if (vendor, product) != ids:
                raise OSError("Input device identity changed during enumeration")
            keys = int.from_bytes(ioctl_bytes(fd, "E", 0x21, 96), "little")
            pen = bool(keys & ((1 << 320) | (1 << 321)))
            touch = ids == (0x34C7, 0x2700) and bool(keys & (1 << 330))
            if not pen and not touch:
                os.close(fd)
                continue
            entry["role"] = "pen" if pen else "touch"
            if pen and ids == (0x0ED1, 0x7821):
                active = int.from_bytes(ioctl_bytes(fd, "E", 0x18, 96), "little")
                init_pen_states(entry, active)
            for code, axis_name in AXES.items():
                try:
                    values = struct.unpack("=iiiiii", ioctl_bytes(fd, "E", 0x40 + code, 24))
                except OSError as exc:
                    if exc.errno == errno.EINVAL:
                        continue
                    raise
                axis = dict(zip(("value", "minimum", "maximum", "fuzz", "flat", "resolution"), values))
                if axis["resolution"] > 0 and axis_name in ("x", "y", "touch_x", "touch_y"):
                    axis["size_mm"] = (axis["maximum"] - axis["minimum"]) / axis["resolution"]
                entry["axis_info"][axis_name] = axis
            opened.append((fd, "evdev", entry))
        except (OSError, ValueError) as exc:
            if fd is not None:
                os.close(fd)
            entry["error"] = str(exc)
        report["evdev"].append(entry)


def collect_hidraw(report, opened):
    for node_sys in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        props = dict(line.split("=", 1) for line in text(node_sys / "device/uevent").splitlines()
                     if "=" in line)
        try:
            _, vendor, product = props.get("HID_ID", "").split(":")
            if (int(vendor, 16), int(product, 16)) != (0x0ED1, 0x7821):
                continue
        except ValueError:
            continue
        node = Path("/dev") / node_sys.name
        entry = {"node": str(node), "pen_reports": {}, "ignored_reports": 0}
        fd = None
        try:
            fd = open_readonly(node)
            _, vendor, product = struct.unpack("=IHH", ioctl_bytes(fd, "H", 0x03, 8))
            if (vendor, product) != (0x0ED1, 0x7821):
                raise OSError("HID identity changed during enumeration")
            opened.append((fd, "hidraw", entry))
        except (OSError, ValueError) as exc:
            if fd is not None:
                os.close(fd)
            entry["error"] = str(exc)
        report["hidraw"].append(entry)


def process_pen_report(data, entry):
    if not data or data[0] not in (1, 0x0A) or len(data) not in (8, 10):
        entry["ignored_reports"] += 1
        return
    if data[0] == 0x0A and len(data) != 10:
        entry["ignored_reports"] += 1
        return
    key = "%02x" % data[0]
    summary = entry["pen_reports"].setdefault(key, {
        "packets": 0, "axes": {}, "tip_set_packets": 0,
        "in_range_packets": 0, "barrel_set_packets": 0,
    })
    summary["packets"] += 1
    summary["tip_set_packets"] += int(bool(data[1] & 1))
    summary["barrel_set_packets"] += int(bool(data[1] & 2))
    summary["in_range_packets"] += int(bool(data[1] & (0x10 if data[0] == 1 else 0x40)))
    for name, value in zip(("x", "y", "pressure"), struct.unpack_from("<HHH", data, 2)):
        summary_add(summary["axes"], name, value)
    if len(data) == 10:
        for name, value in zip(("tilt_x", "tilt_y"), struct.unpack_from("bb", data, 8)):
            summary_add(summary["axes"], name, value)


def sample(opened, seconds):
    deadline = time.monotonic() + seconds
    with selectors.DefaultSelector() as selector:
        for fd, kind, entry in opened:
            selector.register(fd, selectors.EVENT_READ, (kind, entry))
        while selector.get_map() and time.monotonic() < deadline:
            for key, _ in selector.select(max(0, min(0.2, deadline - time.monotonic()))):
                kind, entry = key.data
                try:
                    data = os.read(key.fd, EVENT.size * 128 if kind == "evdev" else 4096)
                    if not data:
                        raise OSError("Device disconnected")
                    if kind == "hidraw":
                        process_pen_report(data, entry)
                        continue
                    if len(data) % EVENT.size:
                        raise OSError("Incomplete input event record")
                    for _, _, event_type, code, value in EVENT.iter_unpack(data):
                        process_evdev_event(entry, event_type, code, value)
                except BlockingIOError:
                    pass
                except OSError as exc:
                    entry["sampling_error"] = str(exc)
                    selector.unregister(key.fd)


def duration(value):
    number = float(value)
    if not 0 <= number <= 30:
        raise argparse.ArgumentTypeError("seconds must be between 0 and 30")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=duration, default=0,
                        help="sample only pen/touch axes for up to 30 seconds (default: query only)")
    parser.add_argument("--raw", action="store_true",
                        help="also identify active raw pen reports 01/0a; discard all other reports")
    args = parser.parse_args()
    report = {"read_only": True, "requested_sample_seconds": args.seconds, "evdev": [], "hidraw": []}
    opened = []
    try:
        collect_evdev(report, opened)
        if args.raw:
            collect_hidraw(report, opened)
        if args.seconds and opened:
            print("Move the pen across all four screen corners and press lightly/firmly. "
                  "Touch corners too if diagnosing touch. Sampling %.1f seconds; no settings change."
                  % args.seconds, file=sys.stderr)
            sample(opened, args.seconds)
    except KeyboardInterrupt:
        report["interrupted"] = True
    finally:
        for fd, _, _ in opened:
            os.close(fd)
    print(json.dumps(report, indent=2))
    return 0 if opened else 1


if __name__ == "__main__":
    raise SystemExit(main())
