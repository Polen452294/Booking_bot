from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from booking_bot.deployment.files import (
    DeploymentError,
    atomic_write,
    private_directory,
    read_json,
    registry_lock,
)
from booking_bot.deployment.templates import compose_model, legacy_compose_model, render_specialist
from booking_bot.specialist_config import SpecialistTemplate, validate_specialist_template

SERVICES = {"postgres", "redis", "api", "worker"}
SECRET_KEYS = {
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_HEADER_SECRET",
}


class DockerCommandError(DeploymentError):
    def __init__(self, output: str):
        super().__init__("Docker operation failed; check daemon, image, status and logs")
        self.output = output


def validate_template(template: SpecialistTemplate) -> None:
    try:
        validate_specialist_template(template)
        if not template.location.address.strip():
            raise ValueError
    except (ValueError, ZoneInfoNotFoundError):
        raise DeploymentError(
            "Invalid specialist configuration; check profile, timezone and address"
        ) from None


def validate_slug(slug: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", slug) or len(slug) > 40:
        raise DeploymentError("Slug: 1–40 lowercase ASCII letters/digits, single internal hyphens")
    if slug in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }:
        raise DeploymentError("Slug is reserved by the operating system")
    return slug


def validate_image(image: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", image):
        return image
    match = re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*:([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})", image)
    if not match or match[1].lower() in {"latest", "stable", "main", "master", "dev"}:
        raise DeploymentError("Use an explicit release image tag or repository@sha256 digest")
    return image


def validate_domain(domain: str) -> str:
    if not domain:
        return ""
    if (
        len(domain) > 253
        or "." not in domain
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        )
    ):
        raise DeploymentError("Domain must be a lowercase DNS hostname, without URL/path/port")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise DeploymentError("Use a DNS hostname, not an IP address")
    return domain


@dataclass(frozen=True)
class BotIdentity:
    id: int
    username: str


def get_bot_identity(token: str) -> BotIdentity:
    if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,128}", token):
        raise DeploymentError("Telegram token has an invalid format")
    # Fixed HTTPS endpoint; no test endpoint option that could redirect real credentials.
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        result = data["result"]
        if (
            data.get("ok") is not True
            or result.get("is_bot") is not True
            or type(result.get("id")) is not int
            or result["id"] != int(token.split(":")[0])
            or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", result.get("username", ""))
        ):
            raise ValueError
        return BotIdentity(result["id"], result["username"])
    except urllib.error.HTTPError as error:
        if error.code in {401, 404}:
            raise DeploymentError("Telegram getMe rejected the token") from None
        raise DeploymentError(
            "Telegram getMe failed; check network/rate limits and retry"
        ) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise DeploymentError(
            "Telegram getMe unavailable or invalid response; check token/network"
        ) from None


def docker_environment(values: dict[str, str] | None = None) -> dict[str, str]:
    # Compose interpolation must not inherit another client's variables or override files.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("COMPOSE_", "BOOKING_", "TELEGRAM_", "POSTGRES_", "REDIS_"))
    }
    environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    environment["COMPOSE_ANSI"] = "never"
    environment.update(values or {})
    return environment


def run_docker(
    arguments: list[str],
    *,
    values: dict[str, str] | None = None,
    timeout: int = 240,
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=docker_environment(values),
            timeout=timeout,
            input=input_text,
        )
    except FileNotFoundError:
        raise DeploymentError("Docker CLI not found; install Docker and Compose v2+") from None
    except subprocess.TimeoutExpired:
        raise DeploymentError(
            "Docker operation timed out; inspect status/logs before retry"
        ) from None
    if result.returncode:
        # Docker/Compose errors can contain fully expanded environment and connection URLs.
        raise DockerCommandError(result.stdout + result.stderr)
    return result.stdout


def docker_preflight() -> None:
    if run_docker(["info", "--format", "{{.OSType}}"], timeout=20).strip() != "linux":
        raise DeploymentError("Linux Docker containers are required")
    run_docker(["compose", "version", "--short"], timeout=20)


def resolve_image(reference: str) -> str:
    try:
        output = run_docker(["image", "inspect", reference, "--format", "{{.Id}}"], timeout=20)
    except DeploymentError:
        run_docker(["pull", reference], timeout=600)
        output = run_docker(["image", "inspect", reference, "--format", "{{.Id}}"], timeout=20)
    image_id = output.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise DeploymentError("Cannot resolve immutable local image ID")
    return image_id


