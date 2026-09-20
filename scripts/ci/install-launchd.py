#!/usr/bin/env python3
"""Install only the dedicated user's Sweetmeter broker LaunchAgent."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--config', type=Path, required=True)
args = p.parse_args()
config = args.config.resolve(strict=True)
controller = Path(__file__).with_name('controller.py').resolve(strict=True)
label = 'dev.sweetmeter.ci'
folder = Path.home() / 'Library/LaunchAgents'
folder.mkdir(parents=True, exist_ok=True)
plist = folder / (label + '.plist')
value = {'Label': label, 'ProgramArguments': [sys.executable, str(controller), '--config', str(config)],
         'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 60,
         'WorkingDirectory': str(config.parent),
         'StandardOutPath': str(config.parent / 'controller.log'),
         'StandardErrorPath': str(config.parent / 'controller.log')}
plist.write_bytes(plistlib.dumps(value)); plist.chmod(0o600)
domain = f'gui/{os.getuid()}'
subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
subprocess.run(['launchctl', 'bootstrap', domain, str(plist)], check=True)
subprocess.run(['launchctl', 'enable', domain + '/' + label], check=True)
print('Sweetmeter LaunchAgent installed. Starts at user login and restarts on exit.')
