#!/usr/bin/env python3
"""Install the repository's tracked version-policy hooks for this checkout."""
from pathlib import Path
import os
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.", file=sys.stderr)
        return 1
    hooks = root / ".githooks"
    for name in ("pre-commit", "pre-push", "post-commit"):
        path = hooks / name
        if not path.is_file():
            print(f"Missing hook: {path}", file=sys.stderr)
            return 1
        os.chmod(path, path.stat().st_mode | 0o111)
    subprocess.run(["git", "-C", str(root), "config", "--local", "core.hooksPath", ".githooks"], check=True)
    print("Installed Sweetmeter hooks for this checkout. CI still checks all commits independently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
