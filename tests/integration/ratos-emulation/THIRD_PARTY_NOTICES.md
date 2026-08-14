# RatOS emulation tooling notices

This local-only test image uses the digest-pinned official Python slim image
and installs exact Debian Bookworm package versions from the official
`20260803T000000Z` Debian and Debian Security snapshots for:

- QEMU system emulation and image tools (`qemu-system-arm`, `qemu-utils`),
  version `1:7.2+dfsg-7+deb12u18+b3`;
- Mtools (`mtools`), version `4.0.33-1+really4.0.32-1`; and
- XZ Utils (`xz-utils`), version `5.4.1-1+deb12u1`.

The packages and their transitive libraries retain their Debian-provided
copyright and licence files under `/usr/share/doc`. QEMU is predominantly
GPL-2.0; individual components carry compatible or separately identified
licences. Mtools is GPL-3.0-or-later. XZ Utils contains public-domain and
free-software-licensed components. See the corresponding Debian source package
copyright files for the authoritative per-file notices.

The image is test tooling. The lifecycle scripts do not publish it, and it is
not part of the Klove runtime or release artifact.
