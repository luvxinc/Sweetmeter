"""Small desktop setup/update UI. Every install requires a deliberate click.

All Tk calls happen on the main thread: workers only put events on queues,
which ``Desktop.poll`` drains from a Tk ``after`` callback.
"""
from __future__ import annotations
import logging
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from .setup_flow import DONE, OPEN, STEPS, WAKE, SetupFlow
from .version import get_version

SETUP_STEPS = ('Allow Bluetooth if asked. Turn on the meter.\n'
               'Hold its lower button for 3 seconds, then select this computer\n'
               'with the wheel and press to confirm.\n'
               'Already signed in to Claude Code / Codex? No extra login is needed.\n'
               'Choose Connect a meter… for step-by-step help.')
# While the setup window waits for a meter or for its computer list to open,
# scan almost continuously instead of backing off.
SETUP_SCAN_EVERY_MS = 2000

# Plain, OS-specific steps for Bluetooth problems.
BLUETOOTH_HELP = {
    'off': {
        'darwin': 'Bluetooth is turned off. Turn it on in Control Center, or in '
                  'System Settings > Bluetooth. Sweetmeter reconnects by itself.',
        'win32': 'Bluetooth is turned off. Open Settings > Bluetooth & devices and turn '
                 'Bluetooth on. Sweetmeter reconnects by itself.',
        'linux': 'Bluetooth is turned off. Turn it on in Settings > Bluetooth (check that the '
                 'Bluetooth adapter is plugged in). Sweetmeter reconnects by itself.',
    },
    'unauthorized': {
        'darwin': 'Sweetmeter is not allowed to use Bluetooth. Open System Settings > '
                  'Privacy & Security > Bluetooth, turn Sweetmeter on, then quit and reopen Sweetmeter.',
        'win32': 'Windows is blocking Bluetooth for apps. Open Settings > Privacy & security > '
                 'Radios, turn on "Let apps control radios", then quit and reopen Sweetmeter.',
        'linux': 'This account is not allowed to use Bluetooth. Make sure the Bluetooth service is '
                 'running and your user may use it (ask your administrator if unsure), then reopen Sweetmeter.',
    },
    'error': {
        'darwin': 'Bluetooth is not responding. Turn Bluetooth off and on again in Control Center. '
                  'Sweetmeter keeps trying.',
        'win32': 'Bluetooth is not responding. Turn Bluetooth off and on again in Settings > '
                 'Bluetooth & devices. Sweetmeter keeps trying.',
        'linux': 'Bluetooth is not responding. Turn Bluetooth off and on again in Settings > '
                 'Bluetooth. Sweetmeter keeps trying.',
    },
}

PROVIDER_TITLES = {'claude': 'Claude Code', 'codex': 'Codex'}
# Python exception class names never reach the window.
_TECHNICAL = re.compile(r'\b[A-Z][A-Za-z]*(?:Error|Exception|Expired|Timeout|Refused)\b|Traceback|\bErrno\b')


def os_key(platform=None):
    platform = platform or sys.platform
    return 'darwin' if platform == 'darwin' else 'win32' if platform == 'win32' else 'linux'


def bluetooth_help(state, platform=None):
    """Plain text for a bluetooth_state value, or '' when all is well."""
    if state == 'ok':
        return ''
    return BLUETOOTH_HELP.get(state, BLUETOOTH_HELP['error'])[os_key(platform)]


def plain(text, fallback='Something went wrong. Sweetmeter will try again.'):
    """Keep user-facing text; replace raw technical detail with a fallback."""
    text = ' '.join(str(text or '').split())
    if not text or _TECHNICAL.search(text):
        return fallback
    return text[:300]


def selection_text(reason, name):
    name = name or 'this computer'
    if reason == 'other_computer':
        return ('Your meter is set to use another computer.',
                'To use it here: hold the meter’s lower button for 3 seconds,\n'
                'choose ' + name + ' with the wheel and press to confirm.')
    return ('Confirm this computer',
            'Hold the meter’s lower button for 3 seconds.\n'
            'Choose ' + name + ' with the wheel and press to confirm.')


