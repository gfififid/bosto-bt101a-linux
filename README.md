# Bosto BT-101A Linux driver

[Русская инструкция](README.ru.md)

Experimental Linux support for the **Bosto BT-101A Touch** USB pen display.
The application sends baseline JPEG frames to the RacerTech MI8808 display
controller and uses EVDI to expose a normal DRM monitor to the desktop.

| Component | USB ID | Support |
| --- | --- | --- |
| USB display | `34c7:2700` | Native `1280×800` desktop and test image |
| Pen | `0ed1:7821` | Native Linux HID input; targeted coordinate quirk included |
| Touchscreen | `34c7:2700`, interface 1 | Native Linux HID input; KDE output mapping helper included |

Verified on Fedora 44, KDE Plasma Wayland, x86-64, with kernel
`7.3.0-4.cachyrt1.fc44.x86_64`. The hardware test reached **59.4 desktop updates/s**
with a 60 fps target. Pen position, pressure, drawing and touch mapping were
also tested. Other distributions, architectures and Bosto revisions need their
own verification. The driver currently supports one connected display.

## Build

Dependencies: a C compiler, Make, pkg-config, libusb 1.0 development files,
libjpeg-turbo development files, libdrm development files, Python 3, and headers
for the exact running kernel.

Fedora userspace build dependencies:

```bash
sudo dnf install gcc make pkgconf-pkg-config libusb1-devel libjpeg-turbo-devel libdrm-devel
```

Install the matching kernel development package separately. Custom/RT kernels
need their corresponding development package. The build expects
`/lib/modules/$(uname -r)/build` to exist.

```bash
make -j4
make module -j4
make test
```

EVDI source is included at a pinned revision, so these build commands do not
download source code. `make module` builds for the current kernel. Override the
compiler if necessary with `make CC=clang`.

## First hardware test

From the project directory:

```bash
sudo ./build/bosto-display --probe
sudo ./build/bosto-display --pattern
```

`--probe` reads the USB status and EDID. `--pattern` sends a `1280×800` color-bar
image. A successful transfer message confirms the USB transfer; check the
physical screen for the image. EVDI is not required for this test.

## Run manually

```bash
sudo ./run-display.sh
```

The launcher loads the matching local EVDI module if needed, then starts the
display application. Enable the new `DVI-I-1` monitor in desktop display settings
if it is not enabled automatically. Keep the process running; Ctrl+C stops it
and disconnects the virtual monitor.

The default target is 60 fps at JPEG quality 90. The 60 Hz DRM mode and the rate
of changed frames transferred over USB are different measurements.

```bash
sudo ./run-display.sh --fps 60 --stats
```

While measuring, move a window or draw. `--stats` reports changed updates/s,
JPEG throughput and capture/encoding/USB time every two seconds. An idle desktop
needs few or no updates. The target rate is a cap, not a throughput guarantee.

## Install automatic startup

After a successful manual test and build:

```bash
sudo ./scripts/install.sh --autostart
```

The installer copies the application, its EVDI library and the current-kernel
module into a root-owned installation under `/usr/local/lib/bosto-display`.
Systemd and udev start a display service for the matching USB device at boot or
when it is connected, and stop that instance when it is unplugged. A terminal
and password prompt are not needed for subsequent automatic starts. Stop a
manually launched copy before enabling the service so two processes do not
claim the same USB interface.

For optional DKMS registration of the included EVDI module, when DKMS is
installed and no conflicting EVDI registration exists:

```bash
sudo ./scripts/install.sh --autostart --dkms
```

Without DKMS, rebuild and reinstall after a kernel update. DKMS also requires
matching headers for each new kernel. Module signing for Secure Boot is not
automated by this project.

To stage installation files without changing system services:

```bash
./scripts/install.sh --destdir ./build/stage --autostart
```

Remove the installed display service and files with:

```bash
sudo ./scripts/uninstall.sh
```

The pen coordinate override is preserved by default. See the next section for
its separate rollback.

## Pen coordinates and touch mapping

The inspected BT-101A Stylus descriptor makes Linux advertise an incorrect X
maximum of `8191`; the active pen report uses `17920`. A broad distro quirk for
another Bosto model can also give this pen an incorrect physical size. For the
matching inspected revision:

```bash
sudo ./fix-pen.sh
```

The script checks the HID descriptor fingerprint and installs
`/etc/udev/hwdb.d/99-bosto-bt101a.hwdb`. The exact Stylus collection receives
X range `0..17920`, Y range `0..10240`, and axis resolutions `83/76` units/mm.
Pressure and the separate report-`0A` Pen collection are unchanged. Reconnect
the tablet afterward so libinput and the desktop reopen it with the new ranges.

To remove that override, run `sudo ./fix-pen.sh --undo` and reconnect again.

For KDE Plasma Wayland, run this as the logged-in desktop user, **without sudo**,
after the display is enabled:

```bash
python3 tools/map_inputs.py --apply-touch --reset-pen-area
```

It maps only the known touchscreen and pen devices to `DVI-I-1` and resets the
pen output area to the full monitor. Use `--output NAME` if the connector differs.
The helper backs up affected settings before changes; `--restore BACKUP_JSON`
restores them. Other desktop environments need their own output mapping tools.

## Diagnostics and limits

```bash
python3 tools/diagnose.py
python3 tools/calibrate_pen.py --show
python3 tools/inspect_pen.py --seconds 20
```

These commands read device information or a short, tablet-only input sample.
The diagnostics exclude keyboard input and serial properties. The calibration
helper's explicit `--apply`/`--restore` actions are for a temporary, backed-up
axis correction; the persistent solution is `fix-pen.sh`.

Supported desktop buffers are 32-bit XRGB/ARGB, with frame dimensions aligned
to 16×8 pixels and within the native panel. Full-frame transfer is the tested
default. `--damage` enables experimental partial updates; hardware validation
of that option is still outstanding. Brightness control, audio, DDC/CI,
suspend/resume and broader device support are not implemented or validated.
The display application exits on a USB error; automatic startup manages a new
service instance for a reconnected USB device.

Protocol details and firmware fingerprints are in [PROTOCOL.md](PROTOCOL.md).
This repository contains source only; build outputs and local diagnostic
backups should not be committed.

## License

The original Bosto application, tools and packaging are under [MIT](LICENSE).
Vendored EVDI keeps its own licenses: GPL-2.0 for the kernel module,
LGPL-2.1-or-later for the library, and MIT for other upstream portions.
See [THIRD_PARTY.md](THIRD_PARTY.md) and [UPSTREAM.md](UPSTREAM.md).
Proprietary Windows/macOS driver binaries are not included.
