"""Packaged as a separate one-file process, copied outside the app before use."""
import sys
from meter.self_update import apply_update, launch_installed

if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--launch':
        launch_installed(sys.argv[2:])
    elif len(sys.argv) != 2:
        raise SystemExit('Expected one update plan path')
    else:
        apply_update(sys.argv[1])
