#!/bin/bash
# Run as root inside a NEW, stopped-template clone, never on an existing runner.
set -euo pipefail
[[ $(id -u) == 0 ]] || exit 2
[[ -d /opt/actions-runner && -f /tmp/sweetmeter-ci/policy.json ]] || exit 2
[[ ! -f /opt/actions-runner/.runner && ! -f /opt/actions-runner/.credentials ]] || { echo 'Refusing configured runner template'; exit 2; }
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12 python3.12-venv python3-tk g++ git gh curl unzip ca-certificates
install -d -o root -g root -m 755 /opt/sweetmeter-ci
install -o root -g root -m 755 /tmp/sweetmeter-ci/job_guard.py /tmp/sweetmeter-ci/job-started.sh /tmp/sweetmeter-ci/start-runner.sh /opt/sweetmeter-ci/
install -o root -g root -m 644 /tmp/sweetmeter-ci/policy.json /opt/sweetmeter-ci/policy.json
install -o admin -g admin -m 600 /tmp/sweetmeter-ci/authorized_keys /home/admin/.ssh/authorized_keys
# Golden contains no host token, checkout, saved JIT registration, or host shares.
rm -rf /tmp/sweetmeter-ci
