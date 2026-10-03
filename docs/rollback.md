# Release recovery

Сначала остановить rollout и посмотреть `bookingctl status CLIENT`, `history`, `doctor`,
`logs`. Там есть confirmed old version, target, active image ID, backup и migration status.
Не удалять volumes, lock-файлы, state, previous image и backups. Recovery затрагивает
только выбранного клиента; других клиентов не выключать.

## Application-only rollback

```sh
bookingctl rollback anna-tattoo
bookingctl doctor anna-tattoo
```

Допустим, когда текущая Alembic revision равна сохранённой previous revision и не было
попытки перейти на другую head. Bookingctl проверяет saved image ID/version и отсутствие
admin container, останавливает API/worker, возвращает exact previous image, пересоздаёт
API/worker, проверяет /live, /ready, worker/webhook и doctor. Затем подтверждает старую
версию и пишет history. Никаких DB downgrade/migration команд этот путь не выполняет.
Проверки блокируют слепой rollback при изменённой/неизвестной схеме. После recovery повторный
rollback без нового update недоступен. При аварийном RUNNING attempt rollback также допустим
после проверки схемы; unfinished create/configure/restore требуют своих recovery команд.

## Database restore required

Любая отличающаяся Alembic revision или начатая миграция на другую head классифицируется
консервативно как `database restore required`, даже если изменение объявлено compatible.
После failed migration не считать прежнюю revision доказательством отсутствия частичных
изменений. Универсальный автоматический `alembic downgrade` запрещён.

Проверить выбранный backup и оценить потерю записей, сделанных после его создания:

```sh
bookingctl history anna-tattoo
bookingctl backup verify anna-tattoo BACKUP_ID_FROM_ATTEMPT
bookingctl rollback anna-tattoo --restore-database --yes
bookingctl doctor anna-tattoo
```

`--restore-database` является явным destructive recovery, `--yes` — подтверждением замены
данных этого клиента. Сначала сохраняется проверенный safety backup **активной** БД под
активным image, затем выбирается previous image и выполняется Phase 5 restore из pre-update
backup. Recovery использует exact image, single saved head, pg_restore и config snapshot,
сбрасывает Redis только этого клиента и проверяет doctor. Предварительная safety копия
сохраняется; она не создаётся под уже выбранным старым image. Если safety backup не удался,
эта команда прекращается до замены данных. Dangerous skip для update/rollback отсутствует.

Обычная `bookingctl restore CLIENT BACKUP_ID` по-прежнему требует совпадения текущего image
с backup. Поэтому после смены image используйте release recovery команду выше, которая
безопасно выбирает старый image до restore. Потеря host/volume/deployment files: отдельный
Phase 5 [disaster recovery](disaster-recovery.md), [backup/restore](backup-restore.md).
Lost-volume restore не подменяется image rollback и может потребовать ручной disaster recovery.

При ошибке rollback остаётся FAILED, API/worker останавливаются где возможно. Сохранены
previous_release, pre-update backup, safety_backup и restore logs. Проверить admin container
и DB; только затем повторять recovery. Lock освобождается автоматически, незавершённая
операция в metadata не исчезает от перезапуска CLI. Не использовать `create --resume`
для failed release: это могло бы применить migration/configure неподходящего image.
