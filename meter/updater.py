"""Signed GitHub releases and explicit user decisions. Never auto-install."""
from __future__ import annotations
import base64
import json
import os
import queue
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin
import requests
from .protocol import (BOARD_ID, MAX_MANIFEST_SIZE, select_artifact, validate_asset_url,
                       validate_download_url, verify_manifest, verify_artifact,
                       verify_envelope, match_firmware_artifact, firmware_image_version)
from .paths import compatible_platforms, platform_id as host_platform
from .version import Version, get_version

RELEASE_API = 'https://api.github.com/repos/luvxinc/Sweetmeter/releases/latest'
INTERVAL = 6 * 3600
# A rocker hold, or several, never causes more than one GitHub check per minute.
DEVICE_CHECK_MAX_AGE = 60
# Results shown on the meter after its rocker is held (protocol `u` codes).
NOTICE_CURRENT, NOTICE_INSTALLING, NOTICE_FAILED, NOTICE_COMPANION = 2, 3, 4, 5
# Failed targets remembered (most recent last) so none is offered again
# automatically; a manual check still offers them.
FAILED_KEEP = 8

def save_json(path, value, *, durable=False):
    """Atomic JSON replace; `durable` also survives power loss (update decisions)."""
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            if durable:
                output.flush()
                os.fsync(output.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    if durable and os.name != 'nt':
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)

def platform_id():
    return host_platform()

class RateLimited(RuntimeError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__('Update server rate limit; retry later')

class NoPublishedRelease(RuntimeError):
    pass

class DownloadHTTPError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'Update server HTTP {status}')

def retry_deadline(headers, now):
    value = headers.get('Retry-After', '')
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            delay = 60
    try:
        reset = float(headers.get('X-RateLimit-Reset', 0))
    except (ValueError, TypeError):
        reset = 0
    return max(now + max(60, min(delay, 86400)), min(reset, now + 86400))

class Downloader:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def fetch(self, url, maximum, *, headers=None, api=False, cancel=None, destination=None):
        if api:
            if url != RELEASE_API:
                raise ValueError('Unexpected update API')
        else:
            validate_asset_url(url)
        for attempt in range(5):
            response = self.session.get(url, headers=headers or {}, timeout=(10, 30),
                                        stream=True, allow_redirects=False)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    if api or attempt == 4:
                        raise RuntimeError('Unexpected release redirect')
                    url = validate_download_url(urljoin(url, response.headers.get('Location', '')))
                    continue
                if response.status_code in (403, 429):
                    raise RateLimited(retry_deadline(response.headers, time.time()))
                if response.status_code == 304 and api:
                    return None, dict(response.headers)
                if response.status_code == 404 and api:
                    raise NoPublishedRelease()
                if response.status_code != 200:
                    raise DownloadHTTPError(response.status_code)
                length = response.headers.get('Content-Length')
                if length and int(length) > maximum:
                    raise ValueError('Download exceeds size limit')
                result, received = bytearray(), 0
                handle = Path(destination).open('xb') if destination is not None else None
                try:
                    for block in response.iter_content(65536):
                        if cancel is not None and cancel.is_set():
                            raise RuntimeError('Download cancelled')
                        received += len(block)
                        if received > maximum:
                            raise ValueError('Download exceeds size limit')
                        if handle:
                            handle.write(block)
                        else:
                            result.extend(block)
                finally:
                    if handle:
                        handle.close()
                return bytes(result), dict(response.headers)
            finally:
                response.close()
        raise RuntimeError('Too many redirects')

    def artifact(self, artifact, directory, cancel=None):
        # Only called on the Install path. The size comes from a verified manifest.
        path = Path(directory) / artifact['asset']
        try:
            self.fetch(artifact['url'], artifact['size'], cancel=cancel, destination=path)
            verify_artifact(path, artifact)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

@dataclass(frozen=True)
class Offer:
    kind: str
    current: str
    target: str
    notes: str
    artifact: dict
    manifest: dict
    blocked: str = ''

