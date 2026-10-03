# Production report: Phase 7 / 0.7.0

Дата: 2 октября 2026. Локальная среда: Windows / Docker Desktop, Linux containers,
linux/amd64. Изменения находятся в рабочем дереве; commit, push, GitHub Release и
публикация GHCR не выполнялись. Предшествующие изменения Phase 1–6 сохранены.
Новых продуктовых функций и схемных миграций в этой фазе нет.

**Решение: безусловный production-ready не подтверждён.** Приложение, управление
deployment и локальное восстановление проверены; остаются уязвимые storage
images и внешняя acceptance на выделенном Linux VPS. Успешный `production-check`
не заменяет vulnerability scan, off-host restore и reboot test.

## 1. Итоговая архитектура

Один клиент — отдельный специалист и Telegram-бот. FastAPI принимает webhook,
бизнес-сервисы работают с PostgreSQL; worker доставляет durable notification
jobs. Redis хранит временные holds/FSM/leases/heartbeat. Traefik маршрутизирует
HTTPS по домену. Host bookingctl управляет приватным registry, backup/update/
restore и диагностикой под locks. [Подробная схема](architecture.md).

## 2. Количество сервисов

Четыре постоянных контейнера на клиента: API, worker, PostgreSQL 17, Redis 7.4.
Admin/migration — одноразовый контейнер. На host два общих постоянных контейнера:
Traefik и restricted socket proxy; init-acme одноразовый. Monitoring и backup
timer работают на Linux host без новой постоянной инфраструктуры.

## 3. Security model и audit

Раздельные projects, data/egress networks, volumes, DB/Redis credentials, tokens,
webhook secrets и TOML. Нет публичных DB/Redis/worker ports. API/worker: UID 10001,
read-only filesystem, tmpfs, cap_drop ALL, no-new-privileges. Production docs и
OpenAPI отключены, webhook проверяет secret header. Registry/backups/host env
приватны; JSON logs скрывают secrets/phones, исключают exception payloads и SQL
parameters. Для всех services ограничены Docker logs 10m × 3.

Traefik больше не монтирует socket; socket proxy имеет отдельную private network,
не публикует port, запрещает POST/start/stop/exec/images/volumes/secrets. GET
container inspect нужен Traefik и способен читать environment: shared proxy и
Docker administrator остаются доверенной host boundary. Docker daemon rootful;
PostgreSQL/Redis сохраняют официальный entrypoint для ownership/drop privileges.

Trivy 0.74.0, HIGH/CRITICAL, локальные JSON scans:

| Image | HIGH | CRITICAL | Результат |
|---|---:|---:|---|
| booking-bot:0.7.0-phase7-final | 0 | 0 | PASS |
| booking-socket-proxy:0.7.0-phase7-final | 0 | 0 | PASS |
| traefik:v3.7.13 | 0 | 0 | PASS |
| postgres:17-alpine (cached) | 30 | 1 | BLOCKER |
| redis:7.4-alpine (cached) | 6 | 0 | BLOCKER |

PostgreSQL digest: `sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193`;
Redis digest: `sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99`.
PostgreSQL findings: libcrypto3/libssl3 3.5.7, libuuid 2.42.1 и gosu с Go 1.24.6,
включая CRITICAL CVE-2025-68121. Redis: libcrypto3/libssl3 3.3.7-r0.
Это число package findings, не число независимых атак и не доказательство
эксплуатации в данной конфигурации. Два повторных `docker pull` официальных tags
завершились registry TLS handshake timeout; исправление storage не подтверждено.

