"""Host operator CLI. Token is read without echo, never via process arguments."""

import argparse
import getpass
import json
import os
import sys
import warnings
from dataclasses import replace
from pathlib import Path

from booking_bot.deployment.files import DeploymentError, registry_lock
from booking_bot.deployment.manager import (
    SERVICES,
    CreateRequest,
    DeploymentManager,
    validate_slug,
    validate_template,
)
from booking_bot.specialist_config import (
    SpecialistConfigError,
    load_specialist_template,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bookingctl")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".booking-bot" / "deployments",
        help="Private registry directory (use the same root for all clients)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show_version = commands.add_parser("version")
    show_version.add_argument("slug", nargs="?", help="Also show a deployment's confirmed version")
    update = commands.add_parser("update", help="Backup and update one READY client")
    update.add_argument("slug")
    update.add_argument("--version", required=True)
    update.add_argument("--dry-run", action="store_true")
    rollout = commands.add_parser("update-all", help="Serial rollout; stop at the first failure")
    rollout.add_argument("--version", required=True)
    rollout.add_argument("--dry-run", action="store_true")
    rollback = commands.add_parser(
        "rollback", help="Return to previous release; never downgrade DB"
    )
    rollback.add_argument("slug")
    rollback.add_argument("--restore-database", action="store_true")
    rollback.add_argument("--yes", action="store_true", help="Confirm destructive database restore")
    history = commands.add_parser("history")
    history.add_argument("slug")
    parser.add_argument("--backup-root", type=Path, help="Private backup root outside registry")
    backup = commands.add_parser("backup", help="Create, list, verify, pull or schedule backups")
    backup.add_argument("action_or_slug")
    backup.add_argument("arguments", nargs="*")
    backup.add_argument("--at", default="03:00", help="Daily systemd schedule time in UTC")
    backup.add_argument("--retention", action="store_true")
    all_backups = commands.add_parser(
        "backup-all", help="Sequential backups; nonzero on any failure"
    )
    all_backups.add_argument("--retention", action="store_true")
    restore = commands.add_parser("restore", help="Destructive single-client restore")
    restore.add_argument("slug")
    restore.add_argument("backup_id")
    restore.add_argument("--yes", action="store_true")
    restore.add_argument("--dangerous-skip-safety-backup", action="store_true")
    recover = commands.add_parser(
        "recover-files", help="Recover lost deployment files, no DB writes"
    )
    recover.add_argument("slug")
    recover.add_argument("backup_id")
    create = commands.add_parser("create", help="Provision a client, or safely resume creation")
    create.add_argument("slug")
    create.add_argument("--image", help="Versioned local image or GHCR repository@sha256 digest")
    create.add_argument(
        "--config",
        type=Path,
        default=Path("specialist.toml"),
        help="Existing specialist.toml template; services/texts/schedule are preserved",
    )
    create.add_argument("--resume", action="store_true")
    expose = commands.add_parser("expose", help="Publish one client through shared HTTPS proxy")
    expose.add_argument("slug")
    expose.add_argument("--domain", help="DNS hostname; required if create had no domain")
    domain_check = commands.add_parser("domain-check", help="Compare DNS with configured VPS IPs")
    domain_check.add_argument("slug")
    webhook = commands.add_parser("webhook", help="Manage one client's Telegram webhook")
    webhook.add_argument("action", choices=["set"])
    webhook.add_argument("slug")
    proxy = commands.add_parser("proxy", help="Manage shared Traefik infrastructure")
    proxy_commands = proxy.add_subparsers(dest="proxy_action", required=True)
    init = proxy_commands.add_parser("init")
    init.add_argument("--email", required=True)
    init.add_argument("--server-ipv4", default="")
    init.add_argument("--server-ipv6")
    init.add_argument("--staging", action="store_true")
    mode = proxy_commands.add_parser("mode", help="Switch ACME staging/production storage")
    mode_choice = mode.add_mutually_exclusive_group(required=True)
    mode_choice.add_argument("--staging", action="store_true")
    mode_choice.add_argument("--production", action="store_true")
    for name in ("status", "start", "restart", "logs"):
        proxy_commands.add_parser(name)
    commands.add_parser("list", help="List provisioning states without contacting Docker")
    monitor = commands.add_parser("monitor", help="One safe monitoring pass")
    monitor.add_argument("--schedule", choices=("install", "status"))
    for name in ("resources", "versions"):
        commands.add_parser(name)
    notifications = commands.add_parser("notifications")
    notifications.add_argument("action", choices=("failed", "retry", "retry-failed"))
    notifications.add_argument("slug")
    notifications.add_argument("job_id", nargs="?")
    for name in ("status", "start", "stop", "restart", "logs", "doctor", "production-check"):
        command = commands.add_parser(name)
        command.add_argument("slug", nargs="?" if name in {"status", "doctor"} else None)
        if name in {"status", "doctor", "production-check"}:
            command.add_argument("--json", action="store_true")
        if name == "logs":
            command.add_argument("--service", choices=sorted(SERVICES))
            command.add_argument("--tail", type=int, default=100)
            command.add_argument("--follow", action="store_true")
    configure = commands.add_parser(
        "configure", help="Apply profile/text changes; preserve owner data"
    )
    configure.add_argument("slug")
    source = configure.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, help="Separate candidate specialist TOML")
    source.add_argument("--resume", action="store_true", help="Recover interrupted configuration")
    return parser


