# Резервное копирование и восстановление (Phase 5)

Все команды выполняются на Docker host установленным `bookingctl`. Используйте один
registry root для всех клиентов и один operator account. Не запускайте миграции,
ручные SQL DDL и редактирование конфигурации параллельно с backup/restore.
Команды `bookingctl` сериализуются существующим registry lock; обычный backup не
останавливает приложения и не влияет на другие deployments.

## Состав и модель безопасности

PostgreSQL 17 — durable storage: businesses, профиль специалиста, услуги, клиенты,
записи и история, календарь, рабочие правила/исключения, настройки, приглашения,
notification jobs, transactional Telegram receipts и даже временные slot holds
(они уже хранятся в PostgreSQL). Копируется вся клиентская БД.

Redis содержит FSM с TTL, leases/cache idempotency и heartbeat. Его dump не нужен:
durable receipt уже находится в PostgreSQL. После restore Redis **только этого клиента**
очищается, пользователь начинает незавершённый диалог заново.

Каталог backup содержит ровно:

```text
database.dump     # pg_dump -Fc --no-owner --no-privileges
specialist.toml
metadata.json     # allowlist deployment state и storage image IDs
manifest.json
SHA256SUMS
```

`.env`, Telegram token, PostgreSQL/Redis passwords, webhook secret, creation journal,
master-invite.txt, S3 credentials, proxy ACME account/certificates и логи не экспортируются.
Compose восстанавливается из управляемого шаблона; hand-edited Compose запрещён при
backup/restore, чтобы не потерять неучтённую конфигурацию. PostgreSQL роли/пароли не
экспортируются. Дамп содержит персональные данные и должен считаться конфиденциальным.

Secret backup отключён. Telegram token храните отдельно в password manager/secret vault.
При потере VPS `recover-files` проверяет тот же bot ID и генерирует новые PostgreSQL,
Redis и webhook secrets. Для полного DR нужен доступ к Telegram token и точному образу
приложения. Храните release image по immutable digest или отдельно через `docker save`.
Не храните эти материалы только на том же VPS.

Linux: каталоги 0700, файлы 0600, operator владеет registry и backup root. Восстановленный
specialist.toml имеет 0644 внутри закрытого 0700 каталога для bind mount UID 10001.
Windows: существующий механизм private ACL ограничивает доступ текущим пользователем.
Systemd service по умолчанию запускается root: установка и registry должны принадлежать
этому же оператору; отдельный служебный аккаунт можно задать через systemd drop-in,
передав ему владение каталогами и доступ к Docker (он эквивалентен root-доступу).
Offline `pg_restore --list` validator на Linux запускается с UID/GID этого
оператора, без capabilities/network и с read-only backup mount. Это позволяет
проверять private archive без ослабления 0700/0600 permissions.
Validator использует hardened `booking-postgres` package той же версии,
что установленный host CLI. Этот image должен быть доступен в GHCR/локальном
Docker cache; произвольные images из backup metadata не исполняются.

## Ручной backup, проверка и статус

```bash
export BOOKING_BACKUP_ROOT=/opt/booking/backups
bookingctl --root /opt/booking/clients backup anna-tattoo
bookingctl --root /opt/booking/clients backup list anna-tattoo
bookingctl --root /opt/booking/clients backup verify anna-tattoo BACKUP_ID
bookingctl --root /opt/booking/clients status anna-tattoo
bookingctl --root /opt/booking/clients backup-all --retention
```

Альтернатива environment: глобальный `--backup-root /path`. ID имеет формат
`20261001T030000Z-a1b2c3d4` (UTC + случайный suffix для одновременных запусков).
Создание идёт в `.incomplete-*`; backup публикуется atomic rename только после проверки
всех файлов, SHA-256, структуры manifest, slug и `pg_restore --list`. Неполные каталоги
не показываются как доступные backup; их можно исследовать вручную.

`pg_dump` даёт согласованный MVCC snapshot без остановки приложения. Миграции и
configure через bookingctl исключены registry lock; revision проверяется до и после
dump. Для неподдерживаемых внешних миграций такой гарантии нет.
Не копируйте PostgreSQL volume как замену логического backup.

