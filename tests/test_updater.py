import isolation  # noqa: F401  (test sandbox; must be the first import)
import copy
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from meter.updater import UpdateService, Downloader, RateLimited, retry_deadline, RELEASE_API
from meter.updater import NoPublishedRelease
from meter.protocol import BOARD_ID
from test_protocol import sample_manifest, sample_image, sample_metadata, sign_raw, TEST_TRUST, TEST_KEY
from scripts.sign_release import sign_envelope

class Feed:
    def __init__(self):
        self.manifest = sample_manifest()
        self.envelope = sign_envelope(sample_metadata(), TEST_KEY)
        firmware = self.manifest['artifacts'][0]
        firmware['metadata_size'] = len(self.envelope)
        firmware['metadata_sha256'] = hashlib.sha256(self.envelope).hexdigest()
        self.calls = []
        self.downloads = []
        self.cached = False
    def fetch(self, url, maximum, **kwargs):
        self.calls.append(url)
        raw = json.dumps(self.manifest).encode()
        base = 'https://github.com/luvxinc/Sweetmeter/releases/download/2026.9.2/'
        if url == RELEASE_API:
            if self.cached: return None, {'ETag': 'test'}
            return json.dumps({'assets': [{'name': name, 'browser_download_url': base+name}
                                         for name in ('manifest.json','manifest.json.sig')]}).encode(), {'ETag':'test'}
        if url.endswith('manifest.json'): return raw, {}
        if url.endswith('manifest.json.sig'): return sign_raw(raw), {}
        raise AssertionError('Unexpected download')
    def artifact(self, artifact, directory, cancel=None):
        self.downloads.append(artifact['asset'])
        path = Path(directory)/artifact['asset']
        path.write_bytes(self.envelope if path.suffix == '.ota' else sample_image())
        return path

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.state=Path(self.temp.name)
        home=self.state/'home'
        sandbox=patch.dict(os.environ,HOME=str(home),USERPROFILE=str(home),XDG_DATA_HOME=str(home/'data'),
                           LOCALAPPDATA=str(home/'local'),APPDATA=str(home/'roaming'))
        sandbox.start(); self.addCleanup(sandbox.stop)
        self.events=[]; self.feed=Feed(); self.radio=Mock()
        self.service=UpdateService(self.state,self.radio,self.events.append,downloader=self.feed,
                                   version='2026.9.1',trusted_keys=TEST_TRUST)
        self.service.set_device({'protocol':4,'board':BOARD_ID,'firmware':'2026.9.1',
                                 'boot_health':'valid','last_update':'none'},True,'device-one')
    def tearDown(self): self.temp.cleanup()
    def check(self):
        with patch('meter.updater.platform_id',return_value=('macos','arm64')):
            self.service.check_now()
    def test_check_downloads_only_signed_metadata_not_firmware(self):
        self.check()
        self.assertEqual(len(self.feed.calls),3)
        self.assertFalse(self.feed.downloads)
        self.radio.install_firmware.assert_not_called()
        self.assertIn('Show signed firmware',self.service.offers['firmware'].notes)
        self.assertNotIn('Show quota dashboard',self.service.offers['firmware'].notes)
    def test_bad_signature_has_no_offer(self):
        with patch('meter.updater.verify_manifest',side_effect=ValueError('bad signature')): self.check()
        self.assertFalse(self.service.offers)
        self.assertFalse(self.feed.downloads)
    def test_new_repository_without_release_is_normal_status(self):
        with patch.object(self.feed, 'fetch', side_effect=NoPublishedRelease):
            self.check()
        self.assertEqual(self.events[-1], {'event':'update_notice', 'message':'No published updates yet.'})
        self.assertFalse(self.service.offers)
    def test_304_reverifies_cached_original_bytes(self):
        self.check(); self.feed.cached=True; self.check()
        self.assertTrue(self.service.offers)
        self.service.state['signature']='ZmFrZQ=='
        self.events.clear(); self.check()
        self.assertTrue(any(e['event']=='update_error' for e in self.events))
    def test_skip_persists_and_automatic_startup_respects_it(self):
        self.check(); offer=self.service.offers['firmware']
        self.service.decide(offer,'skip')
        service=UpdateService(self.state,self.radio,self.events.append,downloader=self.feed,
                              version='2026.9.1',trusted_keys=TEST_TRUST)
        service.set_device(self.service.device,True,'device-one')
        self.events.clear()
        with patch('meter.updater.platform_id',return_value=('macos','arm64')): service.check_now(manual=False)
        self.assertFalse(any(e.get('offer') and e['offer'].kind=='firmware' for e in self.events))
        self.assertFalse(self.feed.downloads)
    def test_later_reappears_when_deadline_passes_in_same_process(self):
        self.check(); offer=self.service.offers['firmware']; self.service.decide(offer,'later')
        self.events.clear(); self.service._offers()
        self.assertFalse(self.events)
        self.service.state['choices']['firmware']['later']=time.time()-1
        self.service._offers()
        self.assertEqual(self.events[-1]['offer'].kind,'firmware')
    def test_unmet_minimum_blocks_firmware_before_any_download(self):
        self.feed.manifest['artifacts'][0]['minimum_companion']='2026.9.2'
        self.check()
        self.assertIn('companion',self.service.offers)
        with self.assertRaises(RuntimeError): self.service.install(self.service.offers['firmware'],usb_power=True)
        self.assertFalse(self.feed.downloads)
    def test_usb_ack_and_connected_device_required(self):
        self.check(); offer=self.service.offers['firmware']
        with self.assertRaises(ValueError): self.service.install(offer)
        self.service.connected=False
        with self.assertRaises(RuntimeError): self.service.install(offer,usb_power=True)
        self.assertFalse(self.feed.downloads)
    def test_explicit_install_verifies_then_queues_ota(self):
        self.check(); offer=self.service.offers['firmware']
        class InlineThread:
            def __init__(self,target,args,**kwargs): self.target,self.args=target,args
            def start(self): self.target(*self.args)
        with patch('meter.updater.threading.Thread',InlineThread): self.service.install(offer,usb_power=True)
        self.radio.install_firmware.assert_called_once()
        self.assertEqual(self.service.state['pending']['device_id'],'device-one')
        self.assertEqual(len(self.feed.downloads),2)
    def device_update(self):
        class InlineThread:
            def __init__(self,target,args,**kwargs): self.target,self.args=target,args
            def start(self): self.target(*self.args)
        with patch('meter.updater.platform_id',return_value=('macos','arm64')), \
             patch('meter.updater.threading.Thread',InlineThread):
            self.service.device_update()
    def test_rocker_hold_installs_newer_firmware_without_dialog(self):
        self.device_update()
        self.radio.update_notice.assert_called_once_with(3)
        self.assertTrue(self.radio.install_firmware.call_args.kwargs['usb_power'])
        self.assertFalse(any(e['event']=='update_offer' and e['offer'].kind=='firmware' for e in self.events))
        self.events.clear(); self.service.busy=False; self.service._offers()
        self.assertFalse(any(e['event']=='update_offer' and e['offer'].kind=='firmware' for e in self.events))
    def test_rocker_hold_reports_current_firmware(self):
        self.service.set_device({**self.service.device,'firmware':'2026.9.2'},True,'device-one')
        self.device_update()
        self.radio.update_notice.assert_called_once_with(2)
        self.radio.install_firmware.assert_not_called()
    def test_rocker_hold_reports_required_companion_update(self):
        self.feed.manifest['artifacts'][0]['minimum_companion']='2026.9.2'
        self.device_update()
        self.radio.update_notice.assert_called_once_with(5)
        self.assertFalse(self.feed.downloads)
    def test_rocker_hold_reports_failed_check(self):
        with patch('meter.updater.verify_manifest',side_effect=ValueError('bad signature')): self.device_update()
        self.radio.update_notice.assert_called_once_with(4)
        self.radio.install_firmware.assert_not_called()
    def test_reboot_requires_version_health_result_and_same_device(self):
        self.service.state['pending']={'kind':'firmware','target':'2026.9.2','device_id':'device-one'}
        self.events.clear()
        status={'protocol':4,'firmware':'2026.9.2','boot_health':'valid','last_update':'success'}
        self.service.set_device(status,False,'another-device')
        self.assertIn('pending',self.service.state)
        self.service.set_device({**status,'boot_health':'pending'},False,'device-one')
        self.assertIn('pending',self.service.state)
        self.service.set_device(status,False,'device-one')
        self.assertNotIn('pending',self.service.state)
        self.assertTrue(any(e['event']=='firmware_verified' for e in self.events))
        self.assertFalse(any(e['event']=='update_success' for e in self.events))
    def test_lost_final_ack_keeps_pending_until_later_reconciliation(self):
        self.service.state['pending']={'kind':'firmware','target':'2026.9.2','device_id':'device-one'}
        self.service.transfer_event({'event':'ota_unconfirmed'})
        self.service.awaiting_until=time.monotonic()-1; self.service.tick()
        self.assertIn('pending',self.service.state)
        self.assertFalse(self.service.busy)
        self.radio.install_firmware.assert_not_called()
    def install_from_dialog(self):
        self.check(); offer=self.service.offers['firmware']
        class InlineThread:
            def __init__(self,target,args,**kwargs): self.target,self.args=target,args
            def start(self): self.target(*self.args)
        with patch('meter.updater.threading.Thread',InlineThread): self.service.install(offer,usb_power=True)
    def notices(self):
        return [c.args[0] for c in self.radio.update_notice.call_args_list]
    def test_firmware_job_names_the_meter_it_was_offered_for(self):
        self.install_from_dialog()
        self.assertEqual(self.radio.install_firmware.call_args.kwargs['device_id'],'device-one')
    def test_expired_job_clears_busy_and_pending_and_tells_the_meter(self):
        self.device_update()
        self.assertTrue(self.service.busy)
        self.assertEqual(self.service.state['pending']['phase'],'transfer')
        self.service.transfer_event({'event':'ota_error','code':'job_expired',
                                     'error':'The meter disconnected before the update started.'})
        self.assertFalse(self.service.busy)
        self.assertNotIn('pending',self.service.state)
        self.assertNotIn('pending',json.loads((self.state/'updates.json').read_text()))
        self.assertEqual(self.notices(),[3,4])  # Installing, then failed, on the meter's screen.
        # Nothing is left blocking a new attempt.
        self.service.transfer_event({'event':'ota_error','code':'job_expired','error':'again'})
        self.assertEqual(self.notices(),[3,4])
    def test_dialog_install_failure_is_not_shown_on_the_meter(self):
        self.install_from_dialog()
        self.service.transfer_event({'event':'ota_error','error':'Transfer failed'})
        self.assertFalse(self.service.busy)
        self.assertNotIn('pending',self.service.state)
        self.radio.update_notice.assert_not_called()
    def test_error_after_commit_keeps_target_for_reconciliation(self):
        self.device_update()
        self.service.transfer_event({'event':'ota_progress','cancellable':False})
        self.service.transfer_event({'event':'ota_error','error':'Connection lost'})
        self.assertFalse(self.service.busy)
        self.assertEqual(self.service.state['pending']['phase'],'commit')
        # The image may be fine: the meter is told nothing until its status reconciles.
        self.assertEqual(self.notices(),[3])
    def test_commit_error_then_verified_boot_never_shows_a_failure(self):
        self.device_update()
        self.service.transfer_event({'event':'ota_progress','cancellable':False})
        self.service.transfer_event({'event':'ota_error','error':'Connection lost'})
        self.service.set_device({'protocol':4,'board':BOARD_ID,'firmware':'2026.9.2','boot_health':'valid',
                                 'last_update':'success'},True,'device-one')
        self.assertEqual(self.notices(),[3])
        self.assertTrue(any(e['event']=='firmware_verified' for e in self.events))
    def test_commit_error_then_reported_rollback_shows_the_failure(self):
        self.device_update()
        self.service.transfer_event({'event':'ota_progress','cancellable':False})
        self.service.transfer_event({'event':'ota_error','error':'Connection lost'})
        self.service.set_device({'protocol':4,'board':BOARD_ID,'firmware':'2026.9.1','boot_health':'valid',
                                 'last_update':'rollback','ota_target':'2026.9.2'},True,'device-one')
        self.assertEqual(self.notices(),[3,4])
    def test_unconfirmed_rocker_install_shows_failure_on_the_meter(self):
        self.device_update()
        self.service.transfer_event({'event':'ota_unconfirmed'})
        self.assertEqual(self.notices(),[3])  # It may still boot fine: wait first.
        self.service.awaiting_until=time.monotonic()-1; self.service.tick()
        self.assertFalse(self.service.busy)
        self.assertEqual(self.notices(),[3,4])
        self.assertIn('pending',self.service.state)
    def test_device_reported_rollback_after_rocker_install_is_shown(self):
        self.device_update()
        self.service.set_device({'protocol':4,'board':BOARD_ID,'firmware':'2026.9.1','boot_health':'valid',
                                 'last_update':'rollback','ota_target':'2026.9.2'},True,'device-one')
        self.assertEqual(self.notices(),[3,4])
        self.assertEqual(self.service.failed_targets()[-1]['outcome'],'rollback')
    def test_verified_rocker_install_is_not_reported_as_failed(self):
        self.device_update()
        self.service.set_device({'protocol':4,'board':BOARD_ID,'firmware':'2026.9.2','boot_health':'valid',
                                 'last_update':'success'},True,'device-one')
        self.service.transfer_event({'event':'ota_error','error':'late stray error'})
        self.assertEqual(self.notices(),[3])
    def test_stray_ota_error_never_clears_a_companion_update(self):
        self.check()
        self.service.busy=True; self.service.installing='companion'
        self.service.state['pending']={'kind':'companion','target':'2026.9.2'}
        self.service.transfer_event({'event':'ota_error','code':'job_expired','error':'x'})
        self.assertTrue(self.service.busy)
        self.assertEqual(self.service.state['pending']['kind'],'companion')
    def test_rolled_back_companion_version_is_not_offered_again_automatically(self):
        self.service.state['failed']={'kind':'companion','target':'2026.9.2','outcome':'rollback'}
        self.check()
        self.assertIn('companion',self.service.offers)
        self.assertFalse(any(e['event']=='update_offer' and e['offer'].kind=='companion' for e in self.events))
        with patch('meter.updater.platform_id',return_value=('macos','arm64')):
            self.service.check_now(manual=True)  # "Check updates" still shows it.
        self.assertTrue(any(e['event']=='update_offer' and e['offer'].kind=='companion' for e in self.events))
    def test_legacy_firmware_never_has_ota_offer(self):
        self.service.set_device({'protocol':3,'firmware':'QM3.2'},True,'device-one')
        self.check()
        self.assertNotIn('firmware',self.service.offers)
    def test_rate_limit_manual_checks_do_not_bypass_backoff(self):
        self.service.state['retry_at']=time.time()+60
        self.service.check_now(manual=True)
        self.assertFalse(self.feed.calls)
    def test_repeated_rocker_holds_reuse_a_fresh_manifest(self):
        self.service.set_device({**self.service.device,'firmware':'2026.9.2'},True,'device-one')
        for _ in range(3): self.device_update()
        self.assertEqual(len(self.feed.calls),3)  # One GitHub check: API + manifest + signature.
        self.assertEqual([c.args[0] for c in self.radio.update_notice.call_args_list],[2,2,2])
        self.service.checked_monotonic-=61
        self.device_update()
        self.assertEqual(len(self.feed.calls),6)
    def test_rocker_hold_respects_skipped_companion(self):
        self.check(); self.service.decide(self.service.offers['companion'],'skip')
        self.service.checked_monotonic=None; self.events.clear()
        self.device_update()
        self.radio.install_firmware.assert_called_once()
        self.assertFalse(any(e['event']=='update_offer' for e in self.events))
    def test_rocker_hold_respects_companion_later(self):
        self.check(); self.service.decide(self.service.offers['companion'],'later')
        self.events.clear(); self.device_update()
        self.assertFalse(any(e['event']=='update_offer' for e in self.events))
    def test_windows_arm64_falls_back_to_x64_package(self):
        self.feed.manifest['artifacts'][1].update(os='windows',arch='x86_64',asset='Sweetmeter-windows-x86_64.zip',
            url='https://github.com/luvxinc/Sweetmeter/releases/download/2026.9.2/Sweetmeter-windows-x86_64.zip')
        with patch('meter.updater.platform_id',return_value=('windows','arm64')): self.service.check_now()
        self.assertEqual(self.service.offers['companion'].artifact['arch'],'x86_64')
        with patch('meter.updater.platform_id',return_value=('linux','arm64')): self.service.check_now()
        self.assertNotIn('companion',self.service.offers)
    def test_cleanup_removes_finished_downloads_but_not_in_use(self):
        old=self.state/'downloads/update-old'; old.mkdir(parents=True)
        staging=self.state/'updates/companion-old'; staging.mkdir(parents=True)
        manual=self.state/'updates/companion-manual'; manual.mkdir(parents=True)
        self.service.state['manual_staging']=str(manual)
        with patch('meter.self_update.data_dir',return_value=self.state):
            self.service.busy=True; self.service.cleanup()
            self.assertTrue(old.exists())
            self.service.busy=False; self.service.cleanup()
        self.assertFalse(old.exists()); self.assertFalse(staging.exists()); self.assertTrue(manual.exists())
    def test_several_failed_companion_targets_are_remembered(self):
        self.service.state['failed']={'kind':'firmware','target':'2026.9.5','outcome':'failed'}  # Older format.
        for target in ('2026.9.2','2026.9.3'):
            self.service.state['pending']={'kind':'companion','target':target}
            (self.state/'companion-update-result.json').write_text(json.dumps(
                {'status':'rollback','version':target,'reason':'synthetic'}))
            self.service.report_companion_result()
        targets=[(r['kind'],r['target']) for r in self.service.failed_targets()]
        self.assertEqual(targets,[('firmware','2026.9.5'),('companion','2026.9.2'),('companion','2026.9.3')])
        self.check()  # 2026.9.2 failed earlier, before 2026.9.3: still never offered automatically.
        self.assertFalse(any(e['event']=='update_offer' and e['offer'].kind=='companion' for e in self.events))
        from meter.updater import FAILED_KEEP
        for number in range(FAILED_KEEP + 3):
            self.service._remember_failed({'kind':'companion','target':'2027.1.%d' % number})
        self.assertEqual(len(self.service.failed_targets()),FAILED_KEEP)
    def test_interrupted_installer_is_not_reported_as_a_failed_update(self):
        (self.state/'companion-update-result.json').write_text(json.dumps(
            {'status':'install_interrupted','version':'2026.9.2','reason':'x'}))
        self.service.report_companion_result()
        self.assertEqual(self.events[-1]['event'],'update_notice')
        self.assertIn('installation was undone',self.events[-1]['message'])
        self.assertEqual(self.service.failed_targets(),[])
        # A rollback with no update started here is not blamed on a version either.
        (self.state/'companion-update-result.json').write_text(json.dumps(
            {'status':'rollback','version':'2026.9.2','reason':'x'}))
        self.service.report_companion_result()
        self.assertEqual(self.events[-1]['event'],'update_notice')
        self.assertEqual(self.service.failed_targets(),[])
    def test_rollback_result_is_shown_once_and_clears_pending(self):
        self.service.state['pending']={'kind':'companion','target':'2026.9.2'}
        (self.state/'companion-update-result.json').write_text(json.dumps(
            {'status':'rollback','version':'2026.9.2','reason':'Bluetooth permission is not available'}))
        self.service.report_companion_result()
        self.assertIn('Bluetooth permission',self.events[-1]['error'])
        self.assertNotIn('pending',self.service.state)
        self.events.clear(); self.service.report_companion_result()
        self.assertFalse(self.events)
    def test_state_is_saved_durably_and_atomically(self):
        from meter.updater import save_json
        with patch('meter.updater.os.fsync') as fsync:
            save_json(self.state/'x.json',{'a':1},durable=True)
        self.assertTrue(fsync.called)
        self.assertEqual(json.loads((self.state/'x.json').read_text()),{'a':1})
        self.assertEqual(sorted(p.name for p in self.state.iterdir() if p.name.startswith('.')),[])

