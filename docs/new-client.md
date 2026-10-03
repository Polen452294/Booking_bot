# Новый клиент: кандидат v1

Целевая платформа: **Ubuntu Server 24.04 LTS, x86_64, Docker Engine,
Docker Compose plugin, systemd**. Полная VPS qualification ещё не завершена;
поддержка Debian, ARM64 и других ОС не заявляется. Локальный Ubuntu container
подтвердил только установку host CLI. Статус выпуска: [release-readiness-v1.md](release-readiness-v1.md).

## Один раз на чистом сервере

1. Подготовьте отдельный тестовый VPS, доступ по SSH и только SSH/80/443 в firewall.
   Docker ports нужно проверять отдельно от UFW. Не используйте действующий клиентский сервер.
2. Поместите проверенный исходный release checkout в `/opt/booking-source`.
   До bootstrap должны быть опубликованы и просканированы четыре images версии
   `1.0.0-rc.1`: `booking-bot`, `booking-socket-proxy`, `booking-postgres`,
   `booking-redis` в `ghcr.io/polen452294`. В текущей qualification они собраны
   только локально; публикация и GitHub CI остаются отдельными gates.
3. Запустите от root:

```bash
sudo bash /opt/booking-source/scripts/bootstrap-server.sh \
  --acme-email operator@example.org --server-ipv4 VPS_PUBLIC_IPV4
sudo bookingctl version
sudo bookingctl doctor
```

При первом запуске можно добавить `--server-ipv6` при наличии действующего IPv6.
Для тестового ACME есть `--staging`; он не считается production HTTPS.
Реализация установки Docker следует [официальному apt workflow](https://docs.docker.com/engine/install/ubuntu/).
Bootstrap отказывается автоматически заменять конфликтующий container runtime.

Bootstrap устанавливает постоянный venv и launcher, готовит private directories,
инициализирует штатный Traefik/proxy network и включает backup/monitor timers.
Повторите **ту же команду**: конфигурация proxy, ACME, шаблон и client secrets
должны сохраниться; DB volumes не удаляются. Разная версия CLI/конфигурация
proxy требует явной операции оператора, bootstrap не является auto-update.
`--prepare-only` устанавливает только CLI и явно оставляет Docker/proxy/timers
непроверенными; это не production installation.

```text
/opt/booking/
├── bin/bookingctl
├── venv/
├── infra/specialist.toml
├── infra/proxy/
├── clients/<slug>/
├── backups/<slug>/
├── state/bootstrap.json
└── logs/
```

Launcher `/usr/local/bin/bookingctl` задаёт `/opt/booking/clients` и
`/opt/booking/backups`, использует установленный wheel без PYTHONPATH и
сохранённый template из infra. Не запускайте CLI другой версии случайно из checkout.
Текущее durable состояние клиентов хранится в `clients/<slug>/state.json`,
monitoring state — штатным monitoring module; logs приложения — bounded Docker logs.

## Каждый новый клиент

1. Создайте отдельного бота в BotFather, сохраните токен в password manager.
2. Создайте A/AAAA для отдельного домена. Не оставляйте AAAA на чужой адрес.
3. Создайте клиента в интерактивном SSH terminal:

```bash
sudo bookingctl create release-test-1 \
  --image ghcr.io/polen452294/booking-bot:1.0.0-rc.1
sudo bookingctl domain-check release-test-1
sudo bookingctl expose release-test-1
sudo bookingctl webhook set release-test-1
```

CLI спрашивает brand, имя, специализацию, timezone, currency, адрес, домен и
BotFather token с hidden input. Config, passwords и webhook secret генерируются
штатной командой. `getMe` проверяется до provisioning. Образ и оба storage images
фиксируются по image ID; `latest` не используется. Generated Compose, SQL и
networks руками не исправлять. При ошибке исправьте automation и повторите шаг.

4. Используйте выданный master invite в личном чате бота. Настройте рабочие дни,
   часы и услуги FIXED/FROM/NEGOTIABLE через существующий кабинет специалиста.
   Вторым Telegram пользователем пройдите запись и согласование 15k → 17k,
   отклонение старой цены, переписку до/после записи и text/photo/document.
5. Создайте backup, проверьте архив и off-host copy по [backup-restore.md](backup-restore.md).
   Заполните `/etc/booking-backup.env` и `/etc/booking-monitor.env` безопасным
   редактором (owner root, mode 0600). Monitoring использует отдельного бота,
   а не токен клиента. Timers без credentials не доказывают доставку alerts.

```bash
sudo bookingctl backup release-test-1
sudo bookingctl backup list release-test-1
sudo bookingctl monitor
sudo bookingctl production-check release-test-1
sudo systemctl list-timers booking-backup.timer booking-monitor.timer
sudo ss -tulpn
```

`production-check` должен завершиться успешно без deferred mandatory checks.
Пройдите [handover checklist](client-handover-checklist.md).
Restore/update/recovery выполнять по [operations-runbook.md](operations-runbook.md)
и [updates.md](updates.md), учитывая downtime и данные после backup.
Второй клиент создаётся той же командой с отдельными token/domain; существующий
deployment не копируется.
