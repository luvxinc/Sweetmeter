#!/bin/bash
# No checkout code, PATH lookup, or Python environment is used by the trust gate.
exec /usr/bin/timeout 15 /usr/bin/python3 -I /opt/sweetmeter-ci/job_guard.py
