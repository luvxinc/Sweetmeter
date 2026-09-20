"""One numeric calendar version, shared by source and packaged builds."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys

_VERSION_RE = re.compile(r"([1-9][0-9]{3})\.([1-9]|1[0-2])\.([1-9][0-9]*)", re.ASCII)


@dataclass(frozen=True, order=True)
class Version:
    year: int
    month: int
    sequence: int

    def __post_init__(self):
        if any(type(v) is not int for v in (self.year, self.month, self.sequence)):
            raise ValueError("Version components must be integers")
        if not (1000 <= self.year <= 9999 and 1 <= self.month <= 12
                and 1 <= self.sequence <= 0xFFFFFFFF):
            raise ValueError("Version components are outside protocol bounds")

    @classmethod
    def parse(cls, value: str | Version) -> Version:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or len(value) > 18:
            raise ValueError("Invalid YYYY.M.N version")
        match = _VERSION_RE.fullmatch(value)
        if not match:
            raise ValueError("Invalid YYYY.M.N version")
        return cls(*(int(component) for component in match.groups()))

    def __str__(self) -> str:
        return f"{self.year}.{self.month}.{self.sequence}"

    def as_tuple(self) -> tuple[int, int, int]:
        return self.year, self.month, self.sequence


def get_version() -> str:
    """Fail closed when the single source/bundled VERSION resource is absent."""
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    raw = (root / "VERSION").read_text(encoding="ascii")
    if raw.endswith("\n"):
        raw = raw[:-1]
    return str(Version.parse(raw))
