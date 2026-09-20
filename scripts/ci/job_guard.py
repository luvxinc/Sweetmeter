#!/usr/bin/python3
"""Installed root-owned outside checkout; receives runner-generated context only."""
import json
import os
from pathlib import Path
import re
import sys

POLICY = Path('/opt/sweetmeter-ci/policy.json')


def authorize(env, event, policy):
    repo, actor = policy['repository'], policy['actor']
    if env.get('GITHUB_REPOSITORY') != repo:
        return False
    if env.get('GITHUB_ACTOR') != actor or env.get('GITHUB_TRIGGERING_ACTOR') != actor:
        return False
    if env.get('GITHUB_EVENT_NAME') not in {'push', 'workflow_dispatch'}:
        return False
    ref = env.get('GITHUB_REF', '')
    if not (ref == 'refs/heads/main' or re.fullmatch(r'refs/heads/codex/[A-Za-z0-9._/-]+', ref)):
        return False
    if event.get('repository', {}).get('full_name') != repo:
        return False
    if event.get('repository', {}).get('fork') is not False:
        return False
    if event.get('sender', {}).get('login') != actor:
        return False
    if env['GITHUB_EVENT_NAME'] == 'push':
        if event.get('ref') != ref or event.get('deleted') is not False:
            return False
        if event.get('after') != env.get('GITHUB_SHA'):
            return False
    return bool(re.fullmatch(r'[a-f0-9]{40}', env.get('GITHUB_SHA', '')))


def main():
    try:
        policy = json.loads(POLICY.read_text())
        payload = Path(os.environ['GITHUB_EVENT_PATH'])
        if payload.stat().st_size > 5 * 1024 * 1024:
            raise ValueError('oversized event')
        event = json.loads(payload.read_text())
        allowed = authorize(os.environ, event, policy)
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        allowed = False
    if not allowed:
        print('Sweetmeter host policy rejected this job before user steps.', file=sys.stderr)
        return 1
    print('Sweetmeter host policy accepted trusted owner branch job.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
