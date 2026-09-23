# Building and releasing Sweetmeter

Every accepted commit has a calendar version and changelog entry. Publishing is
separate: **do not release every commit automatically**. Keep main append-only,
pass the trusted version policy and `test` checks on the exact head, then
fast-forward that same head into main. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Native companion builds

First-install entry points are root-level `install.sh` and `install.ps1`, linked
from README. They resolve the latest published stable release through the
`github.com/.../releases/latest` redirect (`install.ps1` falls back to the REST
API), verify its exact manifest bytes with the embedded P-256 public key, then
check the native ZIP size and SHA-256 before running `--install`. They work with
any published package that supports `--install`; features such as repair,
uninstall and the Windows Run-key startup take effect only from the first
release built from this revision. Do not claim an unreleased native change is
already shipped. On an existing installation the installers run the installed
copy's `--install`, which re-registers missing login startup and opens it; only
if that fails do they download and install the latest release again.
The bootstrap is HTTPS-trusted repository code, separate from authenticated
release payloads. The installers pin the *current signing* key; see
[key rotation](#rotating-the-release-key).
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
Trusted primary tests, firmware and release validation run on the isolated
Mac mini Linux ARM64 runner; native bundles use hosted workers because they
require their target OS/architecture. Release signing and publishing run on
fresh GitHub-hosted runners, never the self-hosted one. Pull requests never use
the private runner. See [CI.md](CI.md) for routing and isolation.

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

Sweetmeter's ECDSA release signature authenticates packages to Sweetmeter
itself. Platform code signing is separate; never imply Apple or Microsoft trust
from the Sweetmeter signature, and do not ask users to disable OS security.
Without any secrets configured, macOS builds are ad-hoc signed and Windows
builds are unsigned; say so in the published notes.

All platform-signing secrets live only in the protected `release` environment.
Each group is optional, must be configured completely or not at all, and
activates automatically in **Publish verified release**:

| Secret(s) | Effect |
| --- | --- |
| `MACOS_CERTIFICATE_P12`, `MACOS_CERTIFICATE_PASSWORD`, `MACOS_SIGN_IDENTITY` | Stable macOS signing identity (self-signed or Developer ID) |
| `APPLE_NOTARY_KEY`, `APPLE_NOTARY_KEY_ID`, `APPLE_NOTARY_ISSUER` | Notarization + stapling (requires a Developer ID identity) |
| `WINDOWS_CERTIFICATE_PFX`, `WINDOWS_CERTIFICATE_PASSWORD` (+ optional variable `WINDOWS_TIMESTAMP_URL`) | Authenticode signing of `Sweetmeter.exe` and the launcher/update helper |

### Why a stable macOS identity matters (do this first)

macOS privacy permissions (TCC), including Bluetooth, are bound to the app's
*designated requirement*. An ad-hoc signature's requirement is its `cdhash`,
which changes with every build, so each self-update makes macOS forget
Sweetmeter's Bluetooth permission and ask again. A certificate-based
requirement (`identifier "com.sweetmeter.companion" and certificate leaf = H"…"`)
stays the same across releases signed with the same certificate. The builder
prints the requirement and fails if a configured identity still yields a
cdhash requirement. The companion also refuses to discard the previous version
after an update when Bluetooth worked before and is denied afterwards (see
[Acceptance and recovery](#acceptance-and-recovery)).

A self-signed code-signing certificate gives this stability today, without an
Apple Developer account. It does **not** satisfy Gatekeeper; first-open warnings
remain until Developer ID + notarization are configured. One-time setup, on a
trusted offline Mac:

```sh
cat > sweetmeter-codesign.cnf <<'EOF'
[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=Sweetmeter Code Signing
[ext]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
EOF
openssl req -x509 -newkey rsa:3072 -nodes -days 7300 -config sweetmeter-codesign.cnf \
  -keyout sweetmeter-codesign.key -out sweetmeter-codesign.pem
# OpenSSL 3 needs -legacy so macOS `security import` can read the file;
# the LibreSSL /usr/bin/openssl shipped with macOS does not accept that flag.
openssl pkcs12 -export -legacy -inkey sweetmeter-codesign.key -in sweetmeter-codesign.pem \
  -out sweetmeter-codesign.p12        # choose an export password
base64 -i sweetmeter-codesign.p12 | pbcopy   # paste as MACOS_CERTIFICATE_P12
```

Set `MACOS_CERTIFICATE_PASSWORD` to the export password and
`MACOS_SIGN_IDENTITY` to `Sweetmeter Code Signing`. The workflow imports it into
a temporary keychain, trusts it for code signing on the disposable hosted
runner only (`codesign` refuses an untrusted identity), signs, then deletes the
keychain. Keep the `.p12` and key offline with the release-key backups:
**replacing this certificate changes the designated requirement and makes every
Mac ask for Bluetooth permission once more**, so use a long validity and never
regenerate it casually. Moving later to Developer ID changes the requirement
once, for the same reason.

### Developer ID and notarization (when an Apple Developer account exists)

Export the *Developer ID Application* certificate and key as `.p12` into the
same three `MACOS_*` secrets; `MACOS_SIGN_IDENTITY` is the full
`Developer ID Application: NAME (TEAMID)` string. Developer ID builds use the
hardened runtime with `packaging/macos-entitlements.plist` and a secure
timestamp. For notarization create an App Store Connect API key (Users and
Access > Integrations, role Developer) and set `APPLE_NOTARY_KEY` (base64 of the
`.p8`), `APPLE_NOTARY_KEY_ID` and `APPLE_NOTARY_ISSUER`. The builder submits the
app with `notarytool --wait`, prints the log on rejection, staples the ticket,
validates it and runs a Gatekeeper assessment **before** archiving, so the ZIP
digest in the signed manifest covers the stapled app. Only then may notes say
the macOS build is notarized.

### Windows Authenticode

Set `WINDOWS_CERTIFICATE_PFX` (base64 `.pfx`) and `WINDOWS_CERTIFICATE_PASSWORD`.
The builder signs the one-file launcher/update helper before it is embedded, and
`Sweetmeter.exe`, with SHA-256 and an RFC 3161 timestamp, then verifies both.
The password is passed to `signtool` on the disposable hosted runner only.
Without a certificate, Windows builds stay unsigned; SmartScreen may warn on
first run. The installer and the in-app install remove the Mark-of-the-Web from
files only after the package matched the signed manifest, so the installed app
and launcher do not warn at every login.

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

Every `meter/assets/keys/<key-id>.pem` is a trusted verification key named by its
key ID (1–15 characters of `[a-z0-9-]`, starting with a letter or digit, fitting
the 16-byte firmware header field; the companion loader and the firmware build
`scripts/firmware_build.py` enforce the same rule, so dots and underscores are
rejected by both). `meter.protocol.KEY_ID` (currently `release-1`) is the key new releases
are signed with; manifests and firmware envelopes carry the signing key ID and
are accepted if that ID is shipped. The firmware keeps an equivalent trusted-key
list. Release data can never add a key. The signer refuses a package whose
bundled key set differs from the repository's.

Keep each ECDSA P-256 private key outside any checkout, with owner-only
permissions. Never place one in a release package, build artifact, log,
command-line value or source file. **The only online copy of the active private
key is the `SWEETMETER_RELEASE_PRIVATE_KEY` secret of the `release`
environment**; delete any other copy after moving it there (keep only the
offline backups below). A key on a laptop is how a release can be hand-signed
past hardware acceptance, as happened with 2026.9.13.

`scripts/generate_signing_key.py --private-key <EXTERNAL_PATH>` is a one-time
maintainer setup tool. Do not regenerate a deployed trust root for each build;
existing devices would reject it.

### Backup key ceremony (once, before it is needed)

Prepare the *next* key before the current one is lost or compromised:

1. On an offline computer (no network, freshly booted), run
   `scripts/generate_signing_key.py --private-key <REMOVABLE_MEDIA>/release-2.key --public-key release-2.pem`.
2. Write the private key to two encrypted removable media (or a hardware
   token that can sign P-256) with a strong passphrase. Store them in two
   separate physical locations; record custody and date. Never upload it.
3. Also keep an offline, encrypted backup of the *active* key the same way.
4. Commit only `release-2.pem` into `meter/assets/keys/` (the `.gitignore`
   excludes `*.pem` except `release-1.pem`, so add an exception for the new file)
   and add the same public key to the firmware trust list. Release it signed by
   the current key. From then on every companion and device trusts both keys,
   while releases are still signed with `release-1`.

### Rotating the release key

Planned rotation (or recovery after loss):

1. Make sure a release that ships the new public key (step 4 above) has been
   published and adopted by companions and devices. Only those can verify
   releases signed with the new key; older ones must first update.
2. In one commit: set `KEY_ID` to the new ID, replace the key embedded in
   `install.sh` and `install.ps1` (and `tests/test_bootstrap.py` expectations),
   and document the change. Replace the `SWEETMETER_RELEASE_PRIVATE_KEY`
   environment secret with the new private key.
3. Publish through the workflow as usual. Keep the old `.pem` for at least one
   release cycle so a failed rotation can be undone, then remove it in a later
   release to retire it.

Compromise of the active key: remove its `.pem` from `meter/assets/keys/` and
the firmware trust list and switch to the backup key **in the same release**,
signed with the backup key. Devices and companions that already trust the
backup key accept it; those that do not must be updated via USB/manual
download. Publish an advisory, and never re-sign old versions.

Local signing is for testing only (a clean, committed tree and the exact tested
artifacts); public releases go through the workflow:

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

**Publish verified release** is the only supported way to publish. Run it from the `main` workflow ref, with the full SHA
of a previously tested commit already in main. Check the hardware-acceptance
input only after doing the stated real-device checks for that exact image and
recording limitations. Supply `accepted_firmware_run_id` from a successful
**Test** push run at that same commit and `accepted_firmware_sha256` from the
`firmware.bin` actually tested. Download its `firmware-<COMMIT_SHA>` artifact for
acceptance. PR merge-test artifacts cannot be used. CI validates run identity,
source provenance and the exact image digest; it does not independently prove
the manual hardware attestation.

The workflow requires successful `test`, `version-policy-tests` and
`version-policy/trusted` results **for that exact commit**, and refuses to run
when a tag, release or draft for the version already exists. It builds native
packages and reuses the accepted CI firmware bytes and application objects
**without rebuilding them**. It installs the pinned SDK to collect notices and
matching rebuild/relink sources.

Signing runs in its own `sign` job on a fresh GitHub-hosted Ubuntu VM in the
protected `release` environment, with a virtual environment containing only
`cryptography` and its dependencies, installed with `--require-hashes` from
`packaging/release-signing-requirements.txt`. The self-hosted runner, which
installs PlatformIO and build tools, never receives the key. Set
`SWEETMETER_RELEASE_PRIVATE_KEY` there to the key's PEM; a
missing/invalid/mismatched key fails closed. The temporary key file exists
outside the checkout and is removed in an always-run step; the job has no write
permission. When `cryptography` changes in `requirements.txt`, update the
hashes from PyPI in the same commit.

A separate `publish` job creates a draft release, uploads the complete signed
manifest, signature, firmware, metadata envelope, native packages, cumulative
changelog, licenses, source, accepted firmware build ZIP and relinking archive,
records a [build provenance attestation](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations)
for every asset (verify with `gh attestation verify <file> --repo luvxinc/Sweetmeter`),
and only then publishes when its explicit `publish` input is true. Otherwise it
remains a draft. All third-party actions are pinned to full commit SHAs;
update them deliberately. Published immutable assets must not be replaced;
correct mistakes with a new commit, version and release. Do not reuse or move a
released tag.

### Repository settings the owner must enable

These cannot be set from the repository files; configure them once:

1. **Environment `release`** (Settings > Environments): required reviewer =
   the maintainer; deployment branches = `main` only; store every signing secret
   here and nowhere else (no repository- or organization-level copies).
2. **Tag ruleset** (Settings > Rules > Rulesets > New tag ruleset), target all
   tags: restrict updates and deletions so a released tag can never move.
   Restricting *creations* as well also blocks the workflow's `GITHUB_TOKEN`
   (the release creates its tag), so enable it only with the GitHub Actions
   app listed as a bypass actor. Also enable **immutable releases**
   (Settings > General > Releases) so published assets and tags cannot change.
3. **Branch protection / ruleset for `main`**: require the `test`,
   `version-policy-tests` and `version-policy/trusted` checks, block force pushes
   and deletions.
4. Move the release private key into the environment secret and delete local
   copies (see [Release key](#release-key)). Clients accept only manifests
   signed by a shipped key, so without a local key a hand-made release cannot
   reach users even by an administrator.

## Acceptance and recovery

Before publishing, verify normal BLE/dashboard behavior and actual signed OTA,
reconnection/version health, bad-signature refusal, interrupted transfer and
controlled failed-start rollback. Restore production firmware after fixture
tests. Battery wiring/runtime and Windows/Linux physical pairing are separate
acceptance items; software CI is not evidence those passed.

Companion replacement waits for the old PID to exit, uses sibling incoming and
backup directories on the installation filesystem, launches the new version,
and requires a matching version/nonce/PID health receipt. Renames are retried
with backoff because antivirus or Explorer can briefly lock a file on Windows.
Any failure — including a rename that never succeeded — restores the previous
app when it was moved and always restarts it; the reason is saved and shown by
the restored app. The update helper runs outside the replaced application,
including on Windows where the old executable is locked. A durable swap journal
records each rename boundary and is kept when restoration itself failed, so the
next login finishes it. Login startup uses a stable launcher outside the
application: after a crash/reboot it restores an unconfirmed backup or finishes
confirmed cleanup before starting the app. Recovery and apply share an exclusive
lock; recovery also refuses to replace a surviving running app until it exits.
Installers and uninstallers take that lock *before* stopping the running app
and refuse while it is held, because the helper holds it during the health
check: stopping the app under check would roll back a good update. The journal
records the swap's origin (`update` or `installer`); recovering an interrupted
installer swap reports "installation undone", never a failed companion update,
and marks no version as failed.
After the new version is confirmed healthy, the helper atomically replaces the
launcher with the one from the new bundle, so launcher fixes reach existing
installations.

On macOS the launcher and helper start the app through LaunchServices
(`open -a`) so the privacy "responsible process" is the app bundle, not the bare
launcher, Terminal or helper; Bluetooth permission is therefore requested for
and recorded against Sweetmeter. On Linux the launcher waits for the app so the
XDG autostart unit stays alive; on Windows it starts the app without a console
window and exits.

The health receipt includes Bluetooth when the previous version had working
Bluetooth (`radio.health == 'ok'`): the new version confirms as soon as it
reaches `ok` (or `off`, which is the user's choice and never causes a rollback).
No state yet (`None`, e.g. while the macOS permission prompt is open) means
waiting: after about 10 s the user is reminded to allow Bluetooth, and the
watcher follows a Bluetooth worker the app restarted meanwhile. The helper
gives the app `HEALTH_TIMEOUT` (180 s, capped at 300 s) and passes that
deadline as `SWEETMETER_UPDATE_DEADLINE`; the app answers about 15 s before it
(or within 110 s when an older helper passed no deadline, since those stop
waiting after 150 s). If the state is still unknown at the deadline, the update
is kept. Only a persisting `unauthorized` (for example an ad-hoc re-signed macOS
app that lost its permission and the user chose Don't Allow) or a radio that
failed to start (its startup error, or every bounded restart used up) is
reported as unhealthy; the backup is kept and restored, and the user is told to
allow Bluetooth and retry. A rolled-back version is recorded and not offered
again automatically. On a rollback the helper also stops every process running
from the new app folder (found by executable path, so a macOS app that hangs
before reporting its PID is stopped too) and never deletes a folder a process
still runs from; the launcher then finishes the rollback at the next login.
If the previous version already lacked Bluetooth,
the new one is not held to it, so a denied permission cannot cause a loop of
failed updates. Source/unmanaged or unwritable installs offer verified manual
extraction without claiming success.

If an interrupted device is asleep, wake it with the top button. If BLE recovery
is unavailable, use USB and the matching source build/your own board backup as
described in [HARDWARE.md](HARDWARE.md). Do not erase all NVS as a routine update;
that discards the selected computer and pairing state.
