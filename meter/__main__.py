"""Sweetmeter desktop entry point; diagnostics are strictly offline."""
import argparse
import logging
import os
import sys
import time
from pathlib import Path
from .version import get_version


from .instance_lock import InstanceLock


def self_test():
    import tkinter
    assert tkinter.Tcl().eval('info patchlevel')
    import bleak  # Import-only; no Bluetooth adapter, account or network access.
    from .protocol import PROTOCOL, BOARD_ID, trusted_keys
    from .render import render, pack_frame
    from .providers import parse_claude, parse_codex
    assert PROTOCOL == 4 and BOARD_ID
    assert trusted_keys()
    screen = render({'as_of': 1, 'clock_at': 1, 'stale': True,
                     'rows': parse_claude({}) + parse_codex({})})
    assert len(pack_frame(screen)) == 4000
    return True


# Preserve the snapshot helper import used by existing tests and integrations.
from .app import display_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sweetmeter Bluetooth quota dashboard')
    parser.add_argument('--version', action='version', version=get_version())
    parser.add_argument('--self-test', action='store_true', help='Offline imports/render/protocol check; no accounts, network or BLE')
    parser.add_argument('--install', action='store_true', help='Install the packaged app for this user and start at login')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--preview-only', action='store_true')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--background', action='store_true', help='Start with the main window hidden')
    parser.add_argument('--state-dir', type=Path)
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        print('Sweetmeter ' + get_version() + ' offline self-test passed')
        return 0
    if args.install:
        from .installation import install_current
        install_current(start_at_login=True)
        return 0
    from .onboarding import needs_install, welcome
    if needs_install(args):
        welcome()
        return 0
    from .paths import default_state_dir
    from .app import Application
    state = args.state_dir or default_state_dir()
    os.umask(0o077)
    state.mkdir(parents=True, exist_ok=True)
    try:
        lock = InstanceLock(state / 'meter.lock')
    except OSError:
        (state / 'show-window').touch()
        print('Sweetmeter is already running; opening its window.')
        return 0
    handlers = [logging.FileHandler(state / 'agent.log', encoding='utf-8')]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', handlers=handlers)
    app = Application(state, preview_only=args.preview_only)
    gui = None
    try:
        self_test()
        if not (args.headless or args.once or args.preview_only):
            from .gui import Desktop
            gui = Desktop(app, background=args.background)
        app.start()
        from .self_update import confirm_update_health
        confirm_update_health(get_version())
        if app.updates:
            app.updates.confirm_companion_startup()
        if gui:
            gui.run()
            return 0
        started = time.monotonic()
        while True:
            for event in app.pump():
                if args.once and event['event'] == ('snapshot' if args.preview_only else 'ack'):
                    return 0
                if event['event'] == 'exit':
                    logging.error('Worker stopped: %s', event.get('error', event['event']))
                    return 1
                if event['event'] == 'provider_error':
                    logging.warning('%s', event['error'])
                if event['event'] == 'update_offer':
                    offer = event['offer']
                    logging.info('%s update %s available; open Sweetmeter to review and confirm.', offer.kind, offer.target)
            if args.once and time.monotonic()-started > 90:
                logging.error('Timed out waiting for a display acknowledgement')
                return 1
            time.sleep(.1)
    except KeyboardInterrupt:
        return 0
    finally:
        app.close()
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
