# Phase 6 — release, update и recovery

Дата проверки: 2026-10-01. Версия приложения: `0.6.0`.
Phase 6 реализована и проверена локально, включая реальные PostgreSQL, Redis,
Docker build/push/pull, миграцию, потерю volume, failed update и recovery.
Публикация в GHCR и запуск workflows на GitHub ещё не выполнялись.
Phase 7 не начата.

## 1. Versioning

Единственный источник версии — `src/booking_bot/version.py`.
Hatch читает его для package metadata; Docker build требует совпадающий
`APP_VERSION`. Release tag имеет вид `v0.6.0` и должен точно соответствовать
версии исходников и разделу CHANGELOG. Новые release targets — стабильные
SemVer `MAJOR.MINOR.PATCH`; существующий образ `0.5.0-phase5` поддерживается
как предыдущая версия для upgrade и recovery.

## 2. Версия deployment

`<deployments-root>/<slug>/state.json` содержит `current_version`,
`previous_version`, ссылки на backup и `release_history`. Подтверждённая
`current_version` меняется только после успешных health checks и doctor.
Выбранный образ и незавершённая операция записываются отдельно, поэтому
FAILED update не выдаётся за успешно установленную версию. Фактические
OCI labels проверяются при preflight.

## 3. Docker tags

Release публикует `ghcr.io/polen452294/booking-bot:X.Y.Z` и alias `X.Y`.
Полный tag повторно публиковать запрещено: guard проверяет manifest и
аутентифицированный GitHub Packages API. Ошибки доступа/сети не считаются
доказательством отсутствия версии. Deployments используют проверенный image
ID, сохраняя исходный reference; alias не используется для update.
OCI labels содержат version, полный Git revision и repository source.

## 4. CI

`.github/workflows/ci.yml` запускается для PR, push и reusable workflow.
Он устанавливает зависимости из hash-pinned lockfiles, выполняет Ruff,
unit и integration tests с настоящими PostgreSQL/Redis, собирает Docker
image и запускает DR и полный двухклиентский rollout на собранном образе.
CI не обращается к production deployment и не требует его секретов.

## 5. Release pipeline

`.github/workflows/release.yml` для `v*` сначала выполняет CI, затем
проверяет tag/CHANGELOG, запрещает перезапись полного tag, собирает образ,
сканирует его официальным Trivy и публикует именно проверенный образ.
HIGH/CRITICAL блокируют публикацию. Создаётся GitHub Release с notes из
CHANGELOG. Используется встроенный `GITHUB_TOKEN`; production credentials
не требуются. Release jobs сериализованы, автоматического deployment нет.

## 6. Update

`bookingctl update SLUG --version X.Y.Z --dry-run` проверяет deployment,
doctor, текущий образ/версию/схему, доступность target и место для backup,
не меняя deployment и не выполняя pull/migration.
Target migration graph проверяется после обязательного backup и pull;
dry-run явно сообщает об отложенной проверке.

Обычный update получает locks, сохраняет operation journal, создаёт и
верифицирует backup, при настроенном S3 проверяет remote copy, затем делает
pull и target preflight. Останавливаются API/worker выбранного клиента,
выполняется upgrade, сервисы запускаются и проверяются `/live`, `/ready`,
worker health, public webhook при его наличии и doctor. Только затем
deployment становится READY с новой подтверждённой версией.

## 7. Backup

Update не продолжает работу без проверенного backup. Используются
существующие dump/manifest/checksum и проверка archive, сохраняются image,
schema и конфигурация. Настроенный remote upload с read-back должен пройти
до pull. Retention защищает backup текущей release/restore операции,
последний verified backup и safety backup. Прерванный BACKING_UP сохраняет
предыдущее состояние для восстановления, а не затирает его повторным запуском.

## 8. Migrations

Target image запускает release-check: package version совпадает с target,
Alembic имеет единственный head, текущая revision соответствует preflight,
переход направлен вперёд и включает текущую revision. Выполняется только
`alembic upgrade head`. Неопределённый результат попытки migration к новому
head консервативно требует DB restore для recovery. Downgrade не вызывается.
Expand/contract и несовместимые изменения описаны в runbooks.

## 9. Application rollback

`bookingctl rollback SLUG` выбирает сохранённый точный предыдущий образ,
проверяет текущую schema revision и выполняет health/doctor проверки.
Если схема совпадает и нет неопределённой migration, достаточно image-only
rollback. В остальных случаях команда отказывается делать такой rollback.
Поддерживается recovery незавершённой или failed release операции.

## 10. Database restore

После изменения схемы или неопределённого исхода migration требуется
`bookingctl rollback SLUG --restore-database --yes`.
До выбора старого образа создаётся verified safety backup текущего состояния,
затем восстанавливается проверенный pre-update backup, запускается прежний
образ и проверяется doctor. Restore может отменить записи, сделанные после
backup; оператор принимает это явно. Автоматического DB rollback нет.

## 11. Locks и статусы

Update, rollback, backup и restore используют общий registry lock и lock
deployment. Существующие registry writers также остаются под общим lock.
Это намеренно строго сериализует операции одного registry, включая разных
клиентов. Статусы UPDATING, RESTORING, BACKING_UP и FAILED и operation journal
позволяют отличить работу, interruption и завершённое действие.

## 12. Update-all

