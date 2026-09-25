import isolation  # noqa: F401  (test sandbox; must be the first import)
import unittest

from meter.naming import default_name, display_name, encode_meter_name
from meter.setup_flow import BLUETOOTH, CONFIRM, DONE, HELP_AFTER, NAME, OPEN, WAKE, SetupFlow, signal_text


class NamingTests(unittest.TestCase):
    def test_accepts_ascii_and_chinese_within_sixteen_bytes(self):
        self.assertEqual(encode_meter_name('  Desk  '), b'Desk')
        self.assertEqual(encode_meter_name('x' * 16), b'x' * 16)
        self.assertEqual(encode_meter_name('书房客厅卧'), '书房客厅卧'.encode())  # 15 bytes
        self.assertEqual(encode_meter_name(''), b'')  # restores the default name
        self.assertEqual(encode_meter_name('   '), b'')
        # Decomposed input is normalized to the composed form before encoding.
        self.assertEqual(encode_meter_name('Café'), 'Café'.encode())

    def test_rejects_what_the_firmware_would_refuse(self):
        for text in ('x' * 17, '书房客厅卧室', 'a\x01b', 'a\x7fb', 'a\x85b', 'Desk-PAIR', 'desk-pair'):
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                encode_meter_name(text)
            self.assertNotIn('Error', str(caught.exception))  # a sentence for the owner

    def test_display_and_default_names(self):
        self.assertEqual(display_name('Sweetmeter-CF24-PAIR'), 'Sweetmeter-CF24')
        self.assertEqual(display_name('书房'), '书房')
        self.assertEqual(display_name(None), '')
        self.assertEqual(default_name('d405927bcf24'), 'Sweetmeter-CF24')
        self.assertEqual(default_name('bad'), '')


def meter(address='m1', *, kind='closed', paired=False, name='Sweetmeter-CF24', rssi=-60):
    return {'address': address, 'name': name, 'rssi': rssi, 'kind': kind, 'paired': paired}


class SetupFlowTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.flow = SetupFlow('Aaron-Mac', clock=lambda: self.now)

    def send(self, *events):
        for event in events:
            self.flow.handle(event)
        return self.flow.view()

    def test_first_pairing_walks_through_every_step(self):
        view = self.flow.view()
        self.assertEqual((view.step, view.heading), (BLUETOOTH, 'Starting Bluetooth…'))
        view = self.send({'event': 'bluetooth_state', 'state': 'ok'}, {'event': 'nearby', 'meters': []})
        self.assertEqual(view.step, WAKE)
        self.assertIn('top button', view.body)
        view = self.send({'event': 'nearby', 'meters': [meter()]})
        self.assertEqual(view.step, OPEN)
        self.assertIn('lower button for 3 seconds', view.body)
        self.assertEqual(view.meters, [('Sweetmeter-CF24', 'Strong', 'Ready to pair')])
        view = self.send({'event': 'nearby', 'meters': [meter(kind='menu')]})
        self.assertEqual((view.step, view.heading), (OPEN, 'Adding this computer…'))
        self.assertEqual(view.meters[0][2], 'Computer list open')
        view = self.send({'event': 'registered', 'name': 'Aaron-Mac'})
        self.assertEqual(view.step, CONFIRM)
        self.assertIn('Aaron-Mac', view.body)
        view = self.send({'event': 'connected', 'device_id': 'm1', 'name': 'Sweetmeter-CF24'},
                         {'event': 'status', 'trusted': True, 'status': {'protocol': 4, 'rename': 1}})
        self.assertEqual(view.step, NAME)
        self.assertTrue(view.naming and view.can_rename)
        self.assertEqual(view.name, 'Sweetmeter-CF24')
        self.assertEqual(view.meters[0], ('Sweetmeter-CF24', '', 'Connected'))
        view = self.send({'event': 'renamed', 'ok': False, 'error': 'The meter could not save the name. Try again.'})
        self.assertEqual(view.step, NAME)
        self.assertIn('could not save', view.alert)
        view = self.send({'event': 'renamed', 'ok': True, 'name': '书房'})
        self.assertEqual((view.step, view.heading), (DONE, 'Sending your dashboard…'))
        view = self.send({'event': 'ack', 'displayed': True})
        self.assertEqual(view.heading, '书房 is ready')

    def test_naming_can_be_skipped_and_explains_old_firmware(self):
        view = self.send({'event': 'connected', 'device_id': 'm1', 'name': 'Sweetmeter-CF24'},
                         {'event': 'status', 'trusted': True, 'status': {'protocol': 4}})
        self.assertTrue(view.naming)
        self.assertFalse(view.can_rename)
        self.assertIn('firmware update', view.body)
        self.flow.skip_name()
        self.assertEqual(self.flow.view().step, DONE)

    def test_untrusted_status_never_enables_renaming(self):
        view = self.send({'event': 'connected', 'device_id': 'm1'},
                         {'event': 'status', 'trusted': False, 'status': {'protocol': 4, 'rename': 1}})
        self.assertFalse(view.can_rename)

    def test_list_closed_without_this_computer_goes_back_a_step(self):
        self.send({'event': 'bluetooth_state', 'state': 'ok'}, {'event': 'nearby', 'meters': [meter()]},
                  {'event': 'registered', 'name': 'Aaron-Mac'})
        view = self.send({'event': 'selection_required', 'name': 'Aaron-Mac', 'reason': 'unpaired'})
        self.assertEqual((view.step, view.heading), (OPEN, 'Open the meter’s computer list'))

    def test_meter_set_to_another_computer(self):
        view = self.send({'event': 'bluetooth_state', 'state': 'ok'},
                         {'event': 'nearby', 'meters': [meter(paired=True)]},
                         {'event': 'selection_required', 'name': 'Aaron-Mac', 'reason': 'other_computer'})
        self.assertEqual(view.step, OPEN)
        self.assertTrue(view.body.startswith('This meter is set to another computer.'))

    def test_paired_meter_reconnects_by_itself(self):
        view = self.send({'event': 'bluetooth_state', 'state': 'ok'},
                         {'event': 'nearby', 'meters': [meter(paired=True)]})
        self.assertEqual((view.step, view.heading), (CONFIRM, 'Reconnecting…'))
        self.assertEqual(view.meters[0][2], 'Paired with this computer')

    def test_bluetooth_problems_block_the_first_step(self):
        view = self.send({'event': 'bluetooth_state', 'state': 'unauthorized'})
        self.assertEqual((view.step, view.heading), (BLUETOOTH, 'Bluetooth needs attention'))
        self.assertIn('not allowed', view.body)

    def test_more_help_when_nothing_is_found_for_a_while(self):
        self.send({'event': 'bluetooth_state', 'state': 'ok'}, {'event': 'nearby', 'meters': []})
        self.assertNotIn('system Bluetooth settings', self.flow.view().body)
        self.now += HELP_AFTER
        self.assertIn('system Bluetooth settings', self.flow.view().body)
        self.flow.restart()
        self.assertNotIn('system Bluetooth settings', self.flow.view().body)

    def test_newer_meter_firmware_offers_an_app_update(self):
        view = self.send({'event': 'meter_newer', 'firmware': '2026.9.30', 'companion': '2026.9.19'})
        self.assertTrue(view.update_hint)
        self.assertIn('2026.9.30', view.alert)
        self.assertIn('2026.9.19', view.alert)

    def test_registration_problems_and_pairing_errors_are_shown_until_progress(self):
        view = self.send({'event': 'bluetooth_state', 'state': 'ok'}, {'event': 'nearby', 'meters': [meter(kind='menu')]},
                         {'event': 'registration_failed', 'result': 5, 'error': 'The meter’s computer list is full.'})
        self.assertIn('list is full', view.alert)
        view = self.send({'event': 'registered', 'name': 'Aaron-Mac'})
        self.assertEqual(view.alert, '')
        view = self.send({'event': 'error', 'code': 'timeout', 'error': 'The meter did not respond in time.'})
        self.assertEqual(view.alert, '')  # transient: retried silently
        view = self.send({'event': 'error', 'code': 'meter_conflict', 'error': 'A device could not prove it.'})
        self.assertIn('could not prove', view.alert)

    def test_the_macos_connection_request_is_explained_before_and_while_it_shows(self):
        flow = SetupFlow('Aaron-Mac', clock=lambda: self.now, platform='darwin')
        for event in ({'event': 'bluetooth_state', 'state': 'ok'}, {'event': 'nearby', 'meters': [meter()]}):
            flow.handle(event)
        self.assertIn('Connection Request', flow.view().body)
        flow.handle({'event': 'nearby', 'meters': [meter(kind='menu', name='Desk')]})
        flow.handle({'event': 'os_pairing_prompt'})
        view = flow.view()
        self.assertIn('Connection Request from Desk', view.alert)
        self.assertIn('click Connect now', view.alert)
        flow.handle({'event': 'os_pairing_done'})
        self.assertEqual(flow.view().alert, '')
        windows = SetupFlow('PC', platform='win32')
        windows.handle({'event': 'bluetooth_state', 'state': 'ok'})
        windows.handle({'event': 'nearby', 'meters': [meter()]})
        self.assertNotIn('Connection Request', windows.view().body)
        windows.handle({'event': 'os_pairing_prompt'})
        self.assertIn('asks to pair', windows.view().alert)

    def test_an_old_os_pairing_offers_the_bluetooth_settings(self):
        view = self.send({'event': 'bluetooth_state', 'state': 'ok'}, {'event': 'nearby', 'meters': [meter(kind='menu')]},
                         {'event': 'error', 'code': 'stale_pairing', 'error': 'macOS kept an old Bluetooth pairing.'})
        self.assertTrue(view.settings_hint)
        self.assertEqual(view.heading, 'Remove the old Bluetooth pairing')  # not "Wake your meter"
        self.assertIn('old Bluetooth pairing', view.body)
        self.assertEqual(view.alert, '')
        view = self.send({'event': 'registered', 'name': 'Aaron-Mac'})
        self.assertFalse(view.settings_hint)  # removing it worked

    def test_a_meter_missed_by_one_scan_stays_listed_for_a_while(self):
        self.send({'event': 'bluetooth_state', 'state': 'ok'}, {'event': 'nearby', 'meters': [meter()]})
        self.now += 5
        view = self.send({'event': 'nearby', 'meters': []})
        self.assertEqual(len(view.meters), 1)
        self.assertEqual(view.step, OPEN)
        self.now += 11
        view = self.send({'event': 'nearby', 'meters': []})
        self.assertEqual(view.meters, [])
        self.assertEqual(view.step, WAKE)

    def test_a_neighbours_meter_does_not_undo_the_confirm_step(self):
        self.send({'event': 'bluetooth_state', 'state': 'ok'},
                  {'event': 'nearby', 'meters': [meter('mine', kind='menu'), meter('theirs')]},
                  {'event': 'registered', 'name': 'Aaron-Mac', 'device_id': 'mine'})
        view = self.send({'event': 'selection_required', 'reason': 'unpaired', 'device_id': 'theirs'})
        self.assertEqual(view.step, CONFIRM)
        view = self.send({'event': 'selection_required', 'reason': 'unpaired', 'device_id': 'mine'})
        self.assertEqual(view.step, OPEN)

    def test_saving_a_name_never_sticks(self):
        self.send({'event': 'connected', 'device_id': 'm1', 'name': 'Sweetmeter-CF24'},
                  {'event': 'status', 'trusted': True, 'status': {'protocol': 4, 'rename': 1}})
        self.flow.begin_rename()
        self.assertTrue(self.flow.view().saving)
        self.send({'event': 'disconnected'})
        self.assertFalse(self.flow.saving)
        self.flow.begin_rename()
        self.flow.restart()
        self.assertFalse(self.flow.saving)

    def test_newer_firmware_does_not_hide_other_problems(self):
        view = self.send({'event': 'bluetooth_state', 'state': 'ok'}, {'event': 'nearby', 'meters': [meter(kind='menu')]},
                         {'event': 'registration_failed', 'result': 5, 'error': 'The list is full.'},
                         {'event': 'meter_newer', 'firmware': '2026.9.30', 'companion': '2026.9.19'})
        self.assertIn('The list is full.', view.alert)
        self.assertIn('2026.9.30', view.alert)

    def test_signal_text(self):
        self.assertEqual([signal_text(v) for v in (-50, -70, -90, None, 0)],
                         ['Strong', 'Good', 'Weak — move closer', '', ''])


if __name__ == '__main__':
    unittest.main()
