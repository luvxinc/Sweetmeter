# Sweetmeter protocol 4

This document is the normative contract for the companion, firmware and release
builder. Integers on BLE and in signed firmware metadata are **unsigned little
endian**, except the existing signed timezone field. Offsets count bytes. An
implementation must reject a malformed length before reading a field. Unknown
opcodes, reserved bits, enum values and nonzero reserved bytes are rejected.

Protocol 4 retains the 250×122/4000-byte framebuffer transport. `QM3.2` is a
legacy firmware name, not a numeric release version; its migration is a USB
bootstrap. Never silently parse it as version zero. Once bootstrapped, firmware
and companion versions use the root `VERSION`: `YYYY.M.N`, with numeric tuple
comparison, year 1000–9999, month 1–12, sequence 1–4294967295, and no leading
zeroes. The release policy chooses the UTC month.

Build scripts read the single root `VERSION` and inject that exact value into
firmware and the packaged companion resource. Runtime status, UI, update
comparison and package names consume that injected/resource value; no independent
QM-style version constants are allowed. Source-mode companion startup reads root
`VERSION`; packaged startup reads its bundled copy. A missing/invalid version is
a build/startup error, never a fallback version.

## 1. GATT and connection rules

All UUIDs below are complete UUIDs; the changing `000x` is in the **first** field.

| Purpose | UUID | Properties |
| --- | --- | --- |
| Service | `7a1e0001-ff1b-4d9f-a023-47c7752c1a01` | Primary service |
| Dashboard control | `7a1e0002-ff1b-4d9f-a023-47c7752c1a01` | Write with response, notify |
| Dashboard data | `7a1e0003-ff1b-4d9f-a023-47c7752c1a01` | Write with response |
| Device status | `7a1e0004-ff1b-4d9f-a023-47c7752c1a01` | Read |
| OTA control | `7a1e0005-ff1b-4d9f-a023-47c7752c1a01` | Write with response |
| OTA data | `7a1e0006-ff1b-4d9f-a023-47c7752c1a01` | Write with response |
| OTA status | `7a1e0007-ff1b-4d9f-a023-47c7752c1a01` | Read, notify |

Reads/writes require BLE encryption; notifications have a CCCD. Preserve existing
bonding and the `quota-meter` NVS namespace with `host` and `name` keys. A stable
host UUID is a routing/selection identifier, **not cryptographic authentication**.
Just Works bonding does not turn it into one. Signed OTA protects authenticity of
the firmware; this is not secure boot or protection against physical USB flashing.

The ESP32 is the peripheral; Windows/macOS/Linux companions are GATT centrals.
There is one active connection. Advertise the fixed service UUID using a legacy
advertisement of at most 31 bytes: flags (3 bytes) plus complete 128-bit service
UUID list (18 bytes). Put the local name in the separate scan response (also at
most 31 bytes). Do not invent a Bluetooth manufacturer company ID, put a second
128-bit discovery UUID into the advertisement, or require computer advertising.
Discovery state is read from GATT after connecting.

The device requests MTU 185. New fragmented packets use at most 182-byte GATT
values and must also work with MTU 23 (20-byte values). Start with a 20-byte value
budget if the platform has no reliable negotiated limit; a BlueZ-reported MTU of
23 is not evidence of a larger supported value. Every write explicitly requests
a GATT response. A GATT response means the callback accepted the write, **not**
that a frame is displayed or an OTA flash write has finished.

BLE callbacks only validate packet bounds and enqueue bounded work. Crypto,
flash erase/write, panel drawing, NVS writes and sleep transitions run outside
the callback, with a queue and worker stack sized for them. There is one
application operation in flight; queue capacity is four packets, each at most
182 bytes (legacy H has its own bounded buffer). Queue exhaustion returns BUSY,
never silently drops a packet. Status reads copy an already prepared snapshot.

## 2. Existing dashboard messages

