"""Strict protocol-4 codecs and release trust checks; no network or private keys.

ECDSA API: https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ec/
DER API: https://cryptography.io/en/latest/hazmat/primitives/asymmetric/utils/
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
import hashlib
import hmac
import json
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit, unquote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from .version import Version

PROTOCOL = 4
BOARD_ID = "elecrow-crowpanel-2.13-v1.2-jd79661"
# The key new releases are signed with. Every *.pem under assets/keys is a
# trusted verification key named by its key ID (see docs/RELEASING.md for
# rotation): shipping the next key before switching KEY_ID lets installed
# companions accept releases signed by either key during the transition.
KEY_ID = "release-1"
KEYS_DIR = Path(__file__).resolve().parent / "assets" / "keys"
# Same rule as scripts/firmware_build.py KEY_ID_PATTERN (the firmware's
# embedded key table): 1-15 of [a-z0-9-], starting with [a-z0-9]. A key the
# firmware cannot embed must not be trusted by the companion either.
_KEY_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,14}", re.ASCII)
REPOSITORY = "luvxinc/Sweetmeter"
SERVICE_UUID, CONTROL_UUID, DATA_UUID, STATUS_UUID, OTA_CONTROL_UUID, OTA_DATA_UUID, OTA_STATUS_UUID = (
    f"7a1e000{i}-ff1b-4d9f-a023-47c7752c1a01" for i in range(1, 8)
)
HEADER_SIZE = 160
MAX_ENVELOPE_SIZE = 234
MAX_IMAGE_SIZE = 0x330000
MAX_MANIFEST_SIZE = 131072
MAX_COMPANION_SIZE = 512 * 1024 * 1024
MAX_VALUE_SIZE = 182
_MAGIC = b"SWMOTA4\x00"
_HEADER = struct.Struct("<8sHHHH48sIIIIIII32s16s20s")
_STATUS = struct.Struct("<cBBBIIIBBH")
_SHA_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", re.ASCII)
_HOST_RE = re.compile(r"7a1e1000-ff1b-4d9f-a023-[0-9a-f]{12}", re.ASCII)
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class ProtocolError(ValueError):
    """Untrusted wire/release input failed a required check."""


def _integer(value, minimum=0, maximum=0xFFFFFFFF, label="integer"):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"Invalid {label}")
    return value


def _version(value):
    try:
        return Version.parse(value)
    except ValueError as exc:
        raise ProtocolError("Invalid release version") from exc


def _padded(value, size):
    if not isinstance(value, str):
        raise ProtocolError("Invalid fixed string")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolError("Fixed string must be ASCII") from exc
    if not raw or len(raw) >= size or b"\0" in raw or any(b < 33 or b > 126 for b in raw):
        raise ProtocolError("Invalid fixed string")
    return raw.ljust(size, b"\0")


def _unpadded(raw):
    try:
        end = raw.index(0)
        value = raw[:end].decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("Invalid fixed string termination") from exc
    if _padded(value, len(raw)) != raw:
        raise ProtocolError("Nonzero fixed string padding")
    return value


@dataclass(frozen=True)
class FirmwareMetadata:
    version: Version
    minimum_companion: Version
    image_size: int
    image_sha256: bytes
    board: str = BOARD_ID
    key_id: str = KEY_ID
    protocol: int = PROTOCOL

    def __post_init__(self):
        object.__setattr__(self, "version", _version(self.version))
        object.__setattr__(self, "minimum_companion", _version(self.minimum_companion))
        _integer(self.image_size, 1, label="image size")
        _integer(self.protocol, 1, 65535, "protocol")
        if not isinstance(self.image_sha256, bytes) or len(self.image_sha256) != 32:
            raise ProtocolError("Image SHA-256 must be 32 bytes")
        _padded(self.board, 48)
        _padded(self.key_id, 16)

    def encode_header(self):
        return encode_header(self)


def encode_header(metadata: FirmwareMetadata) -> bytes:
    return _HEADER.pack(_MAGIC, 1, HEADER_SIZE, metadata.protocol, 0,
                        _padded(metadata.board, 48), *metadata.version.as_tuple(),
                        *metadata.minimum_companion.as_tuple(), metadata.image_size,
                        metadata.image_sha256, _padded(metadata.key_id, 16), bytes(20))


def decode_header(header: bytes) -> FirmwareMetadata:
    if len(header) != HEADER_SIZE:
        raise ProtocolError("Firmware header must be exactly 160 bytes")
    magic, fmt, size, protocol, reserved, board, y, m, n, cy, cm, cn, image_size, digest, key, tail = _HEADER.unpack(header)
    if (magic, fmt, size, reserved, tail) != (_MAGIC, 1, HEADER_SIZE, 0, bytes(20)):
        raise ProtocolError("Invalid firmware header format/reserved bytes")
    try:
        return FirmwareMetadata(Version(y, m, n), Version(cy, cm, cn), image_size,
                                digest, _unpadded(board), _unpadded(key), protocol)
    except ValueError as exc:
        raise ProtocolError("Invalid firmware metadata") from exc


def _public_key(value):
    if isinstance(value, (str, Path)):
        value = Path(value).read_bytes()
    if isinstance(value, bytes):
        try:
            value = serialization.load_pem_public_key(value)
        except ValueError as exc:
            raise ProtocolError("Invalid public key") from exc
    if not isinstance(value, ec.EllipticCurvePublicKey) or not isinstance(value.curve, ec.SECP256R1):
        raise ProtocolError("Signing key must be ECDSA P-256")
    return value


def trusted_keys(directory=None) -> dict:
    """Only explicitly shipped keys are trust roots; release data cannot add one.

    Returns {key_id: public key} for every `<key_id>.pem` shipped with the
    app. Key IDs fit the 16-byte firmware header field. A malformed file fails
    closed rather than being skipped, and the current signing key must exist.
    """
    folder = Path(directory) if directory is not None else KEYS_DIR
    keys = {}
    for path in sorted(folder.glob("*.pem")):
        key_id = path.stem
        if not _KEY_ID_RE.fullmatch(key_id) or path.is_symlink():
            raise ProtocolError("Invalid trusted key file name: " + path.name)
        keys[key_id] = _public_key(path)
    if directory is None and KEY_ID not in keys:
        raise ProtocolError("Current release signing key is missing")
    if not keys:
        raise ProtocolError("No trusted release keys")
    return keys


def _verify_signature(data, signature, key_id, keys):
    if not isinstance(signature, bytes) or not 8 <= len(signature) <= 72:
        raise ProtocolError("Invalid DER signature length")
    try:
        r, s = utils.decode_dss_signature(signature)
        if not (0 < r < _P256_ORDER and 0 < s < _P256_ORDER):
            raise ValueError("ECDSA integer outside P-256 bounds")
        if utils.encode_dss_signature(r, s) != signature:
            raise ValueError("Noncanonical DER")
    except ValueError as exc:
        raise ProtocolError("Invalid strict DER signature") from exc
    keys = trusted_keys() if keys is None else keys
    if key_id not in keys:
        raise ProtocolError("Untrusted signing key ID")
    try:
        _public_key(keys[key_id]).verify(signature, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ProtocolError("Signature verification failed") from exc


def verify_envelope(envelope: bytes, *, trusted_keys=None, current_version=None,
                    companion_version=None, board=BOARD_ID,
                    max_image_size=MAX_IMAGE_SIZE) -> FirmwareMetadata:
    if not isinstance(envelope, bytes) or not HEADER_SIZE + 2 + 8 <= len(envelope) <= MAX_ENVELOPE_SIZE:
        raise ProtocolError("Invalid firmware envelope size")
    size = struct.unpack_from("<H", envelope, HEADER_SIZE)[0]
    if not 8 <= size <= 72 or len(envelope) != HEADER_SIZE + 2 + size:
        raise ProtocolError("Firmware envelope signature length mismatch")
    metadata = decode_header(envelope[:HEADER_SIZE])
    _verify_signature(envelope[:HEADER_SIZE], envelope[HEADER_SIZE + 2:], metadata.key_id, trusted_keys)
    if metadata.board != board:
        raise ProtocolError("Firmware board mismatch")
    if metadata.protocol != PROTOCOL:
        raise ProtocolError("Unsupported firmware OTA protocol")
    if metadata.image_size > _integer(max_image_size, 1):
        raise ProtocolError("Firmware exceeds inactive partition")
    if current_version is not None and metadata.version <= _version(current_version):
        raise ProtocolError("Firmware version must be newer than installed version")
    if companion_version is not None and metadata.minimum_companion > _version(companion_version):
        raise ProtocolError("Update companion before installing this firmware")
    return metadata


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ProtocolError("Nonfinite JSON number")


def validate_asset_url(url: str) -> str:
    """Manifest URLs must identify this repository's HTTPS release assets."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 for c in url):
        raise ProtocolError("Invalid release asset URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        raise ProtocolError("Release assets must use HTTPS github.com URLs")
    prefix = f"/{REPOSITORY}/releases/download/"
    if not parsed.path.startswith(prefix):
        raise ProtocolError("Release asset is outside the trusted repository")
    components = parsed.path[len(prefix):].split("/")
    if len(components) != 2 or any(not c or unquote(c) != c for c in components):
        raise ProtocolError("Invalid release tag/asset path")
    tag, asset = components
    _version(tag[1:] if tag.startswith("v") else tag)
    if not _ASSET_RE.fullmatch(asset):
        raise ProtocolError("Invalid release asset basename")
    return url


def validate_download_url(url: str) -> str:
    """Redirect allowlist; initial URLs must separately pass validate_asset_url."""
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 for c in url):
        raise ProtocolError("Invalid download URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.fragment or parsed.username or parsed.password or parsed.port:
        raise ProtocolError("Invalid HTTPS download URL")
    if parsed.hostname == "github.com":
        return validate_asset_url(url)
    if parsed.hostname not in {"release-assets.githubusercontent.com", "objects.githubusercontent.com",
                               "github-releases.githubusercontent.com"}:
        raise ProtocolError("Untrusted release redirect host")
    return url


def _digest(value):
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ProtocolError("Invalid SHA-256 hex digest")
    return value


def validate_manifest(manifest: dict) -> dict:
    """Schema validation alone does not make a manifest trusted; verify its signature."""
    if not isinstance(manifest, dict):
        raise ProtocolError("Manifest must be an object")
    if manifest.get("schema") != 1 or type(manifest.get("schema")) is not int:
        raise ProtocolError("Unsupported manifest schema")
    if manifest.get("product") != "Sweetmeter" or manifest.get("channel") != "stable":
        raise ProtocolError("Wrong product/channel")
    latest = _version(manifest.get("version"))
    if not isinstance(manifest.get("commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]):
        raise ProtocolError("Invalid source commit")
    published = manifest.get("published_at")
    if not isinstance(published, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", published):
        raise ProtocolError("Invalid UTC publication timestamp")
    try:
        datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ProtocolError("Invalid publication date") from exc
    _padded(manifest.get("key_id"), 16)
    changes = manifest.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ProtocolError("Signed user-facing release notes are required")
    previous = None
    for change in changes:
        if not isinstance(change, dict):
            raise ProtocolError("Invalid change entry")
        version = _version(change.get("version"))
        if version > latest or (previous is not None and version >= previous):
            raise ProtocolError("Changes must have unique descending versions")
        previous = version
        notes = change.get("notes")
        if not isinstance(notes, list) or not notes or any(not isinstance(n, str) or not n.strip()
                or len(n) > 4096 or any(ord(c) < 32 and c not in "\n\t" for c in n) for n in notes):
            raise ProtocolError("Invalid user-facing release notes")
    if _version(changes[0]["version"]) != latest:
        raise ProtocolError("Current release has no change entry")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 16:
        raise ProtocolError("Invalid artifact list")
    matches, assets = set(), set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ProtocolError("Invalid artifact")
        if _version(artifact.get("version")) != latest:
            raise ProtocolError("Artifact version differs from release")
        asset = artifact.get("asset")
        if not isinstance(asset, str) or not _ASSET_RE.fullmatch(asset):
            raise ProtocolError("Invalid artifact filename")
        validate_asset_url(artifact.get("url"))
        tag = artifact["url"].rsplit("/", 2)[1]
        if _version(tag.removeprefix("v")) != latest:
            raise ProtocolError("Artifact URL tag differs from release version")
        if artifact["url"].rsplit("/", 1)[1] != asset:
            raise ProtocolError("Artifact URL/filename mismatch")
        _digest(artifact.get("sha256"))
        kind = artifact.get("kind")
        if kind == "firmware":
            if artifact.get("board") != BOARD_ID or type(artifact.get("protocol")) is not int or artifact["protocol"] != PROTOCOL:
                raise ProtocolError("Unsupported firmware board/protocol")
            if _version(artifact.get("minimum_companion")) > latest:
                raise ProtocolError("Firmware minimum companion cannot exceed release version")
            _integer(artifact.get("size"), 1, MAX_IMAGE_SIZE, "firmware size")
            _integer(artifact.get("metadata_size"), 170, MAX_ENVELOPE_SIZE, "metadata size")
            _digest(artifact.get("metadata_sha256"))
            metadata_asset = artifact.get("metadata_asset")
            if not isinstance(metadata_asset, str) or not _ASSET_RE.fullmatch(metadata_asset):
                raise ProtocolError("Invalid metadata filename")
            validate_asset_url(artifact.get("metadata_url"))
            if _version(artifact["metadata_url"].rsplit("/", 2)[1].removeprefix("v")) != latest:
                raise ProtocolError("Metadata URL tag differs from release version")
            if artifact["metadata_url"].rsplit("/", 1)[1] != metadata_asset:
                raise ProtocolError("Metadata URL/filename mismatch")
            identity = kind, artifact["board"]
            filenames = [asset, metadata_asset]
        elif kind == "companion":
            if artifact.get("os") not in {"macos", "windows", "linux"} or artifact.get("arch") not in {"arm64", "x86_64"}:
                raise ProtocolError("Unsupported companion platform")
            _integer(artifact.get("size"), 1, MAX_COMPANION_SIZE, "companion size")
            identity = kind, artifact["os"], artifact["arch"]
            filenames = [asset]
        else:
            raise ProtocolError("Unsupported artifact kind")
        if identity in matches or any(name in assets for name in filenames) or len(set(filenames)) != len(filenames):
            raise ProtocolError("Duplicate artifact or filename")
        matches.add(identity)
        assets.update(filenames)
    return manifest


def verify_manifest(raw: bytes, signature: bytes, *, trusted_keys=None) -> dict:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_MANIFEST_SIZE:
        raise ProtocolError("Manifest exceeds size limit")
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Invalid manifest JSON") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("key_id"), str):
        raise ProtocolError("Missing manifest signing key")
    _verify_signature(raw, signature, manifest["key_id"], trusted_keys)
    return validate_manifest(manifest)


def select_artifact(manifest: dict, kind: str, **selectors) -> dict | None:
    """Return exactly one match, or None; caller must have verified the manifest."""
    found = [a for a in manifest["artifacts"] if a["kind"] == kind
             and all(a.get(key) == value for key, value in selectors.items())]
    if len(found) > 1:
        raise ProtocolError("Ambiguous matching artifacts")
    return found[0] if found else None


def verify_artifact(source: bytes | Path | str, artifact: dict) -> None:
    expected_size = _integer(artifact.get("size"), 1, MAX_COMPANION_SIZE, "artifact size")
    expected_digest = _digest(artifact.get("sha256"))
    digest = hashlib.sha256()
    if isinstance(source, bytes):
        if len(source) != expected_size:
            raise ProtocolError("Artifact size mismatch")
        digest.update(source)
    else:
        count = 0
        with Path(source).open("rb") as handle:
            while block := handle.read(1024 * 1024):
                count += len(block)
                if count > expected_size:
                    raise ProtocolError("Artifact larger than signed size")
                digest.update(block)
        if count != expected_size:
            raise ProtocolError("Artifact size mismatch")
    if not hmac.compare_digest(digest.hexdigest(), expected_digest):
        raise ProtocolError("Artifact digest mismatch")


def firmware_image_version(source: bytes | Path | str) -> Version:
    """Read ESP32-S3 app description from a raw application, not merged flash data.

    Image header is 24 bytes, followed by first segment header (8 bytes), then
    esp_app_desc_t: magic/reserved fields (16), version (32). Full ESP validation
    remains the bootloader's responsibility; this check prevents mislabeling.
    """
    if isinstance(source, bytes):
        data = source[:80]
        total = len(source)
    else:
        with Path(source).open("rb") as handle:
            data = handle.read(80)
            handle.seek(0, 2)
            total = handle.tell()
    if len(data) < 80 or data[0] != 0xE9 or not 1 <= data[1] <= 16:
        raise ProtocolError("Not a raw ESP application image")
    # esp_image_header_t.chip_id is at 12; ESP32-S3 is 9.
    if struct.unpack_from("<H", data, 12)[0] != 9:
        raise ProtocolError("Firmware image is not for ESP32-S3")
    segment_size = struct.unpack_from("<I", data, 28)[0]
    if segment_size < 256 or total < 32 + segment_size or struct.unpack_from("<I", data, 32)[0] != 0xABCD5432:
        raise ProtocolError("Invalid ESP app description segment")
    return _version(_unpadded(data[48:80]))


def match_firmware_artifact(metadata: FirmwareMetadata, artifact: dict) -> None:
    expected = (str(metadata.version), metadata.board, metadata.protocol, str(metadata.minimum_companion),
                metadata.image_size, metadata.image_sha256.hex())
    actual = tuple(artifact.get(k) for k in ("version", "board", "protocol", "minimum_companion", "size", "sha256"))
    if actual != expected:
        raise ProtocolError("Firmware metadata disagrees with release manifest")


class OTAState(IntEnum):
    IDLE = 0
    METADATA = 1
    PREPARING = 2
    IMAGE = 3
    VERIFYING = 4
    REBOOTING = 5
    CANCELLED = 6
    ERROR = 7


class OTAError(IntEnum):
    OK = 0
    UNAUTHORIZED = 1
    BUSY = 2
    MALFORMED = 3
    SESSION = 4
    STATE = 5
    OFFSET = 6
    METADATA = 7
    SIGNATURE = 8
    BOARD = 9
    VERSION = 10
    COMPANION = 11
    PROTOCOL = 12
    SIZE = 13
    POWER = 14
    FLASH = 15
    DIGEST = 16
    INCOMPLETE = 17
    TIMEOUT = 18
    DISCONNECTED = 19
    IMAGE = 20


@dataclass(frozen=True)
class OTAStatus:
    state: OTAState
    error: OTAError = OTAError.OK
    session: int = 0
    offset: int = 0
    total: int = 0
    opcode: int = 0
    flags: int = 0

    @property
    def next_offset(self):
        return self.offset

    @property
    def cancellable(self):
        return bool(self.flags & 1)

    @property
    def signature_verified(self):
        return bool(self.flags & 2)

    def encode(self) -> bytes:
        try:
            state, error = OTAState(self.state), OTAError(self.error)
        except ValueError as exc:
            raise ProtocolError("Unknown OTA status enum") from exc
        for value in (self.session, self.offset, self.total):
            _integer(value)
        _integer(self.opcode, 0, 255, "OTA trigger opcode")
        if (self.offset > self.total or (error == OTAError.OK and self.opcode not in [0, *b"MmSFXQd"])
                or type(self.flags) is not int or self.flags & ~3):
            raise ProtocolError("Invalid OTA status fields")
        return _STATUS.pack(b"O", PROTOCOL, state, error, self.session, self.offset,
                            self.total, self.opcode, self.flags, 0)

    @classmethod
    def decode(cls, data: bytes) -> OTAStatus:
        if len(data) != 20:
            raise ProtocolError("OTA status must be exactly 20 bytes")
        magic, protocol, state, error, session, offset, total, opcode, flags, reserved = _STATUS.unpack(data)
        if magic != b"O" or protocol != PROTOCOL or reserved:
            raise ProtocolError("Invalid OTA status header")
        try:
            result = cls(OTAState(state), OTAError(error), session, offset, total, opcode, flags)
        except ValueError as exc:
            raise ProtocolError("Unknown OTA status enum") from exc
        result.encode()
        return result


def payload_limit(value_size=20, *, metadata=False):
    _integer(value_size, 20, 512, "GATT value size")
    return min(value_size, MAX_VALUE_SIZE) - (9 if metadata else 8)


def ota_begin(session, envelope_size, companion_version, *, usb_power=False):
    _integer(session, 1)
    _integer(envelope_size, 170, MAX_ENVELOPE_SIZE, "envelope size")
    if type(usb_power) is not bool:
        raise ProtocolError("USB acknowledgement must be boolean")
    return struct.pack("<cIHIIIB", b"M", session, envelope_size,
                       *_version(companion_version).as_tuple(), int(usb_power))


def ota_fragment(session, offset, payload, *, value_size=20):
    _integer(session, 1)
    _integer(offset)
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= payload_limit(value_size, metadata=True):
        raise ProtocolError("Invalid metadata payload length")
    if offset + len(payload) > MAX_ENVELOPE_SIZE:
        raise ProtocolError("Metadata fragment exceeds envelope bounds")
    return struct.pack("<cII", b"m", session, offset) + payload


def ota_data(session, offset, payload, *, value_size=20):
    _integer(session, 1)
    _integer(offset)
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= payload_limit(value_size):
        raise ProtocolError("Invalid image payload length")
    if offset + len(payload) > 0xFFFFFFFF:
        raise ProtocolError("Image offset overflow")
    return struct.pack("<II", session, offset) + payload


def ota_command(opcode, session):
    _integer(session, 1)
    if isinstance(opcode, str):
        opcode = opcode.encode("ascii")
    if opcode not in (b"S", b"F", b"X", b"Q"):
        raise ProtocolError("Invalid OTA control command")
    return opcode + struct.pack("<I", session)


def registration_body(host_id, name):
    if not isinstance(host_id, str) or not _HOST_RE.fullmatch(host_id):
        raise ProtocolError("Invalid stable host ID")
    if not isinstance(name, str) or not 1 <= len(name) <= 20 or any(not 32 <= ord(c) <= 126 for c in name):
        raise ProtocolError("Device name must contain 1–20 printable ASCII bytes")
    return host_id.encode("ascii") + bytes([len(name)]) + name.encode("ascii")


def registration_begin(session, nonce, total):
    return struct.pack("<cIIH", b"J", _integer(session, 1), _integer(nonce, 1), _integer(total, 38, 57))


def registration_fragment(session, offset, payload, *, value_size=20):
    _integer(session, 1)
    _integer(offset)
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= payload_limit(value_size, metadata=True) or offset + len(payload) > 57:
        raise ProtocolError("Invalid registration fragment")
    return struct.pack("<cII", b"j", session, offset) + payload


def registration_commit(session):
    return struct.pack("<cI", b"K", _integer(session, 1))
