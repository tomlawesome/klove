# Third-party notices for the local integration image

The native integration image is built locally for automated testing. It is not
published as a Klove release artifact.

## Klipper

- Project: [Klipper](https://github.com/Klipper3d/klipper)
- Revision: [`fe4eb8650bd7de4c2100a14eaf09b0965c430e29`](https://github.com/Klipper3d/klipper/tree/fe4eb8650bd7de4c2100a14eaf09b0965c430e29)
- Licence: GNU General Public License version 3
- Licence in image: `/opt/klipper/COPYING`

The build retains the pinned Klipper source under `/opt/klipper` and compiles
its Linux-process MCU and host helper for the test fixture.

## Moonraker

- Project: [Moonraker](https://github.com/Arksine/moonraker)
- Revision: [`d5ee17128bb88434aacdab90c2e9e990e2b64e4a`](https://github.com/Arksine/moonraker/tree/d5ee17128bb88434aacdab90c2e9e990e2b64e4a)
- Licence: GNU General Public License version 3
- Licence in image: `/opt/moonraker/LICENSE`

The build retains the pinned Moonraker source under `/opt/moonraker` and runs it
without modifying upstream source.

The corresponding Python dependency sets and their hashes are recorded in the
three `requirements-*.lock` files beside this notice. The Debian and Python
base components retain their upstream package metadata and licences. Anyone
changing an upstream revision, dependency lock, base digest, or image
publication policy must repeat provenance, licence, and integration review.