These byte layouts are unchanged. Subscribe to dashboard control notifications
before sending H. A protocol-4 companion may use the legacy dashboard transport
on protocol 3, but must disable OTA and protocol-4 discovery in that case.
The old Swift helper which requires `protocol == 3` itself needs replacement.

| Direction/characteristic | Layout |
| --- | --- |
| Host → control, hello | `H:u8, host_id:36 ASCII bytes, name:0..20 ASCII bytes` |
| Device → control, hello ACK | `H:u8, result:u8` (`0` selected, `7` not selected/menu open) |
| Host → control, clock | `T:u8, unix_seconds:u32, UTC_offset_seconds:i32` |
| Host → control, begin frame | `B:u8, sequence:u32, CRC32:u32, length:u16` (length 4000) |
| Device → control, protocol-4 begin-ready ACK | `b:u8, result:u8, sequence:u32, CRC32:u32` |
| Host → data | `offset:u16, payload:1..180 bytes` (also bounded by negotiated value size) |
| Host → control, commit frame | `C:u8, sequence:u32` |
| Device → control, displayed ACK | `A:u8, result:u8, sequence:u32, CRC32:u32` |
| Device → control, refresh request | Single byte `R` |
| Device → control, firmware check request | Single byte `U` (rocker held 3 s while selected) |
| Host → control, firmware check result | `u:u8, code:u8` (`2` current, `3` installing, `4` failed, `5` companion update needed) |

Holding the rocker is the physical confirmation for a firmware install: the
companion checks the signed release immediately and, when newer compatible
firmware exists, starts the normal verified OTA without a desktop dialog. A
companion update is never installed from the meter.

H uses a write-with-response long write if its 37–57 bytes exceed the ATT value
budget; this is below the 512-byte GATT attribute limit. Require a backend that
supports this normal long-write path; do not split legacy H into separate writes
or pretend a partial identity is valid. Registration below is explicitly
fragmented and does not depend on long writes.

Host IDs are lower-case UUID text matching
`7a1e1000-ff1b-4d9f-a023-[0-9a-f]{12}`; preserve existing installations' IDs.
For new installations generate the final 48 bits randomly and persist locally.
Names are 1–20 printable ASCII bytes for the device's existing font. Companion
UI may retain the full Unicode computer name; its transmitted short name removes
unsupported characters and falls back to `Mac`, `Windows PC` or `Linux PC` if
empty. Names never contain NUL/newline/control bytes.

Protocol 4 requires **physical selection before a new host can use H**. An empty
selected host no longer causes implicit first-peer selection. Existing persisted
selection is honored. H is refused while discovery is open. A selected H success
is the only network event that cancels the normal target-disconnect timer.

Protocol 4 waits for a matching **b0** begin-ready ACK before sending any
framebuffer data. The 10-byte b ACK echoes B's sequence and CRC: result 0 means the
worker is ready to receive, 2 means busy/invalid begin, 6 means critical battery,
and 7 means unauthorized/discovery open. Wait up to 30 seconds: B may be queued
behind a full panel refresh. After accepting B, the worker defers all normal UI
and clock panel redraws until C is complete or the 15-second receive timeout
expires, and drains frame packets without a fixed per-packet sleep. This prevents
a short four-packet callback queue overflowing behind a multi-second panel draw.
The GATT write response alone is never readiness. Protocol 3 has no b event and
retains its legacy begin behavior. A b error ends that frame attempt; do not send
data. C still receives its A acknowledgement only after panel completion.

Frame A results: 0 displayed; 1 already identical; 2 busy/invalid begin length;
3 incomplete/CRC/commit mismatch; 4 offset error; 5 display failure; 6 critical
battery. The CRC is standard CRC-32/ISO-HDLC (same as Python `zlib.crc32`). Data
must arrive at the exact next offset; no interleaving frames. Commit ACK follows
panel completion; wait up to 30 seconds. An unfinished frame expires after 15
seconds without a valid frame packet. OTA-active frame begins return result 2.
H/T remain available for the selected host when OTA is idle; dashboard data and
new clock writes pause during OTA.

