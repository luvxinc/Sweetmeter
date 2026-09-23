# Supported hardware

Sweetmeter targets the **ELECROW CrowPanel ESP32 Display-2.13(E), PCB V1.2,
JD79661**. Check the board revision and display controller before flashing.
Other CrowPanel sizes and the older SSD1680 variant are not interchangeable.

Hardware ID: `elecrow-crowpanel-2.13-v1.2-jd79661`.

## Parts

| Part | Requirement |
| --- | --- |
| Display board | ELECROW CrowPanel 2.13-inch black/white e-paper, ESP32-S3, V1.2 JD79661 |
| Flash / PSRAM | 8 MB / 8 MB |
| Display | 122 × 250 physical pixels; Sweetmeter uses 250 × 122 landscape |
| Controls | Two front buttons and a three-action rocker; this model is not a touch panel |
| USB cable | USB-C cable with data support for the first firmware installation |
| Computer | Bluetooth Low Energy adapter and the Sweetmeter companion |
| Optional battery | Protected single-cell 3.7 V nominal Li-ion/LiPo; board socket SH 1.0 mm, 2 pin |
| Optional fuel gauge | MAX17048 I²C module, address `0x36`, with correct battery adapters |

Board specifications, socket and buttons are documented in the
[ELECROW hardware wiki](https://media-cdn.elecrow.com/wiki/CrowPanel_ESP32_E-Paper_HMI_2.13-inch_Display.html).
The exact V1.2 initialization is referenced against the vendor's
[V1.2 example](https://github.com/Elecrow-RD/CrowPanel-ESP32-2.13-E-paper-HMI-Display-with-122-250/tree/adf27048da2482b4ba4aa3514a6cd085ea4cb9b5/example/arduino-v1.2).

The e-paper panel has **no backlight**. It retains its last image without power;
a visible image does not prove the processor is awake. Sweetmeter uses a full
clear-and-draw sequence because partial refresh produced visible ghosting on
the development unit. A driver ACK verifies the refresh sequence, not optical
image quality.

## Pin assignments

| Function | ESP32-S3 GPIO | Notes |
| --- | --- | --- |
| Display SCK / MOSI | 12 / 11 | Software SPI |
| Display reset / DC / CS | 10 / 13 / 14 | JD79661 panel |
| Display BUSY | 9 | Low means busy |
| Display power path | 7 | Low-side panel power control; signal pins float for deep sleep |
| Indicator LED | 19 | Disabled during normal operation |
| Top Menu button | 2 | Active low; deep-sleep wake input |
| Bottom Exit button | 1 | Active low |
| Rocker up / down / press | 6 / 4 / 5 | Computer selection; hold press 3 s for a firmware check |
| Optional gauge SDA / SCL | 40 / 41 | 3.3 V I²C logic, shared ground |

The display mapping and power handling also follow the board schematic linked
from the vendor wiki. The firmware packs each landscape image into the panel's
128 × 250 memory: `frame[x * 16 + y / 8]`, 4000 bytes including padding. Its
V1.2 mirrored scan direction is compensated in the display driver.
The final panel output rotates the visible 250 × 122 image by 180° for the
printed enclosure. This applies to the dashboard, Bluetooth selection, update
and sleep screens together; incoming frame coordinates and checksums stay the
same. The six unused bits in each panel column remain white. Button functions
remain assigned to their existing GPIOs.

## Battery and gauge

Use the board's designated BAT input. A matching-looking two-pin plug is not
enough: **check connector pitch and positive/negative pin orientation** against
the actual board and battery. A 1.25 mm plug does not fit the specified SH
1.0 mm socket. Do not connect series cells to the single-cell input.

The board's 4054A charge circuit and a battery protection PCB do not provide a
fuel-gauge percentage. The published schematic does not provide the firmware
with a usable battery ADC divider or charge-complete signal. Without an
external gauge the top-right battery icon contains `?`; it does not report
zero percent or pretend to know charging status.

For a MAX17048 module, connect its battery measurement path to the same cell,
share ground with the board, and use GPIO40/41 for SDA/SCL. Follow the chosen
module's power requirements and pin labels; for example the
[Adafruit MAX17048 pinout](https://learn.adafruit.com/adafruit-max17048-lipoly-liion-fuel-gauge-and-battery-monitor/pinouts)
describes the module's battery pass-through and logic power connections.
Adapters may be necessary on both battery and board sides. The gauge estimates
charge; it does not replace the charging circuit or cell protection.

Firmware enables gauge-based behavior only after validating the chip version
and a plausible voltage. A valid level at or below 15% reduces quota polling to
five minutes; a valid level at or below 5%, or voltage at or below 3.35 V,
triggers low-battery sleep and a later check. With no valid gauge, no inferred
percentage controls these decisions.

**Battery wiring, charge recovery, low-battery behavior and runtime are not
hardware-validated.** There is no measured 3000 mAh battery-life claim. Deep
sleep is a software state, not a physical disconnect of every board component.

## First installation and recovery

Build only for this board and choose its actual serial port:

```sh
python -m pip install platformio
pio device list
pio run -d firmware
pio run -d firmware -t upload --upload-port <YOUR_PORT>
```

A published release also includes `Sweetmeter-YYYY.M.N-firmware-build.zip`.
This is the exact CI artifact used for acceptance, with matching firmware,
bootloader, partitions, OTA bootstrap bytes, application objects and build
receipt. Extract it, change into `firmware/.pio/build/crowpanel213`, and use
esptool 5 for a first installation on this exact board:

```sh
python -m pip install 'esptool>=5,<6'
python -m esptool --chip esp32s3 --port <YOUR_PORT> write-flash --flash-mode keep --flash-freq keep --flash-size keep 0x0 bootloader.bin 0x8000 partitions.bin 0xe000 boot_app0.bin 0x10000 firmware.bin
```

These offsets come from the pinned Arduino ESP32-S3 build and `default_8MB.csv`.
They leave the NVS range `0x9000–0xdfff` intact; do **not** use `erase-flash`.
The bootstrap resets OTA slot selection to app0, so use it only for initial USB
installation or deliberate recovery after keeping your own backup. Routine
confirmed BLE updates write the inactive application slot and preserve NVS.
Do not use this layout on another display/revision or flash size.

Typical port families are `COM…` on Windows, `/dev/cu.…` on macOS and
`/dev/ttyUSB…` on Linux. Serial is for initial flashing and diagnostics;
dashboard data travels through BLE. Where required, install the USB serial
driver appropriate to the chip on your board and grant the current user access
to the port. BOOT plus RESET can enter download mode if normal flashing fails.

Keep a private backup of **your own** board before replacing its firmware:

```sh
python -m pip install esptool
python -m esptool --chip esp32s3 --port <YOUR_PORT> read-flash 0 0x800000 factory-backup.bin
```

A full backup includes device-specific state. Do not publish it or flash it on
another user's board. Restore only your matching backup, using the esptool
instructions for your installed version. Firmware upgrades should preserve NVS
and the selected companion identity; erasing all flash discards them.

## Validation boundary

The prototype's macOS BLE dashboard, clock, full refresh and disconnect sleep
were exercised on one V1.2 JD79661 unit. The imported implementation is the
baseline, not certification of a new release. Cross-platform pairing, OTA
failure recovery, physical wake/power-cycle acceptance and battery operation
must be recorded separately for the release that was actually tested.

## One-time migration from the macOS prototype

After replacing legacy QM3 firmware by USB, macOS may retain the old three-
characteristic GATT cache. Dashboard reads can work while OTA reports a missing
characteristic. In System Settings → Bluetooth, forget **only the Sweetmeter
device**, then let the companion reconnect and pair again. This preserves the
computer selection stored on the meter. Do not erase NVS or reset the Mac's
entire Bluetooth configuration. Subsequent protocol-4 updates keep the same
six-characteristic service. This migration recovery was exercised on one Mac.
