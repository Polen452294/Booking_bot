"""Real Docker acceptance; new isolated clients only, synthetic tokens, no Telegram messages.

Builds a next-patch image with a real additive test migration, pushes/pulls a disposable
localhost registry, backs up business rows, updates two clients and exercises failure/restore.
Only Telegram getMe and an injected post-start failure are stubbed.
An additional candidate with an actually broken API entrypoint verifies real health failure.
"""

import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from booking_bot.deployment import releases as release_module
from booking_bot.deployment.files import DeploymentError
from booking_bot.deployment.manager import BotIdentity, CreateRequest, DeploymentManager, run_docker
from booking_bot.deployment.releases import ReleaseManager, inspect_image
from booking_bot.specialist_config import load_specialist_template
from booking_bot.version import parse_release_version
from disaster_recovery_smoke import SEED, snapshot

MIGRATION = '''"""Additive smoke migration, never shipped to production."""
from alembic import op
import sqlalchemy as sa
revision = "phase6_smoke"
down_revision = "c75a01d29f10"
branch_labels = None
depends_on = None
def upgrade():
    op.create_table("phase6_smoke_marker", sa.Column("id", sa.Integer, primary_key=True))
def downgrade():
    raise RuntimeError("Acceptance never downgrades a database")
'''


def business_snapshot(backups, slug):
    rows = snapshot(backups, slug)
    rows.pop("alembic_version")
    return rows


def build_candidate(context, base, version, revision, *, broken=False):
    """Reuse the qualified runtime/locks; only package + test migration change."""
    version_file = context / "src/booking_bot/version.py"
    version_file.write_text(
        re.sub(
            r'^__version__ = ".*"',
            f'__version__ = "{version}"',
            version_file.read_text(encoding="utf-8"),
            flags=re.M,
        ),
        encoding="utf-8",
    )
    if broken:
        (context / "src/booking_bot/server.py").write_text(
            '"""Intentionally broken test release, never published."""\nraise SystemExit(42)\n',
            encoding="utf-8",
        )
    (context / "Dockerfile").write_text(
        f"FROM {base}\nUSER 0:0\nARG APP_VERSION\nARG VCS_REF\n"
        "LABEL org.opencontainers.image.version=$APP_VERSION "
        "org.opencontainers.image.revision=$VCS_REF\n"
        "WORKDIR /app\nCOPY src ./src\nCOPY pyproject.toml README.md ./\n"
        "RUN python -m pip install --no-deps --no-build-isolation .\nUSER 10001:10001\n",
        encoding="utf-8",
    )
    tag = "booking-bot:qualification-" + secrets.token_hex(4)
    run_docker(
        [
            "build",
            "--build-arg",
            f"APP_VERSION={version}",
            "--build-arg",
            f"VCS_REF={revision}",
            "-t",
            tag,
            str(context),
        ],
        timeout=900,
    )
    return tag


