# Phase 0 / Phase 1: аудит и проверка

Проверено 24 сентября 2026 года. Рабочая копия fast-forward обновлена с
`8038f88` до `4e570a6` из `Polen452294/Booking_bot` перед реализацией:
сохранены последние сценарии настройки специалиста и расписания.
Изменения Phase 1 остаются локальными; публикация не выполнялась.

## Архитектура и baseline

FastAPI: `booking_bot.main:create_app`, webhook
`/api/v1/webhooks/telegram`. Aiogram использует общий dispatcher, роутеры
клиента, кабинета и настройки специалиста; FSM хранится в Redis с TTL 2 часа.
Webhook создаёт и закрывает Telegram session на запрос. Polling и worker —
отдельные команды `booking-admin`; worker захватывает задания через PostgreSQL
`FOR UPDATE SKIP LOCKED`, восстанавливает устаревшие processing-задания.

SQLAlchemy использует asyncpg, async sessions и `pool_pre_ping`. Alembic имеет
одну head `a41d2c9e7b63`. `booking-admin configure` применяет TOML к единственной
строке SpecialistProfile, сохраняет изменения владельца и не сбрасывает график
без `--reset-schedule`. Старые названия таблиц Business/Master не означают
возврат к общей SaaS-БД; каждый deployment сохраняет отдельные хранилища.

Baseline до изменений: Ruff — успешно; unit — **30 passed**;
integration — **17 passed** на отдельной PostgreSQL 17. Полная цепочка миграций
прошла на пустой БД; `heads` и `current` совпали. Исходный Docker image собран,
исходные API/PostgreSQL/Redis запущены, API healthy.

## Риски и исправления

| Уровень | Наблюдение до Phase 1 | Результат |
| --- | --- | --- |
| critical | Подтверждённых critical-дефектов в рассмотренном контуре нет | Проверка текущего Git и истории путей secrets не выявила `.env` или реальных токенов; это не полный аудит всей истории универсальным secret scanner |
| high | Production не требовал secrets, HTTPS и явные URL; использовал development defaults | Обязательная fail-fast валидация, безопасные диагностические сообщения |
| high | PostgreSQL/Redis/API публиковались на всех интерфейсах; `booking/booking`, Redis без auth | Development loopback; production закрывает хранилища, требует отдельные пароли; API только loopback |
| high | API/worker запускались от root, оболочка API не использовала exec | UID 10001, exec/server entrypoint, read-only production filesystem, drop capabilities |
| high | Readiness проверял только `SELECT 1`, Redis критичен для FSM | DB + Redis + TOML + активный согласованный профиль, общий сетевой deadline, безопасный 503 |
| high | Worker не обрабатывал SIGTERM явно; пачка могла оставаться processing | Кооперативная остановка, завершение текущего job, возврат не начатых заданий; cleanup всех ресурсов |
| medium | TOML проверялся частично, типы dataclass не валидировались | Проверки структуры, типов, timezone, услуг и расписания при старте и readiness |
| medium | Автоматические миграции внутри API могли конкурировать при нескольких процессах | Один init перед API/worker в production, миграции вне ASGI lifecycle |
| medium | Логи разрозненные; exception/SQL details могли содержать secrets/PII | Общие форматтеры, JSON production, маскирование, traceback без значений; hide SQL parameters |
| medium | Неявные SQLAlchemy pools по 5 + 10 на процесс, нет заданных timeouts | Настраиваемые 3 + 2, bounded connect/query/Redis timeouts, pre-ping сохранён |
| medium | Redis не сохранял FSM при пересоздании контейнера | Отдельный volume + AOF в production |
| low | README обещал отсутствие дублей уведомлений | Документирована семантика at-least-once при аварии |

FastAPI и ранее использовал безопасный стандартный ответ 500 при `debug=False`.
Добавлены явный JSON-контракт и централизованное безопасное логирование, а также
тест сохранения намеренных `HTTPException`; утверждения об утечке traceback
клиенту в baseline нет.

## Итоговая проверка

