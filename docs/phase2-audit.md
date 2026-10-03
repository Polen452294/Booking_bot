# Phase 2 — отчёт о проверке

Проверка 25 сентября 2026 года. Phase 3 не начиналась.
`master` и обновлённый `origin/master` указывают на `4e570a6`.
Незакоммиченные изменения Phase 1 сохранены; commit/push не выполнялись.

## Baseline и найденные проблемы

Ruff и 89 unit-тестов проходили до изменений. Первый integration-запуск
не имел доступного PostgreSQL; после запуска изолированной PostgreSQL 17
и применения исходных миграций все 18 integration-тестов прошли.
Предыдущие тесты не ослаблялись и не переписывались для скрытия ошибок.

| Проблема | Изменение |
| --- | --- |
| Повтор webhook снова запускал handler | Атомарный Redis processing/processed lease и транзакционный PostgreSQL receipt |
| Одной Redis-метки недостаточно при crash после DB commit | Receipt сохраняется вместе с бизнес-изменениями, unique key сериализует повтор при потере lease |
| FSM могла очиститься до rollback бизнес-операции | Буферизация FSM до commit, per-chat Redis isolation, атомарное применение state/data |
| Неверные типы update_id могли приводиться к integer | Строгий update_id + штатная Telegram schema aiogram/Pydantic |
| Предупреждение aiogram о неизвестном update содержало payload | Безопасное игнорирование с логированием только update_id |
| Повтор использованного hold мог сделать календарную запись expired | Ранняя проверка статуса hold в confirm, manual confirm и двух путях переноса |
| Stale recovery могло захватить долгую отправку, прежний владелец не проверял ownership | SKIP LOCKED recovery, claim_token, row lock на send/commit, проверка владельца при отправке/освобождении |
| Stale recovery и pending jobs могли обойти max attempts | Проверка лимита до повторного захвата и отправки |
| Постоянные Telegram-ошибки повторялись; retry_after игнорировался | Разделение permanent/transient, существующий backoff сохранён, retry_after учитывается |
| Неизвестный client_reminder_* принимался как корректный reminder | Явный список поддерживаемых reminder kinds |
| Worker не имел heartbeat и Docker healthcheck | Heartbeat по hostname, CLI PostgreSQL/Redis/worker-health, healthcheck в Compose |
| Установка webhook не проверяла фактическую конфигурацию Telegram | getWebhookInfo/getMe после set-webhook, новая webhook-status |

## Семантика и ограничения

Redis ключ: `booking:<bot_id>:telegram:update:<update_id>`. Lua атомарно
захватывает update; операции завершения/освобождения проверяют случайный owner token.
Processing TTL 120 секунд, deadline обработки 60 секунд, processed TTL 7 суток.
7 суток дают запас к 24-часовому хранению updates у Telegram; защита имеет конечное окно.
Receipt с таким же сроком сохраняется в одной транзакции с DB-изменениями;
истёкшие receipts удаляются ограниченными пачками при поступлении новых updates.

Redis failure → 503 без небезопасного запуска handler. DB failure/commit failure
→ 5xx, rollback и возможность повторить запрос. Активный конкурентный дубль → 503;
успешно обработанный дубль → 200. Неверный secret → 403, malformed body → 422.
Проверены также потеря Redis после commit, истечение lease и fencing старого владельца.

Heartbeat обновляется в основном цикле и после job, TTL 120 секунд, отдельный
ключ на hostname контейнера. Поэтому здоровая реплика не маскирует зависшую.
SIGTERM/SIGINT останавливают захват новых работ; текущая операция завершается,
не начатые jobs освобождаются, Bot/Redis/FSM/DB resources закрываются.
Grace period worker — 120 секунд, deadline job — 60 секунд.

Notification delivery **не exactly-once**. Если Telegram принял сообщение,
но результат не сохранён в PostgreSQL, stale recovery может отправить его повторно.
Риск двойной отправки остаётся также при неоднозначном сетевом timeout/потере
DB-соединения. При исчерпании лимита попыток сообщение может остаться недоставленным.
Выбраны ограниченные повторные попытки с backoff, а не предварительная отметка sent.

В webhook DB rollback теперь сохраняет FSM для повтора. Авария после DB commit,
но до применения FSM оставляет прежнее меню; сохранённая бизнес-операция защищена
receipt, пользователю может понадобиться /start. Внешние Telegram-ответы не
участвуют в DB-транзакции и тоже могут повторяться. Polling не использует webhook
lease/receipt; ограничения development polling описаны отдельно в документации.

## Итоговые проверки