Manifest format_version=1 содержит client_slug, deployment_identity (Compose project
со случайным suffix), created_at UTC, application_version (OCI label), immutable image_id,
postgres_version_num, alembic_revision, domain и database_format=pg_dump_custom.
SHA256SUMS покрывает dump, TOML, metadata и manifest; полный фиксированный набор файлов
проверяется перед восстановлением. Это защита от повреждения, **не цифровая подпись**:
восстанавливайте только доверенные backup, так как PostgreSQL dump содержит исполняемый SQL.

`backup list` заново проверяет контрольные суммы и manifest; операционный `status`
проверяет metadata и давность без чтения всех dumps. Тяжёлый `pg_restore --list`
делается при create, verify, pull, restore и `production-check`.
Для monitoring пороги давности задают `BOOKING_MONITOR_BACKUP_WARNING_HOURS=24`
и `BOOKING_MONITOR_BACKUP_ERROR_HOURS=48`. Warning не меняет `/ready`.

## Restore и совместимость

```bash
bookingctl --root /opt/booking/clients restore anna-tattoo BACKUP_ID
# Для заранее проверенной автоматизации:
bookingctl --root /opt/booking/clients restore anna-tattoo BACKUP_ID --yes
```

Перед записью проверяются checksum, archive, client slug, deployment identity, bot ID,
domain/public mode, exact application image ID и единственная Alembic head в этом image.
Печатаются source и target metadata; интерактивно нужно ввести `restore SLUG`.
Клонирование другого клиента не поддерживается.

Политика Phase 5 консервативная: restore требует того же immutable image и schema head.
Старый backup под новым кодом автоматически не восстанавливается. Сначала верните
точный release в управляемом deployment, восстановите backup, затем отдельно выполняйте
проверенную forward migration в будущей фазе обновлений. `alembic upgrade head` после
restore должен быть no-op для совпадающей head; downgrade никогда не запускается.
Поддерживается PostgreSQL major 17, не произвольный перенос между major versions.

После подтверждения:

1. Создаётся и проверяется `pre-restore-*` backup текущей БД/config.
2. В registry записывается незавершённая restore-операция. API/worker клиента останавливаются.
3. Поднимаются его PostgreSQL/Redis; проверяется major version.
4. Только его БД `booking` пересоздаётся; `pg_restore --single-transaction --exit-on-error`.
5. Проверяются revision и DB slug; восстанавливается TOML, выполняется `upgrade head`.
6. Очищается клиентский Redis, API/worker пересоздаются для нового bind mount.
   После `recover-files` public webhook регистрируется заново с новым secret.
7. Compose healthchecks и doctor проверяют storage, schema, configuration, image, worker,
   API и isolation. Для public deployment также обязательны DNS/TLS/proxy/getMe/webhook.
8. Только после проверок registry переводится в READY.

Если safety backup невозможен, обычный restore прекращается. Только при уже потерянной
БД или осознанном отказе от страховки используется явный
`--dangerous-skip-safety-backup` с предупреждением. `--yes` сам по себе это не разрешает.

При ошибке: FAILED/restore_failed, приложения останавливаются по возможности,
`restore-health.json`, `restore-failure.log`, `last-error.log` сохраняются приватно.
`state.json` содержит `safety_backup`. Не делайте `create --resume`/configure для обхода:
они заблокированы после interrupted restore. Исправьте причину и выполните
`restore SLUG PRE_RESTORE_ID`; если частичная БД непригодна для новой safety-копии,
явно используйте dangerous flag. Автоматического destructive retry/rollback нет.

Restore возвращает историю на время snapshot. Уведомления/Telegram updates, уже
отправленные после snapshot, невозможно «отменить» через PostgreSQL restore; возможна
повторная отправка сохранённых pending jobs. Перед возвратом трафика учитывайте это
при реальном инциденте. Не удаляйте pending updates автоматически.

## Расписание и retention

```bash
sudo /opt/booking/venv/bin/bookingctl --root /opt/booking/clients \
  --backup-root /opt/booking/backups backup schedule install --at 03:00
sudo /opt/booking/venv/bin/bookingctl --root /opt/booking/clients backup schedule status
```