| Проверка | Результат |
| --- | --- |
| `ruff check .` | Успешно |
| `pytest` | 89 passed, 18 integration deselected |
| `pytest -m integration` | 18 passed, 89 unit deselected |
| Alembic | Единственная head и current: `a41d2c9e7b63` |
| Docker Compose build | Успешно, Python 3.12; пакет установлен в site-packages |
| Development Compose | API/PostgreSQL/Redis и worker запущены с тестовым токеном |
| Production Compose config | Успешно с временными случайными test secrets |
| Production smoke | Init завершён; `/live` и `/ready` дают 200; API/worker UID 10001, Redis UID 999 |
| Отказ PostgreSQL/Redis | `/ready` → 503, `/live` → 200; после восстановления `/ready` → 200, API не перезапускается |
| SIGTERM | API и worker завершаются с кодом 0; корректность состояний job подтверждена integration-тестом |
| Production fail-fast в контейнере | Нет secret / HTTP webhook / неверный TOML path → ненулевой exit, без test secrets в выводе |
| Содержимое image / логи | Нет `.env` и `specialist.toml` в image; значения test secrets не попадают в production logs |

Отдельного type checker в `pyproject.toml` нет. Сеть Telegram в smoke не
использовалась: токены синтетические, тестовые БД пустые, отправки в тестах
заменены mock. Реальная регистрация webhook, получение updates и доставка
сообщений требуют настоящего токена и публичного HTTPS-входа.

Docker Desktop сначала не стартовал из-за временного `dockerInference`.
Восстановлен только runtime-каталог; прежний каталог сохранён рядом как
`run.phase1-preserved-*`, пользовательские volumes не удалялись.
BuildKit на Windows не принял путь с кириллицей; сборка проверена через
junction `C:\Users\alexa\AppData\Local\Temp\booking-phase1-checkout` к этой
же рабочей копии. Изолированные тестовые проекты имели префикс `booking-phase1-`.
После проверки их контейнеры, сети и временные volumes удалены. Исходный API
с shell без exec пришлось завершить принудительно при удалении baseline-стека;
новая версия API и worker завершились по SIGTERM самостоятельно с кодом 0.

## Изменённые файлы

- Container/config: `Dockerfile`, `compose.yaml`, новый `compose.prod.yaml`,
  `.dockerignore`, `.gitignore`, `.env.example`.
- Runtime: `src/booking_bot/config.py`, `specialist_config.py`, `main.py`,
  `cli.py`; новые `logging_config.py`, `lifecycle.py`, `server.py`.
- Зависимости runtime: `src/booking_bot/api/routes/health.py`,
  `bot/dispatcher.py`, `bot/factory.py`, `db/session.py`, `db/migrations/env.py`,
  `services/notification_delivery.py`.
- Tests: `tests/test_health.py`; новые `test_production_config.py`,
  `test_logging_config.py`, `test_lifecycle.py`, `test_worker_shutdown_integration.py`.
- Docs: `README.md`, `docs/production.md`, этот отчёт.

Новые env: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`,
`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE`.
Новых Python dependencies или миграций схемы нет.

## Оставшиеся ограничения и следующая фаза

- Без публичного HTTPS-входа боевой webhook ещё недоступен. Reverse proxy/TLS
  остаются задачами Phase 2; сейчас API намеренно слушает host loopback.
- Официальный PostgreSQL image создаёт `POSTGRES_USER` как superuser. Для
  дальнейшего усиления следует отделить runtime-роль от роли миграций;
  текущая схема credentials и миграций этого ещё не делает.
- Нет backup/restore, мониторинга worker, автоматизации обновлений или CI/CD;
  не следует считать всю коммерческую эксплуатацию готовой после одной Phase 1.
- Telegram updates пока не идемпотентны. После аварии между отправкой
  уведомления и commit возможен повтор; существующий lease recovery сохранён.
- Сохраняются плавающие transitive dependencies и tags базовых images;
  воспроизводимый release/lock следует рассмотреть вместе с процессом выпуска.
- Модель эксплуатации: один API, один worker, один init на deployment;
  миграции не предназначены для параллельного запуска.

Обязательного архитектурного рефакторинга booking-ядра перед Phase 2 не найдено.
Рекомендуемые следующие работы: HTTPS-вход и TLS, изолированное управление
deployment и secrets, проверяемое восстановление данных, разделение ролей БД,
затем release/update workflow и наблюдаемость. Это рекомендации; Phase 2 здесь
не реализована.
