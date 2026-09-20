#!/bin/bash
set -euo pipefail
# Host sends one short-lived JIT configuration on stdin. Never echo it or use xtrace.
IFS= read -r jit
[[ -n "$jit" && ${#jit} -lt 65536 ]] || exit 2
cd /opt/actions-runner
export ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/sweetmeter-ci/job-started.sh
exec ./run.sh --jitconfig "$jit"