def smoke(root: Path, image: str, network_pool: str | None = None):
    if root.exists():
        raise RuntimeError("Use a NEW disposable test registry directory")
    source = Path(__file__).resolve().parents[1]
    version_a = inspect_image(image)["version"]
    major, minor, patch_number = parse_release_version(version_a)
    version_b = (
        f"{major}.{minor}.{patch_number}-rc.{int(version_a.rsplit('.', 1)[1]) + 1}"
        if "-rc." in version_a
        else f"{major}.{minor}.{patch_number + 1}"
    )
    build_context = root.with_name(root.name + "-build")
    if build_context.exists():
        raise RuntimeError("Use a NEW disposable image build directory")
    build_context.mkdir(parents=True)
    for name in (
        "Dockerfile",
        "pyproject.toml",
        "README.md",
        "alembic.ini",
        "requirements.lock",
        "requirements-build.lock",
        ".dockerignore",
    ):
        shutil.copy2(source / name, build_context / name)
    shutil.copytree(
        source / "src", build_context / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    version_file = build_context / "src/booking_bot/version.py"
    version_file.write_text(
        re.sub(
            r'^__version__ = ".*"',
            f'__version__ = "{version_b}"',
            version_file.read_text(encoding="utf-8"),
            flags=re.M,
        ),
        encoding="utf-8",
    )
    migration_dir = build_context / "src/booking_bot/db/migrations/versions"
    (migration_dir / "phase6_smoke.py").write_text(MIGRATION, encoding="utf-8")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    built = build_candidate(build_context, image, version_b, revision)
    registry_name = "booking-phase6-registry-" + secrets.token_hex(4)
    vm_registry = os.name == "nt"
    port = str(40000 + secrets.randbelow(20000)) if vm_registry else ""
    registry_args = (
        [
            "--network",
            "host",
            "-e",
            f"REGISTRY_HTTP_ADDR=127.0.0.1:{port}",
            "-e",
            "REGISTRY_HTTP_DEBUG_ADDR=127.0.0.1:0",
        ]
        if vm_registry
        else ["-p", "127.0.0.1::5000"]
    )
    run_docker(["run", "-d", "--name", registry_name, *registry_args, "registry:3"], timeout=120)
    try:
        if not vm_registry:
            port = json.loads(run_docker(["inspect", registry_name]))[0]["NetworkSettings"][
                "Ports"
            ]["5000/tcp"][0]["HostPort"]
        origin = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                if vm_registry:
                    run_docker(
                        ["exec", registry_name, "wget", "-q", "-O", "-", f"{origin}/v2/"], timeout=3
                    )
                else:
                    with urllib.request.urlopen(f"{origin}/v2/", timeout=2):
                        pass
                break
            except (DeploymentError, OSError):
                time.sleep(0.25)
        else:
            raise RuntimeError("Disposable registry did not become ready")
        repository = f"127.0.0.1:{port}/booking-bot"
        target = f"{repository}:{version_b}"
        run_docker(["tag", built, target])
        run_docker(["push", target], timeout=600)
        manager = DeploymentManager(root)
        if network_pool:
            pool = ipaddress.ip_network(network_pool)
            if pool.version != 4 or pool.prefixlen != 22 or not pool.is_private:
                raise ValueError("Test network pool must be a private IPv4 /22")
            subnets = iter(pool.subnets(new_prefix=24))
            provision = manager.provision

            def test_provision(slug, state):
                for name in ("data", "egress"):
                    run_docker(
                        [
                            "network",
                            "create",
                            "--subnet",
                            str(next(subnets)),
                            "--label",
                            f"com.docker.compose.project={state['project']}",
                            "--label",
                            f"com.docker.compose.network={name}",
                            *(["--internal"] if name == "data" else []),
                            f"{state['project']}_{name}",
                        ]
                    )
                return provision(slug, state)

            manager.provision = test_provision
        releases = ReleaseManager(manager, root.with_name(root.name + "-backups"))
        backups = releases.backups
        template = load_specialist_template(source / "specialist.toml")
        slugs = ("release-a", "release-b")
        before = {}
        for index, slug in enumerate(slugs):
            bot_id = 944444440 + index
            request = CreateRequest(
                replace(template, profile=replace(template.profile, slug=slug)),
                f"{bot_id}:{secrets.token_urlsafe(36)}",
                image,
            )
            with patch(
                "booking_bot.deployment.manager.get_bot_identity",
                return_value=BotIdentity(bot_id, f"phase6_{index}_bot"),
            ):
                manager.create(slug, request)
            manager.compose(slug, "run", "--rm", "--no-deps", "-T", "admin", "python", "-c", SEED)
            assert manager.doctor(slug)["ok"]
            before[slug] = business_snapshot(backups, slug)
        ids_b = manager.compose(slugs[1], "ps", "-q")
        real_docker = release_module.run_docker

        def docker(args, **kwargs):
            if args[:2] == ["manifest", "inspect"]:
                if vm_registry:
                    # Windows and Docker VM loopback differ; read the REAL registry manifest.
                    # Push/pull still use the real daemon and registry over HTTP.
                    return real_docker(
                        [
                            "exec",
                            registry_name,
                            "wget",
                            "-q",
                            "-O",
                            "-",
                            "--header",
                            "Accept: application/vnd.oci.image.index.v1+json, "
                            "application/vnd.docker.distribution.manifest.list.v2+json",
                            f"{origin}/v2/booking-bot/manifests/{args[-1].rsplit(':', 1)[-1]}",
                        ],
                        **kwargs,
                    )
                args = [*args[:2], "--insecure", *args[2:]]
            return real_docker(args, **kwargs)

        with (
            patch.object(release_module, "IMAGE_REPOSITORY", repository),
            patch.object(release_module, "run_docker", docker),
        ):
            plan = releases.update(slugs[0], version_b, dry_run=True)
            assert plan["current_version"] == version_a
            validate = releases.validate

            def validate_with_peer(slug):
                # Check B's API, worker, DB/Redis DURING A's validation, before confirming A.
                if slug == slugs[0]:
                    assert manager.doctor(slugs[1])["ok"]
                    assert manager.compose(slugs[1], "ps", "-q") == ids_b
                    assert business_snapshot(backups, slugs[1]) == before[slugs[1]]
                validate(slug)

            releases.validate = validate_with_peer
            event = releases.update(slugs[0], version_b)
            manifest = backups.verify(slugs[0], event["backup_id"])
            assert manifest["application_version"] == version_a
            assert manifest["alembic_revision"] == "c75a01d29f10"
            assert business_snapshot(backups, slugs[0]) == before[slugs[0]]
            assert (
                backups.sql(slugs[0], "SELECT version_num FROM alembic_version") == "phase6_smoke"
            )
            assert manager.doctor(slugs[0])["ok"]
            releases.update(slugs[1], version_b)
            assert business_snapshot(backups, slugs[1]) == before[slugs[1]]
            assert manager.doctor(slugs[1])["ok"]
            print(
                "TWO-CLIENT ROLLOUT PASSED: backup, real pull/migration, doctor, peer isolation",
                flush=True,
            )
            releases.validate = validate

            # Changed schema cannot be recovered by changing the application image alone.
            try:
                releases.rollback(slugs[0])
            except DeploymentError as error:
                assert "Database restore required" in str(error)
            else:
                raise AssertionError("Unsafe image-only rollback was allowed")
            releases.rollback(slugs[0], restore_database=True, yes=True)
            assert business_snapshot(backups, slugs[0]) == before[slugs[0]]
            assert manager.doctor(slugs[0])["ok"]

            def broken_validation(slug):
                # Start the actual target; then inject a deterministic readiness failure.
                manager.compose(
                    slug,
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "api",
                    "worker",
                )
                raise DeploymentError("Injected test readiness failure")

            releases.validate = broken_validation
            ids_b = manager.compose(slugs[1], "ps", "-q")
            try:
                releases.update_all(version_b)
            except DeploymentError:
                state = manager.state(slugs[0])
                assert state["status"] == "FAILED" and state["current_version"] == version_a
                attempt = state["release_attempt"]
                assert backups.verify(slugs[0], attempt["backup_id"])
                assert attempt["recovery"] == "database restore required"
                assert manager.compose(slugs[1], "ps", "-q") == ids_b
                assert manager.doctor(slugs[1])["ok"]
                assert business_snapshot(backups, slugs[1]) == before[slugs[1]]
                from booking_bot.deployment.alerts import reconcile
                from booking_bot.deployment.monitoring import client_report

                alert_messages = []

                def capture(message):
                    alert_messages.append(message)
                    return True

                incident = client_report(manager, slugs[0], backups.root, telegram=False)
                assert not incident["ok"]
                alert_path = root.with_name(root.name + "-alerts")
                assert reconcile(alert_path, [incident], sender=capture)["sent"] == 1
                assert reconcile(alert_path, [incident], sender=capture)["sent"] == 0
            else:
                raise AssertionError("Failed rollout was accepted")
            print(
                "FAILED UPDATE PASSED: backup retained, FAILED, rollout stopped, peer healthy",
                flush=True,
            )
            releases.validate = validate
            releases.rollback(slugs[0], restore_database=True, yes=True)
            assert business_snapshot(backups, slugs[0]) == before[slugs[0]]
            assert manager.doctor(slugs[0])["ok"]
            recovered = client_report(manager, slugs[0], backups.root, telegram=False)
            assert recovered["ok"], recovered
            assert reconcile(alert_path, [recovered], sender=capture)["sent"] == 1
            assert "RECOVERED" in alert_messages[-1]
            print(
                "RECOVERY PASSED: verified safety backup, exact old image/DB, no downgrade",
                flush=True,
            )
            # A separate immutable test version fails in the actual API process,
            # rather than relying only on the injected validation exception above.
            broken_version = (
                f"{major}.{minor}.{patch_number}-rc.{int(version_b.rsplit('.', 1)[1]) + 1}"
                if "-rc." in version_b
                else f"{major}.{minor}.{patch_number + 2}"
            )
            broken_image = build_candidate(
                build_context,
                image,
                broken_version,
                revision,
                broken=True,
            )
            broken_target = f"{repository}:{broken_version}"
            run_docker(["tag", broken_image, broken_target])
            run_docker(["push", broken_target], timeout=600)
            peer_ids = manager.compose(slugs[1], "ps", "-q")
            try:
                releases.update(slugs[0], broken_version)
            except DeploymentError:
                failed = manager.state(slugs[0])
                assert failed["status"] == "FAILED" and failed["current_version"] == version_a
                assert backups.verify(slugs[0], failed["release_attempt"]["backup_id"])
                assert manager.compose(slugs[1], "ps", "-q") == peer_ids
                assert manager.doctor(slugs[1])["ok"]
                assert business_snapshot(backups, slugs[1]) == before[slugs[1]]
            else:
                raise AssertionError("Actually broken API image was accepted")
            releases.rollback(slugs[0], restore_database=True, yes=True)
            assert business_snapshot(backups, slugs[0]) == before[slugs[0]]
            assert manager.doctor(slugs[0])["ok"]
            print(
                "BROKEN IMAGE RECOVERY PASSED: real API failure, backup, FAILED, peer preserved",
                flush=True,
            )
    finally:
        # Exact container created by this test only; client data stays for diagnosis.
        run_docker(["rm", "-f", registry_name])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--network-pool", help="Optional unused private IPv4 /22 for tests")
    args = parser.parse_args()
    smoke(args.root.absolute(), args.image, args.network_pool)