def provider_summary(providers):
    """One plain line per provider that needs attention."""
    lines = []
    for name, value in (providers or {}).items():
        if isinstance(value, dict):
            error, code = value.get('error'), value.get('code')
        else:
            error, code = value, None
        if not error:
            continue
        title = PROVIDER_TITLES.get(name, str(name).title())
        if code == 'not_set_up':
            detail = ('not set up on this computer (optional).' if name == 'codex'
                      else 'not signed in. Open Claude Code and sign in.')
            lines.append(title + ': ' + detail)
        else:
            lines.append(title + ': ' + plain(error, 'couldn’t refresh; showing the last values. Retrying automatically.'))
    return '\n'.join(lines)


class Desktop:
    def __init__(self, app, *, background=False):
        self.app = app
        self.root = tk.Tk()
        self.root.title('Sweetmeter · ' + get_version())
        self.root.geometry('620x450')
        self.root.minsize(540, 400)
        self.running = True
        self.dialog = None
        self.offer = None
        self.pending_offers = {}
        self.progress_window = None
        self.progress_label = None
        self.progress_bar = None
        self.cancel_button = None
        self.bluetooth_problem = ''
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Sweetmeter', font=('', 22, 'bold')).pack(anchor='w')
        self.connection = tk.StringVar(value='Starting Bluetooth…')
        self.provider_status = tk.StringVar(value='Reading local Claude Code and Codex data…')
        self.update_status = tk.StringVar(value='Checking for signed updates…')
        for variable in (self.connection, self.provider_status, self.update_status):
            ttk.Label(frame, textvariable=variable, wraplength=560).pack(anchor='w', pady=(12, 0))
        ttk.Separator(frame).pack(fill='x', pady=16)
        self.setup_heading = tk.StringVar(value='Connect your meter')
        self.setup_help = tk.StringVar(value=SETUP_STEPS)
        ttk.Label(frame, textvariable=self.setup_heading, font=('', 12, 'bold')).pack(anchor='w')
        ttk.Label(frame, textvariable=self.setup_help, wraplength=560).pack(anchor='w', pady=8)
        buttons = ttk.Frame(frame)
        buttons.pack(side='bottom', fill='x', pady=(12, 0))
        ttk.Button(buttons, text='Refresh quotas', command=self.refresh).pack(side='left')
        ttk.Button(buttons, text='Check updates', command=self.check).pack(side='left', padx=8)
        self.forget_button = ttk.Button(buttons, text='Forget meter…', command=self.forget)
        self.forget_button.pack(side='left')
        meter_buttons = ttk.Frame(frame)
        meter_buttons.pack(side='bottom', fill='x', pady=(12, 0))
        self.setup_button = ttk.Button(meter_buttons, text='Connect a meter…', command=self.open_setup)
        self.setup_button.pack(side='left')
        self.rename_button = ttk.Button(meter_buttons, text='Rename meter…', command=self.rename)
        self.rename_button.pack(side='left', padx=8)
        if app.preview_only:
            for button in (self.forget_button, self.setup_button, self.rename_button):
                button.configure(state='disabled')
        ttk.Button(buttons, text='Quit', command=self.quit).pack(side='right')
        ttk.Button(buttons, text='Uninstall…', command=self.uninstall).pack(side='right', padx=8)
        from .installation import start_at_login_choice
        try:
            available, enabled = start_at_login_choice()
        except OSError:
            available, enabled = False, True
        self.start_at_login = tk.BooleanVar(value=enabled)
        self.start_at_login_box = ttk.Checkbutton(frame, text='Start at login', variable=self.start_at_login,
                                                  command=self.toggle_start_at_login)
        self.start_at_login_box.pack(side='bottom', anchor='w')
        if not available or app.preview_only:
            self.start_at_login_box.configure(state='disabled')
        self.root.protocol('WM_DELETE_WINDOW', self.root.withdraw)
        # Bluetooth starts after this window exists (see run()).
        self.setup = SetupFlow()
        self.setup_window = None
        self.background = background
        if background:
            self.root.withdraw()

    def _needs_setup(self):
        """No meter has proved a pairing secret with this computer yet: a first
        install, or an app updated from a version that paired without secrets
        (a meter with pairing firmware refuses those until it is selected again)."""
        radio = getattr(self.app, 'radio', None)
        store = getattr(radio, 'store', None)
        return bool(store is not None and not self.app.preview_only and not store.has_secret_pairing())

    def open_setup(self):
        """Show the step-by-step window for connecting (or reconnecting) a meter."""
        if self.app.preview_only:
            return
        if self.setup_window is not None and self.setup_window.alive:
            self.setup_window.show()
            return
        name = getattr(getattr(self.app, 'radio', None), 'name', None)
        if name:
            self.setup.computer = name
        self.setup.restart()
        self.setup_window = SetupWindow(self)
        self._setup_open(True)
        self.search(full=False)

    def _setup_open(self, value):
        radio = getattr(self.app, 'radio', None)
        if radio is not None:
            radio.setup_open = value  # the worker waits longer for OS pairing prompts meanwhile

    def search(self, full=True):
        """Look for meters now: ``full`` also forgets retry timers (the Search again button)."""
        radio = getattr(self.app, 'radio', None)
        if radio is None:
            return
        if full:
            radio.rescan()
        elif hasattr(radio, 'hurry'):
            radio.hurry()

    def rename(self, text=None):
        """Ask for a new name (unless given) and send it to the connected meter."""
        if not self.app.connected:
            messagebox.showinfo('Meter not connected', 'Rename the meter while it is connected to this computer. '
                                'Press its top button to wake it.', parent=self.root)
            return False
        if not self.setup.rename_capable:
            messagebox.showinfo('Firmware update needed', 'This meter’s firmware cannot store a name yet. Hold its '
                                'wheel for 3 seconds to check for a firmware update, then rename it.', parent=self.root)
            return False
        if text is None:
            current = self.setup.connected['name'] if self.setup.connected else ''
            text = simpledialog.askstring(
                'Rename meter', 'Name for this meter: up to 16 letters or digits, or up to 5 Chinese '
                'characters.\nLeave it empty to restore the default name.', initialvalue=current, parent=self.root)
            if text is None:
                return False
        try:
            self.app.rename_meter(text)
        except ValueError as error:
            messagebox.showerror('Cannot use this name', str(error), parent=self.root)
            return False
        except RuntimeError:
            messagebox.showerror('Cannot rename', 'Bluetooth is not running right now. Try again in a minute.',
                                 parent=self.root)
            return False
        self.setup.begin_rename()
        self.connection.set('Renaming the meter…')
        return True

    def refresh(self):
        self.app.provider.force.set()
        self.provider_status.set('Refreshing Claude Code and Codex quotas…')

    def check(self):
        if self.app.updates:
            self.app.updates.check()
        self.update_status.set('Checking for signed updates…')

    def forget(self):
        if self.app.updates and self.app.updates.busy:
            messagebox.showinfo('Update in progress', 'Wait for the update to finish, then try again.',
                                parent=self.root)
            return
        if not messagebox.askyesno(
                'Forget this meter?',
                'Sweetmeter will stop sending your dashboard to this meter.\n\n'
                'To connect a meter again later, hold its lower button for 3 seconds '
                'and select this computer.', icon='warning', parent=self.root):
            return
        try:
            self.app.forget_meter()
        except Exception as error:
            logging.warning('Forget meter failed (%s)', type(error).__name__)
            messagebox.showerror('Cannot forget meter',
                                 'Bluetooth is not running right now. Try again in a minute.',
                                 parent=self.root)
            return
        self.connection.set('Forgetting the meter…')

    def toggle_start_at_login(self):
        """Record the choice and add or remove the login item.

        Changing it can wait for the installer lock (up to 5 s) and runs
        launchctl or registry calls, so it runs on a worker thread; the
        checkbox is disabled meanwhile and the outcome is shown from the Tk
        thread (the worker never touches Tk)."""
        from .installation import set_start_at_login
        if getattr(self, 'start_at_login_job', None) is not None:
            return
        wanted = bool(self.start_at_login.get())
        job = self.start_at_login_job = {'wanted': wanted, 'done': threading.Event(), 'error': None}

        def work():
            try:
                set_start_at_login(wanted)
            except Exception as error:  # noqa: BLE001 - shown to the user, never raised on a worker
                job['error'] = error
            finally:
                job['done'].set()
        self.start_at_login_box.configure(state='disabled')
        threading.Thread(target=work, name='sweetmeter-start-at-login', daemon=True).start()
        self.root.after(50, self._start_at_login_finished)

    def _start_at_login_finished(self):
        from .installation import InstallError
        job = getattr(self, 'start_at_login_job', None)
        if job is None:
            return
        if not job['done'].is_set():
            self.root.after(50, self._start_at_login_finished)
            return
        self.start_at_login_job = None
        self.start_at_login_box.configure(state='normal')
        error = job['error']
        if error is None:
            return
        logging.warning('Start at login could not be changed (%s)', type(error).__name__)
        self.start_at_login.set(not job['wanted'])
        messagebox.showerror('Cannot change login startup',
                             plain(str(error) if isinstance(error, InstallError) else '',
                                   'Sweetmeter could not change its login startup. Try again.'),
                             parent=self.root)

    def uninstall(self):
        if self.app.updates and self.app.updates.busy:
            messagebox.showinfo('Update in progress', 'Wait for the update to finish, then try again.',
                                parent=self.root)
            return
        if not messagebox.askyesno(
                'Uninstall Sweetmeter?',
                'Sweetmeter will quit and be removed from this computer, including its login item.',
                icon='warning', parent=self.root):
            return
        remove_data = messagebox.askyesno(
            'Remove local data too?',
            'Also delete Sweetmeter settings, pairing with your meter and the local usage index?\n\n'
            'Choose No to keep them for a later reinstall.', parent=self.root)
        from .installation import InstallError, request_uninstall
        try:
            request_uninstall(remove_data=remove_data)
        except (InstallError, OSError) as error:
            logging.warning('Uninstall could not start (%s)', type(error).__name__)
            messagebox.showerror('Cannot uninstall', str(error) if isinstance(error, InstallError) else
                                 'The uninstaller could not start. Try again after restarting Sweetmeter.',
                                 parent=self.root)
            return
        self.quit(force=True)

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def offer_update(self, offer):
        key = (offer.kind, offer.target)
        if self.dialog:
            self.pending_offers[key] = offer
            return
        self.show()
        self.offer = offer
        window = self.dialog = tk.Toplevel(self.root)
        window.title('Sweetmeter update')
        window.geometry('590x440')
        container = ttk.Frame(window, padding=18)
        container.pack(fill='both', expand=True)
        ttk.Label(container, text=f'{offer.kind.title()} update available', font=('', 16, 'bold')).pack(anchor='w')
        ttk.Label(container, text=f'{offer.current}  →  {offer.target}').pack(anchor='w', pady=8)
        notes = tk.Text(container, wrap='word', height=12, relief='flat')
        notes.pack(fill='both', expand=True)
        notes.insert('1.0', offer.notes)
        notes.configure(state='disabled')
        usb = tk.BooleanVar(value=False)
        if offer.kind == 'firmware':
            ttk.Checkbutton(container, text='I have connected the meter to USB power.', variable=usb).pack(anchor='w', pady=10)
        if offer.blocked:
            ttk.Label(container, text=offer.blocked, wraplength=530).pack(anchor='w', pady=8)
        row = ttk.Frame(container)
        row.pack(fill='x', pady=(10, 0))
        def later(decision='later'):
            self.app.updates.decide(offer, decision)
            window.destroy()
            self.dialog = None
            self._next_offer()
        def install():
            try:
                self.app.updates.install(offer, usb_power=usb.get())
            except (ValueError, RuntimeError) as error:
                messagebox.showerror('Cannot update', plain(str(error), 'The update could not start. Try again.'),
                                     parent=window)
                return
            window.destroy()
            self.dialog = None
            self.progress('Starting update', 0, True)
        ttk.Button(row, text='Skip this version', command=lambda: later('skip')).pack(side='left')
        ttk.Button(row, text='Later', command=later).pack(side='right')
        install_button = ttk.Button(row, text='Install', command=install)
        install_button.pack(side='right', padx=8)
        def eligibility(*_):
            enabled = not offer.blocked and (offer.kind != 'firmware' or usb.get())
            install_button.configure(state='normal' if enabled else 'disabled')
        usb.trace_add('write', eligibility)
        eligibility()
        window.protocol('WM_DELETE_WINDOW', later)

    def _next_offer(self):
        if self.app.updates and not self.app.updates.busy and self.pending_offers and not self.dialog:
            key = next(iter(self.pending_offers))
            self.offer_update(self.pending_offers.pop(key))

    def progress(self, phase, percent, cancellable):
        if self.progress_window is None:
            self.show()
            self.progress_window = tk.Toplevel(self.root)
            self.progress_window.title('Sweetmeter update progress')
            self.progress_window.geometry('460x170')
            panel = ttk.Frame(self.progress_window, padding=20)
            panel.pack(fill='both', expand=True)
            self.progress_label = ttk.Label(panel, text='', wraplength=410)
            self.progress_label.pack(anchor='w', pady=8)
            self.progress_bar = ttk.Progressbar(panel, maximum=100)
            self.progress_bar.pack(fill='x', pady=8)
            self.cancel_button = ttk.Button(panel, text='Cancel update', command=self._cancel_update)
            self.cancel_button.pack(anchor='e')
            self.progress_window.protocol('WM_DELETE_WINDOW', self._hide_progress)
        else:
            self.progress_window.deiconify()
        self.progress_label.configure(text=phase)
        self.progress_bar['value'] = percent
        self.cancel_button.configure(state='normal' if cancellable else 'disabled')

    def _cancel_update(self):
        if self.app.updates:
            self.app.updates.cancel()

    def _hide_progress(self):
        # Closing progress is not implicit cancellation or confirmation.
        if self.progress_window:
            self.progress_window.withdraw()

    def _interval_text(self):
        app = getattr(self, 'app', None)
        device = getattr(app, 'device', None) or {}
        return '5 minutes' if device.get('interval') == 300 else '60 seconds'

    def _setup_event(self, event):
        setup = getattr(self, 'setup', None)
        if setup is None:
            return
        setup.handle(event)
        window = getattr(self, 'setup_window', None)
        if window is not None and window.alive:
            window.render()

    def event(self, event):
        kind = event['event']
        self._setup_event(event)
        if kind == 'connected':
            self.bluetooth_problem = ''
            name = event.get('name')
            self.connection.set(('Connected to ' + name if name else 'Bluetooth connected') +
                                '. Dashboard refreshes every ' + self._interval_text() + '.')
            self.setup_heading.set('Sending your dashboard…')
            self.setup_help.set('Time and usage are synchronized automatically. Waiting for the meter to confirm its display.')
        elif kind == 'ack':
            self.setup_heading.set('Ready')
            self.setup_help.set('Your meter accepted the dashboard; it updates at the start of the next minute.\n'
                                'You can close this window; Sweetmeter keeps running in the background.\n'
                                'If the meter goes to sleep, press its top button to reconnect.')
        elif kind == 'disconnected':
            self.connection.set('Bluetooth disconnected. Reconnecting automatically…')
            self.setup_heading.set('Waiting for your meter')
            self.setup_help.set('Keep the meter nearby. If it shows OFF, press its top button.\n'
                                'Sweetmeter reconnects automatically; no reinstall is needed.')
        elif kind == 'bluetooth_state':
            text = bluetooth_help(event.get('state'))
            self.bluetooth_problem = text
            if text:
                self.connection.set(text)
                self.setup_heading.set('Bluetooth needs attention')
                self.setup_help.set(text)
            else:
                self.connection.set('Bluetooth is on. Looking for your meter…')
                self.setup_heading.set('Connect your meter')
                self.setup_help.set(SETUP_STEPS)
        elif kind == 'selection_required':
            heading, text = selection_text(event.get('reason'), event.get('name'))
            self.connection.set(heading if event.get('reason') == 'other_computer'
                                else 'On the meter, select this computer: ' + (event.get('name') or 'this computer'))
            self.setup_heading.set(heading)
            self.setup_help.set(text)
        elif kind == 'registered':
            name = event.get('name') or 'this computer'
            self.connection.set('Computer listed on meter: ' + name + '. Select it with the wheel.')
            self.setup_heading.set('Confirm this computer')
            self.setup_help.set('On the meter, choose ' + name + '\n'
                                'and press the wheel. Everything else is automatic.')
        elif kind == 'registration_failed':
            self.connection.set(plain(event.get('error'), 'The meter did not add this computer. Try again.'))
        elif kind == 'renamed':
            if event.get('ok'):
                self.connection.set('Meter renamed to ' + (event.get('name') or 'its default name') + '.')
            else:
                self.connection.set(plain(event.get('error'), 'The meter could not be renamed. Try again.'))
        elif kind == 'meter_newer':
            self.update_status.set('Your meter has firmware ' + str(event.get('firmware')) + ', newer than this app ('
                                   + str(event.get('companion')) + '). Checking for an app update…')
        elif kind == 'forgotten':
            self.connection.set('Meter forgotten. Sweetmeter will not connect to it anymore.')
            self.setup_heading.set('Connect a meter')
            self.setup_help.set(SETUP_STEPS)
        elif kind == 'bluetooth_restarted':
            self.connection.set('Bluetooth restarted. Looking for your meter…')
        elif kind == 'status' and event.get('trusted') and (event.get('status') or {}).get('protocol') == 3:
            self.update_status.set('This meter has older firmware: connect it by USB once to install '
                                   'the current firmware, then updates work wirelessly.')
        elif kind == 'snapshot':
            self.provider_status.set(provider_summary(event.get('providers'))
                                     or 'Claude Code / Codex data refreshed. Tokens are local log totals.')
        elif kind == 'provider_error':
            self.provider_status.set(plain(event.get('error'), 'Couldn’t update usage data. Retrying automatically.'))
        elif kind == 'error':
            self.connection.set(plain(event.get('error'), 'Bluetooth had a problem. Sweetmeter keeps trying.'))
        elif kind == 'exit':
            self.show()
            self.connection.set('Bluetooth stopped and could not be restarted. Quit Sweetmeter and open it again.')
        elif kind == 'update_offer':
            self.offer_update(event['offer'])
        elif kind == 'update_checked':
            self.update_status.set('Verified latest release: ' + event['version'])
        elif kind in ('update_notice', 'update_unconfirmed'):
            message = plain(event.get('message'), 'Update status is not confirmed yet.')
            self.update_status.set(message)
            if self.progress_window:
                self.progress(message, 100, False)
        elif kind == 'ota_progress':
            self.progress(event['phase'], event['percent'], event['cancellable'])
        elif kind in ('update_error', 'ota_error'):
            message = plain(event.get('error'), 'The update didn’t work. Check the Internet connection and try again.')
            self.update_status.set(message)
            if self.progress_window:
                self.progress(message, 0, False)
        elif kind == 'update_success':
            self.update_status.set('Firmware ' + event['version'] + ' installed and device health verified.')
            self.progress(self.update_status.get(), 100, False)
        elif kind == 'firmware_verified':
            self.update_status.set('Firmware ' + event['version'] + ' passed boot checks. Restoring dashboard…')
            self.progress(self.update_status.get(), 100, False)
        elif kind == 'companion_manual':
            self.show()
            messagebox.showinfo('Verified package ready', event['message']+'\n\n'+event['path'], parent=self.root)
        elif kind == 'companion_restart':
            self.quit(force=True)

    def poll(self):
        if not self.running:
            return
        try:
            show_signal = self.app.state_dir / 'show-window'
            if show_signal.exists():
                show_signal.unlink(missing_ok=True)
                self.show()
                if self._needs_setup():
                    self.open_setup()
        except OSError:
            pass
        try:
            events = self.app.pump()
        except Exception:
            logging.exception('Event processing failed')
            events = []
        for event in events:
            if not self.running:
                return
            try:
                self.event(event)
            except Exception:
                # One malformed event must never stop the window from updating.
                logging.exception('Could not show event %s', event.get('event'))
        if self.running:
            self.root.after(100, self.poll)

    def run(self):
        """Called once Bluetooth has started. Until a meter is paired the setup
        window opens even when started in the background (at login or right
        after an automatic app update), because nothing works before that."""
        if self._needs_setup():
            self.open_setup()
        self.poll()
        self.root.mainloop()

    def quit(self, force=False):
        if not force and self.app.updates and self.app.updates.busy:
            if not messagebox.askyesno('Quit Sweetmeter?', 'An update is running. Quitting may leave its result unconfirmed. Quit?', parent=self.root):
                return
        self.running = False
        self.root.destroy()