def prompt_request(args: argparse.Namespace) -> CreateRequest:
    if not args.image:
        raise DeploymentError("New deployments require --image with an explicit release version")
    template = load_specialist_template(args.config)
    profile = replace(
        template.profile,
        slug=args.slug,
        brand_name=input("Название бизнеса: ").strip(),
        specialist_name=input("Имя специалиста: ").strip(),
        specialist_role=input("Специализация: ").strip(),
        timezone=input("Часовой пояс (например Europe/Moscow): ").strip(),
        currency=input("Валюта (например RUB): ").strip(),
    )
    template = replace(
        template,
        profile=replace(profile, bio=profile.specialist_role),
        location=replace(template.location, address=input("Адрес: ").strip()),
    )
    validate_template(template)
    domain = input("Домен (необязательно; без https://): ").strip()
    # getpass may fall back to echoed stdin; fail closed instead.
    if not sys.stdin.isatty():
        raise DeploymentError("Token entry needs a terminal with hidden input")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Telegram token (ввод скрыт): ").strip()
    except getpass.GetPassWarning:
        raise DeploymentError("Terminal cannot hide token input") from None
    return CreateRequest(template, token, args.image, domain)


def run(args: argparse.Namespace) -> None:
    manager = DeploymentManager(args.root)
    if args.command == "version":
        from booking_bot.version import __version__

        print(f"bookingctl: {__version__}")
        if args.slug:
            state = manager.state(args.slug)
            print(f"application image: {state.get('current_version', 'unknown (run doctor)')}")
        else:
            print(f"application image: {__version__} (this package; use version SLUG for client)")
        return
    if args.command in {"update", "update-all", "rollback", "history"}:
        from booking_bot.deployment.releases import ReleaseManager

        releases = ReleaseManager(manager, args.backup_root)
        if args.command == "update":
            result = releases.update(args.slug, args.version, dry_run=args.dry_run)
        elif args.command == "update-all":
            result = releases.update_all(args.version, dry_run=args.dry_run)
        elif args.command == "rollback":
            result = releases.rollback(
                args.slug, restore_database=args.restore_database, yes=args.yes
            )
        else:
            result = releases.history(args.slug)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command in {"backup", "backup-all", "restore", "recover-files"}:
        run_backup(args, manager)
        return
    if args.command == "proxy":
        from booking_bot.deployment import proxy

        if args.proxy_action == "init":
            with registry_lock(manager.root):
                proxy.initialize(
                    manager.root,
                    email=args.email,
                    server_ipv4=args.server_ipv4,
                    server_ipv6=args.server_ipv6,
                    staging=args.staging,
                )
            print(f"Proxy initialized: {proxy.proxy_directory(manager.root)}")
        elif args.proxy_action == "mode":
            with registry_lock(manager.root):
                proxy.set_mode(manager.root, staging=args.staging)
                proxy.compose(manager.root, "up", "-d", "--force-recreate", "traefik")
            print("ACME mode updated; certificates use separate persistent files")
        elif args.proxy_action == "start":
            with registry_lock(manager.root):
                proxy.config(manager.root)
                proxy.ensure_network()
                proxy.compose(manager.root, "up", "-d", "--wait")
            print("Proxy started")
        elif args.proxy_action == "restart":
            with registry_lock(manager.root):
                proxy.compose(manager.root, "restart", "traefik")
            print("Proxy restarted")
        elif args.proxy_action == "logs":
            print(proxy.compose(manager.root, "logs", "--no-color", "--tail", "100"), end="")
        else:
            healthy = proxy.status(manager.root)
            print(json.dumps({"proxy": "running" if healthy else "down"}))
            if not healthy:
                raise SystemExit(1)
        return
    if args.command == "list":
        for state in manager.list():
            print(f"{state['slug']}\t{state['status']}\t{state['stage']}")
        return
    if args.command in {"status", "doctor", "production-check", "monitor", "resources", "versions"}:
        run_monitoring(args, manager)
        return
    validate_slug(args.slug)
    if args.command == "notifications":
        from uuid import UUID

        from booking_bot.deployment.files import operation_lock

        if args.action == "retry":
            try:
                job_id = str(UUID(args.job_id or ""))
            except ValueError:
                raise DeploymentError("notifications retry requires a valid job UUID") from None
        elif args.job_id:
            raise DeploymentError("Job UUID is only supported for notifications retry")
        else:
            job_id = None
        with operation_lock(manager.root, manager.directory(args.slug)):
            state = manager.state(args.slug)
            if state["status"] != "READY" or state.get("operation"):
                raise DeploymentError("Notifications inspection/retry requires READY deployment")
            manager.local_config(args.slug)
            output = manager.compose(
                args.slug,
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "booking_bot.deployment.runtime",
                f"notifications-{args.action}",
                *(["--job-id", job_id] if job_id else []),
                timeout=20,
            )
        print(json.dumps(json.loads(output), ensure_ascii=False, indent=2))
        return
    if args.command == "domain-check":
        from booking_bot.deployment import proxy

        result = proxy.domain_check(manager.root, manager.state(args.slug).get("domain", ""))
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "expose":
        result = manager.expose(args.slug, args.domain)
        print(json.dumps(result, indent=2))
        print(
            "Bot is ready for production."
            if result["webhook"] == "verified"
            else "Staging route ready; Telegram webhook not activated."
        )
    elif args.command == "webhook":
        manager.set_webhook(args.slug)
        print(f"{args.slug}: webhook configured and verified")
    elif args.command == "create":
        request = None
        if not manager.directory(args.slug).exists() and not args.resume:
            request = prompt_request(args)
        state = manager.create(args.slug, request, resume=args.resume)
        print(f"{args.slug}: {state['status']} ({state['stage']})")
        print(f"Master invite: {manager.directory(args.slug) / 'master-invite.txt'}")
        print("Webhook не зарегистрирован. Локальный адрес: bookingctl status SLUG.")
    elif args.command == "configure":
        manager.configure(args.slug, args.config, resume=args.resume)
        print(f"{args.slug}: configuration completed; owner services and schedule preserved")
    elif args.command == "logs":
        if not 1 <= args.tail <= 10000:
            raise DeploymentError("--tail must be between 1 and 10000")
        if args.follow:
            manager.follow_logs(args.slug, args.service, args.tail)
        else:
            print(manager.logs(args.slug, args.service, args.tail), end="")
    else:
        manager.action(args.slug, args.command)
        print(f"{args.slug}: {args.command} completed")


