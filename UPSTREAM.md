# Upstream source

EVDI is vendored unchanged from [DisplayLink/evdi](https://github.com/DisplayLink/evdi):

- Revision: `ee3a4b233d3086f2db38dc9ec88a83c92678afc7`
- Retrieval date: 2026-10-28
- [Pinned source archive](https://codeload.github.com/DisplayLink/evdi/tar.gz/ee3a4b233d3086f2db38dc9ec88a83c92678afc7)
- Directory: `vendor/evdi/`

Upstream source, copyright notices and license texts are retained. The kernel
module is GPL-2.0, the library is LGPL-2.1-or-later, and other upstream portions
are MIT licensed. See [THIRD_PARTY.md](THIRD_PARTY.md).

The Bosto application, tools and packaging are original MIT-licensed code. The
USB transport was reimplemented for interoperability from the protocol observed
in vendor Windows/macOS executables. Those proprietary executables are not
included. Reproducible protocol evidence is recorded in [PROTOCOL.md](PROTOCOL.md).
