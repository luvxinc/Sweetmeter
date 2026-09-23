import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from meter.app import ProviderWorker, Application
from meter.__main__ import InstanceLock

class AppTests(unittest.TestCase):
    def test_single_instance_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'lock'
            one=InstanceLock(path)
            try:
                with self.assertRaises(OSError): InstanceLock(path)
            finally: one.close()
            two=InstanceLock(path); two.close()
    def test_sqlite_created_and_closed_on_same_provider_thread(self):
        ids=[]; done=threading.Event(); holder={}
        class Index:
            def __init__(self,path): ids.append(threading.get_ident()); self.db=self
            def scan(self): ids.append(threading.get_ident()); return set()
            def close(self): ids.append(threading.get_ident())
        def event(value):
            if value['event']=='snapshot': holder['worker'].stop.set(); done.set()
        with tempfile.TemporaryDirectory() as directory, patch('meter.app.TokenIndex',Index), patch('meter.app.refresh',return_value={'providers':{}}):
            worker=holder['worker']=ProviderWorker(directory,event)
            worker.start(); self.assertTrue(done.wait(3)); worker.close()
        self.assertEqual(len(set(ids)),1)
        self.assertNotEqual(ids[0],threading.get_ident())
    def test_token_index_failure_never_blocks_startup_or_quotas(self):
        seen=[]
        with tempfile.TemporaryDirectory() as directory, patch('meter.app.TokenIndex',side_effect=OSError('denied')), \
                patch('meter.app.refresh',return_value={'providers':{}}):
            app=Application(directory,preview_only=True)
            try:
                app.start()
                self.assertIsNone(app.provider.startup_error)
                self.assertEqual(app.provider.index_error,'OSError')
                deadline=threading.Event()
                for _ in range(40):
                    seen.extend(e['event'] for e in app.pump())
                    if 'snapshot' in seen: break
                    deadline.wait(.05)
            finally: app.close()
        self.assertIn('snapshot',seen)
    def test_transient_refresh_failure_recovers_on_refresh(self):
        done=threading.Event(); holder={}; failures=[]
        class Index:
            def __init__(self,path): self.db=self
            def scan(self): return set()
            def close(self): pass
        def event(value):
            if value['event']=='provider_error': failures.append(value); holder['worker'].force.set()
            elif value['event']=='snapshot': holder['worker'].stop.set(); done.set()
        with tempfile.TemporaryDirectory() as directory, patch('meter.app.TokenIndex',Index), patch('meter.app.refresh',side_effect=[OSError('temporary'),{'providers':{}}]):
            worker=holder['worker']=ProviderWorker(directory,event); worker.start()
            self.assertTrue(done.wait(3)); worker.close()
        self.assertEqual(len(failures),1)
