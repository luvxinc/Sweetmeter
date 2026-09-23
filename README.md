# Sweetmeter

A small Bluetooth e-paper dashboard for Claude Code and Codex subscription
usage. One screen shows Claude 5-hour, Claude weekly, Fable weekly and Codex
weekly quotas, reset countdowns and local Token totals.

面向 Claude Code / Codex 的蓝牙墨水屏额度仪表盘：四项额度同屏，电脑端读取数据，
ESP32 显示；已刷好固件的设备只需在电脑上运行一次安装入口并授权，
确认目标电脑后自动同步。空白开发板首次刷写需要 USB，正常使用通过蓝牙传输。

**[Get started / 一次安装，自动配置](#quick-setup)** ·
[Downloads](https://github.com/luvxinc/Sweetmeter/releases/latest) · [Hardware](docs/HARDWARE.md)

![Synthetic dashboard preview](docs/assets/dashboard.png)

*Illustrative data and subscription labels, rendered at 250 × 122 and enlarged
4×. This image is not a real account snapshot.*

## Hardware

The supported target is **ELECROW CrowPanel ESP32 Display-2.13(E), PCB V1.2,
JD79661**, with ESP32-S3, 8 MB flash, 8 MB PSRAM and a 2.13-inch black/white
e-paper panel. It has physical buttons and a rocker; it is not a touch screen.
Use a USB-C data cable for the first flash. A protected single-cell 3.7 V battery
and MAX17048 fuel gauge are optional.

See [hardware, pinout, battery connector and recovery](docs/HARDWARE.md) before
buying parts or flashing. Other panel sizes and SSD1680 revisions are not
compatible with this firmware image.

## How it works

The companion reads the current user's locally installed Claude Code/Codex
login and usage information, renders the dashboard, and sends a 4000-byte frame
through Bluetooth Low Energy. The device stores no account credentials and
does not access computer files directly. USB provides power, charging and
initial flashing; it is not the normal dashboard data connection.

- The main numbers and bar fill are **used quota percentages**.
- The left column shows the model/service, smaller `5H` or `7D` period, and
  the actual subscription label. Unknown labels remain `--`.
- Tokens sit inside the bar. `K`, `M`, `B` mean thousands, millions and billions.
- `RESET IN: 01d 18h 32m` means one day, eighteen hours and thirty-two whole
  minutes remain. Under one minute shows `<1m`; expired data shows `RESET:
  PENDING`, not a promise that a new quota has already been fetched.
- The date and `HH:mm` clock synchronize with the selected computer. Seconds
  are intentionally omitted to reduce e-paper refresh work.
- The upper-left version is the firmware actually running on the meter.
- `BT` indicates the selected computer is connected. The battery icon shows
  `?` until a working gauge supplies a valid value. `!` marks stale data.
- Data normally refreshes every 60 seconds; the top button requests a refresh.
  Server rate-limit backoff still applies.

**Quota and Tokens are different measurements.** Quota percentages come from
the account's limits. Tokens count this computer's local logs within the quota
window; they are not a published account-wide Token allowance. Web usage,
other computers and unavailable cloud logs are not counted. Local logs are not
yet separated by signed-in account, so multiple accounts/API sessions can mix.
Claude weekly includes Fable tokens; rows must not be added together.

Claude counts input, output, cache creation and cache read. Codex counts input
and output; cached input/reasoning are already included and are not added twice.

## Buttons and power

| Control | Action |
| --- | --- |
| Top button, short press | Refresh; rescan while the computer menu is open |
| Top button, hold 3 seconds | Show OFF, then sleep after release |
| Top button while asleep | Wake the device |
| Bottom button, hold 3 seconds | Open the computer menu (paired computers and new ones nearby) |
| Rocker up/down, then press (menu) | Switch to that computer at once, or pair a new one |
| Rocker press, hold 3 seconds (menu) | Remove the highlighted computer; press the rocker again to confirm, bottom to keep it |
| Rocker press, hold 3 seconds (dashboard) | Check for firmware and install a newer version through the selected computer |
| Bottom button in the menu | Back |

The meter remembers up to eight paired computers; only the one selected in the
menu drives the dashboard. The menu lists paired computers first (`>` marks the
current one, `+` a new computer that has just appeared) with the last four
characters of each computer's ID, so two computers with the same name can be
told apart. Switching never needs pairing again: pick the computer and it
reconnects within about 15 seconds while the previous computer steps back.
When a ninth computer is paired, the least recently used one is forgotten.
A removed computer, or one that forgot the meter in the companion, must pair
again through the menu.

After a selected computer has connected, a continuous 30-second disconnection
puts the device into deep sleep; a reconnect within that grace period cancels
it. If no selected computer connects for 30 minutes (outside the menu and
updates), the meter also turns itself off. Deep sleep disables Bluetooth; press
the top button to wake. After a sleep longer than five minutes the clock shows
`--:--` until the computer reconnects and sets it. With the optional battery
gauge, a critically low battery shows BATTERY LOW once, then the meter only
wakes briefly every five minutes to measure until it is charged. E-paper
retains its last screen without power. Button sleep does not electrically
disconnect every component on the development board.

The panel redraws at most once a minute: a new dashboard is stored immediately
and appears with the next minute's clock update. Button actions, the computer
menu, update progress and messages still appear right away.

## Install the companion

The companion targets **Windows 11 x64**, **macOS 15+ arm64 / Intel**, and
**Ubuntu 22.04+ x64 with a graphical desktop and BlueZ 5.55+**. These are build
and software-compatibility targets; Windows/Linux physical Bluetooth and battery
acceptance have not been performed. Hardware acceptance records are kept in
[docs/releases](docs/releases/); each names the exact firmware image it tested
(the most recent record is [2026.9.3](docs/releases/2026.9.3.md)). A release
without its own record has passed software CI only.
Native builds include Python, Tk, BLE libraries, fonts, the public update key and
third-party notices. Claude Code and Codex themselves must already be installed
and logged in as the same OS user.

### Quick setup

For a **preflashed Sweetmeter**, run the matching command once as your normal
desktop user. No Espressif client, Git checkout, pip commands or separate Python
installation is needed. The installer selects the latest stable native release,
verifies its signed manifest and package hash, installs for your user, registers
login startup and opens Sweetmeter. Running it again over an existing
installation is safe and says what it did: a current, healthy copy (it passes
its own offline self-test) is kept and only its login startup is repaired; an
older, damaged or failing copy is replaced with the verified latest release in
one atomic swap (an interrupted swap is rolled back at the next login). Without
Internet access it checks and repairs the installed copy instead. A "Start at
login" choice you turned off stays off. Later updates remain **Install / Later / Skip**.

**macOS / Ubuntu desktop — paste into Terminal:**

```sh
curl -fsSL https://raw.githubusercontent.com/luvxinc/Sweetmeter/main/install.sh | sh
```

**Windows 11 — paste into PowerShell:**

```powershell
irm https://raw.githubusercontent.com/luvxinc/Sweetmeter/main/install.ps1 | iex
```

These commands execute this repository's [shell installer](install.sh) or
[PowerShell installer](install.ps1). Payloads are authenticated with Sweetmeter's
pinned release key before extraction and execution. The bootstrap script itself
is obtained over HTTPS from this repository; inspect it first if desired.
On Linux the installer asks for your password only when something is missing:
it installs missing tools with apt, dnf, pacman or zypper (otherwise it lists
the packages to install), and enables BlueZ only when a Bluetooth adapter is
present. Sweetmeter needs an X11 display (XWayland on Wayland desktops). Do not
run the entire installer with `sudo` or from WSL.

**首次使用只需：运行上述对应命令 → 允许蓝牙 → 在设备上确认这台电脑。**
电脑端窗口会引导连接：长按设备下方按钮 3 秒，用摇杆选中电脑并按下确认。
系统设置中的蓝牙配对本身不会启动安装程序，也不能替代这次设备绑定。
已登录的 Claude Code / Codex 不需要重新登录；未登录时，请在官方客户端登录。
之后时间同步、每分钟刷新和重新连接自动完成，关闭窗口仍在后台运行。
设备显示 OFF 时按顶部按钮唤醒。

### Download instead of using a command

Download the matching ZIP from [Releases](https://github.com/luvxinc/Sweetmeter/releases)
when a tested release is available. Extract the ZIP **before** running it. On
macOS use Archive Utility or another extractor that preserves framework links.
New native builds from this revision show **Install and continue** when you open
the extracted app, then open the managed copy automatically. Existing published
packages without that welcome screen can use the one-command setup above, or
their own `--install` option:

| System | From the extracted folder |
| --- | --- |
| macOS | `./Sweetmeter.app/Contents/MacOS/Sweetmeter --install` |
| Windows PowerShell | `.\Sweetmeter\Sweetmeter.exe --install` |
| Linux | `./Sweetmeter/Sweetmeter --install` |

The installer registers login startup (a macOS LaunchAgent, a per-user Windows
`Run` entry, or an XDG autostart entry) and starts the managed companion. The
managed locations are `~/Applications/Sweetmeter.app`,
`%LOCALAPPDATA%/Programs/Sweetmeter`, and `~/.local/lib/Sweetmeter`. On macOS an
app you dragged into `/Applications` is used in place instead of being copied a
second time. Running directly from Downloads is possible, but automatic
application replacement requires the managed installation. The quick installer
preserves existing installs; use the confirmed updater for subsequent releases.
Whenever the installed app starts, it quietly restores a missing install record,
launcher or login entry (for example after an interrupted setup).

**Unless a release's notes say otherwise, macOS builds are not Developer ID
signed or notarized, and Windows builds are not Authenticode signed.** OS
warnings must not be misrepresented as verification by Apple/Microsoft. Verify
the source/release you intend to trust; do not disable OS security globally.
Release signatures checked by Sweetmeter are separate from platform app
signing. Builds signed with a stable certificate keep macOS's Bluetooth
permission across updates; ad-hoc builds may ask for it again after an update.

Allow Bluetooth permission when requested. On Linux ensure BlueZ and its D-Bus
service are running and the current desktop user has Bluetooth permission. Log
into the official Claude Code and Codex clients, open Sweetmeter, then long-press
the meter's bottom button for three seconds and select this computer with the rocker. The device only lists companions that register during
that physical selection window. Pairing alone cannot read account data: the
companion must be running.

First firmware installation still uses USB; see [HARDWARE.md](docs/HARDWARE.md).
Existing `QM3.2` firmware requires this USB bootstrap for protocol 4 OTA. Normal
quota traffic and subsequent firmware updates use BLE.

### Source installation

Python **3.11+ with Tk** is required; release builds use Python 3.12. On Debian /
Ubuntu install `python3-venv`, `python3-tk` and `bluez` first. On Windows use native
Python and native Claude/Codex clients; WSL login directories are not bridged.
On macOS a Python distribution that includes working Tk is required.

```sh
git clone https://github.com/luvxinc/Sweetmeter.git
cd Sweetmeter
python3 scripts/install_agent.py
```

On Windows use `py -3 scripts\install_agent.py`. The installer copies the
companion into a private runtime folder, creates its own virtual environment,
installs the pinned `requirements.txt` there (the only dependency install),
runs the offline self-test and registers login startup. Running it again
reuses those dependencies and only runs pip when `requirements.txt` or the
Python interpreter changed. A development `.venv` for running the tests is
separate and not needed to install. `--no-startup` installs without login
startup (and removes an entry an earlier install created); `--remove-startup`
removes Sweetmeter's current startup entry. Both choices are remembered, so a
later repair does not recreate the entry. In the app, the **Start at login**
checkbox turns login startup on or off at any time.
Source installs receive update notices and verified manual packages; the app
never pretends to automatically replace an arbitrary source checkout.

Optional profile paths are `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and
`SWEETMETER_CODEX_PATH` (a literal Codex executable/npm shim path). Existing
`CLAUDE_SECURESTORAGE_CONFIG_DIR` is honored when the installed official CLI
uses it, including an explicitly empty value. Set these before running the
installer so its startup entry preserves the selected profile. Do not place
access tokens in these variables or repository files.

Per-user state is under `~/Library/Application Support/Sweetmeter/state`,
`%LOCALAPPDATA%/Sweetmeter/state`, or `$XDG_DATA_HOME/sweetmeter/state` (default
`~/.local/share/sweetmeter/state`). On macOS an existing prototype identity at
`~/Library/Application Support/QuotaMeter/state/companion.json` keeps that state
location. Installation retires a legacy macOS startup entry only when it points
to that adopted prototype path. Stop other custom helpers before starting the
new one; only one companion should use the device. Installation never imports another
person's source-checkout caches or account credentials.

### Uninstall

You do not need to quit first; the uninstaller stops the running companion. It removes the
login startup entry, the recovery launcher, the installed app and its install
record. Add `--remove-data` to also delete settings, the device pairing identity
and caches (otherwise they are kept for a later reinstall).

| System | Command |
| --- | --- |
| macOS | `~/Applications/Sweetmeter.app/Contents/MacOS/Sweetmeter --uninstall` |
| Windows | Settings > Apps > Installed apps > Sweetmeter > Uninstall, or `& "$env:LOCALAPPDATA\Programs\Sweetmeter\Sweetmeter.exe" --uninstall` |
| Linux | `~/.local/lib/Sweetmeter/Sweetmeter --uninstall` |
| Source install | `python3 scripts/install_agent.py --uninstall` |

On macOS use `/Applications/Sweetmeter.app/...` if that is where the app is.
Only Sweetmeter's own managed locations and files are removed. Every way of
uninstalling (the app's **Uninstall…** button, the commands above and Windows
Settings > Apps) waits for no update to be in progress: while a companion
update is being swapped in, it stops with a plain message instead. The app is
first moved aside in one step and only then deleted, so a failed uninstall
never leaves a half-deleted app behind (the app and its login item are put
back and nothing is deleted). After an uninstall, select another computer on
the meter or leave it asleep.

## Updates and release notes

Sweetmeter checks GitHub Releases, verifies the signed manifest and shows the
release's cumulative change notes before offering **Install / Later / Skip**.
Downloads and installation require an explicit Install choice. A firmware
update also asks the user to confirm USB power; the board cannot measure that
connection. Keep the computer and device running throughout the transfer.

Firmware is written to the inactive slot and checked before reboot. The new
firmware confirms startup health or rolls back. The companion reports success
only after reconnecting to the expected healthy version. If the firmware needs
a newer companion, update the companion first; this does not automatically
approve a later firmware update.

Managed companion updates use a temporary helper outside the application. It
waits for the current app to exit, swaps in the verified native package, and
restores the previous app if the new process does not confirm healthy startup.
If Bluetooth worked before the update, the new version must also reach
Bluetooth (being switched off is fine). While the system's Bluetooth permission
prompt is still open, Sweetmeter waits (about two and a half minutes) and
reminds you to allow it; an unanswered prompt does not undo the update. Only a
denied permission or a Bluetooth service that fails to start keeps the previous
version, and Sweetmeter explains how to allow it before retrying. A version
that was rolled back is not offered again automatically; **Check updates**
still shows it. After a confirmed update the recovery launcher is refreshed
from the new version.
For source or unwritable installs, the UI offers the verified extracted package
for manual installation. Installed login startup uses a separate recovery
launcher and durable swap journal, so an interrupted replacement rolls back on
the next startup after the app exits or the computer restarts. macOS framework
symlinks are accepted only when their complete chain remains within the app; traversal, cycles, dangling links and
file writes through links are rejected.

The [release guide](docs/RELEASING.md) covers signed assets, native build commands,
recovery, signing limitations and the distinction between CI software checks and
physical hardware acceptance. Battery and cross-platform hardware results are
reported as untested until separately exercised.

## Data and compatibility

Claude's OAuth usage endpoint is a client-facing internal API, not a stable
public API commitment. The Fable row uses its independently reported weekly
model limit; missing Fable data stays unknown and is never substituted with
Sonnet. The companion does not rewrite Claude login credentials. If a login
expires, sign in again through the official client.

The Codex weekly window is selected by its seven-day duration from the Codex
quota bucket. Subscription labels come from login/quota metadata, not example
values. API/log format changes may require adapter updates.

Local runtime state includes provider caches, Token indexes, device selection
and diagnostics. Treat it as private. Never commit `state/`, real screenshots,
full-flash backups, authentication files or release signing keys. Only the
deliberately synthetic example under `docs/assets/` is public test data.

## Development and version policy

```sh
python -m unittest discover -s tests -v
python scripts/render_example.py
python scripts/generate_screen_font.py
```

Trusted primary CI runs in an isolated Linux ARM64 VM on the configured Mac mini;
untrusted pull requests and native Windows/macOS/x64 build checks use GitHub
hosted workers. See [CI setup and isolation](docs/CI.md).

These tests and rendering commands use synthetic data. They do not contact real
accounts or the device. The hardware diagnostic scripts under `scripts/` are
separate, explicitly invoked tools and require the actual serial port and state
directory. They may reset or disconnect the device when requested.

Version format is **`YYYY.M.N`**, using the UTC year/month with no leading zero
on month or sequence. The first accepted commit in a month is `.1`; every
following commit adds one. A new month resets the sequence. For example:
`2026.9.1`, `2026.9.2`, then `2026.10.1`.

Every commit needs user-facing change notes. Install the repository hooks with
`python scripts/install_hooks.py`; hooks and CI validate the version/changelog
contract. Local hooks can be bypassed, so protected-branch CI is also required.
The reviewed `2026.9.1` foundation seeds main with the trusted policy. Later
changes go through an integration-branch pull request, where the trusted base
posts `version-policy/trusted` on the proposed head. After that status and all
required tests pass, fast-forward the exact checked head into protected main.
Do not create merge commits or squash merges; outside-reviewer approval is not
a required gate.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the enforced workflow and
[CHANGELOG.md](CHANGELOG.md) for changes. A versioned commit and a published
firmware release are separate: release only after the required checks pass.

## License and acknowledgments

Original Sweetmeter code is under [Apache License 2.0](LICENSE). Third-party
components keep their own licenses. Bundled fonts and the generated device UI
font use SIL OFL 1.1. Arduino/ESP32 components have separate license obligations.

The display initialization and waveform values reference ELECROW's V1.2
example, whose repository-level license was not provided. Its permission scope
remains unresolved; vendor assets are not relabeled Apache-2.0. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for sources and notices,
including CodexBar, ccusage and CodexIsland. Sweetmeter is an independent
project, not an official product of the AI providers or ELECROW.

The header shows the running firmware as `v YYYY.M.N`, followed by `BT` and four signal bars. Bars use the connected peer’s RSSI (4: ≥ −60 dBm, 3: ≥ −70, 2: ≥ −80, 1: weaker). Unknown, stale or disconnected readings show empty bars. RSSI is sampled every 10 seconds and painted with the normal dashboard refresh to avoid extra e-ink updates.
