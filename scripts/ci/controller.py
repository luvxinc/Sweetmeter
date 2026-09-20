#!/usr/bin/env python3
"""Single VM, single-job Tart broker. Long-lived GitHub auth stays on the host."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

PATH = '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
ENV = {'PATH': PATH, 'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8'}
LABELS = ['self-hosted', 'Linux', 'ARM64', 'sweetmeter-ci']


def log(message):
    print(time.strftime('%Y-%m-%dT%H:%M:%S%z'), message, flush=True)


def run(args, *, timeout=30, check=True, **kwargs):
    return subprocess.run(args, env=ENV, text=True, capture_output=True,
                          timeout=timeout, check=check, **kwargs)


class Broker:
    def __init__(self, config):
        self.c = config
        self.vm = self.ssh = None
        self.runner_id = None
        for field in ('golden', 'work'):
            if not re.fullmatch(r'sweetmeter-ci-[a-z0-9-]+', self.c[field]):
                raise ValueError('VM names must use the dedicated sweetmeter-ci- prefix')
        if self.c['golden'] == self.c['work']:
            raise ValueError('Golden and work VM must differ')
        self.repo = self.c['repository']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repo):
            raise ValueError('Invalid repository')

    def api(self, path, method='GET', payload=None):
        # Read only to authenticate the child API process; no credential is copied,
        # passed as an argument, logged, or sent to a VM.
        token = Path(self.c['credential_file']).read_text().strip()
        env = dict(ENV, GH_TOKEN=token)
        args = ['gh', 'api', '--method', method, path]
        if payload is not None:
            args += ['--input', '-']
        result = subprocess.run(args, env=env, text=True, capture_output=True,
                                input=json.dumps(payload) if payload is not None else None,
                                timeout=30)
        if result.returncode:
            raise RuntimeError(f'GitHub {method} request failed (exit {result.returncode})')
        return json.loads(result.stdout) if result.stdout.strip() else None

    @staticmethod
    def available_memory():
        value = run(['/usr/bin/memory_pressure', '-Q']).stdout
        match = re.search(r'System-wide memory free percentage:\s*(\d+)%', value)
        if not match:
            raise RuntimeError('Cannot determine host memory pressure')
        return int(match[1])

    def ready(self):
        if self.available_memory() < self.c.get('minimum_available_percent', 35):
            return False
        for repo in self.c.get('guard_repositories', []):
            if any(r['busy'] for r in self.api(f'repos/{repo}/actions/runners')['runners']):
                return False
        return True

    def queued(self):
        active = []
        for status in ('queued', 'in_progress'):
            result = self.api(f'repos/{self.repo}/actions/runs?status={status}&per_page=100')
            active.extend(result['workflow_runs'])
        for item in active:
            if item['event'] not in {'push', 'workflow_dispatch'}:
                continue
            if item.get('actor', {}).get('login') != self.c['actor']:
                continue
            if item.get('triggering_actor', {}).get('login') != self.c['actor']:
                continue
            if item.get('head_repository', {}).get('full_name') != self.repo:
                continue
            branch = item.get('head_branch', '')
            if branch != 'main' and not branch.startswith('codex/'):
                continue
            jobs = self.api(f"repos/{self.repo}/actions/runs/{item['id']}/jobs?filter=latest&per_page=100")
            if any(j['status'] == 'queued' and set(LABELS).issubset(j['labels']) for j in jobs['jobs']):
                return True
        return False

    def cleanup(self):
        if self.ssh and self.ssh.poll() is None:
            self.ssh.terminate()
        self.ssh = None
        run(['tart', 'stop', self.c['work']], check=False)
        if self.vm:
            try:
                self.vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.vm.terminate()
                self.vm.wait(timeout=10)
        self.vm = None
        run(['tart', 'delete', self.c['work']], check=False)
        if self.runner_id is not None:
            try:
                # Only our just-created ID, never an existing shared runner.
                self.api(f'repos/{self.repo}/actions/runners/{self.runner_id}', 'DELETE')
            except RuntimeError:
                # Successful JIT runners automatically deregister after one job.
                pass
            self.runner_id = None

    def cycle(self, probe=False):
        self.cleanup()
        try:
            run(['tart', 'clone', self.c['golden'], self.c['work']], timeout=120)
            self.vm = subprocess.Popen(['tart', 'run', '--no-graphics', '--no-audio',
                                        '--no-clipboard', self.c['work']], env=ENV,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ssh = None
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                ip = run(['tart', 'ip', self.c['work']], timeout=8, check=False).stdout.strip()
                if re.fullmatch(r'[0-9.]+', ip):
                    ssh = ['ssh', '-i', self.c['ssh_key'], '-o', 'BatchMode=yes',
                           '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                           '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=5',
                           f"{self.c.get('guest_user', 'admin')}@{ip}"]
                    if run(ssh + ['true'], timeout=8, check=False).returncode == 0:
                        break
                if self.vm.poll() is not None:
                    raise RuntimeError('Tart guest exited before SSH became ready')
                time.sleep(2)
            else:
                raise RuntimeError('Guest SSH startup timed out')
            name = 'sweetmeter-' + time.strftime('%Y%m%d-%H%M%S')
            response = self.api(f'repos/{self.repo}/actions/runners/generate-jitconfig', 'POST',
                                {'name': name, 'runner_group_id': 1,
                                 'labels': LABELS, 'work_folder': '_work'})
            self.runner_id = response['runner']['id']
            jit = response['encoded_jit_config']
            # SSH command arguments contain only a static path. JIT travels stdin.
            self.ssh = subprocess.Popen(ssh + ['/opt/sweetmeter-ci/start-runner.sh'],
                                        env=ENV, stdin=subprocess.PIPE, stdout=sys.stdout,
                                        stderr=sys.stderr, text=True)
            self.ssh.stdin.write(jit + '\n')
            self.ssh.stdin.close()
            del jit, response
            log(f'Registered ephemeral runner {name} id={self.runner_id}')
            deadline = time.monotonic() + (180 if probe else self.c.get('maximum_job_seconds', 3600))
            while self.ssh.poll() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError('Disposable runner lifetime exceeded')
                if self.available_memory() < self.c.get('emergency_available_percent', 15):
                    raise RuntimeError('Host memory pressure: stopping only Sweetmeter VM')
                time.sleep(10)
            log(f'Runner finished, exit={self.ssh.returncode}; deleting disposable VM')
        finally:
            self.cleanup()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--probe', action='store_true', help='Register one idle runner for at most 3 minutes')
    args = p.parse_args()
    config = json.loads(args.config.read_text())
    broker = Broker(config)
    lock = open(args.config.with_suffix('.lock'), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another Sweetmeter controller holds this configuration')
    def stop(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        broker.cleanup()  # Only the dedicated work VM; golden is never altered.
        if args.probe:
            if not broker.ready():
                raise SystemExit('Host guard deferred probe')
            broker.cycle(probe=True)
            return
        log('Sweetmeter broker active; waiting for trusted queued jobs')
        while True:
            try:
                if broker.ready() and broker.queued():
                    broker.cycle()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                # No subprocess stderr, exception repr, API body, or credential data.
                log('Cycle deferred after an operational error; retrying later')
            time.sleep(config.get('poll_seconds', 30))
    except KeyboardInterrupt:
        log('Stopping Sweetmeter broker')
    finally:
        broker.cleanup()


if __name__ == '__main__':
    main()
