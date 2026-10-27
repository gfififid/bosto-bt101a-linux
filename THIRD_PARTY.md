# Third-party components

| Component | Source/location | License |
| --- | --- | --- |
| EVDI kernel module | `vendor/evdi/module/` | GPL-2.0; text in `vendor/evdi/module/LICENSE` |
| EVDI userspace library | `vendor/evdi/library/` | LGPL-2.1-or-later; text in `vendor/evdi/library/LICENSE` |
| Other EVDI upstream portions | `vendor/evdi/` | MIT where no separate license applies; text in `vendor/evdi/LICENSE` |

The pinned EVDI revision and download URL are in [UPSTREAM.md](UPSTREAM.md).
Individual upstream file notices remain authoritative and are preserved.

The application dynamically links system-provided libusb and libjpeg-turbo.
Their development/runtime packages are dependencies; their binaries are not
vendored in this source tree. The original Bosto implementation, scripts, tools
and tests outside `vendor/` use the root [MIT license](LICENSE).

No proprietary Bosto or RacerTech Windows/macOS executables are distributed.
