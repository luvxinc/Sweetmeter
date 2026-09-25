"""What the "Connect your meter" window shows, derived from companion events.

No Tk here: ``SetupFlow`` consumes the same events as the main window and
``view()`` returns plain text and rows, so every step can be tested.
"""
from __future__ import annotations
import sys
import time
from dataclasses import dataclass, field

STEPS = ('Bluetooth', 'Wake the meter', 'Open its computer list', 'Confirm on the meter', 'Name the meter')
BLUETOOTH, WAKE, OPEN, CONFIRM, NAME = range(len(STEPS))
DONE = len(STEPS)
# Without any meter in range for this long, list the less common causes too.
HELP_AFTER = 30
# A meter one scan missed stays listed this long (scans miss advertisements now and then).
NEARBY_KEEP = 15

_BLUETOOTH = {
    'off': 'Bluetooth is turned off. Turn it on; Sweetmeter continues by itself.',
    'unauthorized': ('Sweetmeter is not allowed to use Bluetooth. Allow it in your system privacy or '
                     'Bluetooth settings, then quit and reopen Sweetmeter.'),
    'error': 'Bluetooth is not responding. Turn Bluetooth off and on again; Sweetmeter keeps trying.',
}
# Errors that say something about pairing; transient radio errors are retried silently.
_SHOWN_ERRORS = ('meter_conflict', 'gatt_changed', 'bluetooth_unavailable', 'bluetooth_stopped', 'stale_pairing')
# Shown while the operating system asks the user to allow the first connection.
_PAIRING_PROMPT = {
    'darwin': ('If macOS shows “Connection Request from {name}”, click Connect now; the window may be behind '
               'other windows. The meter waits only a few seconds.'),
    'other': 'If your computer asks to pair with {name}, accept it now; the meter waits only a few seconds.',
}


def signal_text(rssi):
    if type(rssi) is not int or rssi >= 0:
        return ''
    return 'Strong' if rssi >= -65 else 'Good' if rssi >= -80 else 'Weak — move closer'


@dataclass
class SetupView:
    step: int
    heading: str
    body: str
    meters: list = field(default_factory=list)  # (name, signal, status) rows
    alert: str = ''
    update_hint: bool = False   # offer "Check for updates"
    settings_hint: bool = False  # offer "Open Bluetooth settings"
    naming: bool = False        # show the name field
    can_rename: bool = False
    saving: bool = False        # a name was sent; waiting for the meter
    name: str = ''


