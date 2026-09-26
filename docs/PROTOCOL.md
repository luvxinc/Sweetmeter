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
bonding and the `quota-meter` NVS namespace. A stable host UUID is a routing and
display identifier, **not authentication**, and the device never publishes it.
A link is authorized only by proving a per-computer pairing secret (section 2.1).
Just Works (LE Secure Connections) bonding encrypts the link against passive
listeners but does not authenticate either side. Signed OTA protects authenticity
of the firmware; this is not secure boot or protection against physical USB flashing.

The ESP32 is the peripheral; Windows/macOS/Linux companions are GATT centrals.
There is one active connection. Advertise the fixed service UUID using a legacy
advertisement of at most 31 bytes: flags (3 bytes) plus complete 128-bit service
UUID list (18 bytes), plus, from firmware 2026.9.20, the capability marker below
(7 bytes; 28 in total). Put the local name in the separate scan response (also at
most 31 bytes). The marker is in both packets because macOS passes a scan
response on only now and then (measured: many 3-second scans without one), so
an open menu announced only there was often seen tens of seconds late. Do not put a second 128-bit discovery UUID into the
advertisement or require computer advertising. Discovery state is read from
GATT after connecting.

**Capability marker.** Pairing firmware (section 2.1) also puts one
manufacturer-specific data AD into the scan response: company ID `0xFFFF`
(little endian `ff ff`; the Bluetooth SIG value for tests and unassigned
companies, used because Sweetmeter holds no company ID), then ASCII `SM` and a
flags byte: bit 0 pairing secrets supported (always 1), bit 1 physical menu
open; other bits 0. The scan response is `09`-type complete local name
(the meter name, plus `-PAIR` while the menu is open) followed by the
marker: 24 bytes for a default name, or 29 with the suffix; at most 30 bytes
with a 16-byte owner-chosen name and the suffix (`firmware/src/advertising.h`,
size asserted by tests). Pre-secret firmware (2026.9.8/2026.9.13) sends the name
only. A computer classifies a meter from the advertisement merged over one
scan: marker with the menu bit (or, when a scanner reports only the name, the
`-PAIR` suffix) = pairing firmware, menu open; marker without it = pairing
firmware, menu closed; a name without a marker = pre-secret firmware; neither
yet = undecided (the scan response has not arrived; decide on a later scan).
bleak 3.0.2 reports the scan response's manufacturer data on macOS
(CoreBluetooth merges it), Windows (WinRT pairs advertisement and scan
response) and Linux (BlueZ `ManufacturerData`) with its default active scan.

A scanner (or adapter/driver) that drops manufacturer data makes pairing
firmware look pre-secret, so the companion probes it once. The status read is
authoritative: `"auth":1` with the menu closed at an address this computer is
not paired or pairing with closes the link at once, before any write (the
meter then removes the unearned bond; only the computer's OS may keep a stale
key from that one probe), and the address is remembered as pairing firmware
so it is not probed again while its marker stays invisible. The `-PAIR` name
suffix still reveals an open menu.

**Meter name.** The local name is the owner-chosen name (section 2.2) or,
without one, the default `Sweetmeter-XXXX`, where `XXXX` is the last four
characters of `serial` (section 3) in upper case, i.e. the last two bytes of
the eFuse MAC. Firmware up to 2026.9.18 formatted the first two bytes instead,
so every board with the same manufacturer prefix advertised the same name
(for example `Sweetmeter-05D4`). The same name is the GAP device name.
Computers identify meters by serial and address, never by name.

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
182 bytes (legacy H and Y may use a GATT long write). Queue exhaustion returns
BUSY, never silently drops a packet: the callback records the refused packet in
a second four-entry queue and the worker sends the BUSY reply. **Every
notification is sent by the worker task** from its own buffer through
`esp_ble_gatts_send_indicate`; Arduino's `BLECharacteristic::notify()` is not used
because it re-reads the characteristic value without a lock while BTC_TASK may
be replacing it. Status reads copy an already prepared snapshot; the callback
only patches in the current link's challenge. The worker blocks on a task
notification (packets, buttons, connection events) with a 50–250 ms deadline
check instead of polling.

