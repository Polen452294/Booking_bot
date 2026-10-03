# Release policy

Release публикует четыре versioned packages: application, socket proxy, PostgreSQL
и Redis. Guard и Trivy проверяют каждый полный tag/image. CI проверяет
restart/outage/load/alerts, proxy ACL, backup/restore и update/recovery.
Актуальный gate: [Phase 8](release-readiness-v1.md), [Phase 9A](releases/v1.0.0.md).

Единственный источник версии: `src/booking_bot/version.py` (`__version__`). Hatchling
создаёт package metadata из этого файла. Целевые версии: stable SemVer
`MAJOR.MINOR.PATCH` или canonical `MAJOR.MINOR.PATCH-rc.N`, где N >= 1.
Leading zeros, `v` в CLI, другие prereleases и build suffix не допускаются.
SemVer precedence запрещает rc.2 → rc.1, повтор RC и stable → RC того же core.
RC не обновляет stable minor alias и публикуется как GitHub prerelease.
Для Phase 8 см. [release-readiness-v1.md](release-readiness-v1.md);
final v1.0.0 этой фазой не создаётся.
Существующие Phase images с валидной SemVer prerelease вроде `0.5.0-phase5` поддерживаются
как current/previous version; label сохраняется точно, без угадывания версии по tag.
Git release tag имеет префикс `v`.
`bookingctl version` показывает версию установленного CLI; `version CLIENT` — подтверждённую
версию клиента. Для старых deployments без version field сначала выполните doctor.

## Создание release

1. Изменить version.py и добавить секцию этой версии в CHANGELOG.md.
2. В changelog указать Added, Changed, Fixed, Migration notes, Breaking changes.
   Для v1.0.0 финализировать `docs/releases/v1.0.0-notes.md`: Main features,
   Production requirements, Upgrade notes, Known limitations. Draft блокирует stable.
3. Проверить migration policy ниже, Ruff/tests и canary. Просмотреть diff и CI.
4. Создать и отправить неизменяемый tag `vMAJOR.MINOR.PATCH` на проверенный commit.
5. Дождаться release workflow; проверить GHCR digest, Trivy artifact и GitHub Release.
   Workflow сверяет pull по registry digest с scanned image ID и прикладывает
   `release-manifest.json` (version, commit, digests, Alembic head, target OS).
   SPDX SBOM создаётся существующим Trivy; отсутствие optional SBOM не блокирует release.
6. Для v1.0.0 выполнить fresh install именно опубликованного image на
   `release-v1-final`, Telegram/conversation smoke, backup/restore, RC → stable update
   и production-check. Только после этих gates отмечать RELEASED.
   Реального клиента автоматически не устанавливать.
   Дальнейший controlled rollout: [updates.md](updates.md).

Release workflow выполняет тот же reusable CI: реальные PostgreSQL 17/Redis 7.4,
Alembic, unit/integration, production image build, DR и двухклиентский rollout smoke.
Только после CI проверяется точное совпадение tag/version.py/changelog, собирается образ,
сканируется Trivy и публикуется **этот же** образ. HIGH/CRITICAL findings блокируют публикацию;
низкие уровни её не блокируют. JSON findings сохраняются даже при ошибке scan.
Сбой scanner/registry тоже останавливает публикацию. Это не замена security audit.

GHCR: `ghcr.io/polen452294/booking-bot`. Stable теги `X.Y.Z` и `X.Y`
(удобный mutable minor alias); RC публикует только полный `X.Y.Z-rc.N`.
Release также включает `booking-socket-proxy`, `booking-postgres` и `booking-redis`.
Каждый полный tag защищён guard, каждый точный image сканируется до push.
`latest`/`stable` не публикуются. Full-version tag не переиспользуется: workflow отказывается
перезаписать существующий tag и останавливается при неопределённой доступности registry.
Package owners должны запретить ручное изменение full tags организационной политикой:
GHCR tags сами по себе mutable. Клиенты поэтому фиксируются дополнительно по image ID.
Minor alias отражает последнюю опубликованную release в этой minor-ветке; выпускать её
версии последовательно. При частичном publish не пересобирать full tag: восстановить
alias/Release вручную из уже опубликованного digest или выпустить новый patch.