class SetupFlow:
    def __init__(self, computer='this computer', *, clock=time.monotonic, platform=None):
        self.computer = computer or 'this computer'
        self.clock = clock
        self.platform = platform or sys.platform
        self.pairing_prompt = False
        self.stale_pairing = False
        self.started = clock()
        self.health = None
        self.meters = {}          # address -> nearby row from the worker
        self.seen_at = {}         # address -> when a scan last reported it
        self.connected = None     # {'device_id', 'name'} while a meter is connected and authorized
        self.displayed = False    # the meter confirmed a dashboard on this link
        self.registered = False   # this computer is listed on an open meter menu
        self.registered_on = None  # ... of this meter
        self.other_computer = False
        self.rename_capable = False
        self.named = False
        self.saving = False
        self.name_error = ''
        self.alert = ''
        self.newer_firmware = None
        self.legacy_usb = False

    def restart(self):
        """The window was opened again: keep what is known, restart the help timer."""
        self.started = self.clock()
        self.alert = self.name_error = ''
        self.saving = False
        if self.connected is None:
            self.named = False

    def skip_name(self):
        self.named = True

    def begin_rename(self):
        self.saving, self.name_error = True, ''

    def handle(self, event):
        kind = event.get('event')
        if kind == 'bluetooth_state':
            self.health = event.get('state')
        elif kind == 'nearby':
            now = self.clock()
            for meter in event.get('meters') or []:
                if isinstance(meter, dict) and meter.get('address'):
                    self.meters[meter['address']], self.seen_at[meter['address']] = meter, now
            for address in [a for a, at in self.seen_at.items() if now - at > NEARBY_KEEP]:
                self.meters.pop(address, None)
                self.seen_at.pop(address, None)
            if self.health is None:
                self.health = 'ok'
        elif kind == 'os_pairing_prompt':
            self.pairing_prompt = True
        elif kind == 'os_pairing_done':
            self.pairing_prompt = False
        elif kind == 'registered':
            self.registered, self.other_computer, self.alert = True, False, ''
            self.registered_on = event.get('device_id')
            self.stale_pairing = False
            self.computer = event.get('name') or self.computer
        elif kind == 'selection_required':
            # Also after the list closed without this computer being chosen. A
            # neighbour's meter nearby says nothing about the one being paired.
            if self.registered and event.get('device_id') not in (None, self.registered_on):
                return
            self.computer = event.get('name') or self.computer
            self.other_computer = event.get('reason') == 'other_computer'
            self.registered = False
        elif kind == 'registration_failed':
            self.registered = False
            self.alert = event.get('error') or ''
        elif kind == 'connected':
            self.connected = {'device_id': event.get('device_id'), 'name': event.get('name') or 'your meter'}
            self.displayed = self.registered = self.other_computer = False
            self.health, self.alert = 'ok', ''
            self.pairing_prompt = self.stale_pairing = False
        elif kind == 'status' and event.get('trusted'):
            status = event.get('status') or {}
            self.rename_capable = status.get('rename') == 1
            self.legacy_usb = status.get('protocol') == 3
        elif kind == 'ack':
            self.displayed = True
        elif kind in ('disconnected', 'forgotten'):
            self.connected, self.displayed, self.saving = None, False, False
            if kind == 'forgotten':
                self.named = False
        elif kind == 'renamed':
            self.saving = False
            if event.get('ok'):
                self.named, self.name_error = True, ''
                if self.connected is not None:
                    self.connected['name'] = event.get('name') or self.connected['name']
            else:
                self.name_error = event.get('error') or 'The meter could not be renamed. Try again.'
        elif kind == 'meter_newer':
            self.newer_firmware = (event.get('firmware'), event.get('companion'))
        elif kind == 'error' and event.get('code') in _SHOWN_ERRORS:
            self.alert = event.get('error') or ''
            self.stale_pairing = event.get('code') == 'stale_pairing'

    def _rows(self):
        rows = []
        connected = self.connected['device_id'] if self.connected else None
        for address, meter in self.meters.items():
            if address == connected:
                continue
            kind = meter.get('kind')
            status = ('Computer list open' if kind == 'menu' else
                      'Paired with this computer' if meter.get('paired') else
                      'Ready to pair' if kind == 'closed' else
                      'Older firmware: pairs, then updates' if kind == 'legacy' else 'Detecting…')
            rows.append((meter.get('name') or 'Sweetmeter', signal_text(meter.get('rssi')), status))
        if self.connected:
            rows.insert(0, (self.connected['name'], '', 'Connected'))
        return rows

    def view(self):
        alerts, update_hint = [self.alert] if self.alert else [], False
        if self.newer_firmware:
            firmware, companion = self.newer_firmware
            alerts.append('This meter runs firmware ' + str(firmware) + ', which is newer than Sweetmeter ' +
                          str(companion) + ' on this computer. Update Sweetmeter here: choose Check for updates. '
                          'If no update is offered yet, it has not been published; the meter keeps its settings.')
            update_hint = True
        if self.legacy_usb:
            alerts.append('This meter has older firmware: connect it by USB once to install the current firmware, '
                          'then updates work wirelessly.')
        alert = '\n\n'.join(alerts)
        view = SetupView(step=BLUETOOTH, heading='', body='', meters=self._rows(), alert=alert,
                         update_hint=update_hint, settings_hint=self.stale_pairing)
        if self.pairing_prompt and not self.connected:
            names = [m.get('name') for m in self.meters.values() if m.get('kind') == 'menu' and m.get('name')]
            text = _PAIRING_PROMPT['darwin' if self.platform == 'darwin' else 'other']
            view.alert = text.format(name=names[0] if names else 'Sweetmeter')
        name = self.connected['name'] if self.connected else ''
        if self.connected and self.named:
            view.step, view.name = DONE, name
            view.heading = name + ' is ready' if self.displayed else 'Sending your dashboard…'
            view.body = ('The meter shows your Claude Code and Codex usage and updates every minute. '
                         'You can close this window; Sweetmeter keeps running in the background. '
                         'If the meter goes to sleep, press its top button.' if self.displayed else
                         'Time and usage are synchronized automatically. Waiting for the meter to show them.')
        elif self.connected:
            view.step, view.naming, view.can_rename, view.name = NAME, True, self.rename_capable, name
            view.saving = self.saving
            view.heading = 'Connected. Name your meter'
            view.body = ('Give the meter a name you recognize, for example “Desk” or “书房”. Every computer '
                         'paired with it sees this name. Up to 16 letters or digits, or up to 5 Chinese characters.'
                         if self.rename_capable else
                         'This meter’s firmware cannot store a name yet. Hold its wheel for 3 seconds to check '
                         'for a firmware update; you can name it afterwards from the main window.')
            if self.saving:
                view.body = 'Saving the name on the meter…'
            if self.name_error:
                view.alert = self.name_error
        elif self.health not in (None, 'ok'):
            view.heading = 'Bluetooth needs attention'
            view.body = _BLUETOOTH.get(self.health, _BLUETOOTH['error'])
        elif self.health is None:
            view.heading, view.body = 'Starting Bluetooth…', 'Allow Bluetooth if your computer asks.'
        elif self.registered:
            view.step = CONFIRM
            view.heading = 'Confirm on the meter'
            view.body = ('The meter now lists ' + self.computer + ' (marked +). Turn the wheel to highlight it, '
                         'then press the wheel. Sweetmeter connects by itself.')
        elif not self.other_computer and any(m.get('paired') and m.get('kind') != 'menu'
                                             for m in self.meters.values()):
            view.step = CONFIRM
            view.heading = 'Reconnecting…'
            view.body = ('This computer is paired with the meter and reconnects by itself. '
                         'If it takes more than a minute, press the meter’s top button.')
        elif any(m.get('kind') == 'menu' for m in self.meters.values()):
            view.step = OPEN
            view.heading = 'Adding this computer…'
            view.body = 'The meter’s computer list is open. Keep it open; this takes a few seconds.'
        elif self.meters:
            view.step = OPEN
            view.heading = 'Open the meter’s computer list'
            view.body = ('Hold the meter’s lower button for 3 seconds, until it shows SELECT COMPUTER. '
                         'Sweetmeter then adds ' + self.computer + ' to that list.')
            if self.platform == 'darwin':
                view.body += ('\n\nThe first time, macOS asks “Connection Request from Sweetmeter…”: click Connect '
                              'right away. Do not choose Cancel or Ignore this device.')
            if self.other_computer:
                view.body = 'This meter is set to another computer. ' + view.body
        else:
            view.step = WAKE
            view.heading = 'Wake your meter'
            view.body = ('Press the meter’s top button and keep it within a few meters of this computer. '
                         'Sweetmeter looks for it continuously.')
            if self.clock() - self.started >= HELP_AFTER:
                view.body += ('\n\nStill nothing? A screen showing OFF or an old image is asleep: press the top '
                              'button once. Charge it or connect USB power if it does not wake. You do not need '
                              'to pair it in the system Bluetooth settings; Sweetmeter connects by itself.')
        if self.stale_pairing and not self.connected and self.health == 'ok':
            # Nothing else can work until the old pairing is gone: make it the step.
            view.heading = 'Remove the old Bluetooth pairing'
            if self.alert:
                view.body = self.alert
            view.alert = '\n\n'.join(a for a in alerts if a != self.alert)
            view.step = max(view.step, OPEN)
        return view