`bookingctl update-all --version X.Y.Z [--dry-run]` обрабатывает клиентов
последовательно, начиная с первого по slug; READY client на target версии
пропускается только после doctor. Первый failure немедленно останавливает
rollout. Остальные deployments не меняются. Canary workflow описан в
`docs/updates.md`; автоматического continue-on-error нет.

## 13. Реальный двухклиентский rollout

Финальный `tests/release_rollout_smoke.py` проверен на окончательном образе
`booking-bot:0.6.0-phase6-final`. Для тестовой `0.6.1` в отдельной копии
добавлена additive migration; она не входит в release исходники.
Выполнены реальные registry push/pull, verified backup, PostgreSQL migration
и запуск новых API/worker. Во время update A у B не менялись container IDs,
API/worker/DB/Redis оставались здоровыми; затем успешно обновлён B.
Содержимое 13 business tables сверено после действий.

Лог: `tmp/phase6-rollout-verified.log`:

```text
TWO-CLIENT ROLLOUT PASSED: backup, real pull/migration, doctor, peer isolation
```

## 14. Failure и disaster recovery

После реального запуска target services внедрён детерминированный readiness
failure. Update-all остановился на A, состояние стало FAILED, прежняя
подтверждённая версия и verified backup сохранены; B остался здоровым и
не изменился. Recovery с safety backup восстановил точный старый image и
DB. Image-only rollback после migration был отвергнут.

```text
FAILED UPDATE PASSED: backup retained, FAILED, rollout stopped, peer healthy
RECOVERY PASSED: verified safety backup, exact old image/DB, no downgrade
```

Отдельный реальный DR на финальном образе удалял только тестовый PostgreSQL
volume A и восстанавливал данные; B не изменялся. Лог:
`tmp/phase6-dr-verified.log`:

```text
DR ACCEPTANCE PASSED: A/B isolation, volume loss, safety backup, exact business rows
```

После recovery final fixtures имеют READY/complete: rollout A `0.6.0`, B
`0.6.1`; DR A/B `0.6.0`. Временные сервисы остановлены с сохранением volumes,
state, логов и backups. Использовались синтетические Telegram tokens;
подменялся getMe, реальные сообщения не отправлялись. Readiness failure
внедрён только для failure acceptance; migration/pull/restore были настоящими.

## 15. Проверки

Финальные результаты для окончательного исходного кода:

| Проверка | Результат |
| --- | --- |
| `ruff check .` | passed |
| `pytest` | 272 passed, 47 deselected |
| `pytest -m integration` | 47 passed, 272 deselected |
| Focused backup/S3/release/metadata/guard tests | 91 passed |
| `pip check` и compileall | passed |
| actionlint 1.7.12 | passed; shellcheck/pyflakes отключены |
| Production Compose config | passed |
| `release_metadata.py --tag v0.6.0` | passed |
| Финальный Docker build | passed |
| Trivy 0.74.0, HIGH/CRITICAL, exit-code 1 | 0 findings, exit 0 |
| DR, two-client update, failed update и recovery | passed |
| `git diff --check` | passed |

Trivy report: `tmp/phase6-trivy-final.json`.
Для strict scan выбран официальный Python 3.12 Alpine base, закреплённый
digest, и обновлены две зависимые библиотеки: aiohttp `3.14.3`, urllib3
`2.8.0`. Остальные baseline versions сохранены. CVE exceptions/ignore rules
не добавлялись. Образ запускается под UID/GID 10001, исходники root-owned.

## 16. Ограничения и оставшиеся внешние проверки

- Hosted GitHub Actions, GHCR publication и GitHub Release ещё не запускались.
  Нужны review/commit, push версии/tag и проверка доступа workflow к package.
  Локальный образ построен из dirty workspace с текущим HEAD в OCI revision;
  настоящий release будет построен из committed tagged source.
- Production Telegram, публичный domain/DNS/TLS/webhook не проверялись на
  синтетических локальных deployments. В update есть проверки public webhook,
  но реальная площадка требует acceptance после первой публикации.
- Реальный S3 bucket и Linux systemd backup timer не проверены в этой сессии;
  S3 ordering, failures и lock behaviour покрыты тестами без live credentials.
- Проверена архитектура linux/amd64; ARM64 rollout не проверен.
- Locks защищают один локальный registry/root. Ручные Docker/SQL операции и
  независимые registries обходят эти locks. Одновременный rollout из нескольких
  управляющих хостов не поддерживается.
- Tag immutability обеспечена release pipeline guard, а не запретом registry
  administrator переписывать tag; deployment pinning защищает выбранный image.
- Dry-run не скачивает target image, поэтому target migration graph окончательно
  проверяется после mandatory backup/pull и до остановки приложения.
- Scan актуален на момент проверки; новые HIGH/CRITICAL в будущем блокируют
  release до исправления. Массовому production rollout предшествует canary.
- Docker Desktop был восстановлен после ошибки stale runtime sockets.
  Старые runtime directories сохранены под суффиксом `.phase6-stale`; factory
  reset, удаление сторонних volumes и изменение сторонних deployments не делались.

## 17. Готовность к Phase 7

Основа для monitoring и финального production audit готова: versioned release,
CI, backup-first update, operation journal, locks, последовательный rollout и
проверенный recovery реализованы. Phase 7 можно начать отдельной задачей.
Production rollout требует успешного первого hosted release и проверки
реального canary клиента, его Telegram/webhook и backup storage.

Runbooks: `docs/releases.md`, `docs/updates.md`, `docs/rollback.md`.
Git commit/push/tag и production deployment в этой сессии не выполнялись.
