"""Prevent full-tag reuse, including the first package publication. Never log credentials."""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from booking_bot.version import (
    IMAGE_REPOSITORY,
    POSTGRES_IMAGE_REPOSITORY,
    REDIS_IMAGE_REPOSITORY,
    SOCKET_IMAGE_REPOSITORY,
    __version__,
)

ENDPOINT = "https://api.github.com/users/polen452294/packages/container/booking-bot/versions"


def verify_unused(token: str, *, socket: bool = False, repository: str | None = None) -> None:
    if not token:
        raise ValueError("Release package authentication is required")
    repository = repository or (SOCKET_IMAGE_REPOSITORY if socket else IMAGE_REPOSITORY)
    if repository not in {
        IMAGE_REPOSITORY,
        SOCKET_IMAGE_REPOSITORY,
        POSTGRES_IMAGE_REPOSITORY,
        REDIS_IMAGE_REPOSITORY,
    }:
        raise ValueError("Unknown release repository")
    package = repository.rsplit("/", 1)[1]
    manifest = subprocess.run(
        [
            "docker",
            "manifest",
            "inspect",
            f"{repository}:{__version__}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if manifest.returncode == 0:
        raise ValueError("Full release tag already exists; choose a new patch version")
    if not any(
        message in manifest.stderr.lower()
        for message in ("manifest unknown", "name unknown", "no such manifest", "denied")
    ):
        raise ValueError("Registry availability unknown; publication refused")
    page = 1
    while True:
        request = urllib.request.Request(
            f"{ENDPOINT.replace('/booking-bot/', '/' + package + '/')}?per_page=100&page={page}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "Booking-bot-release",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                versions = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and page == 1:
                # No visible package yet: this token cannot overwrite an invisible package.
                # Push still needs packages:write; denied access will fail publication.
                return
            raise ValueError("Cannot verify package versions; publication refused") from None
        if not isinstance(versions, list):
            raise ValueError("Invalid package response; publication refused")
        for item in versions:
            tags = item["metadata"]["container"]["tags"]
            if __version__ in tags:
                raise ValueError("Full release tag already exists; choose a new patch version")
        if len(versions) < 100:
            return
        page += 1


if __name__ == "__main__":
    try:
        verify_unused(os.environ.get("GH_TOKEN", ""))
        verify_unused(os.environ.get("GH_TOKEN", ""), socket=True)
        verify_unused(os.environ.get("GH_TOKEN", ""), repository=POSTGRES_IMAGE_REPOSITORY)
        verify_unused(os.environ.get("GH_TOKEN", ""), repository=REDIS_IMAGE_REPOSITORY)
    except Exception:
        # Avoid printing HTTP headers, credential-bearing Request objects or exception chains.
        print(
            "Full tag is already used or availability is unknown; publication refused",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print("Full release tag is unused; publication may proceed")
