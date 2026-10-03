"""Single source for release/package version; stable and numbered RC releases."""

import re

__version__ = "1.0.0-rc.1"
IMAGE_REPOSITORY = "ghcr.io/polen452294/booking-bot"
SOCKET_IMAGE_REPOSITORY = "ghcr.io/polen452294/booking-socket-proxy"
POSTGRES_IMAGE_REPOSITORY = "ghcr.io/polen452294/booking-postgres"
REDIS_IMAGE_REPOSITORY = "ghcr.io/polen452294/booking-redis"


def parse_version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError("Use a stable Semantic Version: MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in value.split("."))


def parse_current_version(value: str) -> tuple[tuple[int, int, int], bool]:
    """Existing Phase images may use proper SemVer prereleases such as 0.5.0-phase5."""
    match = re.fullmatch(
        r"(\d+\.\d+\.\d+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        value,
    )
    if not match:
        raise ValueError("Current image needs valid Semantic Version metadata")
    core = parse_version(match[1])
    if match[2] and any(
        part.isdigit() and len(part) > 1 and part[0] == "0" for part in match[2].split(".")
    ):
        raise ValueError("Numeric prerelease identifiers cannot contain leading zeros")
    return core, bool(match[2])


def parse_release_version(value: str) -> tuple[int, int, int]:
    """Targets are stable SemVer or canonical rc.N; never aliases/build metadata."""
    if not isinstance(value, str):
        raise ValueError("Use MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-rc.N (N >= 1)")
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-rc\.([1-9][0-9]*))?", value)
    if not match:
        raise ValueError("Use MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-rc.N (N >= 1)")
    return parse_version(match[1])


def version_order(value: str) -> tuple:
    """SemVer precedence, including existing phase releases; ignore build metadata."""
    core, prerelease = parse_current_version(value)
    if not prerelease:
        return core, (1,)
    identifiers = value.split("+", 1)[0].split("-", 1)[1].split(".")
    return core, (0, tuple((0, int(p)) if p.isdigit() else (1, p) for p in identifiers))
