"""Packaged as a separate windowless one-file process, copied outside the app before use.

Modes: `<plan.json>` applies a staged update, `--launch [app args]` is the login
launcher (recovery first), `--uninstall [...]` removes Sweetmeter. Failures are
written to a small log instead of an unhandled-exception dialog.
"""
import sys


def _cap_launchd_output():
    # launchd appends the launcher's rare stdout/stderr here; keep it small.
    try:
        from meter.paths import default_state_dir
        path = default_state_dir() / 'launcher-output.log'
        if path.stat().st_size > 1024 * 1024:
            with path.open('r+b') as handle:
                handle.truncate(0)
    except OSError:
        pass


def main(argv):
    from meter.self_update import apply_update, helper_log, launch_installed
    try:
        if argv[:1] == ['--launch']:
            _cap_launchd_output()
            return launch_installed(argv[1:])
        if argv[:1] == ['--uninstall']:
            from meter.installation import uninstall_main
            return uninstall_main(argv[1:])
        if len(argv) != 1:
            helper_log('Update helper: expected one update plan path')
            return 2
        apply_update(argv[0])
        return 0
    except Exception as error:  # Reported in helper.log and companion-update-result.json.
        helper_log('Update helper failed: ' + type(error).__name__ + ': ' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
