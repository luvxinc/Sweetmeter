"""Small desktop setup/update UI. Every install requires a deliberate click.

All Tk calls happen on the main thread: workers only put events on queues,
which ``Desktop.poll`` drains from a Tk ``after`` callback.
"""
from __future__ import annotations
import logging
import re
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from .version import get_version

SETUP_STEPS = ('Allow Bluetooth if asked. Turn on the meter.\n'
               'Hold its lower button for 3 seconds, then select this computer\n'
               'with the wheel and press to confirm.\n'
               'Already signed in to Claude Code / Codex? No extra login is needed.')

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
        if app.preview_only:
            self.forget_button.configure(state='disabled')
        ttk.Button(buttons, text='Quit', command=self.quit).pack(side='right')
        ttk.Button(buttons, text='Uninstall…', command=self.uninstall).pack(side='right', padx=8)
        self.root.protocol('WM_DELETE_WINDOW', self.root.withdraw)
        if background:
            self.root.withdraw()

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

    def event(self, event):
        kind = event['event']
        if kind == 'connected':
            self.bluetooth_problem = ''
            self.connection.set('Bluetooth connected. Dashboard refreshes every ' + self._interval_text() + '.')
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
        self.poll()
        self.root.mainloop()

    def quit(self, force=False):
        if not force and self.app.updates and self.app.updates.busy:
            if not messagebox.askyesno('Quit Sweetmeter?', 'An update is running. Quitting may leave its result unconfirmed. Quit?', parent=self.root):
                return
        self.running = False
        self.root.destroy()
