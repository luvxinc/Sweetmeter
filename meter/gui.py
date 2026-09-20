"""Small desktop setup/update UI. Every install requires a deliberate click."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox
from .version import get_version

class Desktop:
    def __init__(self, app, *, background=False):
        self.app = app
        self.root = tk.Tk()
        self.root.title('Sweetmeter · ' + get_version())
        self.root.geometry('620x430')
        self.root.minsize(540, 380)
        self.running = True
        self.dialog = None
        self.offer = None
        self.pending_offers = {}
        self.progress_window = None
        self.progress_label = None
        self.progress_bar = None
        self.cancel_button = None
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Sweetmeter', font=('', 22, 'bold')).pack(anchor='w')
        self.connection = tk.StringVar(value='Starting Bluetooth…')
        self.provider_status = tk.StringVar(value='Reading local Claude Code and Codex data…')
        self.update_status = tk.StringVar(value='Checking for signed updates…')
        for variable in (self.connection, self.provider_status, self.update_status):
            ttk.Label(frame, textvariable=variable, wraplength=560).pack(anchor='w', pady=(12, 0))
        ttk.Separator(frame).pack(fill='x', pady=16)
        ttk.Label(frame, text='First connection', font=('', 12, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='1. Sign in to Claude Code and Codex on this computer.\n'
                  '2. Hold the meter’s lower button for 3 seconds.\n'
                  '3. Select this computer with the wheel, then press the wheel.\n'
                  'Keep Sweetmeter running to refresh every minute.', wraplength=560).pack(anchor='w', pady=8)
        buttons = ttk.Frame(frame)
        buttons.pack(side='bottom', fill='x', pady=(12, 0))
        ttk.Button(buttons, text='Refresh quotas', command=lambda: app.provider.force.set()).pack(side='left')
        ttk.Button(buttons, text='Check updates', command=self.check).pack(side='left', padx=8)
        ttk.Button(buttons, text='Quit', command=self.quit).pack(side='right')
        self.root.protocol('WM_DELETE_WINDOW', self.root.withdraw)
        if background:
            self.root.withdraw()

    def check(self):
        if self.app.updates:
            self.app.updates.check()
        self.update_status.set('Checking for signed updates…')

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
                messagebox.showerror('Cannot update', str(error), parent=window)
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
        if self.app.updates and not self.app.updates.busy and self.pending_offers:
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
            self.cancel_button = ttk.Button(panel, text='Cancel update', command=self.app.updates.cancel)
            self.cancel_button.pack(anchor='e')
            self.progress_window.protocol('WM_DELETE_WINDOW', self._hide_progress)
        self.progress_label.configure(text=phase)
        self.progress_bar['value'] = percent
        self.cancel_button.configure(state='normal' if cancellable else 'disabled')

    def _hide_progress(self):
        # Closing progress is not implicit cancellation or confirmation.
        if self.progress_window:
            self.progress_window.withdraw()

    def event(self, event):
        kind = event['event']
        if kind == 'connected':
            self.connection.set('Bluetooth connected. Dashboard refreshes every 60 seconds.')
        elif kind == 'disconnected':
            self.connection.set('Bluetooth disconnected. Reconnecting automatically…')
        elif kind == 'selection_required':
            self.connection.set('On the meter, select this computer: ' + event['name'])
        elif kind == 'registered':
            self.connection.set('Computer listed on meter: ' + event['name'] + '. Select it with the wheel.')
        elif kind == 'status' and event['status'].get('protocol') == 3:
            self.update_status.set('Legacy QM3 firmware: one USB bootstrap is required before wireless updates.')
        elif kind == 'snapshot':
            errors = [name.title()+': '+str(error) for name, error in event['providers'].items() if error]
            self.provider_status.set('\n'.join(errors) or 'Claude Code / Codex data refreshed. Tokens are local log totals.')
        elif kind in ('error', 'provider_error'):
            self.connection.set(event['error'])
        elif kind == 'update_offer':
            self.offer_update(event['offer'])
        elif kind == 'update_checked':
            self.update_status.set('Verified latest release: ' + event['version'])
        elif kind in ('update_notice', 'update_unconfirmed'):
            self.update_status.set(event['message'])
            if self.progress_window:
                self.progress(event['message'], 100, False)
        elif kind == 'ota_progress':
            self.progress(event['phase'], event['percent'], event['cancellable'])
        elif kind in ('update_error', 'ota_error'):
            self.update_status.set(event['error'])
            if self.progress_window:
                self.progress(event['error'], 0, False)
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
        show_signal = self.app.state_dir / 'show-window'
        if show_signal.exists():
            show_signal.unlink(missing_ok=True)
            self.show()
        for event in self.app.pump():
            self.event(event)
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