## 3. Device status (0004)

Return one valid UTF-8 JSON object, **at most 512 bytes**. Never truncate a JSON
string. The following fields are required; omit optional diagnostics rather than
exceeding the limit. Strings containing local names are not included here.

```json
{"protocol":4,"firmware":"2026.9.1","board":"elecrow-crowpanel-2.13-v1.2-jd79661","selected_host":"7a1e1000-ff1b-4d9f-a023-0123456789ab","battery_percent":-1,"battery_mv":-1,"interval":60,"critical":false,"charge_state":"unknown","clock_synced":true,"menu":true,"discovery_nonce":4294967295,"discovery_remaining_ms":60000,"computers":8,"ota":false,"boot_health":"valid","last_update":"none","ota_target":""}
```

The example identity is synthetic. `discovery_nonce` and remaining milliseconds
are zero when closed; `selected_host` is empty before first physical selection.
`boot_health` is `pending`, `valid` or `failed`; `last_update` is `none`, `pending`,
`success`, `failed` or `rollback`; `ota_target` is the last attempted numeric
version or empty. `pending` also covers a durable update intent whose boot
selection/outcome cannot yet be proved; it is never success or proof of rollback. Missing battery measurement is -1, never a fabricated percentage.
The 0007 binary status supplies OTA offsets/errors; do not duplicate a verbose
OTA object in this JSON. Host parsers ignore optional unknown JSON keys.

## 4. Physical computer picker

Long-bottom (3 seconds) opens a **60-second hard discovery window** and creates a
fresh random nonzero 32-bit nonce. Only a physical button action opens/restarts
the window; packets and rocker movement cannot extend its hard deadline. The
list holds at most eight hosts, deduplicated by ID; include the previous selected
host/name initially, when present. Registration adds/updates a list item and
never selects it. Rocker up/down highlights; rocker center selects. Short-bottom
cancels. Short-top explicitly restarts discovery with a fresh nonce and window.

Immediately notify dashboard control with this 9-byte event:

```
D:u8, discovery_nonce:u32, window_ms:u32
```

An already connected companion stops frame submission and disconnects within one
second of D, then enters discovery backoff. The device revokes H authorization
when opening the menu and disconnects any remaining peer after two seconds.
Discovery is not dependent on the old companion's 30-second status polling.

A candidate connects, reads 0004, subscribes to 0002 and registers only if menu is
open and nonce is nonzero. Registration uses these **control** messages (distinct
from all legacy commands):

| Command | Layout |
| --- | --- |
| Begin | `J:u8, session:u32, nonce:u32, total:u16` (11 bytes) |
| Fragment | `j:u8, session:u32, offset:u32, payload` (9-byte prefix) |
| Commit | `K:u8, session:u32` (5 bytes) |
| ACK | `J:u8, result:u8, session:u32, next_offset:u32` (10 bytes) |

A nonzero random session identifies one attempt. The assembled body is exactly
`host_id:36 ASCII bytes, name_length:u8, name:name_length ASCII bytes`, 38–57
bytes. Total must match that body; name length is 1–20. At the minimum MTU each
fragment contains at most 11 payload bytes. One write and its J ACK precede the
next write; ACK's next offset is zero after begin, received count after fragment,
and total after successful commit. A duplicate ID updates its name without
consuming a list slot. No unauthenticated registration can submit a frame.

J result values: 0 success; 1 malformed length/value/name/ID; 2 menu closed or
nonce changed; 3 wrong session; 4 unexpected offset; 5 list full; 6 busy.
Nonzero result ends this registration; disconnect, reread status on a later
attempt, and start with a fresh session. Registration expires after five seconds
without a valid packet or when its window closes; no partial body is persisted.
Wait five seconds for a J ACK. Disconnect immediately after commit ACK/error.
The device forcibly releases a discovery connection after eight seconds from its
first status read/registration packet, or 15 seconds from physical connection,
whichever comes first. This prevents one candidate occupying the full window.

