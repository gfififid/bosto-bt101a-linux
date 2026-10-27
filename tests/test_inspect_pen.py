"""Offline checks for contact summaries and exclusion of keyboard input."""

import importlib.util
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "inspect_pen.py"
SPEC = importlib.util.spec_from_file_location("inspect_pen", SOURCE)
INSPECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSPECT)


class InspectPenTests(unittest.TestCase):
    def pen(self):
        entry = {"role": "pen", "usb_id": "0ed1:7821", "observed": {},
                 "axis_info": {"pressure": {"value": 0}}}
        INSPECT.init_pen_states(entry)
        return entry

    def test_contact_and_pressure_frames(self):
        entry = self.pen()
        events = [(1, 320, 1), (3, 24, 0), (0, 0, 0),
                  (1, 330, 1), (3, 24, 4096), (0, 0, 0),
                  (3, 24, 6000), (0, 0, 0), (1, 331, 1),
                  (1, 330, 0), (3, 24, 0), (0, 0, 0)]
        for event in events:
            INSPECT.process_evdev_event(entry, *event)
        self.assertEqual(entry["pen_states"]["tip"], {"value": 0, "presses": 1, "releases": 1})
        self.assertEqual(entry["pen_states"]["in_range"]["presses"], 1)
        self.assertEqual(entry["pen_states"]["barrel"]["presses"], 1)
        self.assertEqual(entry["contact"]["tip_down_frames"], 2)
        self.assertEqual(entry["contact"]["max_pressure_tip_down"], 6000)
        self.assertEqual(entry["contact"]["max_pressure_tip_up"], 0)

    def test_contact_already_down_at_open(self):
        entry = self.pen()
        INSPECT.init_pen_states(entry, (1 << 320) | (1 << 330))
        INSPECT.process_evdev_event(entry, 3, 24, 2000)
        INSPECT.process_evdev_event(entry, 0, 0, 0)
        self.assertEqual(entry["contact"]["max_pressure_tip_down"], 2000)
        self.assertEqual(entry["pen_states"]["tip"]["presses"], 0)

    def test_keyboard_and_unrelated_buttons_not_recorded(self):
        entry = self.pen()
        snapshot = repr(entry)
        for code in (30, 48, 56, 272, 321, 332):
            INSPECT.process_evdev_event(entry, 1, code, 1)
            INSPECT.process_evdev_event(entry, 1, code, 0)
        self.assertEqual(repr(entry), snapshot)

    def test_touch_remains_axes_only(self):
        entry = {"role": "touch", "usb_id": "34c7:2700", "observed": {}}
        INSPECT.process_evdev_event(entry, 1, 330, 1)
        INSPECT.process_evdev_event(entry, 3, 53, 12000)
        self.assertNotIn("pen_states", entry)
        self.assertEqual(entry["observed"]["touch_x"]["maximum"], 12000)

    def test_ignored_hid_keyboard_report(self):
        entry = {"pen_reports": {}, "ignored_reports": 0}
        INSPECT.process_pen_report(bytes.fromhex("13 ff 04 05 06 07 08 09"), entry)
        self.assertEqual(entry, {"pen_reports": {}, "ignored_reports": 1})

    def test_raw_pen_contact_flags_and_pressure(self):
        entry = {"pen_reports": {}, "ignored_reports": 0}
        INSPECT.process_pen_report(bytes.fromhex("01 13 00 46 00 28 ff 1f 00 00"), entry)
        INSPECT.process_pen_report(bytes.fromhex("0a 43 94 5c f6 8b ff 3f 81 7f"), entry)
        self.assertEqual(entry["pen_reports"]["01"]["axes"]["pressure"]["maximum"], 8191)
        self.assertEqual(entry["pen_reports"]["0a"]["axes"]["pressure"]["maximum"], 16383)
        for report in entry["pen_reports"].values():
            self.assertEqual(report["tip_set_packets"], 1)
            self.assertEqual(report["in_range_packets"], 1)
            self.assertEqual(report["barrel_set_packets"], 1)


if __name__ == "__main__":
    unittest.main()
