# Changelog

User-facing changes for every accepted commit, in chronological order.
Versions use `YYYY.M.N`, with the month determined in UTC.

## [2026.9.1] - 2026-09-20

- Add a single-page Claude, Fable and Codex quota dashboard with subscription labels and reset countdowns.
- Prepare a public source tree with bundled redistributable fonts, hardware documentation and private runtime data excluded.
- Enforce a calendar version and user-facing changelog for every commit before publication.

## [2026.9.2] - 2026-09-20

- Add native desktop companions for macOS, Windows and Linux with per-user startup and update notifications.
- Offer signed Bluetooth firmware updates with explicit confirmation, progress and rollback after failed startup.
- Improve computer selection, reconnect and e-paper frame transfer while keeping the approved four-quota dashboard.
- Recover interrupted companion updates and preserve existing local device identities during installation.
- Run trusted primary CI in disposable Mac mini VMs and publish only the exact hardware-accepted firmware artifact.

## [2026.9.3] - 2026-09-20

- Fix Windows startup updates and archive validation, and isolate native builds so Intel Mac packages load their cryptography runtime correctly.

## [2026.9.4] - 2026-09-20

- Document verified BLE firmware upgrades, rejection and rollback behavior, and the one-time legacy macOS pairing-cache recovery.
- Install GitHub CLI in the isolated Mac mini runner template so release validation and publishing can run.

## [2026.9.5] - 2026-09-20

- Show the device’s running firmware version in the dashboard’s upper-left corner, alongside Bluetooth status.

## [2026.9.6] - 2026-09-20

- Fix release verification of Windows public-key files with CRLF line endings while continuing to reject different signing keys; record owner-confirmed display and wake/reconnect checks.

## [2026.9.7] - 2026-09-20

- Prefix the upper-left running firmware version with "v " on the meter and matching preview.

## [2026.9.8] - 2026-09-20

- Show four Bluetooth signal bars after BT using live peer RSSI; clear stale/disconnected readings and reuse normal e-ink refreshes.