Disconnected companions scan/retry with random 2–5 second delay. A host that
registered successfully waits random 5–9 seconds before its next probe, and does
not register twice for the same nonce unless its previous attempt failed. A
nonselected host outside discovery disconnects and backs off 30–45 seconds.
After physical selection, persist host/name, close the menu, disconnect any
candidate, and advertise; only the chosen host's subsequent H enables data.
The selected companion reconnects automatically; others back off.

Opening a physical menu temporarily suspends disconnect sleep for its bounded
window. On cancel/expiry/selection, if no selected H connection exists, a host
whose disconnect guard was already armed starts a fresh 30-second grace period.
Registration/probing does not arm a never-connected device or count as target
reconnection. Pressing top-long remains a physical shutdown request when no OTA
commit is in progress.

## 5. Signed firmware metadata

The firmware release has two assets: the raw ESP application `.bin` and a binary
`.ota` envelope. The envelope is **160-byte header + u16 signature length + DER
signature**, no extra bytes. Signature length is 8–72; maximum envelope size is
234 bytes. The signature is ECDSA P-256 (`secp256r1`) over SHA-256 of the **exact
160 header bytes**. Host equivalent:

```python
signature = private_key.sign(header, ec.ECDSA(hashes.SHA256()))
public_key.verify(signature, header, ec.ECDSA(hashes.SHA256()))
```

Do not hash twice. Firmware `mbedtls_pk_verify` receives the SHA-256 header digest.
Require a trusted P-256 public key and a strict DER signature (no trailing bytes).
No RSA fallback, key downloaded from the release, or unsigned fallback exists.

| Offset | Length | Field and required value |
| ---: | ---: | --- |
| 0 | 8 | Magic bytes `53 57 4d 4f 54 41 34 00` (`SWMOTA4\0`) |
| 8 | 2 | Metadata format = 1 |
| 10 | 2 | Header length = 160 |
| 12 | 2 | Required device OTA protocol = 4 |
| 14 | 2 | Reserved = 0 |
| 16 | 48 | Board ID, NUL-padded ASCII |
| 64 | 4 | Firmware version year |
| 68 | 4 | Firmware version month |
| 72 | 4 | Firmware version sequence |
| 76 | 4 | Minimum companion version year |
| 80 | 4 | Minimum companion version month |
| 84 | 4 | Minimum companion version sequence |
| 88 | 4 | Raw application image size in bytes |
| 92 | 32 | SHA-256 of exact raw application image bytes |
| 124 | 16 | Trusted key ID, NUL-padded ASCII |
| 140 | 20 | Reserved, all zero |

Board ID is exactly `elecrow-crowpanel-2.13-v1.2-jd79661`; key ID is `release-1`.
Both fixed strings contain one NUL terminator followed by **only zero padding**;
reject missing termination, embedded extra data, and non-ASCII. The key ID selects
an already embedded trusted public key. Additional IDs require a prior trusted
key-rotation release; this field never authorizes an arbitrary public key.

Validate format, strict padding, signature, board, numeric version, protocol,
minimum companion, nonzero size and actual inactive slot capacity **before erase**.
Firmware version must be strictly greater than the running version. After a
rollback, reattempting that higher version is permitted; equal/lower versions
require the explicit USB recovery path, not an OTA override flag. Compare the
signed min-companion tuple to the version supplied in M below. The companion also
checks this before enabling Install; an unmet requirement offers companion update
first and cannot be bypassed by confirming a firmware dialog.

Current partition capacity is `0x330000` (3,342,336 bytes); use the actual partition
size at runtime. The target is `esp_ota_get_next_update_partition(nullptr)`, never
the running slot. The binary's ESP chip/image validation must also succeed.

