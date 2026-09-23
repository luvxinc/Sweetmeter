#!/bin/sh
# First install: HTTPS bootstrap, signed manifest, SHA-256, native bundled app.
sweetmeter_install() {
set -eu
umask 077
fail() { printf 'Sweetmeter: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" != 0 ] || fail 'Run as your desktop user, without sudo.'
case "$(uname -s)" in
  Darwin)
    system=macos
    [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 15 ] || fail 'macOS 15 or later is required.'
    arch=$(uname -m)
    # Prefer the native build even when launched from a Rosetta terminal.
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then arch=arm64; fi
    case "$arch" in arm64|x86_64) ;; *) fail 'Unsupported Mac architecture.' ;; esac
    ;;
  Linux)
    system=linux
    arch=$(uname -m)
    [ "$arch" = x86_64 ] || fail 'The Linux release currently requires x86_64.'
    # Tk draws through X11. A Wayland session works only with XWayland,
    # which provides DISPLAY; WAYLAND_DISPLAY alone is not enough.
    [ -n "${DISPLAY:-}" ] || fail 'Sweetmeter needs an X11 display. Run this from a terminal inside your graphical desktop session; on Wayland, enable XWayland.'
    ;;
  *) fail 'Use install.ps1 on Windows. This installer supports macOS and Linux.' ;;
esac

installed=''
if [ "$system" = macos ]; then
  state="$HOME/Library/Application Support/Sweetmeter/state"
  if [ -f "$HOME/Library/Application Support/QuotaMeter/state/companion.json" ]; then
    state="$HOME/Library/Application Support/QuotaMeter/state"
  fi
  # A copy dragged into /Applications is adopted rather than duplicated.
  for candidate in "$HOME/Applications/Sweetmeter.app" /Applications/Sweetmeter.app; do
    if [ -z "$installed" ] && [ -x "$candidate/Contents/MacOS/Sweetmeter" ]; then
      installed="$candidate/Contents/MacOS/Sweetmeter"
    fi
  done
else
  state="${XDG_DATA_HOME:-$HOME/.local/share}/sweetmeter/state"
  if [ -x "$HOME/.local/lib/Sweetmeter/Sweetmeter" ]; then installed="$HOME/.local/lib/Sweetmeter/Sweetmeter"; fi
fi
if [ -n "$installed" ]; then
  mkdir -p "$state"
  touch "$state/show-window"
  printf 'Opening your existing Sweetmeter. Use its updater for new versions.\n'
  # --install on the installed copy re-checks its login startup, then opens it.
  if "$installed" --install >/dev/null 2>&1; then
    return 0
  fi
  printf 'The existing installation needs repair; installing the latest release.\n'
fi

# Add only missing tools; present ones are never reinstalled. A package
# manager is needed only when something is missing.
if [ "$system" = linux ]; then
  missing=''
  for tool in curl openssl unzip python3 bluetoothctl; do
    if ! command -v "$tool" >/dev/null 2>&1; then missing="$missing $tool"; fi
  done
  if [ -n "$missing" ]; then
    manager=''
    for candidate in apt-get dnf pacman zypper; do
      if [ -z "$manager" ] && command -v "$candidate" >/dev/null 2>&1; then manager=$candidate; fi
    done
    packages=''
    for tool in $missing; do
      # Package names are fixed literals, never server-provided input.
      case "$manager:$tool" in
        pacman:python3) package=python ;;
        pacman:bluetoothctl) package='bluez bluez-utils' ;;
        *:bluetoothctl) package=bluez ;;
        *) package=$tool ;;
      esac
      packages="$packages $package"
    done
    [ -n "$manager" ] || fail "Install these packages with your package manager, then run this again:$packages"
    printf 'Installing required system packages:%s (sudo may ask for your password).\n' "$packages"
    # shellcheck disable=SC2086 # Word splitting of the fixed package list is intended.
    case "$manager" in
      apt-get) sudo apt-get update && sudo apt-get install -y $packages ;;
      dnf) sudo dnf install -y $packages ;;
      pacman) sudo pacman -S --needed --noconfirm $packages ;;
      zypper) sudo zypper --non-interactive install $packages ;;
    esac || fail "Could not install:$packages. Install them with your package manager, then run this again."
  fi
  # Start BlueZ only on a computer that has an adapter and the service. An
  # active, enabled service needs no password prompt on later runs.
  if command -v systemctl >/dev/null 2>&1 && ls /sys/class/bluetooth/hci* >/dev/null 2>&1 \
      && systemctl cat bluetooth.service >/dev/null 2>&1; then
    if ! systemctl is-active --quiet bluetooth.service || ! systemctl is-enabled --quiet bluetooth.service; then
      printf 'Enabling the system Bluetooth service (sudo may ask for your password).\n'
      sudo systemctl enable --now bluetooth.service || fail 'Could not start Bluetooth. Run: sudo systemctl enable --now bluetooth'
    fi
  fi
fi

temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
fetch() { curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --retry 2 --connect-timeout 20 --max-time 600 "$1" -o "$2"; }
base=https://github.com/luvxinc/Sweetmeter
printf 'Finding the latest Sweetmeter release…\n'
latest=$(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 60 -o /dev/null -w '%{url_effective}' "$base/releases/latest") || fail 'Could not reach GitHub to find the latest release. Check the Internet connection and try again.'
version=${latest##*/}
printf '%s\n' "$version" | grep -Eq '^[0-9]{4}\.[1-9][0-9]?\.[1-9][0-9]*$' || fail 'Unexpected release version.'
release="$base/releases/download/$version"
fetch "$release/manifest.json" "$temporary/manifest.json" || fail 'Could not download the release manifest.'
fetch "$release/manifest.json.sig" "$temporary/manifest.json.sig" || fail 'Could not download the release signature.'
cat > "$temporary/release.pem" <<'PUBLIC_KEY'
-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE0+b2/kjA0meIfP8wBfk5vVRLx382
PMEfiRqKBmAynh8tlOM996Sio32vEQvftl6LdC/GV8x5/ZDdQSxVMxw/tA==
-----END PUBLIC KEY-----
PUBLIC_KEY
openssl dgst -sha256 -verify "$temporary/release.pem" -signature "$temporary/manifest.json.sig" "$temporary/manifest.json" >/dev/null 2>&1 || fail 'Release signature verification failed. Nothing was installed.'
asset="Sweetmeter-$version-$system-$arch.zip"

# macOS has JXA built in; Linux uses its system Python only to read JSON.
# The installed app contains its own runtime. Neither path needs pip or Xcode.
if [ "$system" = macos ]; then
  /usr/bin/osascript -l JavaScript - "$temporary/manifest.json" "$version" "$system" "$arch" "$asset" > "$temporary/artifact" 2>/dev/null <<'JXA' || fail 'The signed manifest does not list a valid package for this Mac. Nothing was installed.'
ObjC.import('Foundation');
function run(a) {
    const raw = $.NSString.stringWithContentsOfFileEncodingError(a[0], $.NSUTF8StringEncoding, null);
    const m = JSON.parse(ObjC.unwrap(raw));
    if (m.product !== 'Sweetmeter' || m.schema !== 1 || m.version !== a[1] || m.channel !== 'stable') throw Error('Invalid manifest');
    const found = m.artifacts.filter(x => x.kind === 'companion' && x.os === a[2] && x.arch === a[3]);
    if (found.length !== 1) throw Error('Missing or ambiguous platform package');
    const x = found[0];
    if (x.asset !== a[4] || x.version !== a[1] || !/^[0-9a-f]{64}$/.test(x.sha256) || !Number.isSafeInteger(x.size) || x.size < 1 || x.size > 1073741824) throw Error('Invalid artifact');
    return x.sha256 + '\n' + x.size;
}
JXA
else
  python3 - "$temporary/manifest.json" "$version" "$system" "$arch" "$asset" > "$temporary/artifact" <<'PY' || fail 'The signed manifest does not list a valid package for this computer. Nothing was installed.'
import json, re, sys
path, version, system, arch, asset = sys.argv[1:]
try:
    with open(path, encoding='utf-8') as f: m = json.load(f)
    if (m.get('product'), m.get('schema'), m.get('version'), m.get('channel')) != ('Sweetmeter', 1, version, 'stable'):
        raise ValueError('Invalid manifest')
    found = [x for x in m['artifacts'] if (x.get('kind'), x.get('os'), x.get('arch')) == ('companion', system, arch)]
    if len(found) != 1: raise ValueError('Missing or ambiguous platform package')
    x = found[0]
    if (x.get('asset') != asset or x.get('version') != version or not re.fullmatch('[0-9a-f]{64}', x.get('sha256', ''))
            or type(x.get('size')) is not int or not 0 < x['size'] <= 1073741824):
        raise ValueError('Invalid artifact')
except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
    raise SystemExit('Sweetmeter: ' + str(error))
print(x['sha256'])
print(x['size'])
PY
fi
expected_hash=$(sed -n '1p' "$temporary/artifact")
expected_size=$(sed -n '2p' "$temporary/artifact")
printf 'Downloading Sweetmeter %s for %s %s…\n' "$version" "$system" "$arch"
fetch "$release/$asset" "$temporary/package.zip" || fail 'Could not download the Sweetmeter package.'
actual_hash=$(openssl dgst -sha256 "$temporary/package.zip" | awk '{print $NF}')
actual_size=$(wc -c < "$temporary/package.zip" | tr -d ' ')
[ "$actual_hash" = "$expected_hash" ] && [ "$actual_size" = "$expected_size" ] || fail 'Package verification failed. Nothing was installed.'

if [ "$system" = macos ]; then
  /usr/bin/ditto -x -k "$temporary/package.zip" "$temporary/extracted"
  executable="$temporary/extracted/Sweetmeter.app/Contents/MacOS/Sweetmeter"
else
  unzip -q "$temporary/package.zip" -d "$temporary/extracted"
  executable="$temporary/extracted/Sweetmeter/Sweetmeter"
fi
# Signal before startup, including when bootstrapping older supported releases.
mkdir -p "$state"
touch "$state/show-window"
# The app prints a one-line reason on failure, never a traceback.
"$executable" --install || fail 'Setup did not finish (reason above). Correct it and run this command again.'
printf 'Sweetmeter is opening. Allow Bluetooth if asked, then confirm this computer on the meter.\n'
}
# When piped from curl, nothing executes until the complete function arrives.
sweetmeter_install "$@"
