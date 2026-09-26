"""PlatformIO pre-build: derive firmware version and public trust from source.

The generated include is deliberately ignored. This script needs only the Python
standard library, because it runs inside PlatformIO's separate interpreter.
"""
from pathlib import Path
import json
import base64
import os
import re
import sys
import hashlib

if "Import" in globals():
    Import("env")  # noqa: F821
    _ROOT = Path(env["PROJECT_DIR"]).resolve().parent  # noqa: F821
else:
    _ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from scripts.build_provenance import capture_build_state, require_unchanged_build_state, write_json_atomic


KEY_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,14}")
MAX_TRUSTED_KEYS = 8
_P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004")


def trusted_key_table(root):
    """Every meter/assets/keys/<key-id>.pem public key, sorted by key ID.

    The firmware embeds all of them; a signed header's 16-byte key ID selects
    one. Adding a backup key here (in a prior release) enables later rotation.
    Private keys are never read: only uncompressed P-256 SPKI PEM is accepted.
    """
    folder = Path(root) / "meter/assets/keys"
    keys, seen = [], set()
    for path in sorted(folder.glob("*.pem")):
        key_id = path.stem
        if not KEY_ID_PATTERN.fullmatch(key_id):
            raise ValueError(f"Invalid trusted key ID: {key_id!r}")
        public = path.read_text(encoding="ascii")
        if not re.fullmatch(r"-----BEGIN PUBLIC KEY-----\n[A-Za-z0-9+/=\n]+-----END PUBLIC KEY-----\n", public):
            raise ValueError(f"Missing/invalid public signing key resource: {path.name}")
        der = base64.b64decode("".join(public.splitlines()[1:-1]), validate=True)
        if len(der) != 91 or not der.startswith(_P256_SPKI_PREFIX):
            raise ValueError(f"Public trust resource must be uncompressed P-256 SPKI: {path.name}")
        if der in seen:
            raise ValueError(f"Duplicate trusted public key: {path.name}")
        seen.add(der)
        keys.append((key_id, public))
    if not keys:
        raise ValueError("Missing/invalid public signing key resource")
    if len(keys) > MAX_TRUSTED_KEYS:
        raise ValueError("Too many trusted signing keys")
    return keys


def key_table_source(keys):
    lines = [f"#define SWEETMETER_TRUSTED_KEY_COUNT {len(keys)}u\n"]
    for index, (_, public) in enumerate(keys):
        lines.append(f"static const char SWEETMETER_TRUSTED_KEY_{index}_PEM[] = {json.dumps(public)};\n")
    names = ", ".join(json.dumps(key_id) for key_id, _ in keys)
    pems = ", ".join(f"SWEETMETER_TRUSTED_KEY_{index}_PEM" for index in range(len(keys)))
    sizes = ", ".join(f"sizeof(SWEETMETER_TRUSTED_KEY_{index}_PEM)" for index in range(len(keys)))
    lines.append(f"static const char *const SWEETMETER_TRUSTED_KEY_IDS[] = {{{names}}};\n")
    lines.append(f"static const char *const SWEETMETER_TRUSTED_KEY_PEMS[] = {{{pems}}};\n")
    lines.append(f"static const unsigned SWEETMETER_TRUSTED_KEY_PEM_SIZES[] = {{{sizes}}};\n")
    return "".join(lines)