## 6. OTA messages and application acknowledgements

Subscribe to 0007 before writing 0005. OTA requires selected H authorization,
closed discovery, no pending/receiving frame, and user confirmation in the desktop
UI. All OTA operations use one nonzero random **32-bit session**. The host pauses
normal frame traffic until the update ends. The firmware rejects another session
while one is active. Only one command/chunk is in flight; await its application
ACK before sending another.

Control (0005):

| Opcode | Layout | Meaning |
| --- | --- | --- |
| `M` | `M:u8, session:u32, envelope_size:u16, companion_year:u32, companion_month:u32, companion_sequence:u32, flags:u8` | Begin metadata, exactly 20 bytes |
| `m` | `m:u8, session:u32, offset:u32, payload` | Envelope fragment, 9-byte prefix |
| `S` | `S:u8, session:u32` | Verify completed envelope, then erase/prepare inactive slot |
| `F` | `F:u8, session:u32` | Finish image, verify digest/image, select boot slot, restart |
| `X` | `X:u8, session:u32` | Cancel before commit |
| `Q` | `Q:u8, session:u32` | Publish current OTA status |

M flags: bit 0 means the user explicitly acknowledged USB power in the confirmation
UI; required for this board, whose USB power cannot be measured in software.
All other bits are zero. This is an attestation, not a hardware measurement. If
a working gauge reports battery below 20%, M is rejected even with that flag.
Critical battery during a precommit transfer aborts before entering sleep.

Data (0006): `session:u32, offset:u32, image_payload:1..174 bytes`.
At MTU 23 image payload maximum is 12, metadata payload maximum is 11; at MTU
185 maxima are 174 and 173. Packets exceeding the effective value budget or
182-byte protocol cap are invalid. Offset is the next unreceived image/envelope
byte; 16-bit truncation is forbidden.

Every accepted command/chunk produces this **20-byte** 0007 notification. Reads
of 0007 return the latest state in the same format:

| Offset | Length | Value |
| ---: | ---: | --- |
| 0 | 1 | ASCII `O` (0x4f) |
| 1 | 1 | Protocol 4 |
| 2 | 1 | State enum below |
| 3 | 1 | Error enum below; 0 on success |
| 4 | 4 | Session |
| 8 | 4 | Next offset in the state's envelope/image |
| 12 | 4 | Total bytes in that envelope/image |
| 16 | 1 | Trigger opcode; data uses ASCII `d`, idle uses 0 |
| 17 | 1 | Flags: bit 0 cancellable; bit 1 signature verified; others zero |
| 18 | 2 | Reserved = 0 |

States: 0 IDLE; 1 METADATA; 2 PREPARING; 3 IMAGE; 4 VERIFYING; 5 REBOOTING;
6 CANCELLED; 7 ERROR. METADATA uses envelope offsets/size; from PREPARING onward
use image offsets/size. Terminal CANCELLED/ERROR retain the previous transfer's
last offset/total for diagnosis. REBOOTING has next = total = verified image
size. IDLE after boot has session/offset/total/opcode/flags zero. The readable
snapshot changes when worker state changes, not merely on GATT write receipt.

M success returns METADATA, offset 0. m success returns METADATA after its bytes
have been copied. S may publish PREPARING with opcode S; it is an intermediate
progress event, **not readiness**. Only IMAGE/error completes S. Data ACK is
IMAGE, opcode d, and advances offset **after successful flash write and streaming
hash update**. F may publish VERIFYING; only REBOOTING/error completes F. X ends
in CANCELLED. Q reports state with opcode Q and cannot extend inactivity timers.

Notifications for rejected commands echo the request's session/opcode with the
error. Wrong-session/unauthorized/BUSY requests never abort or change a different
active session. 0007 reads continue to expose that active session. A matching
session's invalid length, offset or state is a terminal transfer error; keep the
previous boot selection. An unparseable packet shorter than a session header has
session 0 and MALFORMED. Never acknowledge an unwritten byte as received.