| Проверка | Результат |
| --- | --- |
| `ruff check .` | Passed |
| `pytest` | 116 passed, 43 integration deselected |
| `pytest -m integration` | 43 passed, 116 unit deselected |
| `alembic upgrade head` | Миграции применены; head `b62f3d910ea4` |
| `alembic check` | No new upgrade operations detected |
| `git diff --check` | Passed |
| `docker compose build` | Passed, финальный runtime image собран |
| Production Compose config | Passed с изолированными тестовыми secrets |
| Production smoke | API/PostgreSQL/Redis и две worker-реплики healthy |
| Конкурентный webhook + replay | Один бизнес-обработчик/одно Telegram-сообщение; 200/503 при гонке, затем 200 |
| Два worker-процесса | Каждая из трёх jobs отправлена один раз, attempt_count = 1 |
| SIGTERM во время HTTP send | Оба worker завершились с exit 0; после запуска очередь обработана |
| Redis down | Webhook 5xx без запуска handler; повтор после восстановления успешен |
| PostgreSQL down | Webhook 5xx без запуска handler; повтор после восстановления успешен |
| CLI webhook | set-webhook и webhook-status → OK на локальном Telegram HTTP stub |
| Production logs | Lifecycle/job events присутствуют, синтетические token/secret отсутствуют |
| Зависший цикл | SIGSTOP PID1 → unhealthy через 204 секунды; вторая worker-реплика остаётся healthy |
| Возобновление цикла | После SIGCONT контейнер снова healthy |

Smoke использует PostgreSQL 17, Redis 7.4, production Compose с двумя отдельными
worker-процессами и синтетическим токеном. Только Telegram HTTP transport направлен
в локальный stub; реальные API/dispatcher/middleware, DB-транзакции и worker остаются
рабочими. Никакие сообщения реальным пользователям не отправлялись, настоящий
webhook не менялся. Публичный HTTPS/настоящий Telegram round trip не проверялись.

Сборка выполнялась через уже существующий junction с ASCII-путём
`C:\Users\alexa\AppData\Local\Temp\booking-phase1-checkout` к текущему репозиторию.
Это обходит ограничение Windows BuildKit на путь с кириллицей.
После проверки удалены только созданные для Phase 2 тестовые контейнеры,
сеть и volumes. Контейнеров и volumes с префиксом `booking-phase2` не осталось;
остальные локальные Docker-проекты не изменялись.

## Файлы, изменённые именно в Phase 2

Существующие Phase 1 изменения в других файлах в этот список не включены.

- Runtime: `src/booking_bot/api/routes/telegram.py`, `bot/dispatcher.py`,
  `bot/middlewares.py`, новый `bot/transactional_fsm.py`, `cli.py`, `config.py`.
- Services: `src/booking_bot/services/telegram_webhook.py`, `notification_delivery.py`,
  `bookings.py`; новые `update_idempotency.py`, `worker_health.py`.
- Schema: `src/booking_bot/db/models/__init__.py`, `notifications.py`, новый `telegram.py`;
  новая миграция `db/migrations/versions/b62f3d910ea4_delivery_reliability.py`.
- Compose/config: `compose.yaml` (production наследует healthcheck и grace period),
  `pyproject.toml` (описание integration marker).
- Новые tests: `tests/test_telegram_webhook.py`, `test_webhook_cli.py`,
  `test_webhook_idempotency_integration.py`, `test_webhook_fsm_integration.py`,
  `test_worker_reliability.py`, `test_worker_reliability_integration.py`,
  `test_booking_concurrency_integration.py`.
- Дополнен `tests/test_lifecycle.py`: проверяется закрытие heartbeat Redis при ошибке worker;
  unit-тест не обращается к настоящему Redis.
- Docs: `README.md`, `docs/production.md`, новые `docs/phase2-reliability.md`
  и этот отчёт. Временные smoke scripts/logs находятся в игнорируемом `tmp/phase2*`.

Новых Python dependencies нет. Booking-архитектура и существующие пользовательские
сценарии сохранены; изменения сервисов booking ограничены защитой использованного hold.

## Готовность к следующей фазе

Проект готов к работе над Phase 3 — автоматизированным развёртыванием отдельных
установок — с документированными ограничениями внешних эффектов. Это не утверждение
о готовности полного эксплуатационного контура: TLS/reverse proxy, backup/restore,
deployment manager, CI/CD и централизованный monitoring в этой задаче не реализованы.
Перед запуском новой версии требуется остановить старые процессы и применить миграцию;
смешанный запуск старого и нового worker не поддерживается.