Upstream socket proxy v0.5.0 также имел HIGH OpenSSL finding. Добавлен собственный
versioned image на pinned upstream digest с адресным обновлением OpenSSL до
3.5.9-r0; повторный scan и реальные ACL checks прошли.
[Upstream ACL documentation](https://github.com/Tecnativa/docker-socket-proxy).
Release workflow блокирует публикацию при HIGH/CRITICAL во всех пяти images.
Это не заменяет scan старых pinned storage IDs действующих deployments.

## 4. Backup model

PostgreSQL custom dump + deployment config/manifest/checksums; private local
storage, явная проверка до restore, optional S3-compatible copy. Locks, stopped
writer processes и safety backup защищают restore. Retention сохраняет daily/
weekly/monthly buckets, последний valid backup и активные recovery checkpoints;
unreferenced safety backups ограничены тремя newest по умолчанию. Status читает
metadata/stat без повторного hashing всех dumps; verify/production-check
выполняют полную проверку. Metadata не подписана: доверять только своим backups.
Real external S3 round-trip в этой сессии не выполнен.
[Процедуры](backup-restore.md).

## 5. Update model

Immutable application image, backup-first, version/head validation, migration,
health/doctor до подтверждения current_version. Failed update сохраняет backup
и FAILED checkpoint, останавливает последовательный rollout; peer не меняется.
При изменении Alembic head rollback требует DB restore, downgrade не применяется.
Exact прежние Compose templates принимаются для backup и обновляются при явном
update/start/restart; custom templates отклоняются, volumes/networks сохраняются.
Phase 7 исправляет API/worker restart на unless-stopped и включает log rotation.
Shared proxy обновляется отдельно с сохранением ACME volume. Storage image IDs
автоматически не обновляются с application release.

## 6. Monitoring model

Client/fleet status, client/host doctor, resources, versions, production-check.
Checks покрывают liveness/readiness, DB/Redis, heartbeat, queue counts, backup
freshness, disk/volume filesystem, image/schema/version drift, proxy/network,
permissions, secrets/TLS/webhook и Linux RAM/load. Severity: OK/WARNING/ERROR/
CRITICAL. Пороги конфигурируемые; дорогое чтение backup dumps не входит в обычный
pass. Busy operation lock откладывает наблюдение, сохраняя прежние alerts.
Production-check не пишет бизнес-данные, запрещает READY при ERROR/CRITICAL или
deferred checks. [Пороги и команды](monitoring.md).

## 7. Alert model

Отдельный host monitoring bot/chat, одноразовый pass и systemd timer; credentials
не передаются клиентам. Private persistent state: first failure/change severity,
cooldown/dedup, send failure retry без ложного acknowledgement, recovery message.
Backup/restore/update failures имеют durable latch; успешный recovery его снимает.
Локально проверены captured messages, restart persistence, cooldown, partial
deferred checks и recovery. Live Telegram delivery и реальный Linux timer ещё
требуют acceptance. Полную потерю host должен обнаруживать внешний monitor.

## 8. Restart/reboot tests

Реальные restart API/worker/Redis/PostgreSQL: data и peer сохранены. Worker SIGKILL
вне его PID namespace: RestartCount увеличился, worker/readiness восстановились.
Остановка DB/Redis дала HTTP 503 readiness; остановки worker/API выявлены doctor.
Failure/dedup/recovery alerts проверены с capture sender. Shared proxy остановлен
и перезапущен: routing восстановился, backend оставался доступен по loopback.
Полный reboot Windows/Docker host не выполнялся: на нём есть другие проекты.
Linux reboot/systemd acceptance — обязательный внешний шаг.

## 9. Disaster recovery

Два disposable клиента с seeded business data. Фактически удалены только их
проверенные dedicated PostgreSQL volumes; restore восстановил точные строки
13 таблиц и config. Wrong-client restore отклонён. Проверены safety backup,
failure gate и явный disaster recovery без safety backup после потери DB.
Peer container IDs/data неизменны. Final DR выполнен на окончательном app image.
Восстановление на другом VPS из off-host backup остаётся непроверенным.

## 10. Cross-client isolation

Раздельные DB/Redis volumes и credentials, bot/header tokens, синтетические
domains/webhook origins. Peer остаётся healthy при restart/outage/restore/update
другого клиента. Прямая TCP-проверка API/worker A → PostgreSQL B отклонена;
own DNS разрешает свою БД, volumes/passwords/tokens/origins различаются.
Socket proxy недоступен с client proxy network.
Real DNS/public TLS/live webhook двух клиентов ещё не проверены.

## 11. Functional regression

Regression включает клиентскую запись/слоты/отмену, specialist cabinet, webhook
idempotency, notification claims/preferences/retry, concurrency, backup/update/
restore gates. Новые real-DB tests проверяют bounded failed-job list, transient
retry, permanent/unknown errors, foreign client, cancelled booking и row locks.
Safe retry пишет audit; payload/customer text в operator listing не выводится.
Полный пользовательский Telegram end-to-end с реальными bots не выполнен.

## 12. Performance smoke

Controlled synthetic workload: 96 запросов, concurrency 32, `/live`, `/ready` и
valid ignored Poll webhook updates. Все ответы HTTP 200, business rows сохранены.
Это короткий smoke, не capacity/SLA benchmark: webhook payload не создаёт
запись и не вызывает sendMessage. Final: 0.820 s суммарно, p95 0.654 s,
maximum 0.805 s. Снимки business rows до/после совпадают.

## 13. Измеренные ресурсы

Docker Desktop VM: 20 CPU, 8 169 623 552 bytes RAM (7.609 GiB), x86_64.
Final per-container snapshot:

| Client | Service | RAM MiB | CPU % |
|---|---|---:|---:|
| A | API | 216.8 | 0.12 |
| A | worker | 178.1 | 68.09 |
| A | PostgreSQL | 33.32 | 3.21 |
| A | Redis | 3.984 | 0.61 |
| B | API | 192.5 | 0.10 |
| B | worker | 178.6 | 0.00 |
| B | PostgreSQL | 55.61 | 2.97 |
| B | Redis | 4.008 | 2.84 |

Сумма RAM: A 432.204 MiB, B 430.718 MiB, восемь контейнеров 862.922 MiB.
Shared proxy/admin, daemon и другие проекты в эту сумму не входят. A DB size
9 877 171 bytes; pending/processing/failed queue counts — 0/0/0 после smoke.
Объём файлов backup по status A: 235 076 bytes; одна incomplete directory осталась после
намеренного write-denial, doctor показал WARNING. Latest backup прошёл полную
checksum/pg_restore verification. Host disk: 86.5% used, около 134.5 GB free;
порог WARNING сработал, реальное заполнение диска не выполнялось.
CPU worker A в этой выборке высокий; короткий snapshot не позволяет считать
это устойчивой нагрузкой или исключить overhead health checks. Требуется
профилирование/soak на целевом VPS перед расчётом числа клиентов.
`docker stats --no-stream` — один момент времени после нагрузки/restarts, не
steady-state или peak profile. Эти результаты не устанавливают минимальную VPS
конфигурацию, гарантированный лимит клиентов или production throughput.

## 14. Ruff/tests/build/Compose

Baseline: Ruff PASS; pytest 272 passed/47 deselected; integration 47 passed.
Phase 7: Ruff PASS; pytest **310 passed / 54 deselected** (268.63 s);
integration **54 passed** (9.73 s), isolated real PostgreSQL/Redis.
Отдельный новый legacy Compose refresh test прошёл вместе с 55 bookingctl tests.
Docker build app/socket proxy PASS; image metadata 0.7.0 PASS; production Compose
`config --quiet` PASS; pip check/compileall/diff whitespace check PASS.
Actionlint PASS (shellcheck/pyflakes отключены). Typechecker в проекте не настроен;
не заявляется выполненная статическая проверка типов. GitHub CI/release pipeline
из этого рабочего дерева ещё не запускался.

Final real rollout PASS: два клиента, backup, push/pull через disposable registry,
additive test migration, injected failure после запуска новых services, остановка
fleet rollout, durable failure alert/dedup и recovery alert; rollback восстановил
точные старые DB rows и image. Final proxy PASS: HTTPS routing с лабораторным
self-signed certificate, allowed GET, denied POST/images, network isolation,
proxy outage/backend alive и restart. Это не публичный ACME acceptance.

Реальный CLI `production-check` с правильным test backup-root вернул exit 1,
machine-readable `NOT PRODUCTION READY`/CRITICAL. Backup integrity при этом OK;
blockers: public deployment отсутствует, синтетический bot не имеет live identity,
host inventory содержит семь небезопасных configured port bindings других
лабораторных контейнеров. Inventory учитывает и stopped container configurations;
это не результат внешнего сканирования открытых TCP ports. Чужие контейнеры и
порты не изменялись. Capture sender получил 10 messages в operations scenarios,
live Telegram сообщения не отправлялись.

Evidence (локальные ignored artifacts, не включены в Git):

- `tmp/phase7-final-unit.log`, `tmp/phase7-final-integration.log`;
- `tmp/phase7-final-operations.log`, `tmp/phase7-final-ops/clients-results.json`;
- `tmp/phase7-final-rollout.log`, `tmp/phase7-final-proxy.log`;
- `tmp/phase7-final-production-check.json` — ожидаемый NOT PRODUCTION READY;
- `tmp/phase7-final-scan-*.json`, storage pull failure logs;
- private test registry/backup directories сохранены для диагностики. Созданные
  Phase 7 client stacks и dedicated integration services после проверок остановлены;
  данные/volumes сохранены. Shared proxy smoke удалил только собственные контейнеры.

Reproduction: `.venv/Scripts/ruff.exe check .`, `.venv/Scripts/pytest.exe`,
`.venv/Scripts/pytest.exe -m integration` с isolated test DB/Redis. Docker scenarios:
`tests/operations_smoke.py`, `tests/proxy_operations_smoke.py`,
`tests/release_rollout_smoke.py` с новым disposable `--root` и test image.
Эти scripts разрушают только собственные тестовые volumes; не использовать
production registry. Secrets/config нельзя публиковать вместе с evidence.

## 15. Known issues

Открытые storage vulnerability findings перечислены выше. Повторный retry после
неопределённого network timeout может дублировать Telegram message: sendMessage
не имеет idempotency key. Locks рассчитаны на один управляющий host. Retention
не удаляет активные checkpoints, incomplete/corrupt backup artifacts автоматически
не чинит; operator cleanup и remote lifecycle остаются явными задачами.
Raw PostgreSQL/third-party logs доступны только доверенному оператору: formatter
приложения не редактирует их на источнике. Секреты на host не шифруются при доступе
root/Docker administrator; для внешних backups требуется storage encryption policy.

## 16. Operational limitations

Maintenance выбранного клиента создаёт downtime/502/503; Telegram повторяет
webhook. Нет автоматического failover/PITR/zero-downtime DB restore. Monitoring
зависит от работающего host, Docker, internet/Telegram; fleet pass последовательный
и при росте installations длится дольше timer interval. Production DNS/cert/network
checks зависят от внешних систем. ARM64 не принят; поддерживаемый release target
linux/amd64. Docker-only port audit не заменяет `ss`/firewall/external port check.
Публикация application и socket images последовательная, не атомарная: до upgrade
оператор проверяет доступность обоих full-version tags; частичный release требует
исправления release procedure, без перезаписи уже опубликованного full tag.

## 17. Приоритеты после локальной фазы

### BLOCKERS

1. Исправить/заменить и повторно просканировать PostgreSQL/Redis images; сохранить
   major 17, проверить entrypoint/UID и restore/restart на копии, затем перевести
   pinned storage IDs действующих clients. Закрыть HIGH/CRITICAL до принятия риска.
2. Выполнить acceptance на выделенном Linux VPS: real production ACME/DNS,
   getMe/webhook, запись/notification двух real bots, live monitoring messages,
   backup/monitor timers и full host reboot с recovery.
3. Реальный off-host backup/upload/download/restore на отдельном host, включая
   проверку credentials/access/encryption/retention выбранного storage.
4. Опубликовать проверенные versioned application/socket images и выполнить
   pull/production-check на целевом host. Локальная 0.7.0 не является опубликованным
   GitHub/GHCR release. `PRODUCTION READY` в синтетической лаборатории не заявляется.

### IMPORTANT BUT NON-BLOCKING

- Внешний uptime monitor для полной потери VPS; SLA/support escalation.
- Capacity soak на выбранном VPS с настоящими booking workloads, дисковым ростом,
  alerts и несколькими клиентами; настроить фактические resource budgets.
- Регулярный DR drill, review protected backup checkpoints и off-host lifecycle.
- Review third-party raw logs/host secret handling и операционные credentials.
- Учёт частичной публикации двух images и документированное окно shared proxy upgrade.

### OPTIONAL IMPROVEMENTS

- ARM64 build и hardware acceptance; внешний time-series dashboard при нужном масштабе.
- Подписанные images/backups, дополнительные supply-chain provenance checks.
- Оптимизация downtime и централизованное управление при подтверждённой нагрузке.

## 18. Production-ready conclusion

Реализована и локально проверена обслуживаемая основа: isolated installations,
backup/update/restore, diagnostics, ограниченные logs, безопасные operator retries,
persistent alerts и воспроизводимые failure tests. Технически назвать проект
безусловно production-ready сейчас нельзя: известные storage CVE и внешняя
acceptance не закрыты. Перед коммерческой установкой выполнить BLOCKERS и
сохранить новый production-check/scan/reboot/off-host restore evidence.