def open_bluetooth_settings(platform=None):
    """Open the system Bluetooth settings (to remove an old pairing)."""
    import subprocess
    platform = platform or sys.platform
    try:
        if platform == 'darwin':
            subprocess.Popen(['open', 'x-apple.systempreferences:com.apple.BluetoothSettings'])
        elif platform == 'win32':
            import os
            os.startfile('ms-settings:bluetooth')
        else:
            import shutil
            for command in (['gnome-control-center', 'bluetooth'], ['blueman-manager'], ['systemsettings', 'kcm_bluetooth']):
                if shutil.which(command[0]):
                    subprocess.Popen(command)
                    break
    except OSError as error:
        logging.warning('Cannot open Bluetooth settings (%s)', type(error).__name__)


class SetupWindow:
    """Renders ``SetupFlow.view()``: step list, instructions, nearby meters and naming."""
    def __init__(self, desktop):
        self.desktop = desktop
        self.alive = True
        self.last_step = None
        window = self.window = tk.Toplevel(desktop.root)
        window.title('Connect your meter')
        window.geometry('700x520')
        window.minsize(620, 480)
        outer = ttk.Frame(window, padding=18)
        outer.pack(fill='both', expand=True)
        buttons = ttk.Frame(outer)
        buttons.pack(side='bottom', fill='x', pady=(12, 0))
        self.search_button = ttk.Button(buttons, text='Search again', command=desktop.search)
        self.search_button.pack(side='left')
        self.update_button = ttk.Button(buttons, text='Check for updates', command=desktop.check)
        self.settings_button = ttk.Button(buttons, text='Open Bluetooth settings', command=open_bluetooth_settings)
        self.close_button = ttk.Button(buttons, text='Close', command=self.close)
        self.close_button.pack(side='right')
        steps = ttk.Frame(outer, padding=(0, 0, 18, 0))
        steps.pack(side='left', fill='y')
        self.step_labels, self.step_titles = [], STEPS + ('Ready',)
        for title in self.step_titles:
            label = ttk.Label(steps, text='   ' + title)
            label.pack(anchor='w', pady=4)
            self.step_labels.append(label)
        content = ttk.Frame(outer)
        content.pack(side='left', fill='both', expand=True)
        self.heading, self.body, self.alert = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(content, textvariable=self.heading, font=('', 17, 'bold'), wraplength=470).pack(anchor='w')
        ttk.Label(content, textvariable=self.body, wraplength=470, justify='left').pack(anchor='w', pady=(10, 0))
        self.alert_label = ttk.Label(content, textvariable=self.alert, wraplength=470, justify='left',
                                     foreground='#b3261e')
        self.alert_label.pack(anchor='w', pady=(8, 0))
        self.name_row = ttk.Frame(content)
        self.name_value = tk.StringVar()
        self.name_entry = ttk.Entry(self.name_row, textvariable=self.name_value, width=22)
        self.name_entry.pack(side='left')
        self.name_entry.bind('<Return>', lambda _event: self.save_name())
        self.save_button = ttk.Button(self.name_row, text='Save name', command=self.save_name)
        self.save_button.pack(side='left', padx=8)
        self.skip_button = ttk.Button(self.name_row, text='Skip', command=self.skip_name)
        self.skip_button.pack(side='left')
        self.table_title = ttk.Label(content, text='Meters nearby', font=('', 11, 'bold'))
        self.table_title.pack(anchor='w', pady=(16, 4))
        self.table = ttk.Treeview(content, columns=('name', 'signal', 'status'), show='headings', height=4,
                                  selectmode='none')
        for column, title, width in (('name', 'Name', 170), ('signal', 'Signal', 120), ('status', 'Status', 190)):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, anchor='w')
        self.table.pack(fill='x')
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.render()
        self.window.after(1000, self._tick)
        self.show()

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def close(self):
        self.alive = False
        self.desktop._setup_open(False)
        self.window.destroy()

    def _tick(self):
        """Re-render (time-based help) and keep scanning promptly while waiting for a meter."""
        if not self.alive:
            return
        view = self.render()
        waiting = view.step in (WAKE, OPEN)
        if waiting:
            self.desktop.search(full=False)
        self.window.after(SETUP_SCAN_EVERY_MS if waiting else 1000, self._tick)

    def save_name(self):
        if self.desktop.rename(self.name_value.get()):
            self.render()

    def skip_name(self):
        self.desktop.setup.skip_name()
        self.render()

    def render(self):
        view = self.desktop.setup.view()
        for index, label in enumerate(self.step_labels):
            mark = '✓ ' if index < view.step else '▶ ' if index == view.step else '   '
            label.configure(text=mark + self.step_titles[index],
                            font=('', 12, 'bold') if index == view.step else ('', 12))
        self.heading.set(view.heading)
        self.body.set(view.body)
        self.alert.set(view.alert)
        if view.update_hint:
            self.update_button.pack(side='left', padx=8)
        else:
            self.update_button.pack_forget()
        if view.settings_hint:
            self.settings_button.pack(side='left', padx=8)
        else:
            self.settings_button.pack_forget()
        if view.naming:
            if self.last_step != view.step:
                self.name_value.set(view.name)
            self.name_row.pack(anchor='w', pady=(12, 0), before=self.table_title)
            editable = view.can_rename and not view.saving
            self.save_button.configure(state='normal' if editable else 'disabled')
            self.skip_button.configure(text='Skip' if view.can_rename else 'Continue',
                                       state='disabled' if view.saving else 'normal')
            self.name_entry.configure(state='normal' if editable else 'disabled')
        else:
            self.name_row.pack_forget()
        self.close_button.configure(text='Done' if view.step == DONE else 'Close')
        self.table.delete(*self.table.get_children())
        for row in view.meters:
            self.table.insert('', 'end', values=row)
        self.last_step = view.step
        return view