| Error | Name | Meaning |
| ---: | --- | --- |
| 0 | OK | No error |
| 1 | UNAUTHORIZED | No selected H authorization |
| 2 | BUSY | Another session, discovery/frame activity or queue full |
| 3 | MALFORMED | Length, enum, reserved bits, encoding or bounds invalid |
| 4 | SESSION | Nonzero session does not match |
| 5 | STATE | Command not legal in current state |
| 6 | OFFSET | Offset is not the exact next byte |
| 7 | METADATA | Header format, padding or envelope size invalid |
| 8 | SIGNATURE | Invalid signature or untrusted key ID |
| 9 | BOARD | Hardware ID mismatch |
| 10 | VERSION | Invalid/equal/lower firmware version |
| 11 | COMPANION | Host version below signed minimum |
| 12 | PROTOCOL | Unsupported protocol field |
| 13 | SIZE | Zero size or larger than inactive partition |
| 14 | POWER | Missing USB acknowledgement or known low/critical battery |
| 15 | FLASH | Begin/write/end/boot-selection API failure |
| 16 | DIGEST | Final image hash differs |
| 17 | INCOMPLETE | S/F before exact expected total |
| 18 | TIMEOUT | Session inactivity/operation/global deadline |
| 19 | DISCONNECTED | Link lost before commit |
| 20 | IMAGE | ESP image/chip validation failed |

Cancel is a normal terminal state with error 0. An unknown opcode is MALFORMED.
Error notifications may echo any trigger opcode byte, including an unknown
opcode rejected as MALFORMED; successful notifications allow only the documented
opcodes or zero. The metadata format/protocol distinction is deliberate: metadata
format changes are METADATA, a valid header requesting a different protocol is
PROTOCOL.

### Timeouts, loss and cancellation

- Device metadata inactivity: 30 seconds; device image inactivity: 30 seconds.
  Only accepted fragments/data/control that advance the transaction reset it.
- PREPARING deadline: 60 seconds; VERIFYING deadline: 30 seconds.
- Hard update lifetime: two hours from M, including slow default-MTU transfers.
  Packet traffic cannot extend this deadline.
- Host ACK deadline: 10 seconds for M/m/data/X/Q, 65 seconds for S, 35 seconds for
  F. On one missing ACK, read 0007 (10-second read deadline). Proceed only if the
  same session has the expected state/opcode and exact advanced offset. If a
  snapshot proves no advance and the worker is not PREPARING/VERIFYING, cancel
  and restart the update from zero. Do not blindly resend an image chunk; exact
  offsets prevent duplicate flash writes. A still-active long operation is
  polled once per second until its original deadline, never indefinitely.
- All precommit link loss, timeout, cancel and critical battery paths abort the
  owned OTA handle and leave the running boot selection unchanged. No transfer
  resume across a disconnect/reboot exists in protocol 4. Reconnect diagnosis
  must not present an aborted download as installed.
- X is accepted in METADATA/PREPARING/IMAGE/VERIFYING until the commit critical
  section begins. The worker checks cancellation before boot selection. Once
  REBOOTING is reached, X returns STATE; UI cancellation is disabled while F is
  outstanding because the commit boundary may already have passed.

After F verifies the exact count/hash and ESP image, end the OTA handle and
atomically persist one NVS update record containing target version, target slot
address, image SHA-256 and stage `intent`. Only after that durable write succeeds
call `esp_ota_set_boot_partition`. An `intent` record means preparation, **not**
proof that a new boot slot was selected or that the candidate ever ran.