Устанавливаются `booking-backup.service`/`.timer`: daily UTC, Persistent=true,
random delay до 5 минут, UMask=0077. Пропущенный запуск выполняется после boot.
Service делает последовательный `backup-all --retention`; ошибка клиента не мешает
остальным, общий exit status ненулевой. Логи: journalctl и `backups/events.jsonl`.
Systemd требует Linux; генерация units проверяется unit-тестами на Windows.

Необязательный `/etc/booking-backup.env` (0600) содержит operational configuration:

```ini
BOOKING_BACKUP_DAILY=7
BOOKING_BACKUP_WEEKLY=4
BOOKING_BACKUP_MONTHLY=3
BOOKING_BACKUP_MAX_AGE_HOURS=36
```

Сохраняется объединение последних N различных daily/ISO-week/month buckets (самая
свежая копия bucket), всегда newest valid и последние три pre-restore safety backups
(BOOKING_BACKUP_SAFETY). Все backup IDs активного update/restore/recovery защищены.
Сверх этого
копии моложе daily дней сохраняются. Перед удалением проверяется другая валидная копия
через pg_restore; corrupted/incomplete backup автоматически не удаляются. Последний
валидный backup не удаляется никогда. `backup prune SLUG` запускает ту же политику.
Unreferenced старые safety copies теперь подпадают под retention; incomplete/
corrupt files остаются для осознанной очистки. Remote lifecycle настраивается
отдельно. Операционный status проверяет freshness/metadata без повторного чтения
всех dumps; `backup list/verify` и `production-check` проверяют целостность.

## Off-site S3

```bash
pip install '.[backup-s3]'
export BOOKING_BACKUP_S3_BUCKET=private-booking-backups
export BOOKING_BACKUP_S3_PREFIX=booking
export BOOKING_BACKUP_S3_ENDPOINT=https://s3.example.org  # omit for AWS
export BOOKING_BACKUP_S3_SSE=AES256  # либо aws:kms
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN или workload role
bookingctl --root /opt/booking/clients backup anna-tattoo
bookingctl --root /opt/booking/clients backup upload anna-tattoo BACKUP_ID
bookingctl --root /opt/booking/clients backup pull anna-tattoo BACKUP_ID
```

При включённом bucket обычные CLI backup/backup-all после local verification загружают
копию; затем скачивают каждый объект потоком и сравнивают SHA-256, проверяя SSE.
SHA256SUMS загружается последним. Multipart поддерживает SDK. Ошибка upload делает
команду неуспешной, но сохраняет local backup и не запускает retention. Safety backup
внутри restore локальный; при необходимости его можно отправить `backup upload`.

Обязательны HTTPS, private bucket и все четыре S3 PublicAccessBlock flags. Адаптер
fail-closed: провайдеры без `GetPublicAccessBlock` пока не поддерживаются, даже если
реализуют базовый S3 API (проверьте конкретный R2/B2/MinIO endpoint). Это не обещание
совместимости со всеми vendors. Используются стандартные boto3 endpoint/credential
механизмы, vendor-specific credentials/URLs в manifest отсутствуют.

Encryption перед upload пока не реализовано. Вместо него обязательны SSE-S3/SSE-KMS
для каждого объекта и bucket policy, запрещающая незашифрованные uploads; optional
`BOOKING_BACKUP_S3_KMS_KEY` задаёт KMS key. Для требования client-side encryption нужен
отдельный адаптер. IAM: List/Put/Get, multipart, GetBucketPublicAccessBlock, KMS по
необходимости; никаких DeleteObject для этого инструмента. Remote retention не удаляет
объекты — настройте отдельную lifecycle/Object Lock policy и recovery credentials.

Pull — только скачивание в отдельный staging и verification, не destructive restore.
Не перезаписывает существующий backup. Реальные облачные credentials/bucket в локальном
acceptance не предоставлены: S3 adapter покрыт тестами, live provider test остаётся
обязанностью настройки инфраструктуры.

Инструменты PostgreSQL: [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html).
