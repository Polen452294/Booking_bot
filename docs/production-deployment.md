# Production deployment

Целевая платформа v1: Ubuntu Server 24.04 LTS x86_64, Docker Engine,
Compose plugin и systemd. Полная проверка VPS остаётся release gate.
Текущий кандидат — `1.0.0-rc.1`; stable `v1.0.0` ещё не выпущен.
Статус: [Phase 8](release-readiness-v1.md), [Phase 9A](releases/v1.0.0.md).

Штатная установка описана в [new-client.md](new-client.md): проверенный
release checkout устанавливает host CLI через `scripts/bootstrap-server.sh`,
а `bookingctl create` скачивает опубликованный application image. Перед
установкой должны быть опубликованы и проверены **четыре** packages одной
версии: `booking-bot`, `booking-socket-proxy`, `booking-postgres`, `booking-redis`
в `ghcr.io/polen452294`. Traefik использует отдельную pinned версию.
Локальные builds не заменяют qualification опубликованных images.

Bootstrap использует `/opt/booking/clients` и `/opt/booking/backups`, persistent
launcher `/usr/local/bin/bookingctl`, private конфигурацию и backup/monitor timers.
Повтор установки сохраняет клиентов, secrets, proxy/ACME и volumes.
`--prepare-only` проверяет только установку CLI.

## Проверки установки

Настройте DNS, production ACME и webhook по [HTTPS runbook](https-webhook.md).
Публичными могут быть только согласованные SSH/80/443. Не публикуйте PostgreSQL,
Redis, внутренний API, Docker API, metrics или Traefik dashboard.

```bash
sudo systemctl is-enabled docker
sudo ss -lntp
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
sudo bookingctl version
sudo bookingctl status
sudo bookingctl doctor
sudo bookingctl production-check CLIENT
```

Не публикуйте expanded Compose или полный docker inspect: там credentials.
Для проверки Compose используйте `config --quiet`. Generated deployment
фиксирует immutable image ID; читаемый versioned tag сохраняется в metadata.
Нельзя вручную заменять его на `latest` или mutable minor alias.

Создайте verified backup и подтвердите off-host copy/restore по
[backup-restore.md](backup-restore.md). Настройте независимые monitoring
credentials и проверьте failure/recovery delivery по [monitoring.md](monitoring.md).
После reboot повторите doctor, production-check и Telegram smoke.
ERROR/CRITICAL или deferred mandatory checks запрещают передачу клиенту.
Заполните [client-handover-checklist.md](client-handover-checklist.md).

## Обновление и recovery

Следуйте [updates.md](updates.md) и [rollback.md](rollback.md).
Update приложения не заменяет pinned PostgreSQL/Redis images автоматически.
Перед отдельным обновлением storage нужны verified backup, совместимость
PostgreSQL major 17, scan точного image и restore/restart smoke на копии.
Не удаляйте существующие volumes. Shared proxy обновляется отдельно с
сохранением ACME volume; его настройки описаны в [reverse-proxy.md](reverse-proxy.md).

Локальный Phase 8 scan пяти финальных images дал HIGH=0/CRITICAL=0;
это не scan будущего stable image или security approval VPS. Отчёты прошлых
фаз являются историческими evidence. Перед выпуском повторите scan именно
публикуемых артефактов. Измерения ресурсов на целевом VPS обязательны;
универсальный предел количества клиентов не установлен.

[production.md](production.md) описывает альтернативный standalone Compose,
который требует отдельной qualification и не заменяет managed installation.
