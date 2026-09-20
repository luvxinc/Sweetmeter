# Validation and hardware acceptance

This file separates automated checks from observations of a physical device.
An offline plan, passing unit test, firmware build, GATT write response or
`REBOOTING` notification is **not** proof that an update installed successfully.
The final version, healthy boot, durable update result and selected host must
agree after reconnect.

The executed 2026.9.3 firmware results, exact image digest and limitations are
recorded in [its acceptance report](releases/2026.9.3.md). The procedures below
remain required for future firmware candidates.

## Current evidence boundary

| Area | Evidence | Remaining acceptance |
| --- | --- | --- |
| Imported macOS prototype | One V1.2 JD79661 unit previously displayed the approved dashboard, clock and BLE data; short/long disconnect sleep was exercised | Protocol-4 BLE/clock/frame ACK repeated for 2026.9.3; owner confirmed dashboard and top-button wake/reconnect; computer selection buttons pending |
| Version/changelog governance | Temporary Git repositories exercise hooks, UTC rollover, staging, retries, invalid history and trusted-base checking; GitHub checks run on reviewed commits | Keep required checks enabled for each accepted commit |
| Protocol-4 BLE and OTA | Implementation and automated checks are recorded by their actual test/build results | 2026.9.3 happy path, rejection, interruption and rollback passed on one Mac/unit; see the linked report |
| Display | Approved 250×122 monochrome reference exists | Human inspection of orientation, legibility, row alignment and residual images |
| Windows/Linux | Portable implementation and platform CI can test software behavior | Actual BLE adapter, OS permission, pairing and reconnect tests on each OS |
| Battery | Unknown battery shown honestly without a working gauge | Wiring, charging, gauge accuracy, low-battery behavior and runtime untested |
| Physical power/wake | Software reset and deep sleep have distinct evidence | Physical top-button wake and real removal/restoration of power |

Do not replace a row with “passed” until its named case has completed on the
named hardware, OS, source commit and image digest. Record test limitations in
the release notes. A new UI or firmware version does not inherit acceptance just
because the earlier prototype worked.

## Before USB bootstrap

Keep the current known-working application/bootloader/partition artifacts and a
full backup of **this unit** in a private directory outside the checkout. Record
the board revision, source version when known, artifact SHA-256, backup SHA-256,
and selected-host fingerprint. A raw flash backup can contain pairing/NVS state;
never commit it, attach it to an issue or use it on another board.

These are explicitly invoked operator commands, not automatic harness actions:

```sh
python -m esptool --chip esp32s3 --port <YOUR_PORT> read-flash 0 0x800000 <PRIVATE_DIRECTORY>/pre-bootstrap.bin
python -m esptool --chip esp32s3 --port <YOUR_PORT> flash-id
```

Use the esptool command spelling supported by the installed version (older
versions use `read_flash`/`flash_id`). Check the saved byte count is exactly
8,388,608 and compute its SHA-256 locally. Back up first, before using the USB
bootstrap command documented in [HARDWARE.md](HARDWARE.md). Normal bootstrap/app
updates preserve the NVS partition; do not run erase-flash. A full-flash restore
is an explicit recovery operation using only the matching unit's own backup,
not a normal OTA step.

The legacy `QM3.2` firmware must be migrated by USB. Never label its binary with
a calendar version or treat it as version zero. The compiled ESP application
descriptor, runtime status and signed metadata must all contain the same actual
`YYYY.M.N` version before protocol-4 OTA is attempted.

## Explicit acceptance harness

`scripts/accept_device.py` reuses the production `Session` and `OTATransfer`.
It never reads Claude/Codex credentials, contacts providers, stops/starts a
companion, changes host selection, erases NVS, flashes USB, or publishes files.
It reads only the existing companion identity needed for the selected-host
handshake. Reports replace Bluetooth addresses and host IDs with fingerprints.

Every invocation is **offline preflight by default**. `--execute` is required
before any BLE connection, clock/frame write, OTA write or serial access.
`--companion-stopped` explicitly acknowledges that the ordinary companion has
already been stopped by the operator; the harness does not control its service.
`--confirm-usb-power` is a user attestation, not a power measurement. Evidence
and the companion state directory must be outside the repository.

Create a synthetic 4000-byte frame from the approved example, outside the
checkout, using `meter.render.render` and `meter.render.pack_frame`. It contains
illustrative data only. Pass that file explicitly for the baseline test:

```sh
python scripts/accept_device.py baseline \
  --device <BLE_ADDRESS_OR_UUID> --state-dir <EXISTING_PRIVATE_STATE> \
  --expected-running-version <ACTUAL_DEVICE_VERSION> \
  --frame <PRIVATE_DIRECTORY>/example.frame \
  --output <PRIVATE_DIRECTORY>/baseline-preflight.json
```

Add `--execute --companion-stopped` only when ready to use the physical device.
Baseline checks protocol, exact board/running version, preserved selected host,
healthy boot, synchronized clock and the completed frame's CRC ACK. A displayed
ACK proves the panel driver completed; the human must still inspect the image.
The harness does not overwrite the normal companion's status or quota caches.

For OTA, supply `--image`, `--envelope`, and, for test builds, `--fixture`:

```sh
python scripts/accept_device.py bad-signature \
  --device <BLE_ADDRESS_OR_UUID> --state-dir <EXISTING_PRIVATE_STATE> \
  --expected-running-version <ACTUAL_DEVICE_VERSION> \
  --image <CANDIDATE.bin> --envelope <CANDIDATE.ota> \
  --fixture <CANDIDATE.acceptance.json> \
  --output <PRIVATE_DIRECTORY>/bad-signature.json \
  --execute --companion-stopped --confirm-usb-power \
  --serial-port <YOUR_PORT> --allow-reset
```

