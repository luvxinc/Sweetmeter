# Building and releasing Sweetmeter

Every accepted commit has a calendar version and changelog entry. Publishing is
separate: **do not release every commit automatically**. Keep main append-only,
pass the trusted version policy and `test` checks on the exact head, then
fast-forward that same head into main. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Native companion builds

First-install entry points are root-level `install.sh` and `install.ps1`, linked
from README. They resolve the latest published stable release, verify its exact
manifest bytes with the embedded P-256 public key, then check the native ZIP size
and SHA-256 before running `--install`. They support existing 2026.9.8 packages;
the direct-open welcome screen requires a native package built from the new
onboarding revision. Do not claim an unreleased native change is already shipped.
The bootstrap is HTTPS-trusted repository code, separate from authenticated
release payloads. Key rotation must update both installers and the app together.
`test_bootstrap.py` exercises real signature verification/extraction with local
signed fixtures on each native CI platform; network and process launch are
mocked. It does not certify physical Bluetooth or clean-machine OS prompts.


Use Python 3.12 with Tcl/Tk 8.6 and a clean environment on each native platform:

```sh
python scripts/setup_native_env.py
.venv/bin/python scripts/build_companion.py
```

Use `.venv\Scripts\python.exe` on Windows. Linux build workers need Tk and a
BlueZ-capable runtime; no physical radio is used in the offline smoke test.
There is no cross-compilation: build macOS arm64, macOS x86_64, Windows x86_64
and Linux x86_64 on corresponding workers. The GitHub matrix uses macOS 15,
Windows Server 2025 for the Windows 11 software target, and Ubuntu 22.04.
Trusted primary tests, firmware and release orchestration run on the isolated
Mac mini Linux ARM64 runner; native bundles use hosted workers because they
require their target OS/architecture. Pull requests never use the private
runner. See [CI.md](CI.md) for routing and isolation.

