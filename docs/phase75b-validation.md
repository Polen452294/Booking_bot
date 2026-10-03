# Phase 7.5B — итоговая проверка, 2 октября 2026

Работа выполнена в локальной ветке `master`, поверх существующих незакоммиченных
изменений Phase 1–7 и Phase 7.5A. Предыдущие изменения сохранены.

## Результаты

| Проверка | Результат |
| --- | --- |
| Baseline Ruff | PASS |
| Baseline unit | 344 passed, 73 integration deselected |
| Baseline integration после запуска disposable PostgreSQL/Redis | 73 passed |
| Финальный `ruff check .` | PASS |
| Финальный `pytest` | **371 passed**, 98 integration deselected |
| Финальный `pytest -m integration` | **98 passed**, 371 unit deselected |
| Отдельный полный Telegram E2E после regression | **1 passed**, 9.74 s |
| E2E `pg_dump` → новая test DB → `pg_restore` | PASS, полные строки шести связанных таблиц совпали |
| Docker build `booking-bot:phase75b-final`, APP_VERSION=0.7.0 | PASS, pip check PASS |
| BackupManager DR на итоговом Docker-образе, два deployment | PASS |
| `/live`, `/ready`, PostgreSQL, Redis, worker heartbeat через `doctor` после restore | PASS |

Integration выполнен на настоящих PostgreSQL 17 и Redis 7.4. Telegram API замокан;
aiogram dispatcher, webhook receipt/lease и transactional FSM настоящие.
Отдельный type checker в проекте не настроен.

## E2E и защита

Клиент выбирает NEGOTIABLE, отправляет описание, photo и document, создаёт заявку.
Мастер открывает inbox/карточку/историю, отвечает, вводит 15 000 ₽ с комментарием
и подтверждает предложение. Клиент читает историю, отвечает, принимает условия,
выбирает существующий слот и подтверждает запись. Проверены DB статусы, file metadata,
уведомление с нужным callback, unread, price snapshot и unique связь request/appointment.
Обе стороны открывают conversation из карточки Appointment и пишут после booking.
Повтор того же финального webhook update оставляет одну Appointment.

Security: чужие request/proposal callbacks, non-master inbox/price, revoked master,
cross-business membership, запрещённые роли, закрытый диалог и старый proposal.
Concurrency: два Accept одного предложения дают accepted/stale и один job;
прежние тесты также покрывают old/current proposal, propose/accept и double final booking.
Slot conflict сохраняет accepted request, proposal и историю.

Отдельно проверены FIXED/FROM routing, клиентские разделы, master unread/New,
pagination, альбомы, телефон, неподдерживаемые типы, лимиты, /start и /cancel
в request/reply/price/comment/confirmation FSM, подтверждение close/cancel,
fallback неудачного edit, сохранение unread при ошибке показа истории и retry job
при transient delivery failure. Цена 0 соответствует backend policy.

Существующие booking, appointments, reschedule/cancel, master schedule/manual booking,
services, notifications, statistics/export, monitoring и operational tests прошли
в полном regression suite. Новый код не пишет текст переписки, контакты или tokens
в operational logs; существующие проверки редактирования PII и secrets проходят.

## Backup и восстановление

E2E использует `tests/requests_backup_support.py` при явно заданном disposable test
container. Сравниваются booking_requests, conversations, conversation_messages,
conversation_read_states, price_proposals и appointments, включая file metadata,
read cursors и связи. Новая UUID restore DB удаляется после сравнения, исходная не меняется.

Настоящий deployment DR: `tests/disaster_recovery_smoke.py` с отдельным registry
`tmp/phase75b-dr-final`, итоговым образом и private network pool. Проверены потеря
тестового PostgreSQL volume, restore, точное совпадение business rows/config,
запрет restore чужого backup, сохранность второго deployment, safety backup и doctor.
Оба deployment прошли проверку. Исходные installation не затрагивались.

Логи: `tmp/phase75b-unit-final.log`, `tmp/phase75b-integration-final.log`,
`tmp/phase75b-e2e-final.log`, `tmp/phase75b-build-final.log`, `tmp/phase75b-dr-final.log`.
DR registry/backups находятся в `tmp/phase75b-dr-final` и `tmp/phase75b-dr-final-backups`.
Тестовые контейнеры остановлены; данные, backups и итоговый образ сохранены.

Для повторения integration/E2E в PowerShell:

```powershell
docker start booking-phase75b-postgres booking-phase75b-redis
$env:DATABASE_URL='postgresql+asyncpg://booking:booking@127.0.0.1:55432/booking'
$env:REDIS_URL='redis://127.0.0.1:56379/0'
$env:BOOKING_TEST_POSTGRES_CONTAINER='booking-phase75b-postgres'
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m pytest -m integration
.venv\Scripts\python.exe -m pytest -m integration tests/test_requests_telegram_integration.py::test_full_telegram_request_price_booking_and_continued_conversation
```

## Ограничения и следующий этап

Живой Telegram клиент, настоящий token и public webhook не использовались.
Оригинальные границы media groups не хранятся в модели Phase 7.5A; соседние фото
одного автора показываются native albums, документы отдельно. После отправки альбома
нужно нажать «Готово / Назад». Reliable presence отсутствует: push не подавляется
и отдельные фото могут создать несколько jobs. Exactly-once Telegram delivery нет.

Вне v1 остаются payments/deposits/refunds, voice/video/calls, web chat, CRM/AI,
editing/deletion, групповые диалоги и multiple masters.

Phase 7.5B готова как основание для Phase 7.5C. Phase 7.5C не начата.
Описание экранов и сервисных границ: [conversations-telegram-flow.md](conversations-telegram-flow.md).
