# RacerTech MI8808 interoperability notes

The transport was recovered from local disassembly of vendor Windows/macOS
display drivers and reimplemented independently. The vendor executables were
not run and are not distributed with this project. The resulting Linux
implementation has been tested on a Bosto BT-101A Touch.

## Device and transport

The implemented target is USB **`34c7:2700`**. The macOS driver identifies PIDs
`2699` and `2700` as MI8808 (`Create:`, x86-64 VA `0x100016b65`); support for
`2699` is not enabled or claimed by this implementation.

Composite interface 0 is vendor-specific display transport. The implementation
validates its active configuration and requires bulk OUT endpoint `0x01`. Touch
HID on interface 1, mouse HID and storage interfaces remain attached to their
existing drivers. The application never detaches a kernel driver.

The vendor `Write:length:` method (VA `0x10001d93c`) uses pipe 1 for bulk OUT.
For `2699`/`2700`, it transfers exactly the packet length with a 2000 ms timeout.
Some older devices add a trailing byte; that behavior is not used here.

## Control requests

The recipient is interface: `bmRequestType=0xc1` for IN and `0x41` for OUT.
`wIndex` is the interface number, 0 on the tested device. `wValue=0` unless
specified below. Multibyte payload integers are little endian.

| Operation | Direction/request | wValue | Payload |
| --- | --- | --- | --- |
| Status | IN `0x40` | 0 | Request 2 bytes; firmware may return 1; zero means ready |
| EDID size | IN `0x41` | 0 | 2-byte EDID byte count |
| EDID block | IN `0x41` | block index + 1 | 128 bytes |
| Product type | IN `0x51` | 0 | 2 bytes |
| Features | IN `0x52` | 0 | 2 bytes |
| JPEG quantization tables | OUT `0x83` | 1 | 138 bytes |
| Resolution | OUT `0x81` | 0 | LE16 width followed by LE16 height |

The reviewed vendor initialization waits for ready status, reads identification
and EDID, constructs an encoder, uploads quantization tables and sends the
resolution request twice identically (VA `0x10001d84a`). No firmware upload is
required in this path.

The quantization-table helper at VA `0x100012ef7` constructs:

```text
FF DB 00 43 00 + 64 luma bytes in zigzag order
FF DB 00 43 01 + 64 chroma bytes in zigzag order
```

Each complete standard JPEG DQT segment is 69 bytes; the control payload is
138 bytes. `SetQuantTable:` at VA `0x10001d8f2` sends it with value 1.

The newer macOS driver requests two status bytes but ignores the actual reply
length, with the high byte initialized to zero. The tested BT-101A returns one
byte. The Linux implementation accepts one or two status bytes into a
zero-initialized buffer. Other controls require their exact expected lengths.
Nonzero status is treated as busy; individual status bits are not decoded.
Readiness polling is bounded to two seconds plus the final control timeout.

## Frame format

`fillHeader:payloadLength:rect:` at VA `0x10000b795` constructs a 32-byte header
followed immediately by a complete baseline JPEG. All multibyte header fields
are little endian and deliberately unaligned:

| Offset | Type | Value |
| --- | --- | --- |
| 0 | u16 | `0x5858` |
| 2 | u32 | JPEG byte length + 32 |
| 6 | u16 | `0x8585` |
| 8 | u32 | JPEG byte length |
| 12 | u16 | `0xdec0` |
| 14 | u32 | Rectangle width |
| 18 | u32 | Rectangle height |
| 22 | u32 | Rectangle x |
| 26 | u32 | Rectangle y |
| 30 | u16 | `0xedfe` |

The JPEG contains SOI `FF D8`, ordinary JPEG headers/entropy data and EOI
`FF D9`. The vendor default `create8808YUV422` (VA `0x10000a7df`) uses YUV 4:2:2
and quality 90, with width aligned to 16 and height to 8.
`encodeFrame:streamPtr:length:` at VA `0x10000adb2` moves the JPEG 32 bytes
forward and fills the header. `renderDivisionImageFor8808` at VA `0x10001a0fb`
passes the contiguous result to the USB writer.

Independent Windows DLL confirmation: constants at VAs `0x180009e1f`,
`0x180009e25`, `0x180009e2f` and `0x180009e39` match the four magic words and
offsets. The golden packet test checks every byte, including lengths above
65535, nonzero rectangle offsets and total-length overflow.

Bulk writes require the entire packet to complete. A partial transfer is not
retried because decoder/parser recovery after a partial frame is not established.

## Reference binary fingerprints

These SHA-256 values identify the analyzed files without redistributing them:

| File | SHA-256 |
| --- | --- |
| Vendor macOS display `.pkg` | `dd12b39bcffaaf9b9d375a7e796c810389fdb036a568b6585d17566ddcbb9004` |
| Vendor Windows display installer | `755ed1d72cc3653eb30aedc68be33890ff19752a598472d2ef4bd1ef6573a73a` |
| Extracted macOS main Mach-O | `2829412c8806e60f801e40df086a2fbdc45d3810dc3e7dc7250d2bc63da0f9b1` |
| Extracted Windows display DLL | `febf706813bedcbf6584200877bdc3329cda40e9e013397a841cfe6f849235bb` |

The older official RacerTech Linux archive `racer-install_2024-09-05.tgz`
has SHA-256 `fcf648a097dea4adc8ef4f74e39ccf08d7f9965760c778d694b80ca400f13525`.
Its product array contains `2105/2104/2114/2115/9904/9905` and implements the
older MI8801 path. Merely changing that binary's USB ID is not an established
MI8808 implementation.

## Native pen coordinate quirk

Pen USB ID `0ed1:7821` uses the Linux HID driver. The inspected 457-byte HID
descriptor has SHA-256
`6f04ee782e6f0a79f3ab349bf4839c4ac1602795fa7534f73e4ce23c98ce23b5`.

Report `01` declares X maximum 17920, Y maximum 10240 and pressure maximum 8191.
Report `02` incorrectly declares a second Generic Desktop X field with maximum
8191 in the same Stylus collection. The resulting native event device
advertises X maximum 8191 even while delivering values above that maximum.
The exact-collection hwdb override corrects X to 17920 and resolutions to
83/76 units/mm, matching the BT-101A's approximately 217×135 mm active area.
The separate report-`0A` Pen collection is not changed.

The installer checks the descriptor fingerprint before installing this quirk.
Pressure, HID modes and input-event grabs are not modified. Desktop clients
must reopen the device after the persistent override is installed.

## Hardware validation and remaining work

The tested panel returned a valid 256-byte EDID with preferred mode `1280×800`.
A 40,137-byte libjpeg baseline YUV422 test image displayed the complete color
bars and gradient. This validated the control sequence, table upload,
resolution, framing, endpoint and JPEG decoder together.

An EVDI desktop subsequently displayed correctly under KDE Plasma Wayland.
With a 60 fps target, measured changed-frame throughput reached 59.4 updates/s,
with approximately 1.8–2 ms JPEG encoding and 4–5 ms USB time per update.
Correct pen position, pressure, canvas drawing and touch mapping were verified.
These results are from one hardware/software configuration, not a guarantee for
other revisions or workloads.

Full-frame transfer is the validated baseline. Rectangle fields are understood
and offline-tested, but the optional partial-update mode still needs physical
validation. Raw YUV modes, color accuracy, other resolutions, suspend/resume,
brightness, audio and DDC/CI have not been established. The application does
not flash firmware, rewrite device IDs or run the old OEM Linux binary.