def generate(root, output=None, *, build_state=None):
    root = Path(root).resolve()
    output = Path(output) if output else root / "firmware/.generated/sweetmeter_release.h"
    build_state = capture_build_state(root) if build_state is None else build_state
    root_version = (root / "VERSION").read_text(encoding="ascii").removesuffix("\n")
    version = (root / "VERSION").read_text(encoding="ascii").removesuffix("\n")
    test_build = os.environ.get("SWEETMETER_TEST_BUILD") == "1"
    test_version = os.environ.get("SWEETMETER_TEST_VERSION")
    if test_version is not None and not test_build:
        raise ValueError("Test version override requires SWEETMETER_TEST_BUILD=1")
    if test_build and not test_version:
        raise ValueError("Test build requires explicit SWEETMETER_TEST_VERSION")
    if "SWEETMETER_TEST_BUILD" in os.environ and not test_build:
        raise ValueError("Invalid test build environment flag")
    variant = os.environ.get("SWEETMETER_TEST_VARIANT", "normal")
    if variant not in {"normal", "health-fail", "reset-before-confirm"}:
        raise ValueError("Unknown acceptance fixture variant")
    if not test_build and "SWEETMETER_TEST_VARIANT" in os.environ:
        raise ValueError("Test variant requires SWEETMETER_TEST_BUILD=1")
    if test_build:
        version = test_version
    project_name = "Sweetmeter"
    if test_build:
        project_name = {"normal": "Sweetmeter-test", "health-fail": "Sweetmeter-test-health",
                        "reset-before-confirm": "Sweetmeter-test-reset"}[variant]
    match = re.fullmatch(r"([1-9][0-9]{3})\.([1-9]|1[0-2])\.([1-9][0-9]{0,9})", version)
    if not match or int(match[3]) > 0xFFFFFFFF:
        raise ValueError("Invalid root VERSION for firmware")
    keys = trusted_key_table(root)
    source = ("// Generated from root VERSION and the public trust resource. Do not edit.\n"
              "#pragma once\n"
              f"#define SWEETMETER_VERSION {json.dumps(version)}\n"
              f"#define SWEETMETER_SOURCE_COMMIT {json.dumps(build_state['source_commit'])}\n"
              f"#define SWEETMETER_SOURCE_FINGERPRINT {json.dumps(build_state['source_fingerprint'])}\n"
              f"#define SWEETMETER_SOURCE_DIRTY {int(build_state['dirty'])}\n"
              f"#define SWEETMETER_TEST_BUILD {int(test_build)}\n"
              f"#define SWEETMETER_PROJECT_NAME {json.dumps(project_name)}\n"
              f"#define SWEETMETER_TEST_HEALTH_FAIL {int(test_build and variant == 'health-fail')}\n"
              f"#define SWEETMETER_TEST_RESET_BEFORE_CONFIRM {int(test_build and variant == 'reset-before-confirm')}\n"
              f"#define SWEETMETER_VERSION_YEAR {int(match[1])}u\n"
              f"#define SWEETMETER_VERSION_MONTH {int(match[2])}u\n"
              f"#define SWEETMETER_VERSION_SEQUENCE {int(match[3])}u\n"
              + key_table_source(keys))
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists() or output.read_text() != source:
        output.write_text(source, encoding="ascii")
    record = {"schema": 1, "kind": "sweetmeter-firmware-build", **build_state,
              "version": version, "root_version": root_version, "test_build": test_build,
              "variant": variant, "project_name": project_name}
    write_json_atomic(output.parent / "build-start.json", record)
    return output


def finish_build(root, image, start_record):
    root, image = Path(root), Path(image)
    before = {name: start_record[name] for name in ("source_commit", "source_tree", "dirty", "source_fingerprint")}
    require_unchanged_build_state(before, root)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    write_json_atomic(Path(str(image) + ".build.json"), {**start_record, "image_sha256": digest})


# SCons provides Import inside an extra script; standalone invocation remains useful.
if "Import" in globals():
    Import("env")  # noqa: F821
    build_root = Path(env["PROJECT_DIR"]).parent  # noqa: F821
    build_state = capture_build_state(build_root)
    generated = generate(build_root, build_state=build_state)
    start_record = json.loads((generated.parent / "build-start.json").read_text())
    env.Append(CPPPATH=[str(generated.parent)])  # noqa: F821
    env.Depends("$BUILD_DIR/${PROGNAME}.elf", str(generated))  # noqa: F821
    env.Depends("$BUILD_DIR/${PROGNAME}.bin", str(generated))  # noqa: F821
    def record_firmware_build(source, target, env):
        finish_build(build_root, Path(str(target[0])), start_record)
    env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", record_firmware_build)  # noqa: F821
elif __name__ == "__main__":
    generate(Path(__file__).resolve().parents[1])
