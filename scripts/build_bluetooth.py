"""Build a native CoreBluetooth helper with its own macOS permission identity."""
import plistlib
import subprocess
from pathlib import Path


def build(source, runtime):
    app = runtime / 'Quota Meter Bluetooth.app'
    contents = app / 'Contents'
    binary = contents / 'MacOS/QuotaMeterBluetooth'
    binary.parent.mkdir(parents=True, exist_ok=True)
    plist = dict(CFBundleIdentifier='com.sweetmeter.bluetooth',
                 CFBundleName='Quota Meter Bluetooth', CFBundleExecutable=binary.name,
                 CFBundlePackageType='APPL', CFBundleVersion='3',
                 CFBundleShortVersionString='0.3.0', LSUIElement=True,
                 NSBluetoothAlwaysUsageDescription='Connect to your CrowPanel quota meter to send its dashboard and read battery status.',
                 NSBluetoothPeripheralUsageDescription='Connect to your CrowPanel quota meter.')
    with (contents / 'Info.plist').open('wb') as handle:
        plistlib.dump(plist, handle)
    subprocess.run(['/usr/bin/swiftc', '-O', '-framework', 'CoreBluetooth',
                    str(source / 'mac/QuotaMeterBluetooth.swift'), '-o', str(binary)], check=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier',
                    'com.sweetmeter.bluetooth', str(app)], check=True)
    return binary


if __name__ == '__main__':
    source = Path(__file__).resolve().parents[1]
    print(build(source, Path.home() / 'Library/Application Support/QuotaMeter'))
