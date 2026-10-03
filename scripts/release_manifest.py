"""Record and verify registry digests of the exact scanned release images."""

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from booking_bot.version import (
    IMAGE_REPOSITORY,
    POSTGRES_IMAGE_REPOSITORY,
    REDIS_IMAGE_REPOSITORY,
    SOCKET_IMAGE_REPOSITORY,
    __version__,
)

SOURCE = "https://github.com/Polen452294/Booking_bot"


def docker(*arguments: str) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    return result.stdout


def verify_published(repository: str, commit: str) -> dict:
    reference = f"{repository}:{__version__}"
    local = json.loads(docker("image", "inspect", reference))[0]
    labels = local["Config"].get("Labels") or {}
    expected = {"version": __version__, "revision": commit, "source": SOURCE}
    if any(
        labels.get(f"org.opencontainers.image.{key}") != value for key, value in expected.items()
    ):
        raise ValueError("Published image OCI metadata does not match release")
    digests = [
        value for value in local.get("RepoDigests", []) if value.startswith(repository + "@sha256:")
    ]
    if len(digests) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", digests[0].split("@")[1]):
        raise ValueError("One exact registry digest is required for each release image")
    docker("pull", digests[0])
    remote = json.loads(docker("image", "inspect", digests[0]))[0]
    if remote["Id"] != local["Id"]:
        raise ValueError("Registry artifact differs from the scanned local image")
    return {
        "image": reference,
        "image_digest": digests[0].split("@")[1],
        "immutable_reference": digests[0],
        "image_id": local["Id"],
    }


def manifest(commit: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Release requires a full Git commit SHA")
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    if len(heads) != 1:
        raise ValueError("Release requires one Alembic head")
    images = {
        repository.rsplit("/", 1)[1]: verify_published(repository, commit)
        for repository in (
            IMAGE_REPOSITORY,
            SOCKET_IMAGE_REPOSITORY,
            POSTGRES_IMAGE_REPOSITORY,
            REDIS_IMAGE_REPOSITORY,
        )
    }
    return {
        "version": __version__,
        "git_commit": commit,
        "source": SOURCE,
        **images["booking-bot"],
        "database_head": heads[0],
        "target_os": "Ubuntu Server 24.04 LTS x86_64",
        "published_at_utc": datetime.now(UTC).isoformat(),
        "images": images,
        "production_qualification": "pending separate published-image acceptance",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--notes", type=Path)
    args = parser.parse_args()
    record = manifest(args.commit)
    args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if args.notes:
        with args.notes.open("a", encoding="utf-8") as stream:
            stream.write(
                f"\n### Verified artifact\n\n"
                f"- Image: `{record['image']}`\n"
                f"- Immutable reference: `{record['immutable_reference']}`\n"
                f"- Git commit: `{record['git_commit']}`\n"
                f"- Alembic head: `{record['database_head']}`\n"
                f"- Target OS: {record['target_os']}\n"
                f"- Installation: [{__version__} documentation]"
                f"({SOURCE}/blob/v{__version__}/docs/new-client.md)\n"
                "\nPublication alone does not close the production qualification gate.\n"
            )