If boot selection returns an error, persist stage `failed`, expose FLASH and
`last_update: failed`, and do not issue a success/reboot notification. Preserve
the running application; if readback shows the boot selection changed despite
the error, restore it to the running slot and verify that operation before
resuming normal operation. A failed/uncertain restore remains `pending` and
requires recovery; never assert the previous boot selection is intact without
checking it. If boot selection succeeds, atomically advance the record to
`selected`, then publish REBOOTING, wait 500 ms for delivery and restart. If that
record write fails after successful boot selection, keep the durable `intent`,
report the outcome as pending/unconfirmed, and perform the same verified restore
before returning to normal operation; a failed restore must not be labeled an
aborted update with an unchanged boot slot.

A power loss between boot selection and the `selected` write can still start the
candidate. Reconcile the record with actual running version and partition state
on **every boot**, as specified below. A lost final notification is not success
or failure by itself. An `esp_ota_end` call consumes its handle even on failure;
never double-abort it.

During a live bounded OTA session, suspend disconnect auto-sleep and computer
selection. Do not suspend the transfer's own deadlines. Progress panel updates
are at most once per 10 percentage-point advance and at least five seconds apart;
flash callbacks never draw. On every precommit exit restore the dashboard and
normal power policy. A disconnected previously armed target receives a fresh
30-second grace period after the aborted transfer. A physical shutdown request
first cancels a precommit transfer; it cannot interrupt the boot-selection
critical section.

## 7. First boot, success and rollback

The pinned Arduino 2.0.17 startup must be overridden with a **strong**
`extern "C" bool verifyRollbackLater() { return true; }` so it cannot confirm an
update before setup. Keep rollback-enabled dual slots; no eFuses are programmed.

On every boot, load the atomic update record and inspect the running slot and
the recorded target slot with `esp_ota_get_state_partition`. Resolve outcomes
using both the record and the actual partition state, not a version mismatch
alone:

- If running slot/address and compiled VERSION match the recorded target and
  the running slot is already `ESP_OTA_IMG_VALID`, finish/persist stage `success`
  and expose `boot_health: valid`, `last_update: success`. This reconciliation
  applies even when the record still says `intent`, `selected` or `booted`; a
  reset after marking the image valid but before writing success must not leave
  the installed update permanently pending. Retry a failed NVS persistence and
  expose pending until it is durable.
- **Any** running slot in `ESP_OTA_IMG_PENDING_VERIFY` immediately enters the
  bounded watchdog/health path, regardless of whether its version/slot matches
  the record. A missing signed target record or mismatched compiled VERSION or
  slot is an immediate health failure: invalidate and roll back, rather than
  leaving the candidate running unconfirmed. For a matching candidate, persist
  stage `booted` and perform the remaining checks below. Failure to write its
  health record is an NVS health failure, not successful confirmation.
- If an older version is running, a persisted stage `selected` or `booted` plus
  target slot state `ESP_OTA_IMG_INVALID` or `ESP_OTA_IMG_ABORTED` proves failed
  candidate boot/rollback. Persist stage `rollback` and report the attempted
  target. An already durable `rollback` result remains rollback on later boots
  with that older running version; likewise retain a durable `failed` result
  unless new authoritative running-target VALID evidence resolves it. A record
  containing only `intent`, or a target version mismatch by
  itself, does **not** prove rollback: it could precede boot selection or follow
  a failed selection API call. Without adequate evidence keep `pending`
  (unconfirmed); retain explicit `failed` selection outcomes as failed.
- With no pending update record, normal bootstrap/current firmware may report
  its ordinary valid health and `last_update: none`. A record/partition mismatch
  outside the cases above remains unconfirmed; do not invent an outcome.

Pending-candidate local health checks finish within 30 seconds: required
allocations/mutexes, NVS access, panel initialization/refresh and BLE service
startup. The compiled VERSION and running slot must equal the persisted signed
target. Provider availability, Internet, clock sync and a connected computer are
**not** health checks. Only after passing all checks call
`esp_ota_mark_app_valid_cancel_rollback()` and require its successful return,
then atomically persist stage `success` before exposing last_update success.
The every-boot VALID reconciliation above closes the reset window between those
two durable operations. Failed checks invoke invalidation and rollback; a
crash/watchdog/reset before confirmation also uses bootloader rollback. Do not
persist success before the ESP OTA valid-state operation succeeds.

