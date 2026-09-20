# Sweetmeter implementation and acceptance plan

## Product scope

Preserve the approved 250×122 monochrome dashboard, with Claude 5H, Claude 7D,
Fable 7D and Codex 7D on one page. Smaller period labels and actual subscription
names sit on the left, Token totals inside broad progress bars, RESET IN
days/hours/minutes below, and percentages vertically centered at the right.
Clock remains YYYY/MM/DD HH:mm; battery is an icon, unknown until measured.

Build a Windows/macOS/Linux companion that reads the user's existing local
Claude/Codex login and logs, sends frames over BLE, offers release notes and
explicit Install/Later/Skip decisions, and transfers signed firmware updates.
No account credential enters a frame, firmware image, repository, or release.
First installation requires a companion and a USB bootstrap firmware flash.
Subsequent firmware updates use BLE; USB can supply power.

Preserve physical refresh, long-bottom computer selection with rocker controls,
top-button sleep/wake and 30-second authenticated-target disconnect sleep.
Discovery and OTA receive bounded, explicit sleep exemptions. Normal operation
refreshes data every 60 seconds. Batteries and Windows/Linux physical hardware
acceptance must not be reported as tested unless actually exercised.

## Phase 0: documentation discovery (completed)

- Pinned Arduino ESP32 2.0.17 / IDF 4.4.7 supports dual-slot OTA and the selected
  bootloader has rollback enabled. Override `verifyRollbackLater()` to prevent
  Arduino confirming a candidate before application self-tests.
- Bleak is a GATT client, not a computer advertiser. Protocol 4 must replace
  host-advertisement discovery with short, physical-menu-authorized registrations.
- Use one persistent asyncio loop for BLE; provider requests must not block it.
- Use a Tk/ttk desktop confirmation window with all downloads/installations gated
  by explicit user action. Package Tcl/Tk and runtime dependencies per platform.
- Local hooks are bypassable. Protect main with CI and an append-only, linear
  history. A version exists per commit; public releases are selected verified
  commits, not an automatic release for every documentation change.

Primary references:

- https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-reference/system/ota.html
- https://github.com/espressif/arduino-esp32/blob/2.0.17/cores/esp32/esp32-hal-misc.c
- https://bleak.readthedocs.io/en/latest/api/client.html
- https://bleak.readthedocs.io/en/latest/troubleshooting.html
- https://docs.python.org/3/library/tkinter.html
- https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ec/
- https://git-scm.com/docs/githooks
- https://docs.github.com/en/rest/repos/rules
- https://docs.github.com/en/rest/releases/releases

## Shared contracts

- One root VERSION, format YYYY.M.N, UTC month, no leading zeroes on M/N.
  Every accepted commit increments N exactly once; a new month starts at 1.
  Existing initial commit a2a4898eb5a3d1f2e2d9043cba82e59b33b8970b is the sole
  bootstrap exception. First implementation version is 2026.9.1.
- Main is append-only and fast-forward integrated. Branch versions are provisional
  until integrated; no unversioned merge commits or multi-commit squash merges.
- Hardware ID: `elecrow-crowpanel-2.13-v1.2-jd79661`; protocol 4.
- Preserve existing service and framebuffer characteristic UUIDs. Add OTA control,
  data and status UUIDs ending 0005/0006/0007 in the same UUID namespace.
- Signing uses ECDSA P-256/SHA-256 with DER signatures. Key ID `release-1` selects
  an embedded trusted public key. The private key is generated outside the repo
  and never printed, committed, logged or included in downloads.
- Signed firmware metadata has a deterministic binary header defined in
  docs/PROTOCOL.md before parallel implementation. Bind board, version, minimum
  companion, protocol, size, image SHA-256 and key ID. Device verifies signature
  before erase and digest before selecting the new boot partition.
- Release manifest is exact UTF-8 JSON bytes with a detached DER signature; host
  verifies those original bytes. User-visible notes are part of signed metadata.
- Existing host installation ID/NVS selection is preserved during local migration.
  No personal state is imported into a public installer or source checkout.

## Phase 1: repository foundation and version policy

Allowlist-import source/tests and synthetic preview only. Keep Apache license and
third-party notices. Remove personal identifiers, generated state and migration
that copies the author's quota/token cache. Bundle redistributable fonts rather
than shipping Mac system fonts. Establish version hooks, semantic changelog notes,
history validator, pre-push checks and CI.

Verification: existing tests; font/layout review; temporary-repository tests for
same-month increments, month/year rollover, retry idempotence, partial staging,
missing notes, skipped hooks, invalid middle commits and merge rejection. Scan
the staged tree for private state, credentials and user-specific paths.

Anti-patterns: whole-directory imports, hardcoded account values, version bumps
on hook retries, generating meaningless notes from a commit subject, claiming
hooks cannot be bypassed, rewriting the user's initial commit.

## Phase 2: companion, signed releases and firmware OTA

Implement in parallel against the shared protocol contract:

1. Firmware: menu registration, OTA worker/queue, signed metadata validation,
   inactive-slot writes, bounded transfer/cancel/disconnect handling, health
   confirmation/rollback, progress UI and power integration.
2. Companion: portable auth paths/locks/fonts, Bleak transport, device selection,
   nonblocking data polling, desktop setup/update UI, signed release checker,
   explicit confirmation and post-reboot version verification.
3. Distribution: native-platform bundles, per-user startup, public-key generation
   and signing tools, GitHub workflows and draft-first release publishing.

Verification: protocol fixtures shared between Python/C++, mocked failure states,
crypto tamper/downgrade/board mismatch tests, GUI decision-state tests, renderer
inspection, pinned firmware compilation and OS CI builds/smoke tests.

Anti-patterns: flashing on discovery alone, trusting an unsigned GitHub body,
blocking BLE callbacks with flash/crypto/display, automatic Arduino confirmation,
accepting lower versions, passing secrets to command-line logs, treating a host
UUID as cryptographic authentication, asserting Windows 10 support with Bleak 3.

## Phase 3: device acceptance and publication

Back up the current working image and retain NVS. Bootstrap the new firmware via
USB, run normal BLE/frame checks, install a signed candidate through real BLE,
verify reboot/version/health, then exercise bad signature, interruption and
controlled failed-boot rollback. Restore the production build after test images.

Run independent verification, anti-pattern and code-quality reviews. Fix findings
before committing. First seed main with the reviewed `2026.9.1` foundation to
establish the trusted version policy, then configure protected-main checks.
For subsequent phases, push reviewed commits to an integration branch and create
a pull request. The policy from the trusted base posts `version-policy/trusted`
on the exact proposed head; wait for that status and all required test checks
before fast-forwarding that same head into protected main. Do not use normal
merge commits or squash merges, and do not require approval from an outside
reviewer. Publish a signed release only with complete assets and recorded
validation limits.

README must cover exact hardware/pins/display controller, battery connector and
fuel-gauge limitations, initial flashing, OS-specific install/login/setup,
buttons, quota vs local Token scope, update/rollback/recovery, contribution hooks,
version policy, changelog/release process and dependency licenses. State clearly
which platforms were hardware-tested and whether app signing/notarization exists.
