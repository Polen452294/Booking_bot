# Controlled production updates

Перед rollout должна быть доказана Phase 5 процедура backup/restore для актуального кода.
CI запускает operations smoke с dump/restore и проверкой данных, затем release rollout smoke.
Настоящий production бот, DNS/TLS/webhook, off-host storage и Linux timer проверяются
оператором отдельно. Для начала используйте staging/test client, затем несколько
production clients, затем остальные. Малый downtime допустим, безопасность данных приоритетна.

## Один клиент

```sh
bookingctl version
bookingctl version anna-tattoo
bookingctl update anna-tattoo --version 1.3.0 --dry-run
bookingctl update anna-tattoo --version 1.3.0
bookingctl doctor anna-tattoo
bookingctl history anna-tattoo
```

Используйте ту же `--root` и `--backup-root`, что при create/backup. Global flags ставятся
перед subcommand. Цель — версия, **новее** текущей; update не является downgrade командой.
Нельзя обновлять FAILED/CREATING/нездорового клиента обычным update.

Порядок: проверить registry/config/doctor, DB/Redis, текущую OCI version, доступность target
manifest, свободное место → создать и проверить pg_dump/SHA256/pg_restore listing backup →
при настроенном S3 загрузить/проверить копию → pull target → проверить OCI version/source/SHA,
package version, текущую Alembic revision и одну forward head → остановить API/worker →
зафиксировать active image ID в state/.env → Alembic upgrade head → пересоздать только
API/worker → Compose healthchecks, /live, /ready, worker-health, public webhook-status → doctor.
PostgreSQL/Redis, volumes, profile/услуги/график и другие клиенты не пересоздаются.
Update никогда не выполняет `booking-admin configure`, чтобы не перезаписать owner data.

Дисковая проверка: 2 размера БД + 1 GiB reserve на host registry/backup filesystems,
на production Linux также на DockerRootDir. Это оценка; pull/backup всё равно могут
отказать при исчерпании ресурсов. Docker image availability — read-only manifest inspection;
целевые labels проверяются после обязательного backup и pull.

Dry-run не меняет registry/.env/metadata, не создаёт backup, не скачивает image и не
применяет миграции. Выполняет read-only doctor/DB/registry probes и показывает current/target,
image, backup action, текущую revision и affected services. Target migration graph становится
доступен только после pull; план явно указывает, что эта проверка ещё впереди.

## Metadata и состояния

`DEPLOYMENT_ROOT/CLIENT/state.json`: confirmed `current_version`, `previous_version`,
`updated_at`, `backup_id`, release_history, release_attempt и previous_release (image ID/ref,
version, revision). `.env` BOOKING_IMAGE и Compose `${BOOKING_IMAGE}` фиксируют active image
по immutable local SHA256 ID, image_reference сохраняет читаемый SemVer tag.
При update active image может отличаться от confirmed version; status показывает обе
через image_id/image_reference и release_attempt. Confirmed current_version меняется
только после успешной проверки всех сервисов. Не редактировать state/.env вручную.

`bookingctl status CLIENT` показывает UPDATING, RESTORING, BACKING_UP, READY или FAILED,
stage, attempt/migration/recovery и runtime. Для незавершённой/неуспешной операции exit != 0.
Каждый успешный/неуспешный update/rollback записывается в release_history.
Restore также записывается в историю. Retention сохраняет backup, на который ссылается
последний update/recovery, независимо от возраста. После прерванного обычного backup
повторная `backup CLIENT` под свободным lock завершает копию и восстанавливает предыдущий status.
Interrupted RUNNING attempt сохраняется на диске и требует явной recovery.

## Locks и rollout

Общий per-deployment filesystem lock находится в `CLIENT/.lock`. Общий registry `.lock`
дополнительно исключает существующие create/configure/expose/start/stop и второй rollout.
Порядок всегда registry → client; OS lock освобождается при завершении/падении процесса.
Lock-файлы не удалять. Это намеренно строгая сериализация: поддерживаемые операции даже
для разных клиентов одного registry выполняются по очереди. Другие работающие клиенты
продолжают обслуживать запросы. Один registry/root на сервер, локальный filesystem;
несколько копий registry и ручные Docker/SQL команды обходят защиту и не поддерживаются.
Оставшийся one-off admin container после сбоя тоже блокирует recovery; сначала проверить его.

```sh
bookingctl update-all --version 1.3.0 --dry-run
bookingctl update-all --version 1.3.0
```

Клиенты обрабатываются последовательно по slug: backup → update → doctor → следующий.
При первом failure команда выходит с ошибкой и остальных не трогает. Continue-on-error
не предусмотрен. При повторе уже обновлённые READY clients пропускаются только после doctor;
FAILED клиент сначала восстановить. В dry-run проверяются все доступные планы до первой ошибки.

## Ошибка

Не повторять rollout вслепую. `status`, `history`, `doctor`, `logs` показывают старую/целевую
версию, backup id, migration_status, recovery classification. `update-health.json` содержит
последний doctor report; Docker errors — redacted `last-error.log`.
Confirmed версия остаётся старой. Новые API/worker при ошибке останавливаются, если их image
уже выбран; preflight/migration-validation ошибки до замены оставляют прежние сервисы.
Backup failure никогда не имеет override. Восстановление: [rollback.md](rollback.md).

Acceptance сценарий: `tests/release_rollout_smoke.py` создаёт два новых клиента, seed business
rows, локальный registry, новый image с additive migration; проверяет backup/pull/update,
business data и doctor, проверяет peer API/worker/DB во время update A, затем обновляет B.
Отдельный injected readiness failure подтверждает FAILED, backup, остановку rollout,
здорового B и явный restore. Synthetic tokens; реальных Telegram сообщений нет.
