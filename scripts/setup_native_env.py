"""Create a fresh native-build venv; never reuse global or cached crypto builds."""
import argparse
import os
from pathlib import Path
import platform
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def crypto_options(environment, *, system=None, machine=None):
    system = sys.platform if system is None else system
    machine = platform.machine().lower() if machine is None else machine
    if system != 'darwin' or machine != 'x86_64':
        return ['--only-binary=cryptography']
    # Upstream stopped Intel macOS wheels in 49.0.0. Keep our pinned version,
    # but statically link its source build so Python's libssl cannot replace it.
    prefix = Path(subprocess.check_output(['brew', '--prefix', 'openssl@3'], text=True).strip())
    for library in ('libssl.a', 'libcrypto.a'):
        if not (prefix / 'lib' / library).is_file():
            raise RuntimeError('Intel macOS builds require Homebrew openssl@3 static libraries')
    environment.update(OPENSSL_STATIC='1', OPENSSL_DIR=str(prefix))
    return ['--no-binary=cryptography']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=ROOT / '.venv')
    args = parser.parse_args()
    destination = args.path.absolute()
    if destination.exists():
        raise SystemExit('Choose a new venv path; existing environments are never overwritten')
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('PYTHONPATH', 'PYTHONHOME') and not key.startswith('PIP_')}
    environment.update(PYTHONNOUSERSITE='1', PIP_CONFIG_FILE=os.devnull)
    options = crypto_options(environment)
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(destination)
    binary = destination / ('Scripts' if os.name == 'nt' else 'bin')
    python = binary / ('python.exe' if os.name == 'nt' else 'python')
    pip = [str(python), '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple', '--no-cache-dir']
    subprocess.run(pip + ['--upgrade', 'pip'], env=environment, check=True)
    subprocess.run(pip + options + ['-r', str(ROOT / 'requirements-build.txt')], env=environment, check=True)
    if os.environ.get('GITHUB_PATH'):
        with open(os.environ['GITHUB_PATH'], 'a', encoding='utf-8') as output:
            output.write(str(binary) + '\n')
        with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as output:
            output.write(f'VIRTUAL_ENV={destination}\nPYTHONNOUSERSITE=1\nPYTHONPATH=\nPYTHONHOME=\n')
    print('Native build interpreter:', python)


if __name__ == '__main__':
    main()
