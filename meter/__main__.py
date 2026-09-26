"""Sweetmeter desktop entry point; diagnostics are strictly offline."""
import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
from pathlib import Path
from .version import get_version


from .instance_lock import InstanceLock

LOG_BYTES = 1024 * 1024
LOG_BACKUPS = 3
# The platform backend bleak really uses; importing only `bleak` would not
# catch a PyInstaller bundle that is missing it.
BLEAK_BACKENDS = {
    'darwin': ('bleak.backends.corebluetooth.scanner', 'bleak.backends.corebluetooth.client'),
    'win32': ('bleak.backends.winrt.scanner', 'bleak.backends.winrt.client'),
    'linux': ('bleak.backends.bluezdbus.scanner', 'bleak.backends.bluezdbus.client'),
}


def bluetooth_backend_modules(platform=None):
    return BLEAK_BACKENDS.get(platform or sys.platform, BLEAK_BACKENDS['linux'])


def self_test():
    import importlib
    import tkinter
    assert tkinter.Tcl().eval('info patchlevel')
    import bleak  # Import-only; no Bluetooth adapter, account or network access.
    for module in bluetooth_backend_modules():
        importlib.import_module(module)
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


def configure_logging(state):
    """One rotating agent.log; a console copy only for an interactive terminal."""
    handlers = [RotatingFileHandler(state / 'agent.log', maxBytes=LOG_BYTES,
                                    backupCount=LOG_BACKUPS, encoding='utf-8')]
    stream = sys.stderr
    try:
        interactive = stream is not None and stream.isatty()
    except (AttributeError, ValueError, OSError):
        interactive = False
    if interactive:
        handlers.append(logging.StreamHandler(stream))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', handlers=handlers, force=True)
    return handlers


def _plain_failure(action, error):
    """Show a setup problem as one readable line, never a traceback."""
    from .installation import InstallError
    detail = str(error) if isinstance(error, (InstallError, ValueError)) and str(error) else (
        type(error).__name__ + (': ' + str(error) if str(error) else ''))
    message = f'Sweetmeter {action} did not finish: {detail}'
    try:
        from .paths import data_dir
        data_dir().mkdir(parents=True, exist_ok=True)
        (data_dir() / 'install-error.txt').write_text(message + '\n', encoding='utf-8')
    except OSError:
        pass
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    return 2


def _run_install():
    from .installation import install_current
    from .paths import data_dir
    for name in ('install-error.txt', 'install-result.txt'):
        try:
            (data_dir() / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        # A copy that cannot import its own runtime must not install itself
        # (and an installed copy that fails here is replaced by the installer).
        self_test()
    except Exception as error:  # noqa: BLE001 - reported as one plain line
        return _plain_failure('setup', RuntimeError('this Sweetmeter copy failed its self-test ('
                                                    + type(error).__name__ + ')'))
    try:
        # None keeps a recorded "Start at login" choice; new installs start at login.
        install_current(start_at_login=None, report=_report)
    except (OSError, ValueError, RuntimeError) as error:
        return _plain_failure('setup', error)
    return 0


def _report(message):
    """Truthful one-line setup outcome for the installer (stdout and a file,
    since a windowed Windows app has no console)."""
    from .paths import data_dir
    if sys.stdout is not None:
        print(message)
    try:
        data_dir().mkdir(parents=True, exist_ok=True)
        (data_dir() / 'install-result.txt').write_text(message + '\n', encoding='utf-8')
    except OSError:
        pass


def main(argv=None):
    # Everything this process creates (install record, logs, state) is private.
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Sweetmeter Bluetooth quota dashboard')
    parser.add_argument('--version', action='version', version=get_version())
    parser.add_argument('--self-test', action='store_true', help='Offline imports/render/protocol check; no accounts, network or BLE')
    parser.add_argument('--install', action='store_true', help='Install the packaged app for this user and start at login')
    parser.add_argument('--uninstall', action='store_true', help='Remove Sweetmeter, its login startup entry and launcher for this user')
    parser.add_argument('--remove-data', action='store_true', help='With --uninstall: also delete settings, pairing and caches')
    parser.add_argument('--wait-pid', type=int, help=argparse.SUPPRESS)
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
        return _run_install()
    if args.uninstall:
        from .installation import uninstall_main
        forwarded = ['--remove-data'] if args.remove_data else []
        if args.wait_pid:
            forwarded += ['--wait-pid', str(args.wait_pid)]
        return uninstall_main(forwarded)
    from .onboarding import needs_install, welcome
    if needs_install(args):
        welcome()
        return 0
    from .paths import default_state_dir
    from .app import Application
    state = args.state_dir or default_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    try:
        lock = InstanceLock(state / 'meter.lock')
    except OSError:
        if not args.background:
            (state / 'show-window').touch()
            print('Sweetmeter is already running; opening its window.')
        return 0
    pid_file = state / 'meter.pid'
    try:
        pid_file.write_text(str(os.getpid()) + '\n', encoding='ascii')
    except OSError:
        pass
    configure_logging(state)
    app = Application(state, preview_only=args.preview_only)
    gui = None
    try:
        self_test()
        custom_state = args.state_dir is not None and args.state_dir.absolute() != default_state_dir().absolute()
        if not (args.headless or args.once or args.preview_only or custom_state):
            try:
                from .installation import repair_running_installation
                repair_running_installation()
            except (OSError, ValueError, RuntimeError) as error:
                logging.warning('Installation repair skipped: %s', error)
        if not (args.headless or args.once or args.preview_only):
            from .gui import Desktop
            gui = Desktop(app, background=args.background)
        app.start()
        from .self_update import confirm_update_health

        def notify(message):
            app.events.put({'event': 'update_notice', 'message': message})
        # Follow app.radio: a Bluetooth worker restarted meanwhile is a new object.
        confirm_update_health(get_version(), radio=(lambda: app.radio) if app.radio else None,
                              radio_failed=lambda: app.bluetooth_failed, notify=notify,
                              on_confirmed=app.updates.confirm_companion_startup if app.updates else None)
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
                if event['event'] == 'update_offer':
                    offer = event['offer']
                    logging.info('%s update %s available; open Sweetmeter to review and confirm.', offer.kind, offer.target)
            if args.once and time.monotonic()-started > 90:
                logging.error('Timed out waiting for a display acknowledgement')
                return 1
            time.sleep(.1)
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.exception('Sweetmeter stopped because of an unexpected error')
        raise
    finally:
        app.close()
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
