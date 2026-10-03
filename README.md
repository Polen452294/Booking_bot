# Booking_bot

Telegram-бот для записи к одному специалисту. Каждый deployment имеет отдельного
бота, PostgreSQL, Redis и конфигурацию; `bookingctl` управляет несколькими такими
установками на одном Linux-сервере.

Текущая версия: **1.0.0-rc.1**. Stable `v1.0.0` ещё не опубликован:
[release gate Phase 8](docs/release-readiness-v1.md),
[подготовка Phase 9A](docs/releases/v1.0.0.md).

## Возможности

- Услуги FIXED, FROM и NEGOTIABLE; выбор свободного окна с защитой от двойной записи.
- Просмотр, перенос и отмена записей клиентом, календарный экспорт.
- Кабинет специалиста: услуги/цены, рабочие часы, выходные, блокировки,
  ручные записи, история, статистика и Excel.
- Заявки и личная переписка: текст, фотографии и документы, unread и история.
- Предложения цены, принятие актуальных условий и сохранение согласованной
  цены в записи. Переписка продолжается после создания записи.
- Уведомления и напоминания, webhook, отдельный worker.
- Managed deployment: HTTPS, backup/restore, versioned updates, recovery и monitoring.

Один специалист на deployment. Нет online payments/deposits, CRM, web admin,
voice/video или групповых conversations; сообщения нельзя редактировать/удалять.
Restore требует downtime. Telegram delivery не гарантирует exactly-once.
Подробности: [conversations](docs/conversations.md),
[release notes draft](docs/releases/v1.0.0-notes.md).

## Архитектура

Python 3.12, aiogram, FastAPI, SQLAlchemy/Alembic, PostgreSQL 17 и Redis 7.4.
API принимает Telegram webhook; worker доставляет durable notifications.
PostgreSQL хранит записи, историю и согласованные условия; Redis хранит временный
FSM/cache. Traefik и restricted socket proxy обеспечивают HTTPS routing.
Runtime API/worker работает от UID 10001.
[Архитектура](docs/architecture.md).

## Development

В Windows PowerShell из корня проекта:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock -r requirements-build.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
docker compose up -d postgres redis
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\booking-admin.exe configure
.\.venv\Scripts\booking-admin.exe create-master-invite
```

Заполните локальный `.env` и пример `specialist.toml`; реальные secrets в Git
не добавляйте. Приглашение привязывает Telegram-профиль специалиста.
`booking-admin configure` сохраняет уже изменённые владельцем услуги и график;
`--reset-schedule` явно восстанавливает график из TOML.

Polling и worker запускаются в разных терминалах:

```powershell
.\.venv\Scripts\booking-admin.exe run-polling
.\.venv\Scripts\booking-admin.exe run-worker
```

Не запускайте polling одновременно с webhook того же бота.
Для development webhook/Compose build задайте версию из единственного источника:

```powershell
$env:APP_VERSION = .\.venv\Scripts\python.exe -c "from booking_bot.version import __version__; print(__version__)"
$env:VCS_REF = git rev-parse HEAD
docker compose up --build -d
```

Development ports доступны только на loopback. Readiness:
`http://localhost:8000/ready`; liveness: `http://localhost:8000/live`;
API docs: `http://localhost:8000/docs`.
При BuildKit ошибке из-за кириллицы используйте checkout/junction с ASCII путём.

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\pytest.exe -m integration
```

Integration требует отдельной disposable PostgreSQL/Redis и миграций.
Никогда не запускайте suite против клиентской БД. Переменные подключения
описаны в `.env.example`; production secrets генерирует `bookingctl`.

## Production

Целевая платформа — Ubuntu Server 24.04 LTS x86_64, Docker Engine/Compose и
systemd. Полная qualification VPS пока не завершена. Установка использует
проверенный release checkout для host CLI и опубликованные versioned images
для приложения; локальный development build не является production artifact.

- [Установка на сервер и новый клиент](docs/new-client.md)
- [Production требования](docs/production-deployment.md)
- [Передача клиенту](docs/client-handover-checklist.md)
- [HTTPS и webhook](docs/https-webhook.md)
- [Backup/restore](docs/backup-restore.md)
- [Updates и recovery](docs/updates.md)
- [Monitoring](docs/monitoring.md)
- [Release policy](docs/releases.md)

Первый реальный клиент устанавливается только после закрытия release gate.