class Response:
    def __init__(self,status=200,data=b'x',headers=None):
        self.status_code,self.data,self.headers=status,data,headers or {}
        self.closed=False
    def iter_content(self,size): yield self.data
    def close(self): self.closed=True

class DownloaderTests(unittest.TestCase):
    def test_latest_404_has_distinct_no_release_result(self):
        session=Mock(); session.get.return_value=Response(404)
        with self.assertRaises(NoPublishedRelease): Downloader(session).fetch(RELEASE_API,100,api=True)
    def test_rejects_external_redirect(self):
        session=Mock(); session.get.return_value=Response(302,headers={'Location':'https://evil.example/firmware'})
        with self.assertRaises(ValueError):
            Downloader(session).fetch('https://github.com/luvxinc/Sweetmeter/releases/download/2026.9.2/a.bin',100)
        self.assertTrue(session.get.return_value.closed)
    def test_bounded_body_without_content_length(self):
        session=Mock(); session.get.return_value=Response(data=b'x'*11)
        with self.assertRaises(ValueError): Downloader(session).fetch(RELEASE_API,10,api=True)
    def test_retry_after_and_limit_reset(self):
        self.assertEqual(retry_deadline({'Retry-After':'120'},1000),1120)
        self.assertEqual(retry_deadline({'X-RateLimit-Reset':'2000'},1000),2000)
        session=Mock(); session.get.return_value=Response(429,headers={'Retry-After':'120'})
        with self.assertRaises(RateLimited): Downloader(session).fetch(RELEASE_API,100,api=True)