The setup helper creates a new venv and refuses to overwrite an existing one;
use `--path` to choose another directory. It installs pinned requirements without
reusing pip's build cache. Most platforms use the upstream cryptography wheel.
Upstream [removed Intel macOS wheels/support in 49.0.0](https://cryptography.io/en/latest/changelog/#v49-0-0).
Sweetmeter retains its pinned 50.0.1 on Intel by compiling it with Rust and
Homebrew `openssl@3` static libraries, using the documented
[`OPENSSL_STATIC=1` build option](https://cryptography.io/en/latest/installation/#building-cryptography-on-macos).
The builder rejects an Intel crypto extension linked to external libssl/libcrypto
before packaging. Intel compatibility is validated by Sweetmeter's native CI
tests and frozen-app smoke checks; it is not an upstream-supported wheel target.
Do not claim the corrected Intel build passed until that CI job succeeds.

The output is `dist/Sweetmeter-YYYY.M.N-OS-ARCH.zip`, containing a complete
`Sweetmeter.app` or `Sweetmeter/` directory. It includes the runtime, Tk,
fonts, public key, root VERSION and collected dependency notices. Build smoke
checks run the actual packaged `--version` and offline `--self-test`. Source
tests, pinned firmware compilation and all native builds feed the single
required `test` check. PR code never receives release secrets.

Each native package includes build metadata with source commit/tree/fingerprint,
version, platform and a clean/dirty marker. Its adjacent `.zip.build.json`
records the final archive and executable digests after code signing. Preserve
that receipt with the ZIP when passing artifacts to the signer. The signer
validates both, executable architecture, bundled version and public key; a local
dirty build is useful for testing but cannot become a public release.

macOS app bundles retain the framework links required by PyInstaller and code
signing. Archive validation allows relative links only inside `Sweetmeter.app`,
resolves the full chain with a depth bound, rejects escapes/cycles/dangling links
and rejects files written through link parents. Regular files are extracted
first, links last. The updater preserves the validated graph. Arbitrary system
links and special files are not accepted.

## App signing is separate from release signing

Development macOS builds use ad-hoc signing, without Developer ID or
notarization. Windows builds currently have no Authenticode signature. Describe
those limits in the published notes; never imply platform trust from a
Sweetmeter ECDSA signature. Do not ask users to disable OS security globally.

Optional macOS Developer ID secrets in the protected `release` environment:

- `MACOS_CERTIFICATE_P12`: base64-encoded certificate/private-key export.
- `MACOS_CERTIFICATE_PASSWORD`: export password.
- `MACOS_SIGN_IDENTITY`: full Developer ID Application identity.

All three must be supplied together, or all omitted. The release runner imports
the certificate into a temporary keychain, signs, then removes the keychain.
This optional path still does not perform notarization. Production notarization
and Windows signing need their own credentials and acceptance before claiming
that support.

## Firmware and license artifacts

```sh
pio run -d firmware
python scripts/collect_firmware_sources.py --output dist
```

The compiler version, public trust key, protocol and hardware identifier are
injected from repository sources. The application descriptor is `Sweetmeter`;
legacy QM names and deliberately failing test fixtures are not release images.

`Sweetmeter-firmware-rebuild.tar.gz` includes the application object files,
generated public build headers, exact installed Arduino SDK, recursive Arduino
2.0.17 and ESP-IDF 4.4.7 source, and rebuild/relink instructions. Firmware
distributions include these sources/objects so LGPL components can be modified
and relinked. Physical USB flashing remains available without the release private
key; secure boot is not enabled. Keep all component licenses in these archives.

`collect_notices.py` collects actual installed wheel licenses and version
metadata, CPython's actual runtime license, Tcl/Tk terms and the bundled font
license. Binary test files whose names happen to start with `copying` are not
license documents. Audit `NOTICES/dependencies.json` when dependencies change.
ELECROW example-derived initialization/waveform permission remains a separate
unresolved item in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md); compiling
successfully does not resolve that permission.

## Release key

The shipped public key is `meter/assets/keys/release-1.pem`; firmware and
companion use it to verify updates. Keep its corresponding ECDSA P-256 private
key outside the checkout, with owner-only permissions. Never place it in a
release package, build artifact, log, command-line value or source file.

`scripts/generate_signing_key.py --private-key <EXTERNAL_PATH>` is a one-time
maintainer setup tool. Do not regenerate a deployed trust root for each build;
existing devices would reject it. Back up the private key securely. Key rotation
requires a deliberately planned signed migration.

Local signing uses a clean, committed tree and the exact tested artifacts:

```sh
python scripts/sign_release.py \
  --firmware firmware/.pio/build/crowpanel213/firmware.bin \
  --minimum-companion YYYY.M.N --output signed-release \
  --key-file <EXTERNAL_PRIVATE_KEY_PATH> --commit <FULL_COMMIT_SHA> \
  --companion macos:arm64:dist/Sweetmeter-YYYY.M.N-macos-arm64.zip \
  --companion macos:x86_64:dist/Sweetmeter-YYYY.M.N-macos-x86_64.zip \
  --companion windows:x86_64:dist/Sweetmeter-YYYY.M.N-windows-x86_64.zip \
  --companion linux:x86_64:dist/Sweetmeter-YYYY.M.N-linux-x86_64.zip
```

The signer refuses stale package versions, mismatched public keys, fixture
firmware and uncommitted tracked changes. The `.ota` file is the signed metadata
envelope accompanying the `.bin` image, not a USB flash image. The manifest is
signed as exact UTF-8 bytes and includes cumulative changelog notes. Clients
show those verified notes, not an unsigned GitHub description.

## GitHub workflow

Run **Publish verified release** from the `main` workflow ref, with the full SHA
of a previously tested commit already in main. Check the hardware-acceptance
input only after doing the stated real-device checks for that exact image and
recording limitations. Supply `accepted_firmware_run_id` from a successful
**Test** push run at that same commit and `accepted_firmware_sha256` from the
`firmware.bin` actually tested. Download its `firmware-<COMMIT_SHA>` artifact for
acceptance. PR merge-test artifacts cannot be used. CI validates run identity,
source provenance and the exact image digest; it does not independently prove
the manual hardware attestation.

The workflow requires successful `test`, `version-policy-tests` and
`version-policy/trusted` results. It builds native packages and reuses the
accepted CI firmware bytes and application objects **without rebuilding them**.
It installs the pinned SDK to collect notices and matching rebuild/relink
sources, then enters the protected `release`
environment. Set `SWEETMETER_RELEASE_PRIVATE_KEY` there to the external key's PEM
only when ready; a missing/invalid/mismatched key fails closed. The temporary key
file exists outside the checkout and is removed in an always-run cleanup step.

The workflow creates a draft release, uploads the complete signed manifest,
signature, firmware, metadata envelope, native packages, cumulative changelog,
licenses, source, accepted firmware build ZIP and relinking archive, and only then publishes when its
explicit `publish` input is true. Otherwise it remains a draft. Published
immutable assets must not be replaced; correct mistakes with a new commit,
version and release. Do not reuse or move a released tag.

## Acceptance and recovery

Before publishing, verify normal BLE/dashboard behavior and actual signed OTA,
reconnection/version health, bad-signature refusal, interrupted transfer and
controlled failed-start rollback. Restore production firmware after fixture
tests. Battery wiring/runtime and Windows/Linux physical pairing are separate
acceptance items; software CI is not evidence those passed.

Companion replacement waits for the old PID to exit, uses sibling incoming and
backup directories on the installation filesystem, launches the new version,
and requires a matching version/nonce/PID health receipt. Failure restores and
launches the old app. The update helper runs outside the replaced application,
including on Windows where the old executable is locked. A durable swap journal
records each rename boundary. Login startup uses a stable launcher outside the
application: after a crash/reboot it restores an unconfirmed backup or finishes
confirmed cleanup before starting the app. Recovery and apply share an exclusive
lock; recovery also refuses to replace a surviving running app until it exits.
The launcher waits for its child so launchd does not terminate the app's process
group. Source/unmanaged or unwritable installs offer verified manual extraction
without claiming success.

If an interrupted device is asleep, wake it with the top button. If BLE recovery
is unavailable, use USB and the matching source build/your own board backup as
described in [HARDWARE.md](HARDWARE.md). Do not erase all NVS as a routine update;
that discards the selected computer and pairing state.
