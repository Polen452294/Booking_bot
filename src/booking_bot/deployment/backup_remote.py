"""Optional private S3 storage with mandatory SSE and read-back SHA256 verification."""

import hashlib
import os

from booking_bot.deployment.backup import ALL_FILES, digest, verify_files
from booking_bot.deployment.files import DeploymentError, private_directory, registry_lock


class S3Storage:
    def __init__(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise DeploymentError("Install bookingctl with the [backup-s3] extra") from None
        self.bucket = os.environ.get("BOOKING_BACKUP_S3_BUCKET", "")
        self.prefix = os.environ.get("BOOKING_BACKUP_S3_PREFIX", "booking").strip("/")
        self.encryption = os.environ.get("BOOKING_BACKUP_S3_SSE", "AES256")
        endpoint = os.environ.get("BOOKING_BACKUP_S3_ENDPOINT")
        if not self.bucket or self.encryption not in {"AES256", "aws:kms"}:
            raise DeploymentError("Private S3 bucket and supported SSE required")
        if endpoint and not endpoint.startswith("https://"):
            raise DeploymentError("S3 endpoint must use HTTPS")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            config=Config(
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 3},
                s3={"addressing_style": "path"},
            ),
        )

    def key(self, slug, backup_id, name):
        return f"{self.prefix}/{slug}/{backup_id}/{name}"

    def check_private(self):
        # No silent fallback: unsupported providers must supply equivalent private policy checks
        # in a future adapter. An operator assertion alone cannot guarantee privacy.
        result = self.client.get_public_access_block(Bucket=self.bucket)
        flags = result["PublicAccessBlockConfiguration"]
        if not all(
            flags.get(key)
            for key in (
                "BlockPublicAcls",
                "IgnorePublicAcls",
                "BlockPublicPolicy",
                "RestrictPublicBuckets",
            )
        ):
            raise DeploymentError("S3 public access must be fully blocked")

    def read(self, slug, backup_id, name, stream=None):
        response = self.client.get_object(Bucket=self.bucket, Key=self.key(slug, backup_id, name))
        if response.get("ServerSideEncryption") not in {"AES256", "aws:kms"}:
            response["Body"].close()
            raise DeploymentError("Remote object lacks server-side encryption")
        checksum = hashlib.sha256()
        with response["Body"] as body:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                checksum.update(chunk)
                if stream is not None:
                    stream.write(chunk)
        return checksum.hexdigest()

    def upload(self, backups, slug, backup_id):
        with registry_lock(backups.manager.root):
            self._upload(backups, slug, backup_id)

    def _upload(self, backups, slug, backup_id):
        """Caller holds the common operation/registry lock."""
        backups.verify(slug, backup_id)
        self.check_private()
        path = backups.directory(slug, backup_id)
        backups.event(slug, "remote upload started", backup_id)
        try:
            for name in ALL_FILES:  # SHA256SUMS last is the remote commit marker.
                extra = {"ServerSideEncryption": self.encryption}
                if self.encryption == "aws:kms" and os.environ.get("BOOKING_BACKUP_S3_KMS_KEY"):
                    extra["SSEKMSKeyId"] = os.environ["BOOKING_BACKUP_S3_KMS_KEY"]
                self.client.upload_file(
                    str(path / name),
                    self.bucket,
                    self.key(slug, backup_id, name),
                    ExtraArgs=extra,
                )
                if self.read(slug, backup_id, name) != digest(path / name):
                    raise DeploymentError("S3 read-back checksum failed")
            backups.event(slug, "remote upload completed", backup_id)
        except Exception:
            backups.event(slug, "remote upload failed", backup_id)
            raise DeploymentError("S3 upload/verification failed; local backup preserved") from None

    def pull(self, backups, slug, backup_id):
        with registry_lock(backups.manager.root):
            target = backups.directory(slug, backup_id)
            if target.exists():
                raise DeploymentError("Local backup already exists; verify it instead")
            self.check_private()
            private_directory(backups.root)
            private_directory(backups.directory(slug))
            staging = target.with_name(".pull-" + backup_id)
            if staging.exists():
                raise DeploymentError("Previous incomplete pull exists; inspect it first")
            private_directory(staging)
            try:
                for name in ALL_FILES:
                    with (staging / name).open("xb") as stream:
                        if os.name != "nt":
                            (staging / name).chmod(0o600)
                        self.read(slug, backup_id, name, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                verify_files(staging, slug)
                backups._verify_path(slug, staging)
                staging.rename(target)
                backups.event(slug, "remote pull completed", backup_id)
            except Exception:
                raise DeploymentError(
                    "S3 pull failed; incomplete files preserved, no restore"
                ) from None