OCI labels: version, revision (Git SHA), source. APP_VERSION обязателен при build и должен
совпасть с version.py. Релизная версия внутри package совпадает с OCI version. Для локальной
сборки см. README; произвольный локальный tag допустим, production update использует GHCR.

GitHub Actions используют штатный GITHUB_TOKEN (`packages:write`, `contents:write` только
в publish job). CI read-only и не получает production secrets. Actions pin по major;
scanner pin `aquasec/trivy:0.74.0`, Python base pin по digest. Нет SSH/VPS credentials,
`set -x`, push-to-production или автоматического обновления при merge.
Для private GHCR оператор отдельно настраивает Docker login с read:packages и stdin;
токены не передаются bookingctl и не сохраняются в repo. Настройки package access/visibility
должен применить владелец GHCR. Workflows не создают production rollout.

## Воспроизводимые зависимости

Runtime: requirements.lock; CI/operator extras: requirements-dev.lock; build tooling:
requirements-build.lock. Все transitive версии и hashes фиксированы для Python 3.12 Linux.
Docker/CI используют `--require-hashes`, затем install package `--no-deps --no-build-isolation`.
Lock-файлы получены с ограничениями из работающего Phase 5 окружения. По результатам
scan обновлены только aiohttp 3.14.2 → 3.14.3 и urllib3 2.7.0 → 2.8.0; массового upgrade нет.
Официальный Python Alpine base заменил Debian base после сравнительного scan;
реальные DR/rollout должны проходить на этом runtime, включая musl wheels.
`uv` — инструмент оператора, не runtime dependency. Обновлять locks отдельным reviewed PR:

```sh
uv pip compile pyproject.toml --python-version 3.12 --python-platform linux --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --extra backup-s3 --python-version 3.12 --python-platform linux --generate-hashes -c requirements.lock -o requirements-dev.lock
uv pip compile requirements-build.in --python-version 3.12 --python-platform linux --generate-hashes -c requirements-dev.lock -o requirements-build.lock
```

Существующие pins сохраняются без `--upgrade`; changes в locks нужно просмотреть и проверить.
Build pin/digest обновлять явно ради security fixes. Это воспроизводимый dependency set,
не обещание побитово одинакового image: package indexes и build infrastructure внешние.
Публикация сейчас linux/amd64; ARM64 acceptance пока не выполнен.

## Migration policy

Phase 8 release gate проверяет также Traefik v3.7.13 и versioned hardened images
PostgreSQL 17 и Redis 7.4: HIGH/CRITICAL останавливают публикацию,
scan JSON сохраняется в artifacts. Эта проверка не заменяет сканирование pinned
storage IDs уже установленного клиента. Локальный audit обнаружил открытые
storage findings; подробности в [production-report.md](production-report.md).

Одна Alembic head. Forward migration, минимальные блокировки, backward-compatible и
non-destructive изменения. Обязательны backup и реальный migration test с бизнес-данными.
`DROP TABLE/COLUMN`, изменение типа с потерей данных, необратимый backfill требуют явных
Migration notes/Breaking changes, оценки downtime/размера, reviewed recovery plan и canary.
Такие изменения нельзя скрывать в patch release. CI headings не заменяют review SQL.

Expand/contract: A добавляет новое поле, приложение поддерживает старое и новое;
B переключает чтение/запись и переносит данные; C удаляет старое после отдельного review.
Не применять destructive contract пока есть installations на прежнем image.
Bookingctl выбирает консервативную классификацию: совпавшая head — application-only rollback;
любое изменение head или неопределённый результат миграции — database restore required.
Даже backward-compatible новая head не включает автоматическое возвращение старого image.
`alembic downgrade` не выполняется при recovery.

Primary references: [GHCR authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
[official Docker build action](https://github.com/docker/build-push-action),
[uv locking](https://docs.astral.sh/uv/pip/compile/),
[Trivy exit codes](https://www.trivy.dev/docs/dev/guide/configuration/others/).
