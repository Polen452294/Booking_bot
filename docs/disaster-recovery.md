# Disaster recovery: потеря PostgreSQL volume или VPS

Начните с [модели backup/restore](backup-restore.md). RPO равен возрасту последней
проверенной копии (по умолчанию до суток плюс задержка); RTO зависит от размера БД,
скачивания image/backup, DNS и доступности Telegram. Local backup не защищает от потери
диска/VPS. Проверяйте отдельную off-host копию и доступ к vault/image registry регулярно.

## Потерян volume одного клиента, registry сохранён

1. Зафиксируйте `bookingctl status SLUG`, остановите клиентский API/worker при необходимости.
   Не удаляйте volumes других клиентов.
2. `bookingctl backup list SLUG`, затем `backup verify SLUG ID`.
3. Проверьте source version, дату/время, slug, deployment identity. При необходимости
   отдельно выполните `backup pull SLUG ID`.
4. `bookingctl restore SLUG ID`. Если исходная БД уже потеряна, ожидаем отказ safety backup.
5. Только в этом случае: `bookingctl restore SLUG ID --dangerous-skip-safety-backup`.
   Подтвердите `restore SLUG`. Не используйте опасный флаг для обычной процедуры.
6. `bookingctl doctor SLUG`, проверьте услуги, клиентов, записи/статусы и расписание.
   Для public deployment doctor обязан проверить корректный Telegram webhook.
7. Создайте новую backup-копию после успешного восстановления и отправьте off-host.

## SERVER LOST → новый VPS

1. Изолируйте потерянный сервер: не допускайте двух активных worker для одного Telegram bot.
   Получите проверенный backup, его ID, сохранённый release image/digest и Telegram token
   из независимого vault. `.env` в data backup намеренно отсутствует.
2. Установите Linux, Docker Engine/Compose, Python 3.12+, системный bookingctl virtualenv
   из проверенного исходного release. Создайте private registry `/opt/booking/clients`
   и `/opt/booking/backups` под одним operator account.
3. Восстановите **точный** app image из registry по digest или `docker load`. Сохранённый
   image_reference должен разрешаться в тот же image_id; пересобранный похожий tag не
   является заменой. PostgreSQL 17 и Redis 7.4 tooling также должны быть доступны.
4. Настройте S3 credentials отдельно в root-only EnvironmentFile/vault. Выполните
   `bookingctl --root /opt/booking/clients --backup-root /opt/booking/backups backup pull SLUG ID`.
   Альтернатива: скопируйте весь каталог backup из доверенного offline носителя в
   `/opt/booking/backups/SLUG/ID`, установите 0700/0600. `backup verify SLUG ID`.
5. Инициализируйте shared proxy: `bookingctl --root /opt/booking/clients proxy init
   --email ADMIN_EMAIL --server-ipv4 NEW_IP`, затем `proxy start`. См. `reverse-proxy.md`.
   Восстановление ACME secret из backup не требуется: выдайте новый сертификат.
6. `bookingctl --root /opt/booking/clients recover-files SLUG ID`. Введите Telegram token
   скрытым вводом. Команда требует отсутствующий client directory, проверяет тот же bot ID,
   сохраняет исходный project identity/domain/public mode, генерирует новые storage/webhook
   secrets и оставляет deployment FAILED до реального restore. Это не clone.
7. Переведите DNS сохранённого domain на новый VPS, дождитесь разрешения A/AAAA, откройте
   80/443 для Traefik. Не публикуйте PostgreSQL/Redis наружу.
8. Выполните `restore SLUG ID --dangerous-skip-safety-backup`. Для public deployment после
   `recover-files` restore автоматически регистрирует webhook с новым secret после запуска
   API и до doctor. `getWebhookInfo` не показывает secret, поэтому совпадение URL не
   заменяет эту регистрацию. При недоступности Telegram restore остаётся FAILED.
9. При ошибке DNS/TLS/webhook исправьте инфраструктуру и повторите restore из исходного
   backup с новой safety-копией либо явным отказом от неё для уже проверенного DR.
   Не выставляйте READY вручную. Для обычной потери volume с сохранённым `.env` смена
   webhook secret не требуется; после восстановления доступен `bookingctl webhook set SLUG`.
10. `doctor SLUG`: PostgreSQL, Redis, single Alembic head, API/worker, config, image,
    permissions, DNS/TLS, proxy, getMe и webhook должны пройти. Проверьте реальные
    business rows и безопасную пользовательскую операцию в Telegram.
11. Установите systemd backup timer; выполните local backup, off-host upload и повторную
    verification. Храните копии на независимом storage с отдельными credentials.

## Ошибка restore и возврат

Сохраните `state.json` (safety_backup), `restore-health.json`, `restore-failure.log`,
`last-error.log` и `backups/events.jsonl`. Не выводите `.env` в тикеты или общие логи.
При нехватке диска/недоступности Docker stop тоже может не пройти: проверьте реальные
контейнеры. Статус registry останется FAILED.

Проверьте `backup verify SLUG PRE_RESTORE_ID`, исправьте причину ошибки и выполните
`restore SLUG PRE_RESTORE_ID`. Safety backup создаётся снова, если частичная БД пригодна
для dump; иначе explicit dangerous flag. Система не перебирает destructive retries и
не делает автоматический Alembic downgrade.

## Реальный acceptance test

```bash
docker build --build-arg APP_VERSION=0.5.0-phase5 -t booking-bot:0.5.0-phase5 .
python tests/disaster_recovery_smoke.py --root tmp/phase5-dr-NEW --image booking-bot:0.5.0-phase5
```

Если автоматический Docker address pool исчерпан, задайте свободную частную IPv4 /22
через `--network-pool 10.253.0.0/22`. Скрипт создаёт четыре /24 только для новых тестовых
projects; занятые диапазоны Docker отклоняет. Другие сети не удаляются.

Скрипт требует новый dedicated root, создаёт `dr-test` и `dr-test-b`, seed услуг,
клиента, подтверждённой записи с историей и schedule exception. Создаёт и проверяет
backup обоих, удаляет только проверенный по Docker labels/name тестовый PostgreSQL
volume, проверяет отказ restore без safety, затем выполняет явный disaster restore.
Сравнивает полные JSON rows 13 business/schema таблиц до/после, TOML, doctor и неизменность
контейнеров/данных соседа в обе стороны. В конце проверяет обычный restore с safety backup.
Оставляет registry, backup и контейнеры для исследования. Никаких production volume
имён, токенов или данных не принимает.

Telegram в тесте: getMe при provisioning подменён, tokens синтетические; API/worker,
PostgreSQL/Redis, pg_dump/pg_restore, Alembic и doctor реальные. Live Telegram, публичный
DNS/ACME/webhook и реальный S3 provider требуют отдельной проверки с настроенной
инфраструктурой. Unit mocks не заменяют этот Docker DR acceptance.