Outside the physical menu and OTA, a link that is not authorized within
**10 seconds** of being encrypted is disconnected, and any rejected hello
disconnects immediately after its reply. A link that is not encrypted yet may
stay up to **30 seconds** after connecting (the pairing protocol's own timeout):
a computer pairing for the first time waits for its user, and macOS asks
"Connection Request from <meter name>" and starts pairing only after Connect is
clicked, so the meter cannot see that pairing is coming. Encryption is the
Bluedroid authentication-complete event, for a new pairing or a bonded
reconnect. Firmware up to 2026.9.18 counted 10 seconds from connecting, which
left a person answering that prompt about 8 seconds. A stray phone or a stale
OS auto-connection cannot occupy the single link: one that never encrypts is
dropped after 30 seconds, a bonded one 10 seconds after it encrypts. Only the
link's first encryption counts, and however often a central pairs again an
unauthorized link never outlasts 40 seconds after connecting (45 in the menu).

## 2. Existing dashboard messages

The frame, clock and notice layouts are unchanged; the hello gained P and Y.
Subscribe to dashboard control notifications before any hello. A protocol-4 companion may use the legacy dashboard transport
on protocol 3, but must disable OTA and protocol-4 discovery in that case.
The old Swift helper which requires `protocol == 3` itself needs replacement.

| Direction/characteristic | Layout |
| --- | --- |
| Host → control, mutual-authentication nonce | `N:u8, host_nonce:16` (directly before P; no reply; section 2.1) |
| Host → control, authenticated hello | `P:u8, proof:16` (section 2.1) |
| Host → control, legacy hello | `H:u8, host_id:36 ASCII bytes, name:0..20 ASCII bytes` (migration only) |
| Host → control, provision secret | `Y:u8, secret:32` (only directly after a legacy H answered `8`) |
| Device → control, hello ACK (H and P) | `H:u8, result:u8`, plus `meter_proof:16` for results 0 and 9 when this link sent N |
| Device → control, provision ACK | `Y:u8, result:u8` (`0` stored and authorized, `2` busy, `5` storage failed, `7` refused) |
| Host → control, clock | `T:u8, unix_seconds:u32, UTC_offset_seconds:i32` |
| Host → control, begin frame | `B:u8, sequence:u32, CRC32:u32, length:u16` (length 4000) |
| Device → control, protocol-4 begin-ready ACK | `b:u8, result:u8, sequence:u32, CRC32:u32` |
| Host → data | `offset:u16, payload:1..180 bytes` (also bounded by negotiated value size) |
| Host → control, commit frame | `C:u8, sequence:u32` |
| Device → control, displayed ACK | `A:u8, result:u8, sequence:u32, CRC32:u32` |
| Device → control, refresh request | Single byte `R` |
| Device → control, firmware check request | Single byte `U` (rocker held 3 s while selected) |
| Host → control, firmware check result | `u:u8, code:u8` (`2` current, `3` installing, `4` failed, `5` companion update needed) |
| Host → control, rename meter | `L:u8, length:u8, name:length bytes` (length 0–16; section 2.2) |
| Device → control, rename ACK | `L:u8, result:u8` (section 2.2) |

Holding the rocker is the physical confirmation for a firmware install: the
companion checks the signed release immediately and, when newer compatible
firmware exists, starts the normal verified OTA without a desktop dialog. A
companion update is never installed from the meter.

The meter sends U only on an authorized link and then shows "Checking for
updates..."; without one it shows "Select a computer first." (none selected) or
"Computer not connected." (selected but not connected). It accepts `u` only while
a check is outstanding (and `u 2` or `u 4` after `u 3`); unsolicited `u` is
ignored. A new or lost link clears a pending check; no answer within 45 seconds
shows "Update check failed." "Update found. Installing..." (`u 3`) stays until
the companion reports `u 2`/`u 4`, the OTA transfer ends (an OTA error shows
"Update check failed."; a cancel just removes it; success restarts the meter),
or 10 minutes pass without either (then "Update check failed."). A lost link
does not hide it. Other results stay about eight seconds and disappear with the
next minute draw. The companion drops a notice it could not deliver within 60
seconds. A firmware job that never starts on its meter (section 4) ends with a
`job_expired` error on the computer; the companion should then send `u 4`,
and otherwise the meter falls back to its 10-minute deadline.

Hello results: `0` authorized; `2` busy (queue full, retry, link stays open);
`7` rejected (unknown secret, replayed/second hello, menu open, legacy H refused
or outside its migration window);
`8` legacy H accepted, send Y now; `9` the proof is valid but that computer is
not the one selected on the meter (or the menu is open, below). Every result
except 0 and 2 is followed by a disconnect. N and P (17 bytes each) fit the
minimum 20-byte value budget, and so does the 18-byte ACK with a meter proof.
A busy N is answered `H 2` like a busy hello. H (37–57 bytes)
and Y (33 bytes) use a write-with-response long write if they exceed the ATT
value budget; do not split them into separate writes. Registration below is
explicitly fragmented and does not depend on long writes.

### 2.1 Pairing secrets and the authenticated hello

Firmware that implements this reports `"auth":1` in its status. Each computer
generates a random 32-byte secret **per meter** (keyed by the meter's `serial`,
never by an OS-specific BLE address), stores it in a private file (`0600`) and
sends it in its registration while the owner has the physical menu open. When
the owner selects it, the meter stores host ID, name and secret.

The meter keeps up to **eight** paired computers in one checksummed NVS blob
(`pairs`); exactly one of them is selected. Adding a ninth replaces the least
recently used. The legacy `host`/`name` keys mirror the selected computer for
downgrade tooling only; a valid `pairs` blob is authoritative and a corrupt one
fails closed (no selection) rather than falling back to a bare host ID.

Every physical connection gets a fresh random 16-byte challenge, published as
32 lower-case hex characters in the status `challenge` field. The computer proves
possession of its secret:

```
proof = HMAC-SHA256(secret, "SWM-AUTH-1" || challenge(16 raw bytes)
                    || serial (12 ASCII) || host_id (36 ASCII))[0:16]
```

and sends `P || proof`. The meter tries each stored secret (constant-time
comparison) so the hello never names a computer. A match for the selected
computer authorizes the link (`0`); a match for another paired computer, or any
match while nothing is selected, returns `9`; no match returns `7`. Each link
allows one hello; the challenge cannot be reused, and a new connection gets a
new one. A hello while OTA is active never changes or revokes authorization.

**Mutual authentication.** The hello above only convinces the meter. Firmware
that reports `"mutual":1` also proves the secret back: a computer sends
`N || host_nonce` (16 fresh random bytes) directly before P, and the ACK to 0
or 9 then carries

```
meter_proof = HMAC-SHA256(secret, "SWM-METER-1" || challenge(16 raw bytes)
                          || host_nonce(16) || serial (12 ASCII) || host_id (36 ASCII))[0:16]
```

for the secret that matched. N is accepted once per link, only before the
hello and only with exactly 16 bytes; anything else is refused (`H 7`,
disconnect). Without N the ACK stays two bytes, so older companions are
unaffected; firmware without mutual authentication ignores N. A companion
that sent N treats a 0 or 9 without a valid meter proof as a device that is
not its meter: it sends no clock, frame, notice or firmware on that link,
disconnects, and changes no pairing record. Once a meter has proven itself,
the companion records that and refuses that serial whenever its status lacks
`"mutual":1` (a downgrade). Reinstalling pre-mutual firmware over USB
therefore needs Forget on the computer.

While the physical menu is open a hello never authorizes, but after N the
meter still answers it: `9` with the meter proof when the secret belongs to a
paired computer, `7` otherwise. This lets a paired computer learn cheaply
whether the owner removed it (section 4). P without N is refused in the menu
as before.

**Secrets on the computer.** A secret is transmitted exactly once, in the
registration that created it, and a computer never sends a secret the meter
has already proven it stores. The companion keeps per meter (keyed by serial)
at most one **confirmed** secret, **bound to the BLE address at which the meter
proved it**, and, per BLE address, one **pending** secret:

- Every registration generates a fresh pending secret and saves it before
  sending it; a registration never moves the confirmed secret's address. A
  pending secret becomes the confirmed one only after the meter accepts an
  authenticated hello (`P` → `0`) with it; a refused pending secret (`7`) is
  discarded and the confirmed one is tried on the next connection (one hello
  per link). `9` leaves the pending secret pending.
- Only the meter at the bound address can revoke the confirmed secret: a `7`
  for it there marks it refused. A `7` from any other address changes nothing.
  With nothing selected the companion still sends its hello: `9` means it is
  still paired (it keeps its secret and does not register again), `7` at the
  bound address means the meter forgot it.
- A pending secret accepted at another address replaces the confirmed one only
  after the meter at the bound address refused it (the owner reset that meter
  or removed this computer); until then the old record stays, the pending
  secret stays pending and that device is not driven.
- A device at another address that accepts the confirmed secret is driven, and
  the binding moved to its address, only if it also proved the secret (meter
  proof). Without mutual authentication a peripheral that accepts any proof
  is indistinguishable from the meter, so it is ignored instead.
- A computer holding a confirmed secret that has not been refused does not
  register in an open menu (switching back to it needs no registration). With
  `"mutual":1` it checks its membership there instead (N, P → 9 or 7); a `7`
  from the bound address marks the secret refused and it registers on its next
  connection, so a removed or evicted computer reappears in the same menu. With
  older pairing firmware it learns this only from its next hello outside the
  menu. It also registers again after the user chose Forget.
- A peripheral that copies a meter's public `serial` therefore never obtains a
  secret that the real meter holds and cannot demote, replace or move the
  pairing with the real meter: it can at most receive a fresh pending secret
  (only after the real meter refused ours) that the real meter never stored.
  The proof also binds the meter's serial, so a secret is useless with any
  other meter. A device that also spoofs the real meter's BLE address is out
  of scope (the address is where the binding lives).

**Migration from pre-secret firmware (2026.9.8, 2026.9.13).** Those meters stored
only a selected host ID. After updating, the status shows `"selected":true,
"secured":false`. The selected computer sends one legacy H; if the ID matches
the meter answers `8` and the computer must immediately send `Y` with a new
secret (trust on first use). Y is durable in NVS before it is acknowledged and
authorizes the link; the companion then treats that secret as confirmed. This
trust-on-first-use is bounded on both sides:

- The meter accepts it only within **10 minutes after boot** (the OTA restart,
  a reset or the owner's wake) and only for the **first** legacy H that names
  the stored computer; that hello consumes the window even if Y never follows.
  Outside the window every legacy H is refused (`7`) and the owner pairs the
  computer again with the physical menu (the stored selection then gains a
  secret without a confirmation prompt). From the moment a secret exists,
  legacy H is always refused.
- The companion runs H/Y only when it holds no confirmed secret for that
  serial **and** a pre-secret companion record exists for that exact BLE
  address (its earlier `bluetooth.json` pin or an `address:` pairing made
  with pre-secret firmware). It always provisions a fresh secret.

A companion connecting to pre-secret firmware (status has `selected_host` and
no `auth`) keeps using legacy H and the legacy registration body so it can
still deliver the OTA that adds pairing secrets; firmware that adds this
protocol must be released with a `minimum_companion` that implements it.

Host IDs are lower-case UUID text matching
`7a1e1000-ff1b-4d9f-a023-[0-9a-f]{12}`; preserve existing installations' IDs.
For new installations generate the final 48 bits randomly and persist locally.
Names are 1–20 printable ASCII bytes for the device's existing font. Companion
UI may retain the full Unicode computer name; its transmitted short name removes
unsupported characters and falls back to `Mac`, `Windows PC` or `Linux PC` if
empty. Names never contain NUL/newline/control bytes.

Protocol 4 requires **physical selection before a computer is authorized**. An
empty selection never causes implicit first-peer selection. Hellos are refused
while discovery is open. An authorized hello (P, or Y after migration) is the
only network event that cancels the normal target-disconnect timer.

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
data.

Frame A results: 0 stored and drawn now; 1 stored and accepted, drawn with the
next minute boundary; 2 busy/invalid begin length; 3 incomplete/CRC/commit
mismatch; 4 offset error; 5 display failure; 6 critical battery. Both 0 and 1 are
success. The CRC is standard CRC-32/ISO-HDLC (same as Python `zlib.crc32`). Data
must arrive at the exact next offset; no interleaving frames. Wait up to 30
seconds for A. An unfinished frame expires after 15 seconds without a valid frame
packet. OTA-active frame begins return result 2. Hellos and T remain available
when OTA is idle; dashboard data and new clock writes pause during OTA.

**Display timing.** The panel uses the full clear-and-draw waveform (a DU trial
ghosted), so each draw is a full two-pass refresh. In steady state the panel
refreshes **at most once per minute**: a valid frame is copied into the
dashboard buffer and acknowledged with result 1 immediately, and the dashboard
(with the new clock minute) is drawn at the next minute boundary. Only
user-visible state changes draw immediately: the first frame after boot or
wake (A result 0), a frame that answers a physical refresh press (A result 0),
and the menu, OTA progress, rocker-hold notices, low-battery and power screens.
Removing a notice banner, the refresh marker, or a changed Bluetooth indicator
waits for the next minute draw. A minute draw is skipped entirely if nothing
visible changed.

- **Clock.** Once synchronized, a T that differs from the meter's clock by less
  than 2 seconds does not step it (transport jitter). The scheduled redraw
  happens only when the displayed local minute advances, or jumps by more than
  one minute in either direction (a real correction or a timezone change);
  a step back into the previous minute never redraws that minute.
- **First T of a link.** It defers the scheduled redraw by up to 5 seconds so
  the link's first frame, which normally follows at once, is drawn together
  with the corrected time in a single refresh.
- **Top-button refresh.** With an authorized computer the press itself is not
  drawn: the meter notifies `R` and draws the answering frame at once (A
  result 0), refreshing the panel even if the frame is identical to what is
  shown (that refresh is the press's acknowledgement; one refresh per press).
  If no frame arrives within 10 seconds, the `*` marker is drawn to
  acknowledge the press; a frame within 30 seconds of the press still counts
  as its answer. Without an authorized computer the marker is drawn at once.
  On `R` the companion forces a provider refresh and sends the first frame
  the app renders after the request, **even if it equals the last frame
  sent** (otherwise unchanged data would send nothing and the meter would
  fall back to the marker). Later identical frames are not resent.

### 2.2 Meter name

The owner names the meter from the computer that is selected on it. Firmware
that implements this reports `"rename":1` in its status (section 3); a
companion sends `L` only when a status read on the current link reported it,
and only on an authorized link.
Earlier firmware answers the unknown opcode with `A 3`.

`L:u8, length:u8, name` is 2 + length bytes (at most 18, within the minimum
20-byte value budget); a packet whose size differs from 2 + length is
malformed. `length` 0 removes the owner-chosen name and restores the default
(section 1). Otherwise the name must be:

- 1–16 bytes of well-formed UTF-8: shortest-form encodings only, no surrogate
  code points (U+D800–U+DFFF), nothing above U+10FFFF;
- free of control characters: U+0000–U+001F, U+007F and U+0080–U+009F;
- without a leading or trailing space (U+0020);
- not ending in `-PAIR`, the open-menu suffix of section 1.

Sixteen bytes are, for example, sixteen ASCII characters or five CJK
characters. The limit keeps the scan response within 31 bytes including the
`-PAIR` suffix and the marker. Companions normalize input (Unicode NFC, trim
surrounding white space) before encoding it; firmware validates exactly the
bytes it received and never alters them.

The worker stores the name in NVS key `label` of the `quota-meter` namespace
and reads it back; removing the name deletes the key. Only after the stored
value is verified does the meter use it as its GAP device name and in its scan
response, which apply from its next advertisement; the current link continues.
A stored value that fails validation at boot is ignored (the default is used)
and left for the next successful `L` to replace.

Rename ACK results: `0` stored (or default restored) and applied; `1`
malformed packet or invalid name (nothing changed); `2` busy, an OTA is active
(retry after it ends); `5` storage failed (the previous name remains); `7` not
authorized (no authorized link, or the menu is open). The meter shows its name
on the start screen that is drawn before the first dashboard; a name that the
screen font cannot draw (anything but printable ASCII) is shown as the default
name there.

## 3. Device status (0004)

Return one valid UTF-8 JSON object, **at most 512 bytes**. Never truncate a JSON
string. The following fields are required; omit optional diagnostics rather than
exceeding the limit. Strings containing local names are not included here.

```json
{"protocol":4,"firmware":"2026.9.1","board":"elecrow-crowpanel-2.13-v1.2-jd79661","auth":1,"mutual":1,"serial":"a1b2c3d4e5f6","selected":true,"secured":true,"challenge":"5f0c3a9e1b7d2c4e8a6f1d3b5c7e9a0b","battery_percent":-1,"battery_mv":-1,"interval":60,"critical":false,"charge_state":"unknown","clock_synced":true,"menu":true,"discovery_nonce":4294967295,"discovery_remaining_ms":60000,"computers":8,"ota":false,"boot_health":"valid","last_update":"none","ota_target":"","rename":1,"rssi":-61}
```

The example values are synthetic. The status **never names the selected
computer** (the former `selected_host` field is gone): any nearby central can
read it. `auth` is 1 for the pairing protocol of section 2.1. `mutual` is 1
when the firmware implements mutual authentication (N and the meter proof,
section 2.1); it is omitted by earlier pairing firmware and only valid with
`auth`. `rename` is 1 when the firmware accepts the meter name command `L`
(section 2.2); it is omitted by earlier firmware and only valid with `auth`.
It is the first optional field, appended before `rssi`: the required fields
can reach 511 bytes only with implausibly long version strings, and then it
is dropped rather than exceeding the limit. A companion treats a missing
`rename` as "not supported". `serial` is the
meter's stable eFuse MAC as 12 lower-case hex characters; computers key pairing
secrets by it. `selected` says whether a computer is selected; `secured` whether
that selection has a pairing secret (false only after migrating from pre-secret
firmware). `challenge` is this connection's 32-hex-character challenge.
`computers` is the number of rows in the open menu (0 while it is closed). `rssi` is an optional
diagnostic, present only while an authorized link has a recent reading and only
if it fits. `discovery_nonce` and remaining milliseconds are zero when closed.
Pre-secret firmware reported `selected_host` and no `auth`; companions accept
both formats.
`boot_health` is `pending`, `valid` or `failed`; `last_update` is `none`, `pending`,
`success`, `failed` or `rollback`; `ota_target` is the last attempted numeric
version or empty. `pending` also covers a durable update intent whose boot
selection/outcome cannot yet be proved; it is never success or proof of rollback. Missing battery measurement is -1, never a fabricated percentage.
The 0007 binary status supplies OTA offsets/errors; do not duplicate a verbose
OTA object in this JSON. Host parsers ignore optional unknown JSON keys.

## 4. Physical computer menu: pairing, switching and removal

Long-bottom (3 seconds) opens a **60-second hard discovery window** and creates a
fresh random nonzero 32-bit nonce. Only a physical button action opens/restarts
the window; packets and rocker movement cannot extend its hard deadline. The menu
lists the paired computers first (the selected one, then most recently used),
then computers that register while it is open, at most twelve rows,
deduplicated by host ID. Each row shows a marker (`>` selected, `+` new, `!`
needs attention), the name and the last four hex characters of the host
ID, so equal names are distinguishable. `!` rows also carry a label:
`CONFLICT` (two different secrets claimed this ID in this window; not
selectable), `NEW KEY` (a paired computer registered a different secret) or
`UPDATE APP` (an old companion asked to pair; see J result 9). Rocker up/down
highlights; while an `UPDATE APP` row is highlighted the hint line reads
"Update Sweetmeter on <name>".

- **Rocker press** on a paired row switches to that computer at once; its stored
  secret is reused, nothing is re-registered. On a new row it pairs and selects
  that computer (replacing the least recently used pairing if eight exist). On
  a `NEW KEY` row it first shows a **NEW KEY** confirmation ("This computer sent
  a new key. Accept only if you reset it."): a second rocker press stores the
  new secret and selects the computer; any other key cancels and keeps the
  stored secret. A new row from an old companion (`UPDATE APP`) cannot be
  selected; a paired computer running an old app can still be switched to.
- **Rocker hold (3 s)** on a row asks "REMOVE COMPUTER"; rocker press confirms,
  any other key keeps it. Removing the selected computer leaves none selected.
- Short-bottom cancels. Short-top restarts discovery with a fresh nonce and window.

Immediately notify dashboard control with this 9-byte event:

```
D:u8, discovery_nonce:u32, window_ms:u32
```

An already connected companion stops frame submission and disconnects within one
second of D, then enters discovery backoff. The device revokes the link's
authorization when opening the menu and disconnects any remaining peer after two
seconds. While the menu is open the capability marker's menu bit is set and the
scan-response local name gains the suffix `-PAIR` (`Sweetmeter-ABCD-PAIR`, 22
bytes including its AD header; kept as the fallback for scanners that report
only the name), so computers that have never paired only probe a meter with
pairing firmware whose owner is actually pairing.

A candidate connects, reads 0004, subscribes to 0002 and registers only if menu is
open and nonce is nonzero (and, per section 2.1, only when it holds no
confirmed secret that the meter at its bound address has not refused). A
computer that holds such a secret and sees `"mutual":1` sends N and P once per
nonce instead: `9` (with a valid meter proof) means it is still listed, so it
does not register; `7` from the bound address means the owner removed it, so
it registers on its next connection (within about two seconds, same window). Registration uses these **control** messages (distinct
from all legacy commands):

| Command | Layout |
| --- | --- |
| Begin | `J:u8, session:u32, nonce:u32, total:u16` (11 bytes) |
| Fragment | `j:u8, session:u32, offset:u32, payload` (9-byte prefix) |
| Commit | `K:u8, session:u32` (5 bytes) |
| ACK | `J:u8, result:u8, session:u32, next_offset:u32` (10 bytes) |

A nonzero random session identifies one attempt. With `"auth":1` the assembled
body is exactly `host_id:36 ASCII bytes, name_length:u8, name:name_length ASCII
bytes, secret:32 bytes`, **70–89 bytes**; the secret must not be all zero.
Pre-secret firmware accepts only the 38–57-byte body without the secret, which
companions up to 2026.9.14 send. Pairing firmware still receives such a legacy
body (J with total 38–57) so it can name the computer, validates it, lists it
as an `UPDATE APP` row and answers the commit with result **9**; it is never
paired. Total must match the body; name length is 1–20. At the minimum MTU each fragment
contains at most 11 payload bytes. One write and its J ACK precede the next write;
ACK's next offset is zero after begin, received count after fragment, and total
after successful commit. A duplicate ID updates its row without consuming a slot.
A paired computer that registers again with the **same** secret just updates its
row. With a **different** secret (legitimate after it forgot the meter, or an
impostor that learned its ID) the row is marked `NEW KEY` and the stored secret
is replaced only after the owner's explicit second confirmation (above). If a
second, different secret then claims the same ID in the window the row is
marked as a conflict and cannot be selected. No registration can submit a frame.

J result values: 0 success; 1 malformed length/value/name/ID/secret; 2 menu
closed or nonce changed; 3 wrong session; 4 unexpected offset; 5 list full;
6 busy; 7 too many registrations from this connection address in this window
(two); 8 identity conflict; 9 this companion predates pairing secrets and must
be updated (legacy-length body; the computer is shown as `UPDATE APP`). Nonzero result ends this registration; disconnect,
reread status on a later attempt, and start with a fresh session. Registration
expires after five seconds without a valid packet or when its window closes; no
partial body is persisted. Wait five seconds for a J ACK. The device disconnects
immediately after commit ACK/error. It forcibly releases a discovery connection
after eight seconds from its first status read/registration packet, or 15
seconds from encryption (30 seconds from physical connection while not yet
encrypted; section 1), whichever comes first. Firmware up to 2026.9.18 counted
the 15 seconds from physical connection.

After selection, persist the registry, close the menu, disconnect any candidate
and advertise; only the selected computer's next hello is authorized.

**Companion connection policy.** Scan 3 seconds at a time. When no meter is seen,
back off 5, 10, 20, then 30 seconds between scans; reset on a user action
(`Bluetooth.rescan()`), on forgetting, or when a meter appears. Per meter:
reconnect at once after a session ends; after registering wait 5–9 seconds and
do not register twice for one nonce unless the attempt failed; after `9` (paired
but another computer selected) retry after 8–12 seconds, so switching back on the
meter reconnects within about 15 seconds; after `7` or when another computer is
selected and this one holds no secret, back off 30–45 seconds; when no computer
is selected, 3–5 seconds. A meter showing the `-PAIR` suffix is probed
immediately. Meters for which this computer holds a proven secret are tried
first. Any number of meters can be paired; one is driven at a time.

Connecting makes a meter with pairing firmware bond with the computer, and
that meter keeps only bonds that were earned (section 9), so a probe of an
unrelated one would leave a stale key in the computer's OS. The companion
therefore decides from the advertisement (section 1) before connecting:

- Pairing firmware, menu open: connect and register.
- Pairing firmware, menu closed: connect only if this computer is paired or
  pairing with that meter (a confirmed secret bound to that address, a pending
  registration or a pre-secret pin for that address). Otherwise do not
  connect; a companion that has never paired shows the selection instructions
  from the advertisement.
- Pre-secret firmware (no marker; it never removes bonds): probe as before. If
  its menu is open, register with the legacy body; if it names this computer
  as selected, use the legacy hello; if nothing is selected, show the
  selection instructions. A factory-fresh pre-secret meter can so be selected
  from any computer and then updated; after the update the migration of
  section 2.1 applies (that computer holds the pre-secret record for the
  meter's address).
- Undecided (no scan response yet): do not connect this scan.
- Marker not reported although the status says `"auth":1`: see section 1;
  the status decides and the link is closed at once.

**Operating-system pairing.** The first encrypted read on a link makes a
computer that has no bond with the meter pair (Just Works). macOS first shows
"Connection Request from <name>" and pairs only after Connect; the companion
waits up to 35 seconds for that read (re-reading after a backend's own read
timeout) so the meter's deadline decides, and tells the user after 2 seconds.
If the meter drops a link whose pairing then completes, the meter removes that
unearned bond (section 9) while the computer keeps its keys; every later
connection from that computer then fails (macOS: CBError 14, "Peer removed
pairing information") until the user removes the Sweetmeter device in the
system Bluetooth settings. Companions up to 2026.9.13 probe pairing firmware
and leave such keys whenever the user accepted the prompt. The companion
reports this case with the settings to open, rather than retrying silently.

Status read before this link authenticated is reported to the application as
untrusted: a copied serial proves nothing. A firmware job is bound to the BLE
address of the meter it was checked against, starts only on that meter and is
dropped with `job_expired` if that meter disconnects first or it has not
started within 60 seconds. A Forget during a connection attempt is never undone
by that attempt.

Opening the menu temporarily suspends disconnect sleep for its bounded window.
On cancel/expiry/selection, if no authorized connection exists, a meter whose
disconnect guard was already armed starts a fresh 30-second grace period.
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

Board ID is exactly `elecrow-crowpanel-2.13-v1.2-jd79661`; current releases use
key ID `release-1`. Both fixed strings contain one NUL terminator followed by
**only zero padding**; reject missing termination, embedded extra data, and
non-ASCII. The key ID selects one of the public keys embedded at build time: the
firmware build embeds every `meter/assets/keys/<key-id>.pem` (uncompressed P-256
SPKI, key ID `[a-z0-9][a-z0-9-]{0,14}`, at most eight, no duplicates, sorted by
ID). An unknown ID is SIGNATURE. To rotate, first ship a release that embeds the
backup public key; only later releases may be signed with it. This field never
authorizes an arbitrary public key, and no private key is read by the build.

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

Subscribe to 0007 before writing 0005. OTA requires an authorized link (section 2.1),
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
| 1 | UNAUTHORIZED | Link not authorized for the selected computer |
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
its hello, T and a normal frame and reports any display/connection failure separately.
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

A pairing secret authenticates a computer only as well as the registration
that delivered it: registration happens over a Just Works link while the owner's
menu is open, so an active attacker present at that moment (not a passive
listener) could interpose. The legacy migration path trusts the first hello
from the previously selected host ID within 10 minutes of a boot (section 2.1).
A retried registration in the same window carries a new secret and is shown
as a conflict; the owner rescans (top button) to pair again. The device cannot measure USB power
without additional hardware, and cannot claim complete Windows/Linux hardware
acceptance from Mac BLE tests. These limitations are part of the product
contract.

## 9. Power, clock and bonds

- **Disconnect sleep:** 30 seconds after the authorized computer's link is lost
  (section 4 describes the menu exceptions).
- **Idle power-off:** if no selected computer has been authorized for 30 minutes
  (any key press restarts the period) and neither the menu nor OTA is active,
  the meter shows OFF and deep-sleeps.
- **Top wake:** after the release wait the top button is always armed as the
  EXT0 wake source, so a press in that instant wakes the meter instead of
  leaving it without one.
- **Critical battery (valid MAX17048 only):** at ≤5% or ≤3350 mV; once latched
  it clears only above 7% and 3450 mV. The gauge is read in `setup()` before BLE
  or the panel start. The BATTERY LOW screen is drawn once per discharge (and
  again on a top-button wake); 300-second timer wakes then only measure and
  sleep. A pending-verify OTA image always finishes its health checks first.
- **Clock:** deep sleep keeps time on the internal RC oscillator. The meter stores
  the sleep-entry time in RTC memory and accumulates the time slept since the
  last T there; the clock is trusted only while that total is below five
  minutes (so repeated 300-second low-battery sleeps never keep it). Otherwise
  `clock_synced` is false and the clock shows `--:--` until T arrives, which
  resets the total.
- **Light/modem sleep:** not enabled. The pinned Arduino-ESP32 2.0.17 prebuilt
  ESP-IDF 4.4.7 has `CONFIG_PM_ENABLE` off (no automatic light sleep or tickless
  idle) and `CONFIG_BT_CTRL_SLEEP_MODE_EFF 0` (no controller modem sleep); both
  need a rebuilt SDK and hardware validation. The CPU runs at 80 MHz and the
  worker blocks instead of polling.
- **Persistent-write retry:** a failed pairing-registry NVS write is retried with
  exponential backoff from 1 to 60 seconds, logging once per attempt.
- **Bonds:** `CONFIG_BT_SMP_MAX_BONDS` is 15. In ESP-IDF 4.4.7 Bluedroid keeps
  bonds most-recent-first and, once more than 15 exist, silently deletes the
  least recent ones (`btc_ble_storage.c`, `_btc_storage_save`). Arduino's
  BLEServer requests encryption from every central that connects, so stray
  phones bond too and could push out a paired computer's bond, forcing the
  owner to forget the device in the OS. **A bond persists only for a link that
  earned it.** The meter reads the bond list (`esp_ble_get_bond_device_list`)
  when a link connects; when a link ends without earning its bonds, it removes
  (`esp_ble_remove_bond_device`, before advertising again) only the bonds that
  are in the list now but were not in the snapshot. Bonds are compared as the
  identity addresses Bluedroid stores, so no address resolution is needed. If
  either list cannot be read, nothing is removed; no other bond is ever
  removed, and nothing is removed at boot. A link earns its bonds when it
  proves a pairing secret (hello `0` or `9`, or a migration `Y`), completes a
  menu registration (J `0`, or `9` for an old app that will register again
  after updating), or, for a central whose connection address completed a
  registration (J `0`) in the menu window that closed, when that menu closes
  during the link or closed less than 10 seconds before it began (a computer
  that lost the race with the owner's choice keeps the bond its OS already
  stored). Any other central connected at the close or the timeout, or within
  10 seconds after it, earns nothing from the race. A probe that connects
  while the menu is open and ends without registering loses its bond.
  Bluedroid's LRU remains the backstop; it can still drop a paired computer's
  bond only if more than 15 centrals earned bonds. A hello queued at the moment
  the link drops is not processed, so that link does not earn its bond. The
  former NVS `peers` list is deleted at boot.