class UpdateService:
    def __init__(self, state_dir, radio, emit, *, downloader=None, version=None, trusted_keys=None):
        self.state_dir, self.radio, self.emit = Path(state_dir), radio, emit
        self.version = version or get_version()
        self.downloader = downloader or Downloader()
        self.trusted_keys = trusted_keys
        self.path = self.state_dir / 'updates.json'
        try:
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.state, dict):
                self.state = {}
        except (OSError, ValueError):
            self.state = {}
        # A damaged local preference file must not kill the scheduler or UI.
        for key in ('retry_at', 'checked_at'):
            if not isinstance(self.state.get(key, 0), (int, float)):
                self.state.pop(key, None)
        if not isinstance(self.state.get('choices', {}), dict):
            self.state.pop('choices', None)
        else:
            self.state['choices'] = {kind: value for kind, value in self.state.get('choices', {}).items()
                                     if kind in ('firmware', 'companion') and isinstance(value, dict)
                                     and isinstance(value.get('later', 0), (int, float))}
        pending = self.state.get('pending')
        if pending is not None:
            try:
                if not isinstance(pending, dict) or pending.get('kind') not in ('firmware', 'companion'):
                    raise ValueError('Invalid pending operation')
                Version.parse(pending.get('target'))
            except ValueError:
                self.state.pop('pending', None)
        self.manifest, self.device, self.offers = None, {}, {}
        self.device_id = None
        self.connected = False
        self.busy = False
        self.lock = threading.RLock()
        self.requests = queue.Queue()
        self.stop, self.cancel_download = threading.Event(), threading.Event()
        self.thread = None
        self.awaiting_until = None
        self._reconciled = False
        self.prompted = set()
        self.checked_monotonic = None
        self.download_dir = None
        # A firmware install started by holding the meter's rocker: its
        # outcome is also shown on the meter (protocol `u` codes).
        self.device_initiated = False
        self.installing = None  # Kind of the install this process started last.

    def start(self):
        self.report_companion_result()
        self.thread = threading.Thread(target=self._run, name='sweetmeter-updates', daemon=True)
        self.thread.start()
        self.requests.put('automatic')

    def report_companion_result(self):
        """Show the outcome the update helper recorded (success or rollback reason)."""
        path = self.state_dir / 'companion-update-result.json'
        try:
            if path.stat().st_size > 16384:
                raise ValueError('Oversized update result')
            result = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            result = {}
        try:
            path.unlink()
        except OSError:
            pass
        if not isinstance(result, dict):
            return
        version = str(result.get('version', ''))[:20]
        if result.get('status') == 'install_interrupted':
            # An installer or repair was interrupted and undone at login: not
            # an update the user started here, so nothing is marked failed.
            self.emit({'event': 'update_notice',
                       'message': 'An interrupted Sweetmeter installation was undone and the previously '
                                  'installed version was kept. Run the installer again to finish it.'})
        elif result.get('status') == 'rollback':
            with self.lock:
                pending = self.state.get('pending')
                started = bool(pending and pending.get('kind') == 'companion')
                if started:
                    self._remember_failed({**pending, 'outcome': 'rollback'})
                    self.state.pop('pending', None)
                    self._save()
            reason = str(result.get('reason') or 'The updated app did not start correctly.')[:600]
            if started:
                self.emit({'event': 'update_error',
                           'error': 'Companion update ' + version + ' was not kept; this version was restored. ' + reason})
            else:
                self.emit({'event': 'update_notice',
                           'message': 'An interrupted Sweetmeter update was undone; this version was kept.'})

    def failed_targets(self):
        """Remembered failed installs (dicts with kind and target), oldest first.
        Older versions stored a single record; it is read as a one-item list."""
        value = self.state.get('failed')
        if isinstance(value, dict):
            value = [value]
        return [record for record in value if isinstance(record, dict)] if isinstance(value, list) else []

    def _remember_failed(self, record):
        """Add a failed install (caller holds the lock); keeps FAILED_KEEP."""
        key = record.get('kind'), record.get('target')
        kept = [old for old in self.failed_targets() if (old.get('kind'), old.get('target')) != key]
        self.state['failed'] = (kept + [record])[-FAILED_KEEP:]

    def check(self):
        self.requests.put('check')

    def device_request(self):
        """The meter's rocker was held: check now and install newer firmware."""
        self.requests.put('device')

    def _save(self):
        save_json(self.path, self.state, durable=True)

    def cleanup(self):
        """Remove finished download/staging folders; keep what an operation still needs."""
        with self.lock:
            if self.busy or self.state.get('pending'):
                return
            keep = self.state.get('manual_staging')
        downloads = self.state_dir / 'downloads'
        if downloads.is_dir():
            for folder in downloads.glob('update-*'):
                if folder.is_dir() and not folder.is_symlink():
                    shutil.rmtree(folder, ignore_errors=True)
        try:
            from .self_update import cleanup_staging
            cleanup_staging(self.state_dir, keep=[keep] if isinstance(keep, str) else [])
        except (OSError, RuntimeError):
            pass

    def _run(self):
        self.cleanup()
        next_check = time.monotonic() + INTERVAL
        while not self.stop.is_set():
            try:
                request = self.requests.get(timeout=1)
            except queue.Empty:
                request = 'automatic'
                if time.monotonic() < next_check:
                    self.tick()
                    continue
            if request == 'device':
                self.device_update()
            else:
                self.check_now(manual=request == 'check')
                if request == 'automatic':
                    self.cleanup()
            next_check = time.monotonic() + INTERVAL

    def check_now(self, *, manual=False, quiet=(), max_age=None):
        """Check GitHub; with `max_age`, reuse a manifest verified that recently."""
        with self.lock:
            if self.busy:
                return False
            if (max_age is not None and self.manifest is not None and self.checked_monotonic is not None
                    and time.monotonic() - self.checked_monotonic < max_age):
                self._offers(manual, quiet=quiet)
                return True
            if time.time() < self.state.get('retry_at', 0):
                self.emit({'event': 'update_notice', 'message': 'Update checks are waiting for the server retry time.'})
                return False
        try:
            headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Sweetmeter/' + self.version,
                       'X-GitHub-Api-Version': '2022-11-28'}
            if self.state.get('etag'):
                headers['If-None-Match'] = self.state['etag']
            raw, response_headers = self.downloader.fetch(RELEASE_API, 1024*1024, headers=headers, api=True)
            if raw is None:
                manifest_raw = base64.b64decode(self.state['manifest'], validate=True)
                signature = base64.b64decode(self.state['signature'], validate=True)
            else:
                release = json.loads(raw)
                if release.get('draft') or release.get('prerelease'):
                    raise ValueError('Release is not stable')
                assets = release.get('assets', [])
                def asset_url(name):
                    matches = [a['browser_download_url'] for a in assets if a.get('name') == name]
                    if len(matches) != 1:
                        raise ValueError('Release is missing unique signed metadata')
                    return matches[0]
                manifest_raw, _ = self.downloader.fetch(asset_url('manifest.json'), MAX_MANIFEST_SIZE)
                signature, _ = self.downloader.fetch(asset_url('manifest.json.sig'), 72)
            manifest = verify_manifest(manifest_raw, signature, trusted_keys=self.trusted_keys)
            with self.lock:
                self.manifest = manifest
                self.checked_monotonic = time.monotonic()
                self.state.update(manifest=base64.b64encode(manifest_raw).decode(),
                                  signature=base64.b64encode(signature).decode(),
                                  etag=response_headers.get('ETag', self.state.get('etag', '')),
                                  checked_at=time.time(), retry_at=0)
                self._save()
                self._offers(manual, quiet=quiet)
            self.emit({'event': 'update_checked', 'version': manifest['version']})
            return True
        except NoPublishedRelease:
            with self.lock:
                self.manifest, self.offers = None, {}
            self.emit({'event': 'update_notice', 'message': 'No published updates yet.'})
            return True
        except RateLimited as error:
            with self.lock:
                self.state['retry_at'] = error.retry_at
                self._save()
            self.emit({'event': 'update_error', 'error': str(error)})
        except DownloadHTTPError as error:
            self.emit({'event': 'update_error', 'error': str(error) + '. Try checking again later.'})
        except requests.RequestException as error:
            self.emit({'event': 'update_error', 'error': 'Cannot reach the update server (' +
                       type(error).__name__ + '). Check the Internet connection and retry.'})
        except Exception as error:
            # Response bodies never become executable HTML or diagnostic dumps.
            self.emit({'event': 'update_error', 'error': 'Update check failed: ' + type(error).__name__})
        return False

    def device_update(self):
        """Holding the rocker is the physical confirmation for a firmware install."""
        if self.busy:
            self._notify_device(NOTICE_INSTALLING)
            return
        self.emit({'event': 'update_notice', 'message': 'Firmware check requested on the meter…'})
        # The rocker confirms firmware only. A companion offer the user skipped
        # or postponed stays quiet (manual=False respects those choices).
        if not self.check_now(manual=False, quiet=('firmware',), max_age=DEVICE_CHECK_MAX_AGE):
            self._notify_device(NOTICE_FAILED)
            return
        with self.lock:
            offer = self.offers.get('firmware')
        if offer is None:
            self._notify_device(NOTICE_CURRENT)
            self.emit({'event': 'update_notice', 'message': 'Meter firmware is up to date.'})
            return
        if offer.blocked:
            self._notify_device(NOTICE_COMPANION)
            self.emit({'event': 'update_error', 'error': offer.blocked})
            return
        self._notify_device(NOTICE_INSTALLING)
        try:
            # The meter's own OTA screen asks to keep USB power connected.
            self.install(offer, usb_power=True, device_initiated=True)
        except (ValueError, RuntimeError) as error:
            self._notify_device(NOTICE_FAILED)
            self.emit({'event': 'update_error', 'error': str(error)})

    def _notify_device(self, code):
        try:
            self.radio.update_notice(code)
        except (AttributeError, ValueError):
            pass

    def _firmware_failed(self):
        """A firmware install ended without a verified result (caller holds
        the lock): a rocker-hold install shows the failure on the meter."""
        self.busy, self.awaiting_until = False, None
        if self.device_initiated:
            self.device_initiated = False
            self._notify_device(NOTICE_FAILED)

    def _offers(self, manual=False, quiet=()):
        if self.manifest is None or self.busy:
            return
        manifest, offers = self.manifest, {}
        os_name, arch = platform_id()
        package = None
        # Most preferred runnable package first (Windows on Arm falls back to x64).
        for os_choice, arch_choice in compatible_platforms(os_name, arch):
            package = select_artifact(manifest, 'companion', os=os_choice, arch=arch_choice)
            if package:
                break
        if package and Version.parse(package['version']) > Version.parse(self.version):
            offers['companion'] = self._offer(package, self.version)
        firmware = select_artifact(manifest, 'firmware', board=self.device.get('board'))
        if self.device.get('protocol') == 4 and firmware:
            try:
                current = Version.parse(self.device['firmware'])
                if Version.parse(firmware['version']) > current:
                    blocked = ''
                    if Version.parse(firmware['minimum_companion']) > Version.parse(self.version):
                        blocked = 'Update the companion first (requires ' + firmware['minimum_companion'] + ').'
                    offers['firmware'] = self._offer(firmware, str(current), blocked)
            except ValueError:
                pass
        self.offers = offers
        for kind, offer in offers.items():
            key = kind, offer.target
            if kind in quiet:
                # Handled by the caller; later status reads must not re-prompt.
                self.prompted.add(key)
                continue
            choice = self.state.get('choices', {}).get(kind, {})
            if not manual and (choice.get('skip') == offer.target or time.time() < choice.get('later', 0)):
                continue
            if (not manual and kind == 'companion'
                    and any(failed.get('kind') == 'companion' and failed.get('target') == offer.target
                            for failed in self.failed_targets())):
                continue  # Rolled back on this computer; only a manual check offers it again.
            if not manual and key in self.prompted:
                continue
            self.prompted.add(key)
            self.emit({'event': 'update_offer', 'offer': offer})

    def _offer(self, artifact, current, blocked=''):
        notes = []
        for change in self.manifest['changes']:
            if Version.parse(change['version']) > Version.parse(current):
                notes.append(change['version'] + '\n' + '\n'.join('• ' + n for n in change['notes']))
        return Offer(artifact['kind'], current, artifact['version'], '\n\n'.join(notes),
                     artifact, self.manifest, blocked)

    def decide(self, offer, decision):
        if decision not in ('later', 'skip'):
            raise ValueError('Install must use the explicit install operation')
        with self.lock:
            choice = {'skip': offer.target} if decision == 'skip' else {'later': time.time()+INTERVAL}
            self.state.setdefault('choices', {})[offer.kind] = choice
            self.prompted.discard((offer.kind, offer.target))
            self._save()

    def set_device(self, status, connected=None, device_id=None):
        with self.lock:
            self.device = dict(status)
            if device_id is not None:
                self.device_id = device_id
            if connected is not None:
                self.connected = connected
            pending = self.state.get('pending')
            if (pending and pending.get('kind') == 'firmware'
                    and pending.get('device_id') == self.device_id):
                target = pending['target']
                if (status.get('firmware') == target and status.get('boot_health') == 'valid'
                        and status.get('last_update') == 'success'):
                    self.state['completed'] = {**pending, 'completed_at': time.time()}
                    self.state.pop('pending', None)
                    self.busy, self.awaiting_until = False, None
                    self.device_initiated = False
                    self._save()
                    self.emit({'event': 'firmware_verified', 'version': target})
                elif status.get('ota_target') == target and status.get('last_update') in ('rollback', 'failed'):
                    self._remember_failed({**pending, 'outcome': status['last_update']})
                    self.state.pop('pending', None)
                    self._firmware_failed()
                    self._save()
                    self.emit({'event': 'update_error', 'error': 'Device reported ' + status['last_update'] + '; current firmware ' + status.get('firmware', '--')})
                elif not self.busy and not self._reconciled:
                    self._reconciled = True
                    self.emit({'event': 'update_unconfirmed', 'message': 'A previous update has no verified outcome yet. Target: ' + target + '. Installation will not retry automatically.'})
            self._offers()

    def transfer_event(self, event):
        with self.lock:
            kind = event['event']
            if kind in ('ota_rebooting', 'ota_unconfirmed'):
                self.awaiting_until = time.monotonic() + 120
                self.emit({'event': 'update_notice', 'message': 'Reconnecting and checking the installed firmware…'})
            elif kind == 'ota_error':
                # Includes code 'job_expired': the Bluetooth worker dropped the
                # job (meter disconnected, another meter, or 60 s passed).
                pending = self.state.get('pending')
                firmware = pending and pending.get('kind') == 'firmware'
                if not firmware and self.installing != 'firmware':
                    return  # Not about a firmware install (never clears a companion update).
                if firmware and pending.get('phase') == 'commit':
                    # The meter may already have switched images and boot
                    # the new firmware fine: keep the target so its next
                    # status report reconciles it, and tell the meter nothing
                    # yet. set_device reports the verified outcome (a failure
                    # also on the meter for a rocker-hold install).
                    self._reconciled = False
                    self._save()
                    self.busy, self.awaiting_until = False, None
                    return
                if firmware:
                    self.state.pop('pending', None)
                    self._save()
                self._firmware_failed()
            elif kind == 'ota_progress' and event.get('cancellable') is False:
                pending = self.state.get('pending')
                if pending:
                    pending['phase'] = 'commit'
                    self._save()

    def tick(self):
        with self.lock:
            if self.awaiting_until is not None and time.monotonic() >= self.awaiting_until:
                self._firmware_failed()
                self.emit({'event': 'update_unconfirmed', 'message': 'No verified boot result after 120 seconds. Pending target is saved; reconnect to check it.'})

    def confirm_companion_startup(self):
        """Called only after local startup health and actual VERSION are checked."""
        with self.lock:
            pending = self.state.get('pending')
            if pending and pending.get('kind') == 'companion' and pending.get('target') == self.version:
                self.state['completed'] = {**pending, 'completed_at': time.time()}
                self.state.pop('pending', None)
                self._save()
                self.emit({'event': 'update_notice', 'message': 'Companion ' + self.version + ' installed and startup verified.'})

    def install(self, offer, *, usb_power=False, device_initiated=False):
        with self.lock:
            if self.busy or self.offers.get(offer.kind) != offer:
                raise RuntimeError('Update offer expired or another update is running')
            if offer.blocked:
                raise RuntimeError(offer.blocked)
            if offer.kind == 'firmware':
                if usb_power is not True:
                    raise ValueError('Confirm the device is connected to USB power')
                if not self.connected or self.device.get('menu') or self.device.get('critical'):
                    raise RuntimeError('Connect the selected device and close its menu first')
                if self.device.get('battery_percent', -1) in range(0, 20):
                    raise RuntimeError('Battery is below 20%; charge before updating')
            self.busy = True
            self.installing = offer.kind
            self.device_initiated = bool(device_initiated) and offer.kind == 'firmware'
            self.cancel_download.clear()
        threading.Thread(target=self._install, args=(offer, usb_power), name='sweetmeter-install', daemon=True).start()

    def _install(self, offer, usb_power):
        try:
            self.emit({'event': 'ota_progress', 'phase': 'Downloading verified update', 'percent': 0, 'cancellable': True})
            staging = self.state_dir / 'downloads'
            staging.mkdir(exist_ok=True)
            for old in staging.glob('update-*'):
                if old.is_dir() and not old.is_symlink():
                    shutil.rmtree(old, ignore_errors=True)
            directory = Path(tempfile.mkdtemp(prefix='update-', dir=staging))
            self.download_dir = directory
            artifact = offer.artifact
            image = self.downloader.artifact(artifact, directory, self.cancel_download)
            if offer.kind == 'firmware':
                metadata_asset = {'asset': artifact['metadata_asset'], 'url': artifact['metadata_url'],
                                  'size': artifact['metadata_size'], 'sha256': artifact['metadata_sha256']}
                metadata_path = self.downloader.artifact(metadata_asset, directory, self.cancel_download)
                envelope = metadata_path.read_bytes()
                metadata = verify_envelope(envelope, trusted_keys=self.trusted_keys,
                                           current_version=offer.current, companion_version=self.version)
                match_firmware_artifact(metadata, artifact)
                if firmware_image_version(image) != metadata.version:
                    raise ValueError('Firmware compiled version disagrees with signed metadata')
                with self.lock:
                    if self.cancel_download.is_set():
                        raise RuntimeError('Update cancelled')
                    if not self.connected or self.device.get('firmware') != offer.current:
                        raise RuntimeError('Device changed while downloading; check the update again')
                    self.state['pending'] = {'kind': 'firmware', 'target': offer.target,
                                             'device_id': self.device_id, 'previous': offer.current,
                                             'phase': 'transfer', 'started_at': time.time()}
                    self._save()
                    # The Bluetooth worker runs the job only on this meter.
                    self.radio.install_firmware(image_path=image, envelope=envelope,
                                                companion_version=self.version, usb_power=usb_power,
                                                device_id=self.device_id)
            else:
                from .self_update import stage_update
                staged = stage_update(image, artifact, self.state_dir,
                                      bluetooth_baseline=getattr(self.radio, 'health', None))
                shutil.rmtree(directory, ignore_errors=True)  # Verified and unpacked.
                if self.cancel_download.is_set():
                    raise RuntimeError('Update cancelled')
                if staged.supported:
                    with self.lock:
                        self.state['pending'] = {'kind': 'companion', 'target': offer.target,
                                                 'previous': self.version, 'started_at': time.time()}
                        self._save()
                    staged.launch()
                    self.emit({'event': 'companion_restart'})
                else:
                    with self.lock:
                        # Keep only this verified package for the user to install by hand.
                        self.state['manual_staging'] = str(staged.manual_path.parent.parent)
                        self._save()
                    self.emit({'event': 'companion_manual', 'message': staged.reason, 'path': str(staged.manual_path)})
                    self.busy = False
        except Exception as error:
            with self.lock:
                self.busy = False
                pending = self.state.get('pending')
                if pending and pending.get('kind') == offer.kind and pending.get('target') == offer.target:
                    self.state.pop('pending', None)
                self._save()
            if offer.kind == 'firmware':
                with self.lock:
                    self._firmware_failed()  # Shown on the meter for a rocker-hold install.
            self.emit({'event': 'update_error', 'error': str(error)[:200]})

    def cancel(self):
        self.cancel_download.set()
        self.radio.cancel_update()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
