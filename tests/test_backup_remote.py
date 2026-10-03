import hashlib
import io
from unittest.mock import Mock

import pytest

from booking_bot.deployment.backup import ALL_FILES, BackupManager
from booking_bot.deployment.backup_remote import S3Storage
from booking_bot.deployment.files import DeploymentError
from test_backups import backups  # noqa: F401
from test_bookingctl import manager  # noqa: F401


class Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


@pytest.fixture
def remote():
    storage = object.__new__(S3Storage)
    storage.bucket, storage.prefix, storage.encryption = "private", "booking", "AES256"
    objects = {}
    client = Mock()
    client.get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": {
            name: True
            for name in (
                "BlockPublicAcls",
                "IgnorePublicAcls",
                "BlockPublicPolicy",
                "RestrictPublicBuckets",
            )
        }
    }

    def upload_file(path, bucket, key, ExtraArgs):
        with open(path, "rb") as stream:
            objects[key] = stream.read(), ExtraArgs["ServerSideEncryption"]

    def get_object(Bucket, Key):
        value, encryption = objects[Key]
        return {"Body": Body(value), "ServerSideEncryption": encryption}

    client.upload_file.side_effect = upload_file
    client.get_object.side_effect = get_object
    storage.client = client
    return storage, objects


def test_upload_readback_and_pull(backups, remote):  # noqa: F811
    storage, objects = remote
    backup_id = backups.create("alice")
    storage.upload(backups, "alice", backup_id)
    assert len(objects) == len(ALL_FILES)
    pulled = BackupManager(backups.manager, backups.root.with_name("pulled"))
    storage.pull(pulled, "alice", backup_id)
    assert pulled.verify("alice", backup_id) == backups.verify("alice", backup_id)
    assert storage.client.upload_file.call_args_list[-1].args[2].endswith("SHA256SUMS")
    for name in ALL_FILES:
        assert (pulled.directory("alice", backup_id) / name).read_bytes() == (
            backups.directory("alice", backup_id) / name
        ).read_bytes()


def test_private_bucket_required(remote):
    storage, _ = remote
    storage.client.get_public_access_block.return_value["PublicAccessBlockConfiguration"][
        "BlockPublicPolicy"
    ] = False
    with pytest.raises(DeploymentError, match="public access"):
        storage.check_private()


def test_remote_corruption_keeps_local(backups, remote):  # noqa: F811
    storage, _ = remote
    backup_id = backups.create("alice")
    storage.client.get_object.side_effect = lambda **kw: {
        "Body": Body(b"corrupt"),
        "ServerSideEncryption": "AES256",
    }
    with pytest.raises(DeploymentError, match="local backup preserved"):
        storage.upload(backups, "alice", backup_id)
    assert backups.verify("alice", backup_id)


def test_unencrypted_object_refused(remote):
    storage, objects = remote
    objects[storage.key("alice", "id", "file")] = b"data", None
    with pytest.raises(DeploymentError, match="encryption"):
        storage.read("alice", "id", "file")


def test_pull_corruption_never_published(backups, remote):  # noqa: F811
    storage, objects = remote
    backup_id = backups.create("alice")
    storage.upload(backups, "alice", backup_id)
    key = storage.key("alice", backup_id, "database.dump")
    objects[key] = b"PGDMPcorrupt", "AES256"
    pulled = BackupManager(backups.manager, backups.root.with_name("pulled"))
    with pytest.raises(DeploymentError, match="pull failed"):
        storage.pull(pulled, "alice", backup_id)
    assert pulled.list("alice") == []


def test_stream_read_hash(remote):
    storage, objects = remote
    data = b"abcdef" * 400000
    objects[storage.key("alice", "id", "file")] = data, "AES256"
    stream = io.BytesIO()
    assert storage.read("alice", "id", "file", stream) == hashlib.sha256(data).hexdigest()
    assert stream.getvalue() == data
