# Changelog

## [1.0.0] - Unreleased

Stable release is blocked by production qualification; this section is preparation,
not a published version. The application remains 1.0.0-rc.1.

### Added
- Telegram booking with FIXED/FROM/NEGOTIABLE services, client appointment management,
  specialist cabinet, schedules, notifications and reminders.
- Private Conversations with text/photos/files, PriceProposal acceptance and immutable
  agreed-price snapshots in appointments.
- PostgreSQL/Redis, webhook/HTTPS, isolated multi-deployment tooling, backup/restore,
  versioned updates, recovery and monitoring.
- Verified registry-digest release manifest and optional SPDX SBOM release assets.

### Changed
- Production runbooks use the managed bootstrap/client layout and pinned release images.
- Stable notes describe features, requirements, upgrades and known limitations.

### Fixed
- Development build instructions supply the canonical APP_VERSION.
- Release provenance rejects wrong OCI metadata and substituted registry artifacts.

### Migration notes
- Single Alembic head c75a01d29f10; schema unchanged from 1.0.0-rc.1.
- Existing pre-conversations services migrate to FIXED; backup precedes any upgrade.
- RC to stable update requires backup, pull, migration, restart and doctor qualification.

### Breaking changes
- No additional product features beyond the qualified RC; one specialist per deployment.
- No online payments/deposits, web admin, voice/video/group chat or message edit/delete.
- Restore needs downtime; Telegram delivery cannot guarantee exactly-once semantics.

## [1.0.0-rc.1] - 2026-10-03

### Added
- Ubuntu 24.04 x86_64 bootstrap for a persistent CLI, private /opt/booking layout,
  shared proxy and existing backup/monitoring timers. CLI-only preparation is explicit.
- Release and client handover checklists; qualification evidence and open release gates.

### Changed
- Canonical numbered RC targets are accepted by deployment/update preflight.
- RC publishing does not advance the stable minor alias and creates a prerelease.
- Includes the Phase 7.5 request, conversation and price-negotiation implementation
  and hardening in this source snapshot; no additional product features in Phase 8.

### Fixed
- RC update ordering rejects repeated versions, older candidates and stable-to-RC updates.
- Bootstrap preserves existing templates, proxy configuration, clients, secrets and volumes.
- New deployments use versioned hardened PostgreSQL/Redis images; vulnerable Go gosu
  is replaced by the pinned entrypoint's su-exec compatibility wrapper, OpenSSL is patched.
- Socket proxy PCRE2 security findings are fixed; all four release packages are guarded.
- Redis's generated shell command now reaches the official privilege-drop entrypoint;
  the server runs as UID 999 with no effective capabilities.

### Migration notes
- Single Alembic head c75a01d29f10 adds requests, conversations and price proposals.
- Existing services default to FIXED; agreed appointment prices are stored as snapshots.
- Backup precedes migrations; recovery after a schema change uses verified restore,
  never an automatic Alembic downgrade.
- Bootstrap is an operator installation command, not an implicit host CLI upgrade.

### Breaking changes
- Final production acceptance requires real VPS, public TLS/Telegram, reboot,
  off-host restore and a reviewed vulnerability scan. Local smoke alone is insufficient.
- No final v1.0.0 tag is created by qualification.

## [0.7.0] - 2026-10-02

### Added
- Fleet status, host/client doctor, resources, versions and non-destructive production-check.
- One-shot monitoring, configurable severity thresholds, independent Telegram alerts,
  persistent deduplication, retry on delivery failure, recovery alerts and systemd timer.
- Notification queue counters, bounded failed-job inspection and explicit safe retry commands.
- Operations and incident runbooks, production audit and reproducible failure/load smoke tests.

### Fixed
- API/worker restart after daemon restart with unless-stopped policies.
- Bounded Docker log storage for applications, storage and shared proxy.
- Restricted Docker socket proxy on a private network; no direct Traefik socket mount.
- Versioned socket-proxy release image with a pinned upstream base and targeted OpenSSL
  security fixes; both images must pass Trivy before publication.
- Deployment/service/event fields in JSON logs; phone and monitoring/S3 secret redaction.

### Changed
- Existing exact Phase 3–6 Compose templates can be backed up and explicitly refreshed
  by update/start/restart; a previous Compose copy is preserved. Custom models are rejected.

### Migration notes
- No database migration; Alembic head remains b62f3d910ea4.
- Existing deployments need generated Compose refreshed during the explicit update.
- Existing proxy must be upgraded explicitly; preserve the letsencrypt volume and ACME files.
- Retention bounds unreferenced safety backups (three newest by default); active update,
  restore and rollback checkpoints remain protected.
- Monitoring credentials live on the Linux host, never in client environments/images.

### Breaking changes
- production-check requires public HTTPS/webhook, fresh backups and Phase 7 diagnostics.
- doctor/status return nonzero for ERROR/CRITICAL; warnings remain visible and non-blocking.

## [0.6.0] - 2026-10-01

### Added
- GHCR release pipeline, real PostgreSQL/Redis CI, production image build and free Trivy scan.
- Explicit update, dry-run, sequential rollout, history and application/database recovery commands.
- Backup-before-update, migration preflight, health/doctor gates and persisted failure diagnostics.

### Changed
- Runtime uses the official Python 3.12 Alpine image pinned by digest; full image scan is required.
- Single version source in `booking_bot/version.py`; package metadata derives from it.
- Production builds install hash-locked dependencies and require a matching APP_VERSION.
- Production Compose requires an explicit image; clients keep immutable local image IDs.

### Fixed
- Targeted security updates for aiohttp and urllib3 found by the image scanner.
- Backup/update/restore cannot race through supported bookingctl commands.
- An unvalidated release never becomes the confirmed deployment version.

### Migration notes
- This release adds no application schema migration beyond the Phase 5 head `b62f3d910ea4`.
- Same-head releases allow application-only rollback. Any changed/uncertain migration state
  requires a reviewed backup restore; no automatic Alembic downgrade is performed.

### Breaking changes
- Automated releases accept stable MAJOR.MINOR.PATCH versions only.
- Docker builds require APP_VERSION matching version.py. Legacy image tags remain readable,
  but updating requires a valid OCI version label and a healthy deployment.
