import json
import runpy
from pathlib import Path

import pytest

from booking_bot.version import IMAGE_REPOSITORY, __version__

script = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/release_manifest.py"))
verify = script["verify_published"]
COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def published(monkeypatch):
    image = {
        "Id": "sha256:" + "c" * 64,
        "Config": {
            "Labels": {
                "org.opencontainers.image.version": __version__,
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.source": script["SOURCE"],
            }
        },
        "RepoDigests": [f"{IMAGE_REPOSITORY}@{DIGEST}"],
    }
    remote = {"Id": image["Id"]}
    calls = []

    def docker(*args):
        calls.append(args)
        return json.dumps([image if args[-1].endswith(f":{__version__}") else remote])

    monkeypatch.setitem(verify.__globals__, "docker", docker)
    return image, remote, calls


def test_registry_pull_verifies_exact_scanned_artifact(published):
    _, _, calls = published
    record = verify(IMAGE_REPOSITORY, COMMIT)
    assert record["immutable_reference"] == f"{IMAGE_REPOSITORY}@{DIGEST}"
    assert ("pull", record["immutable_reference"]) in calls


def test_registry_image_substitution_blocks_release(published):
    _, remote, _ = published
    remote["Id"] = "sha256:" + "d" * 64
    with pytest.raises(ValueError, match="differs"):
        verify(IMAGE_REPOSITORY, COMMIT)


@pytest.mark.parametrize("field", ["version", "revision", "source"])
def test_wrong_provenance_blocks_release(published, field):
    local, _, _ = published
    local["Config"]["Labels"][f"org.opencontainers.image.{field}"] = "wrong"
    with pytest.raises(ValueError, match="metadata"):
        verify(IMAGE_REPOSITORY, COMMIT)


@pytest.mark.parametrize(
    "digests", [[], ["other@sha256:" + "b" * 64], [f"{IMAGE_REPOSITORY}@sha256:invalid"]]
)
def test_missing_or_foreign_digest_blocks_release(published, digests):
    local, _, _ = published
    local["RepoDigests"] = digests
    with pytest.raises(ValueError, match="digest"):
        verify(IMAGE_REPOSITORY, COMMIT)
