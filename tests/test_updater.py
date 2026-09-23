import copy
import hashlib
import json
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
    def test_legacy_firmware_never_has_ota_offer(self):
        self.service.set_device({'protocol':3,'firmware':'QM3.2'},True,'device-one')
        self.check()
        self.assertNotIn('firmware',self.service.offers)
    def test_rate_limit_manual_checks_do_not_bypass_backoff(self):
        self.service.state['retry_at']=time.time()+60
        self.service.check_now(manual=True)
        self.assertFalse(self.feed.calls)

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
