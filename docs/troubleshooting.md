# Incident runbook

Начните с bookingctl status, doctor CLIENT, logs CLIENT --tail 200. Сохраните
private state/history/backup IDs; проверьте active lock. ERROR не исправляется
слепым recreate или удалением volumes.

| Incident / симптомы | Diagnosis | Recovery |
| --- | --- | --- |
| Bot не отвечает | doctor: API/ready/getMe/webhook/DNS/HTTPS; API logs | Восстановить dependency; для READY start CLIENT; webhook/production-check; реальные сообщения |
| Worker down / reminders не идут | worker logs, heartbeat, queue, notifications failed | Исправить DB/Redis/token, start CLIENT; только разрешённый explicit retry; recovery alert |
| DB down / ready 503 | PostgreSQL logs/container/volume/disk | Восстановить service/storage; при потере volume reviewed DR restore, не пустой configure |
| Redis down / FSM сброшен | Ping/ready/worker logs/memory/disk | start CLIENT; holds/FSM могут исчезнуть; начать сценарий заново, durable appointments сохраняются |
| Disk full | host doctor, resources, df -h, docker system df, backups size/oldest/latest | Удалять только проверенные ненужные files/images; backup prune использует bounded retention; не удалять volumes/recovery checkpoints |
| Webhook broken | domain-check, domain/route/Telegram URL, pending/error counts | Исправить DNS/TLS; bookingctl webhook set CLIENT; не менять token/secret вслепую |
| TLS broken / expiry | Chain/hostname/expiry, proxy logs, ACME mode/permissions, ports | Восстановить DNS/challenge/firewall/ACME; production mode; proxy start; trusted HTTPS до webhook |
| Proxy down | proxy status/logs; internal API может быть жив | proxy start; socket/routing checks; clients production-check |
| Update failed | history/state/release_attempt/update-health, backup, actual/confirmed versions | Image rollback только при одинаковой schema; иначе reviewed rollback --restore-database --yes; возможна потеря записей после backup |
| Backup failed | Disk/permissions, events.jsonl/operations.json, S3/connectivity/read-back, journal | Исправить storage; повторить verified backup; mandatory update не продолжается; успешная копия снимает latch |
| Restore failed | State/restore-health/last-error/safety backup, exact image/schema | Исправить и повторить reviewed restore; не обходить FAILED новым create; skip safety только при реальной потере БД |
| Server lost | Off-host backups/configuration, DNS, release images | Новый VPS → private registry recovery → exact image/DB restore → DNS/ACME/webhook → production-check |

При active lock monitoring сохраняет прошлые alerts и показывает deferred
checks. Если lock свободен, а state UPDATING/RESTORING/FAILED, завершите recovery
из journal. start требует READY и не заменяет migration/restore recovery.
Другие clients должны оставаться healthy при восстановлении одного клиента.

Если alerts не доходят: host-only bot/chat, начало диалога с bot, 0600, internet,
journalctl. Failed send остаётся pending. При полной потере VPS используйте
внешний uptime monitor/SSH: host timer не сообщает о собственной недоступности.

Docker/root — доверенные роли. Customer PII/DB/token нельзя копировать в issue
или public chat. Не запускайте destructive smoke scripts на production.
