# syntax=docker/dockerfile:1.7
# The test-only runner is based on the already-built local Klove image.  The
# lifecycle script builds that image before this profile service is invoked.
FROM klove-moonraker-sim-klove:local

COPY --chown=10001:10001 tests/integration/moonraker-sim/fixture/dispatch_contract.py /fixture/dispatch_contract.py
COPY --chown=10001:10001 tests/integration/moonraker-sim/fixture/dispatch_reconnect_contract.py /fixture/dispatch_reconnect_contract.py
COPY --chown=10001:10001 tests/integration/moonraker-sim/fixture/sdcard_reset.py /fixture/sdcard_reset.py

USER 10001:10001
ENTRYPOINT ["python", "/fixture/dispatch_contract.py"]