@dataclass
class CreateRequest:
    template: SpecialistTemplate
    token: str = field(repr=False)
    image: str
    domain: str = ""


class DeploymentManager:
    def __init__(self, root: Path):
        self.root = root.expanduser().absolute()

    def directory(self, slug: str) -> Path:
        path = self.root / validate_slug(slug)
        if path.is_symlink() or path.is_junction():
            raise DeploymentError("Deployment directory must not be a link")
        return path

    def state(self, slug: str) -> dict:
        path = self.directory(slug)
        if not path.exists():
            raise DeploymentError("Deployment does not exist")
        state = read_json(path / "state.json")
        if (
            not all(
                isinstance(state.get(key), str)
                for key in (
                    "slug",
                    "status",
                    "stage",
                    "project",
                    "image_id",
                    "image_reference",
                    "bot_username",
                )
            )
            or state.get("slug") != slug
            or state.get("status")
            not in {"CREATING", "READY", "FAILED", "UPDATING", "RESTORING", "BACKING_UP"}
            or not re.fullmatch(
                rf"booking-{re.escape(slug)}-[0-9a-f]{{8}}", state.get("project", "")
            )
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", state.get("image_id", ""))
            or type(state.get("bot_id")) is not int
            or not isinstance(state.get("stage"), str)
            or not isinstance(state.get("domain", ""), str)
            or state.get("public", False) not in (True, False)
        ):
            raise DeploymentError("Invalid deployment state; files were preserved")
        return state

    def save(self, slug: str, state: dict, status: str, stage: str) -> None:
        state.update(status=status, stage=stage, updated_at=datetime.now(UTC).isoformat())
        atomic_write(self.directory(slug) / "state.json", json.dumps(state, indent=2) + "\n")

    def values(self, slug: str) -> dict[str, str]:
        path = self.directory(slug) / ".env"
        try:
            if path.is_symlink():
                raise ValueError
            values = dict(line.split("=", 1) for line in path.read_text().splitlines() if line)
            if not (SECRET_KEYS | {"BOOKING_IMAGE", "POSTGRES_IMAGE", "REDIS_IMAGE"}).issubset(
                values
            ) or any(not re.fullmatch(r"[A-Za-z0-9_:/@.-]+", value) for value in values.values()):
                raise ValueError
            return values
        except (OSError, ValueError):
            raise DeploymentError(
                "Missing or invalid deployment .env; files were preserved"
            ) from None

    def compose(
        self,
        slug: str,
        *arguments: str,
        timeout: int = 240,
        input_text: str | None = None,
        image: str | None = None,
    ) -> str:
        path = self.directory(slug)
        try:
            return self._compose(slug, arguments, timeout, input_text, image)
        except DockerCommandError as error:
            atomic_write(path / "last-error.log", self.redact(slug, error.output))
            raise DeploymentError(
                "Docker operation failed; details saved in last-error.log (bookingctl logs)"
            ) from None

    def _compose(
        self,
        slug: str,
        arguments: tuple[str, ...],
        timeout: int,
        input_text: str | None = None,
        image: str | None = None,
    ) -> str:
        path = self.directory(slug)
        state = self.state(slug)
        return run_docker(
            [
                "compose",
                "--project-name",
                state["project"],
                "--project-directory",
                str(path),
                "--env-file",
                str(path / ".env"),
                "-f",
                str(path / "compose.yaml"),
                *arguments,
            ],
            values={**self.values(slug), **({"BOOKING_IMAGE": image} if image else {})},
            timeout=timeout,
            input_text=input_text,
        )

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        result = []
        for path in sorted(self.root.iterdir()):
            if path.is_dir():
                try:
                    result.append(self.state(path.name))
                except DeploymentError:
                    result.append(
                        {
                            "slug": path.name,
                            "status": "FAILED",
                            "stage": "state",
                            "diagnostic": "Incomplete/corrupt state; run doctor",
                        }
                    )
        return result

    def create(
        self, slug: str, request: CreateRequest | None = None, *, resume: bool = False
    ) -> dict:
        path = self.directory(slug)
        with registry_lock(self.root):
            if path.exists():
                if (
                    resume
                    and not (path / "state.json").exists()
                    and (path / "creation.json").exists()
                ):
                    intent = read_json(path / "creation.json")
                    self.save(slug, intent["state"], "CREATING", "files")
                state = self.state(slug)
                if state.get("operation") == "restore":
                    raise DeploymentError("Restore interrupted; recover with bookingctl restore")
                if state.get("operation") == "configure":
                    raise DeploymentError("Configuration interrupted; use configure SLUG --resume")
                if state.get("operation") in {"update", "rollback"}:
                    raise DeploymentError(
                        "Release operation interrupted; use rollback/status/history"
                    )
                if state["status"] == "READY":
                    return state  # Never rotate secrets, reconfigure, or issue another invite.
                if not resume:
                    raise DeploymentError("Deployment exists; use create SLUG --resume to recover")
                if state.get("stage") == "files":
                    self.finish_files(slug)
                docker_preflight()
            else:
                if resume or request is None:
                    raise DeploymentError(
                        "New deployment needs configuration and an explicit image"
                    )
                validate_template(request.template)
                if request.template.profile.slug != slug:
                    raise DeploymentError("Specialist slug differs from deployment slug")
                validate_image(request.image)
                validate_domain(request.domain)
                docker_preflight()
                identity = get_bot_identity(request.token)
                for existing in self.list():
                    if existing.get("stage") == "state":
                        raise DeploymentError(
                            "Registry has corrupt state; inspect it before create"
                        )
                    if existing.get("bot_id") == identity.id:
                        raise DeploymentError(
                            "This Telegram bot already belongs to another deployment"
                        )
                    if request.domain and existing.get("domain") == request.domain:
                        raise DeploymentError("Domain already belongs to another deployment")
                pinned = resolve_image(request.image)
                from booking_bot.version import (
                    POSTGRES_IMAGE_REPOSITORY,
                    REDIS_IMAGE_REPOSITORY,
                    __version__,
                    parse_release_version,
                )

                postgres_image = resolve_image(f"{POSTGRES_IMAGE_REPOSITORY}:{__version__}")
                redis_image = resolve_image(f"{REDIS_IMAGE_REPOSITORY}:{__version__}")
                # A random suffix also prevents collisions across registries on the same daemon.
                project = f"booking-{slug}-{secrets.token_hex(4)}"
                private_directory(path)
                state = {
                    "slug": slug,
                    "project": project,
                    "bot_id": identity.id,
                    "bot_username": identity.username,
                    "image_reference": request.image,
                    "image_id": pinned,
                    "domain": request.domain,
                    "public": False,
                }
                try:
                    candidate_version = request.image.rsplit(":", 1)[-1]
                    parse_release_version(candidate_version)
                    state["current_version"] = candidate_version
                except ValueError:
                    pass  # Legacy tags/digests are discovered from OCI metadata at first update.
                values = {key: secrets.token_urlsafe(36) for key in SECRET_KEYS}
                values.update(
                    TELEGRAM_BOT_TOKEN=request.token,
                    BOOKING_IMAGE=pinned,
                    POSTGRES_IMAGE=postgres_image,
                    REDIS_IMAGE=redis_image,
                )
                intent = {
                    "state": state,
                    "files": {
                        ".env": "".join(f"{k}={v}\n" for k, v in values.items()),
                        "specialist.toml": render_specialist(request.template),
                        "compose.yaml": json.dumps(compose_model(project), indent=2),
                    },
                }
                atomic_write(path / "creation.json", json.dumps(intent))
                self.save(slug, state, "CREATING", "files")
                try:
                    self.finish_files(slug)
                except BaseException:
                    self.save(slug, state, "FAILED", "files")
                    raise
            return self.provision(slug, state)

    def finish_files(self, slug: str) -> None:
        path = self.directory(slug)
        intent = read_json(path / "creation.json")
        if set(intent.get("files", {})) != {".env", "specialist.toml", "compose.yaml"}:
            raise DeploymentError("Invalid creation manifest; manual inspection required")
        for name, content in intent["files"].items():
            target = path / name
            if target.exists():
                if target.is_symlink() or target.read_text(encoding="utf-8") != content:
                    raise DeploymentError(
                        "Creation files differ from manifest; preserved for inspection"
                    )
            else:
                atomic_write(target, content, public_config=name == "specialist.toml")

    def assert_no_admin(self, slug: str) -> None:
        project = self.state(slug)["project"]
        if run_docker(
            [
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=admin",
            ]
        ).strip():
            raise DeploymentError("An admin container is still running; wait before resuming")

    def local_config(self, slug: str) -> None:
        from booking_bot.config import Settings
        from booking_bot.specialist_config import load_specialist_template

        path = self.directory(slug)
        state, values = self.state(slug), self.values(slug)
        template = load_specialist_template(path / "specialist.toml")
        validate_template(template)
        if (
            template.profile.slug != slug
            or values["TELEGRAM_BOT_TOKEN"].split(":")[0] != str(state["bot_id"])
            or values["BOOKING_IMAGE"] != state["image_id"]
        ):
            raise DeploymentError("Config identity/image differs from saved state")
        try:
            Settings(
                _env_file=None,
                app_env="production",
                telegram_webhook_mode="internal",
                telegram_webhook_base_url=None,
                telegram_bot_token=values["TELEGRAM_BOT_TOKEN"],
                telegram_webhook_header_secret=values["TELEGRAM_WEBHOOK_HEADER_SECRET"],
                database_url=f"postgresql+asyncpg://booking:{values['POSTGRES_PASSWORD']}"
                "@postgres:5432/booking",
                redis_url=f"redis://:{values['REDIS_PASSWORD']}@redis:6379/0",
                specialist_config_path=str(path / "specialist.toml"),
            )
        except ValueError:
            raise DeploymentError(
                "Invalid production configuration; inspect private files"
            ) from None

    def config_digest(self, slug: str) -> str:
        return hashlib.sha256((self.directory(slug) / "specialist.toml").read_bytes()).hexdigest()

    def doctor(self, slug: str) -> dict:
        from booking_bot.deployment.diagnostics import diagnose

        return diagnose(self, slug)

    def assert_domain_available(self, slug: str, domain: str) -> None:
        for existing in self.list():
            if existing.get("slug") != slug and existing.get("domain") == domain:
                raise DeploymentError("Domain already belongs to another deployment")

    def expose(self, slug: str, domain: str | None = None) -> dict:
        from booking_bot.deployment import proxy

        with registry_lock(self.root):
            state = self.state(slug)
            if state["status"] != "READY":
                raise DeploymentError("Deployment must be READY before exposure")
            hostname = validate_domain(domain if domain is not None else state.get("domain", ""))
            if not hostname:
                raise DeploymentError("Provide a domain with expose SLUG --domain HOSTNAME")
            self.assert_domain_available(slug, hostname)
            dns = proxy.domain_check(self.root, hostname)
            if not dns["ok"]:
                raise DeploymentError(f"DNS does not point only to this server: {dns}")
            if not proxy.status(self.root):
                raise DeploymentError("Proxy is not running; use bookingctl proxy start")
            staging = proxy.config(self.root)["staging"]
            self.local_config(slug)
            path = self.directory(slug) / "compose.yaml"
            previous = path.read_text(encoding="utf-8")
            proposed = json.dumps(compose_model(state["project"], hostname, public=True), indent=2)
            if previous != proposed:
                atomic_write(path, proposed)
                try:
                    self.compose(slug, "config", "--quiet")
                    self.compose(
                        slug,
                        "up",
                        "-d",
                        "--wait",
                        "--wait-timeout",
                        "180",
                        "--no-deps",
                        "--force-recreate",
                        "api",
                    )
                    health = proxy.https_check(hostname, staging=staging)
                    if not all(health.values()):
                        raise DeploymentError(
                            "HTTPS /live or /ready failed; inspect DNS/TLS/proxy logs"
                        )
                except BaseException:
                    atomic_write(path, previous)
                    # Restore the previous private or public API without touching other clients.
                    self.compose(
                        slug,
                        "up",
                        "-d",
                        "--wait",
                        "--wait-timeout",
                        "180",
                        "--no-deps",
                        "--force-recreate",
                        "api",
                    )
                    raise
            else:
                health = proxy.https_check(hostname, staging=staging)
                if not all(health.values()):
                    raise DeploymentError("HTTPS /live or /ready failed; deployment preserved")
            state["domain"] = hostname
            state["public"] = True
            self.save(slug, state, "READY", "complete")
            if not staging:
                self.set_webhook(slug)
            return {
                "domain": hostname,
                "dns": dns,
                "https": health,
                "webhook": "staging-skipped" if staging else "verified",
            }

    def set_webhook(self, slug: str) -> None:
        from booking_bot.deployment import proxy

        state = self.state(slug)
        if state["status"] != "READY" or not state.get("public"):
            raise DeploymentError("Public HTTPS deployment required before webhook setup")
        if proxy.config(self.root)["staging"]:
            raise DeploymentError("Telegram webhook needs a trusted production certificate")
        if not proxy.domain_check(self.root, state["domain"])["ok"] or not proxy.status(self.root):
            raise DeploymentError("DNS or proxy is not ready")
        if not all(proxy.https_check(state["domain"], timeout_seconds=12).values()):
            raise DeploymentError("HTTPS health checks failed; webhook was not changed")
        self.compose(
            slug, "run", "--rm", "--no-deps", "-T", "admin", "booking-admin", "set-webhook"
        )

    def configure(self, slug: str, config: Path | None = None, *, resume: bool = False) -> None:
        from booking_bot.deployment.configuration import configure

        configure(self, slug, config, resume=resume)

    def provision(self, slug: str, state: dict) -> dict:
        stage = "validation"
        try:
            self.save(slug, state, "CREATING", stage)
            from booking_bot.specialist_config import load_specialist_template

            template = load_specialist_template(self.directory(slug) / "specialist.toml")
            validate_template(template)
            if template.profile.slug != slug:
                raise DeploymentError("Specialist slug differs from deployment slug")
            values = self.values(slug)
            if (
                values["TELEGRAM_BOT_TOKEN"].split(":")[0] != str(state["bot_id"])
                or values["BOOKING_IMAGE"] != state["image_id"]
            ):
                raise DeploymentError("Bot identity or pinned image differs from saved state")
            # A killed/timed-out docker CLI can leave its one-off migration container alive.
            if run_docker(
                [
                    "ps",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={state['project']}",
                    "--filter",
                    "label=com.docker.compose.service=admin",
                ]
            ).strip():
                raise DeploymentError("An admin container is still running; wait before resuming")
            self.compose(slug, "config", "--quiet")
            stage = "dependencies"
            self.save(slug, state, "CREATING", stage)
            self.compose(slug, "up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis")
            # Recovery must never migrate under running application processes.
            self.compose(slug, "stop", "api", "worker")
            for stage, command in (
                ("migration", ["alembic", "upgrade", "head"]),
                ("configure", ["booking-admin", "configure"]),
            ):
                self.save(slug, state, "CREATING", stage)
                self.compose(slug, "run", "--rm", "--no-deps", "-T", "admin", *command)
            stage = "healthcheck"
            self.save(slug, state, "CREATING", stage)
            self.compose(slug, "up", "-d", "--wait", "--wait-timeout", "180", "api", "worker")
            stage = "invite"
            self.save(slug, state, "CREATING", stage)
            invite_path = self.directory(slug) / "master-invite.txt"
            if not invite_path.exists():
                invite = self.compose(
                    slug,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "admin",
                    "booking-admin",
                    "create-master-invite",
                    "--bot-username",
                    state["bot_username"],
                )
                if not re.search(
                    r"https://t.me/[A-Za-z0-9_]+\?start=master_[A-Za-z0-9_-]+", invite
                ):
                    raise DeploymentError("Invite command returned no invitation")
                atomic_write(invite_path, invite)
            state["config_sha256"] = self.config_digest(slug)
            try:
                image = json.loads(run_docker(["image", "inspect", state["image_id"]]))[0]
                release = (image["Config"].get("Labels") or {}).get(
                    "org.opencontainers.image.version"
                )
                if release and release != "unknown":
                    state["current_version"] = release
            except (ValueError, KeyError, IndexError, TypeError):
                pass  # Older local images can be diagnosed; release updates require valid labels.
            self.save(slug, state, "READY", "complete")
            return state
        except BaseException:
            self.save(slug, state, "FAILED", stage)
            raise

    def runtime(self, slug: str) -> list[dict]:
        output = self.compose(slug, "ps", "--all", "--format", "json")
        try:
            entries = (
                json.loads(output)
                if output.lstrip().startswith("[")
                else [json.loads(line) for line in output.splitlines() if line.strip()]
            )
            return [
                {
                    "service": entry.get("Service"),
                    "state": entry.get("State"),
                    "health": entry.get("Health"),
                    "ports": entry.get("Publishers") or [],
                }
                for entry in entries
                if entry.get("Service") in SERVICES
            ]
        except (ValueError, TypeError):
            raise DeploymentError("Cannot read Compose runtime status") from None

    def action(self, slug: str, action: str) -> None:
        with registry_lock(self.root):
            state = self.state(slug)
            if action != "stop" and state["status"] != "READY":
                raise DeploymentError("Deployment is incomplete; use create SLUG --resume")
            if action != "stop":
                self.local_config(slug)
                if state.get("config_sha256", self.config_digest(slug)) != self.config_digest(slug):
                    raise DeploymentError(
                        "Config changed outside bookingctl; restore it and use configure"
                    )
                self.refresh_compose(slug)
            if action in {"stop", "restart"}:
                self.compose(slug, "stop", "worker", "api")
                self.compose(slug, "stop", "redis", "postgres")
            if action in {"start", "restart"}:
                self.compose(
                    slug,
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "postgres",
                    "redis",
                    "api",
                    "worker",
                )

    def refresh_compose(self, slug: str) -> None:
        """Upgrade only an exact known template, under the caller's operation lock."""
        state = self.state(slug)
        path = self.directory(slug) / "compose.yaml"
        installed = read_json(path)
        arguments = (state["project"], state.get("domain", ""))
        model = compose_model(*arguments, public=state.get("public", False))
        if installed == model:
            return
        if installed != legacy_compose_model(*arguments, public=state.get("public", False)):
            raise DeploymentError("Compose differs from known templates; reconcile manually")
        atomic_write(path.with_name("compose-previous.json"), json.dumps(installed, indent=2))
        atomic_write(path, json.dumps(model, indent=2))

    def logs(self, slug: str, service: str | None, tail: int) -> str:
        self.state(slug)
        previous_error = self.directory(slug) / "last-error.log"
        historical = previous_error.read_text(encoding="utf-8") if previous_error.exists() else ""
        try:
            output = self.compose(
                slug,
                "logs",
                "--no-color",
                "--tail",
                str(tail),
                *([service] if service else sorted(SERVICES)),
            )
        except DeploymentError:
            if not historical:
                raise
            output = "Container logs unavailable; showing saved diagnostic only.\n"
        if historical:
            output += "\nLast failed operation (historical):\n" + historical
        return self.redact(slug, output)

    def redact(self, slug: str, output: str) -> str:
        secrets_to_hide = [self.values(slug)[key] for key in SECRET_KEYS]
        invite = self.directory(slug) / "master-invite.txt"
        if invite.exists():
            secrets_to_hide.extend(re.findall(r"master_([A-Za-z0-9_-]+)", invite.read_text()))
        for value in sorted(secrets_to_hide, key=len, reverse=True):
            output = output.replace(value, "[REDACTED]")
        output = re.sub(r"\b\d{5,16}:[A-Za-z0-9_-]{20,}", "[REDACTED]", output)
        output = re.sub(r"https://t.me/\S+\?start=master_\S+", "[REDACTED_INVITE]", output)
        return re.sub(r"(?<!\w)\+?\d[\d ()-]{9,}\d(?!\w)", "[REDACTED_PHONE]", output)

    def follow_logs(self, slug: str, service: str | None, tail: int) -> None:
        """Stream Docker logs with the same redaction as a finite snapshot."""
        state = self.state(slug)
        path = self.directory(slug)
        arguments = [
            "docker",
            "compose",
            "--project-name",
            state["project"],
            "--project-directory",
            str(path),
            "--env-file",
            str(path / ".env"),
            "-f",
            str(path / "compose.yaml"),
            "logs",
            "--no-color",
            "--follow",
            "--tail",
            str(tail),
            *([service] if service else sorted(SERVICES)),
        ]
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=docker_environment(self.values(slug)),
        )
        try:
            for line in process.stdout:
                print(self.redact(slug, line), end="", flush=True)
            if process.wait():
                raise DeploymentError("Docker log streaming failed")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