After the expected reboot, the companion scans/reconnects for up to 120 seconds,
reads status and requires firmware exactly equal to the requested version plus
`boot_health == "valid"` and `last_update == "success"` for success. It then sends
H/T and a normal frame and reports any display/connection failure separately.
A previous version with last_update rollback is an explicit failed update. An
unavailable device at 120 seconds is **unconfirmed**, not declared bricked or
successful. Persist the pending target locally and reconcile on the next
connection; do not automatically retry installation without user confirmation.

## 8. Release manifest and desktop confirmation

Release assets include `manifest.json`, its raw DER detached signature
`manifest.json.sig`, the firmware `.bin`, its `.ota` envelope, and native companion
packages. The manifest is signed with the same ECDSA P-256/SHA-256 scheme and
trusted key-ID table. Verify the **original downloaded bytes**, never a parsed
and reserialized object. Reject duplicate JSON keys, invalid UTF-8, documents
larger than 131072 bytes, signature lengths outside 8–72, and unknown key IDs.
The release builder emits sorted-key compact JSON UTF-8 plus one final LF; that
serialization is for reproducibility, not a reason to reserialize before verify.

Schema 1 required top-level fields are `schema` (1), `product` (`Sweetmeter`),
`channel` (`stable`), `version` (numeric version string), `published_at` (UTC
RFC3339), `commit` (40 lower-case hex), `key_id` (`release-1`), `changes` (array of
`{"version": "YYYY.M.N", "notes": ["user-facing change", ...]}`), and `artifacts`.
`changes` is newest first with unique numeric versions no newer than the manifest;
include retained user-facing version history so clients can display every entry
newer than their installed version, including skipped releases. Never use an
unsigned GitHub release body as the source of the confirmation dialog's notes.

Each artifact has `kind` (`firmware` or `companion`), `version`, `asset` (basename),
`url` (HTTPS GitHub release asset URL), `size` (positive integer) and `sha256`
(64 lower-case hex). A firmware artifact additionally has `board`, `protocol`
(4), `minimum_companion`, `metadata_asset`, `metadata_url`, `metadata_size` and
`metadata_sha256`; its version/board/protocol/minimum/size/hash must agree with the
signed binary header. `minimum_companion` cannot exceed the manifest release
version: all companion artifacts in this release use that same version, so a
future minimum would make its offered companion update unable to satisfy it.
A companion artifact additionally has `os` (`macos`,
`windows`, `linux`) and `arch` (`arm64`, `x86_64`). Reject duplicate matching
artifacts and non-GitHub asset URLs; redirects are limited to HTTPS GitHub release
asset/CDN hosts by the downloader. Verify every size and digest before use.

The checker can download bounded manifests automatically but cannot download or
install a selected update until the user chooses **Install**. The desktop shows
current/target versions, verified notes, Install/Later/Skip and the USB-power
acknowledgement for firmware. Closing means Later. Persist skip/reminder choices.
An unmet firmware minimum companion disables firmware Install and offers the
matching signed companion update first. After companion replacement/restart,
recheck its actual VERSION, device version and update eligibility. Updating the
companion does not implicitly authorize a later firmware installation.

An OS helper may replace a staged verified companion package after the current
process exits; failure leaves/restores the previous package. Unsupported or
unwritable installation locations offer a verified native installer/manual path
instead of claiming automatic replacement succeeded. Installation success is
reported by the newly launched actual version. Platform app signing/notarization
is separate from these release signatures and must be stated accurately.

The device cannot display that a computer is cryptographically authentic merely
because its host ID matches, cannot measure USB power without additional
hardware, and cannot claim complete Windows/Linux hardware acceptance from Mac
BLE tests. These limitations are part of the product contract.