Preflight verifies the envelope signature, supported board/protocol, newer target
version, actual companion minimum, exact image size/digest, and compiled ESP
application version. A fixture must also match the sidecar's source commit/tree,
root version, variant, digests and compiled test project name. Its embedded
build-time provenance must identify clean committed source, the same image hash
and a marked test build; missing/dirty/inconsistent provenance is refused. The harness never
offers a downgrade override or supplies a fabricated companion version.

## Case order and passing evidence

Run rejection/interruption cases before installing the higher-version happy-path
candidate. Cases do not change original artifact files; corruption exists only
in temporary test copies. Successful test firmware may have an intentionally
higher version than the source release, so returning to the normal release can
require the explicit USB recovery path. Do not publish these test images.

| Scenario | Deliberate action | Required pass evidence |
| --- | --- | --- |
| `baseline` | Submit an explicit synthetic frame and current clock | Exact device version/board/host, valid health, clock sync, matching completed CRC ACK |
| `bad-signature` | Flip a signature byte after verifying the original candidate locally | Device `SIGNATURE` error; no REBOOTING; EN reset, matching UART READY and old healthy version |
| `bad-digest` | Flip an image byte while retaining signed metadata | Device `DIGEST` error; no REBOOTING; EN reset and old healthy version |
| `cancel` | Cancel after `--interrupt-after-bytes` have been acknowledged | Actual threshold recorded, CANCELLED ACK, no REBOOTING, EN reset and old healthy version |
| `disconnect` | Drop BLE after the acknowledged byte threshold | Actual threshold recorded, no commit/reboot acknowledgement, EN reset and old healthy version |
| `health-fail` | Install the explicitly supplied health-failure fixture | Current session REBOOTING ACK, reconnect to old version with valid health, durable `rollback`, attempted target preserved |
| `reset-before-confirm` | Install the explicitly supplied reset-before-confirm fixture | Current session REBOOTING ACK, reset of pending candidate causes confirmed rollback to old healthy version |
| `upgrade` | Install a locally verified signed normal candidate | Finish attempted; reconnect to exact target, valid health, durable `success`, matching target and preserved host |

Negative cases require both `--serial-port` and `--allow-reset`: reading the old
version without reboot would not prove the previous boot selection survived.
The EN pulse is a reset, **not** a physical power cycle or button wake. A matching
`READY SWEETMETER <version> protocol=4 ...` serial marker must be observed.
Rollback cannot be inferred from a version mismatch, an old rollback record, a
transport exception, a missing final notification or an offline report.

The default interruption threshold is 65,536 acknowledged bytes, exercising
32-bit offsets. Also test a later threshold below the signed image length; the
harness refuses thresholds too close to completion. Reconnect/health observation
is bounded to 120 seconds. Failure or missing evidence leaves `passed: false`.

After each invocation, inspect the private report and resume the normal
companion. The existing 30-second disconnect shutdown remains active after the
harness disconnects; wake the unit physically if needed. The harness does not
claim that the device remains awake indefinitely after a test.

## Controlled fixture builds

Normal builds use root VERSION, and the release signer rejects test artifacts.
Acceptance fixtures must come from the same reviewed source commit/tree, with an
explicit higher test version, separate private outputs and `SWEETMETER_TEST_BUILD=1`.
The default build does not enable either failure hook.

| Fixture variant | Compiled project marker | Pending-candidate-only behavior |
| --- | --- | --- |
| `normal` | `Sweetmeter-test` | Normal local health confirmation |
| `health-fail` | `Sweetmeter-test-health` | `SWEETMETER_TEST_HEALTH_FAIL` fails before marking VALID |
| `reset-before-confirm` | `Sweetmeter-test-reset` | `SWEETMETER_TEST_RESET_BEFORE_CONFIRM` resets after durable Booted, before confirmation |

The build records its source commit/tree and local fingerprint **before**
compilation, verifies source did not change during the build, and records the
resulting image hash. The fixture signer checks that record and embeds it in the
sidecar consumed by the harness; it does not infer build provenance from whatever
source happens to be present at signing time. A sidecar is local evidence, not an independent signing
authority; the board accepts only its embedded trusted firmware signing key.
No acceptance artifact belongs in a public release manifest or installer.

Build only after the reviewed source is committed and clean. In a POSIX shell,
an explicitly marked failure fixture is prepared with:

```sh
SWEETMETER_TEST_BUILD=1 SWEETMETER_TEST_VERSION=<HIGHER_TEST_VERSION> \
  SWEETMETER_TEST_VARIANT=health-fail pio run -d firmware
python scripts/sign_firmware_fixture.py \
  --firmware firmware/.pio/build/crowpanel213/firmware.bin \
  --variant health-fail --version <HIGHER_TEST_VERSION> \
  --minimum-companion <ROOT_VERSION> --key-file <PRIVATE_SIGNING_KEY> \
  --output <NEW_PRIVATE_FIXTURE_DIRECTORY>
```

Set the equivalent environment variables in the chosen Windows shell when
building there. The signer refuses dirty/uncommitted source, checks that the
compiled version and variant match, and requires the minimum companion to equal
the actual root VERSION. The signing key is never a repository artifact. Unset
all test environment variables and rebuild before any normal production build.

After rollback and successful OTA tests, restore the normal production build,
retain NVS, verify its actual descriptor/runtime VERSION, rerun baseline, and
confirm normal companion reconnect, buttons, discovery and 30-second sleep.
Attach only a sanitized summary to the release; retain raw device evidence and
backups privately. Record Windows and Linux physical runs separately from Mac.