def run_monitoring(args, manager):
    from booking_bot.deployment.backup import BackupManager
    from booking_bot.deployment.manager import run_docker
    from booking_bot.deployment.monitoring import all_reports, client_report, host_report, table

    if args.command == "monitor":
        if args.schedule:
            from booking_bot.deployment.monitor_schedule import schedule

            schedule(args.schedule, manager.root, BackupManager(manager, args.backup_root).root)
            return
        from booking_bot.deployment.alerts import monitor

        result = monitor(manager, args.backup_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
        return
    if args.command in {"resources", "versions"}:
        rows = []
        for state in manager.list():
            slug = state["slug"]
            if args.command == "versions":
                from booking_bot.version import __version__

                rows.append(
                    {
                        "slug": slug,
                        "version": state.get("current_version", "unknown"),
                        "operator_target": os.environ.get("BOOKING_LATEST_VERSION", __version__),
                    }
                )
                continue
            try:
                ids = manager.compose(slug, "ps", "--all", "-q").split()
                stats = (
                    run_docker(["stats", "--no-stream", "--format", "{{json .}}", *ids], timeout=30)
                    if ids
                    else ""
                )
                rows.append(
                    {
                        "slug": slug,
                        "containers": manager.runtime(slug),
                        "resources": [json.loads(line) for line in stats.splitlines()],
                    }
                )
            except DeploymentError:
                rows.append({"slug": slug, "error": "Resources unavailable"})
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        if any(row.get("error") for row in rows):
            raise SystemExit(1)
        return
    if args.slug:
        validate_slug(args.slug)
        report = client_report(
            manager,
            args.slug,
            args.backup_root,
            production=args.command == "production-check",
            resources=True,
        )
        if args.command == "production-check":
            host = host_report(manager, args.backup_root)
            from booking_bot.deployment.monitoring import finish

            report["checks"].extend({**c, "check": "host_" + c["check"]} for c in host["checks"])
            finish(report)
        reports = [report]
    else:
        reports = all_reports(manager, args.backup_root)
        if args.command == "doctor":
            reports.insert(0, host_report(manager, args.backup_root))
    healthy = all(
        report["ok"]
        and report.get("observed", True)
        and all(check.get("observed", True) for check in report["checks"])
        for report in reports
    )
    if args.command == "production-check":
        reports[0]["result"] = "PRODUCTION READY" if healthy else "NOT PRODUCTION READY"
    if args.json or args.slug and args.command == "status":
        print(json.dumps(reports[0] if args.slug else reports, ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(table(reports))
    else:
        for report in reports:
            print(f"\n{report['slug']}: {report['severity']}")
            for check in report["checks"]:
                print(f"{check['severity']:8} {check['check']}: {check['detail']}")
    if args.command == "production-check" and not args.json:
        print("RESULT: PRODUCTION READY" if healthy else "RESULT: NOT PRODUCTION READY")
    if not healthy:
        raise SystemExit(1)


def run_backup(args: argparse.Namespace, manager: DeploymentManager) -> None:
    from booking_bot.deployment.backup import BackupManager
    from booking_bot.deployment.backup_remote import S3Storage

    backups = BackupManager(manager, args.backup_root)

    def create(slug):
        try:
            backup_id = backups.create(slug)
            print(f"{slug}: {backup_id}", flush=True)
            if os.environ.get("BOOKING_BACKUP_S3_BUCKET"):
                S3Storage().upload(backups, slug, backup_id)
            if args.retention:
                backups.retain(
                    slug,
                    **{
                        key: int(os.environ.get(f"BOOKING_BACKUP_{key.upper()}", value))
                        for key, value in (
                            ("daily", "7"),
                            ("weekly", "4"),
                            ("monthly", "3"),
                            ("safety", "3"),
                        )
                    },
                )
        except Exception:
            backups.event(slug, "backup failed")
            raise

    if args.command == "restore":
        backups.restore(
            args.slug,
            args.backup_id,
            yes=args.yes,
            dangerous_skip_safety=args.dangerous_skip_safety_backup,
        )
        print(f"{args.slug}: restore completed; doctor passed")
    elif args.command == "recover-files":
        if not sys.stdin.isatty():
            raise DeploymentError("Recovery token entry needs a terminal with hidden input")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Telegram token (hidden): ").strip()
        backups.recover_files(args.slug, args.backup_id, token)
        print("Files recovered; restore DB with explicit --dangerous-skip-safety-backup")
    elif args.command == "backup-all":
        failures = []
        for state in manager.list():
            try:
                create(state["slug"])
            except Exception:
                failures.append(state["slug"])
                print(f"{state['slug']}: backup FAILED; inspect private logs", file=sys.stderr)
        if failures:
            raise DeploymentError(f"Backups failed for: {', '.join(failures)}")
    else:
        action, arguments = args.action_or_slug, args.arguments
        if action == "schedule" and len(arguments) == 1 and arguments[0] in {"install", "status"}:
            from booking_bot.deployment.backup_schedule import schedule

            schedule(arguments[0], manager.root, backups.root, args.at)
        elif action == "list" and len(arguments) == 1:
            print(json.dumps(backups.list(arguments[0]), indent=2))
        elif action in {"verify", "pull", "upload"} and len(arguments) == 2:
            slug, backup_id = arguments
            if action == "verify":
                print(json.dumps(backups.verify(slug, backup_id), indent=2))
            else:
                getattr(S3Storage(), action)(backups, slug, backup_id)
        elif action == "prune" and len(arguments) == 1:
            print(
                backups.retain(
                    arguments[0],
                    **{
                        key: int(os.environ.get(f"BOOKING_BACKUP_{key.upper()}", value))
                        for key, value in (
                            ("daily", "7"),
                            ("weekly", "4"),
                            ("monthly", "3"),
                            ("safety", "3"),
                        )
                    },
                )
            )
        elif not arguments and action not in {
            "schedule",
            "verify",
            "list",
            "pull",
            "upload",
            "prune",
        }:
            create(action)
        else:
            raise DeploymentError(
                "Use backup SLUG | list SLUG | verify/pull/upload SLUG ID | "
                "prune SLUG | schedule install/status"
            )


def record_operation(args, *, failed):
    if args.command not in {"update", "rollback", "restore"} or getattr(args, "dry_run", False):
        return
    from booking_bot.deployment.alerts import operation_event

    try:
        manager = DeploymentManager(args.root)
        if failed and manager.state(args.slug)["status"] != "FAILED":
            return  # A rejected preflight/input is not a failed data-changing operation.
        operation_event(
            manager,
            args.slug,
            args.command,
            failed=failed,
            backup_root=args.backup_root,
        )
    except Exception:
        print(
            "bookingctl: operation alert state unavailable; inspect operation history",
            file=sys.stderr,
        )


def main() -> None:
    args = build_parser().parse_args()
    try:
        run(args)
        record_operation(args, failed=False)
    except (DeploymentError, SpecialistConfigError) as error:
        record_operation(args, failed=True)
        print(f"bookingctl: {error}", file=sys.stderr)
        if getattr(args, "slug", None):
            print(
                "Inspect doctor/status/logs; files and volumes are preserved. "
                "Recover create with create SLUG --resume, configure with configure SLUG --resume.",
                file=sys.stderr,
            )
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        print("bookingctl: interrupted; existing deployment data preserved", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception:
        record_operation(args, failed=True)
        # No arbitrary exceptions / Pydantic inputs / subprocess commandlines in terminal.
        print(
            "bookingctl: operation failed; inspect saved state and private files", file=sys.stderr
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
