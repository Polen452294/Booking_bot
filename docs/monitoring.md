# Monitoring и alerts

```bash
bookingctl status                         # Все клиенты
bookingctl status anna-tattoo              # Полный JSON status
bookingctl doctor                         # Host + все клиенты
bookingctl doctor anna-tattoo
bookingctl production-check anna-tattoo
bookingctl resources                      # Docker stats snapshot + states
bookingctl versions                       # Confirmed versions + operator target
bookingctl logs anna-tattoo --service worker --tail 200 --follow
bookingctl monitor                        # Один проход
```

root/backup-root задаются перед командой; на сервере используйте один registry.
status/doctor поддерживают --json. Version awareness сравнивает с host package
либо BOOKING_LATEST_VERSION: это operator target, не newest GHCR tag.
Автоматического update нет.

Client doctor проверяет metadata/config/permissions, Docker/containers/images/
networks, live/ready, DB/Redis, Alembic/profile, worker heartbeat, очередь,
размер БД, backups и filesystems. Для public client — DNS, file/runtime
Traefik route, getMe, webhook, trusted TLS certificate/expiry. DB size берётся
через pg_database_size; volumes — через df, без рекурсивного du. Worker
выводит last heartbeat; counters — PENDING/PROCESSING/FAILED, без SENT.

Host doctor проверяет Docker, proxy/network/socket restrictions, ACME mode/
permissions, disk/Docker storage/backups, Linux RAM/load, Booking failed
containers и публичные DB/Redis/API/Docker bindings. Host listeners/firewall
отдельно проверяются ss. Docker Desktop не даёт host proc и Docker VM filesystem;
такие проверки явно получают WARNING.

Severity: OK, WARNING, ERROR, CRITICAL. WARNING виден и не делает deployment
unhealthy. PostgreSQL/permissions/isolation failures — CRITICAL; API/worker/
Redis/webhook failures — ERROR. Host thresholds:

| Environment | Default |
| --- | --- |
| BOOKING_MONITOR_DISK_WARNING / ERROR / CRITICAL | 80 / 90 / 95 % |
| BOOKING_MONITOR_BACKUP_WARNING_HOURS / ERROR_HOURS | 24 / 48 h |
| BOOKING_MONITOR_CERTIFICATE_WARNING_DAYS / ERROR_DAYS | 30 / 7 days |
| BOOKING_MONITOR_FAILED_JOBS_ERROR | 10 |
| BOOKING_MONITOR_MEMORY_WARNING / ERROR | 85 / 95 % |
| BOOKING_MONITOR_LOAD_WARNING | 2 load per CPU |
| BOOKING_MONITOR_COOLDOWN_SECONDS | 21600 |

Невалидные/NaN/нарушающие порядок thresholds отклоняются. Невалидная certificate
chain/hostname или expiry — CRITICAL. Короткий WARNING может исчезнуть после
автоматического Let's Encrypt renewal.

Monitoring основан на CLI без обязательных Prometheus/Grafana, public metrics
или daemon. Backup pass читает metadata, возраст/размер известных files,
oldest/latest/incomplete directories; он не перечитывает все dumps.
production-check дополнительно выполняет checksum/pg_restore --list latest
backup. Периодический реальный restore всё равно обязателен.

## Monitoring bot и timer

Используйте отдельного bot и admin chat; начните диалог с bot. Создайте вне Git
host-only /etc/booking-monitor.env mode 0600, owner — timer operator:

```dotenv
MONITORING_TELEGRAM_BOT_TOKEN=REPLACE_WITH_SEPARATE_MONITORING_TOKEN
MONITORING_TELEGRAM_CHAT_ID=REPLACE_WITH_ADMIN_CHAT_ID
```

Credentials не передаются client containers. Alert содержит только deployment,
check и severity, без customer PII/error payload. Без credentials monitoring
работает с явным WARNING; частичная/невалидная конфигурация — ERROR.

```bash
sudo /opt/booking/venv/bin/bookingctl --root /opt/booking/clients monitor --schedule install
sudo /opt/booking/venv/bin/bookingctl --root /opt/booking/clients monitor --schedule status
sudo journalctl -u booking-monitor.service --since today
```

Timer стартует после boot и через пять минут после завершения pass плюс небольшой
random delay. Fleet проверяется последовательно, поэтому pass может быть дольше
пяти минут. На Windows timer не устанавливается.

Private root-parent/monitoring хранит locks, alerts.json и last-pass report.
First failure/change severity отправляется на ближайшем pass; same issues
молчат до cooldown; observed recovery отправляется отдельно. Failed send не
подтверждает доставку и повторяется. Concurrent passes исключены отдельным lock.
Потеря alert state вызывает повтор first-failure alerts.

При active update/restore/backup client checks откладываются и не создают
ложный recovery. Interrupted operation без lock определяется по state.
Backup/restore failures сохраняют durable latch, FAILED update/rollback — state/
history. Успешный recovery снимает соответствующий latch. Alerts не зависят от
client Booking bot, но зависят от Telegram/internet/VPS. Полную потерю VPS
должен обнаруживать внешний uptime monitor: локальный timer её сообщить не может.

## Notification operations и logs

```bash
bookingctl notifications failed anna-tattoo
bookingctl notifications retry anna-tattoo JOB_UUID
bookingctl notifications retry-failed anna-tattoo
```

Listing ограничен 100 jobs: UUID/kind/attempts/classified last_error/created_at.
Unknown error text скрыт. Retry требует READY, explicit transient error и
valid current appointment/preferences. Permanent Forbidden/BadRequest/
Unauthorized/unsupported и unknown/ambiguous errors не повторяются. Past
reminders/cancelled bookings отклоняются; row locks защищают concurrent retry.
Batch максимум 100; повторные вызовы явные, audit фиксирует retry. Telegram
sendMessage не имеет idempotency key: повтор после network timeout может дать
duplicate message, это ограничение delivery model.

Production JSON logs: timestamp/level/logger/event/deployment_slug/service/message
и доступные internal IDs. Secrets/URLs/phones маскируются; exception payloads,
locals/SQL parameters не сериализуются. Customer names/private text в log call
sites не используются. Docker json-file ограничен 10m × 3 для всех services.
Raw logs доступны только доверенному оператору; не публикуйте без review.
