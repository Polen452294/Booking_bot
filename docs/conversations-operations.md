# Conversations: эксплуатация и восстановление

Источник истины — PostgreSQL. Telegram message, inline button и Redis FSM не
подтверждают актуальность цены или доступность слота. Для всех изменяющих действий
сервис повторно проверяет deployment/business, участника и состояние под request
row lock. После BOOKED цена записи и lifecycle appointment независимы от диалога.

## Диагностика

Начните с существующих `bookingctl doctor <slug>`, `bookingctl status <slug>` и
`bookingctl logs <slug> --service worker --tail 100` (общие `--root` и `--backup-root`
должны указывать на операторский registry и отдельное хранилище backup).
Readiness зависит от инфраструктуры и worker heartbeat, а не числа заявок.

Используйте request_id/conversation_id/message_id/proposal_id/appointment_id/job_id
для корреляции. Не включайте body диалогов, телефоны, credentials, token и содержимое
файлов в диагностические logs. Audit фиксирует message_created, price_superseded,
price_proposed/accepted/rejected, request lifecycle и appointment_created; comment
не копируется в audit details. Системные сообщения истории могут содержать условия
предложения — это приватные domain data, их нельзя публиковать вместе с logs.

Безопасные запросы к БД (подставлять ID параметрами):

```sql
SELECT id, status, master_id, service_id FROM booking_requests WHERE id = :request_id;
SELECT id, status, last_message_sequence FROM conversations WHERE booking_request_id = :request_id;
SELECT id, revision, status, amount_minor, currency
FROM price_proposals WHERE booking_request_id = :request_id ORDER BY revision;
SELECT user_id, last_read_sequence FROM conversation_read_states WHERE conversation_id = :conversation_id;
SELECT id, kind, state, attempt_count, scheduled_for, last_error
FROM notification_jobs WHERE booking_request_id = :request_id ORDER BY scheduled_for, id;
```

Не исправляйте status/sequence/price вручную. Сначала воспроизведите операцию в
тестовом deployment и проверьте service layer. Не удаляйте immutable историю.

## Старые кнопки и состояния

Старая revision и повторный accept безопасно отклоняются. CANCELLED/CLOSED/BOOKED
не могут вернуться к согласованию цены или создать новую запись. BOOKED conversation
может оставаться OPEN; мастер закрывает только переписку, request остаётся BOOKED.
Отмена/перенос/завершение/no-show записи сохраняют request и messages. Перенос не
требует повторного согласования цены. Цена service не меняет snapshot appointment.

На stale button пользователь получает alert с пояснением и может открыть заявку
заново. Занятый слот возвращает прежний SlotUnavailable UX. Ошибка PostgreSQL
не показывает успешное принятие/отправку/booking. Webhook возвращает временный
отказ, transaction и receipt откатываются; Telegram может доставить update снова.
Domain commit выполняется перед успешным UI. Если UI не доставлен после commit,
операция уже существует: открыть `/start` → «Мои заявки»/«Мои записи».

FSM TTL — два часа. `/start`, `/cancel`, «Назад» и «Главное меню» дают выход из
ввода. Потеря Redis может удалить ещё не отправленные описание/attachments и
временный state. Отправленные заявки, history, prices, read pointers, appointments
и receipts остаются в PostgreSQL. Восстановите Redis, откройте заявку из меню;
потерянный черновой ввод надо ввести повторно.

## Уведомления

Mutation/event/audit/NotificationJob сохраняются в одной domain transaction.
Worker retry — прежние backoff и max_attempts Phase 2. Permanent Bot API errors
становятся FAILED; stale PROCESSING jobs восстанавливаются существующим worker.
Row lock и claim_token предотвращают одновременное владение job двумя workers.
Jobs старых предложений становятся CANCELLED, если event_id не соответствует
latest PENDING proposal. Уже доставленная устаревшая кнопка не обходит domain checks.

`bookingctl notifications failed <slug>` показывает IDs и классы ошибок.
После устранения причины используйте `bookingctl notifications retry <slug> <job_id>`
или прежнюю команду retry-failed. Retry не должен создавать новую business operation.
Уведомление о закрытии — `client_booking_request_closed`; остальные request kinds
перечислены в conversations.md. Appointment notifications/reminders используют
прежние kinds, настройки и cancellation logic.

Exactly-once доставка Telegram технически не гарантируется: Telegram может принять
send, а процесс/БД потерять подтверждение до отметки SENT. После stale recovery
возможна повторная доставка. Аналогично нельзя атомарно объединить commit БД с UI.
Receipt/outbox обеспечивают consistency и исключают повтор business mutation;
пользовательское сообщение при потере ответа сверяется с сохранённой историей.

## Backup, restore и обновление

Используйте существующие Phase 5/6 команды: backup, backup verify, restore, update,
doctor и rollback. `bookingctl update <slug> --version <version>` сначала создаёт
backup, затем выполняет Alembic и doctor. Универсального automatic Alembic downgrade
нет. Откат image допустим при совместимой schema; иначе — существующая policy
rollback с database restore из verified backup. Учитывайте потерю данных после
времени backup; старые Telegram кнопки валидируются против восстановленного state.

PostgreSQL dump содержит новые таблицы без специального формата. Контролируемый
DR drill использует отдельные synthetic A/B deployments, 54 сообщения, photo,
document, две price revisions, accepted price, appointment link и read pointer.
До уничтожения собственного test volume проверяются Compose labels и backup.
После restore сравниваются полные строки и config, doctor и нетронутый peer.
Нельзя запускать destructive drill против production registry.

Migration integration создаёт UUID database старой версии b62f3d910ea4 с clients,
services, appointments и notifications; проверяет сохранение строк и FIXED default.
Опционально `BOOKING_TEST_PRECONVERSATIONS_IMAGE` и `BOOKING_TEST_MIGRATION_IMAGE`
запускают migrations через старый и новый Docker images только в disposable DB
на 127.0.0.1:55432. `BOOKING_TEST_POSTGRES_CONTAINER=booking-phase75c-postgres`
включает E2E dump/restore и проверку stale price callbacks после восстановления.

## Pagination и нагрузка

Telegram pages — 20 заявок/сообщений; service limit — 1..100. History упорядочена
по sequence и ограничена SQL LIMIT, не materializes весь диалог. Read cursor отмечает
только доставленную страницу; последующее сообщение остаётся unread. Inbox получает
page и одним запросом считает unread для её IDs, без N+1 conversation lookups.
Inbox использует offset pagination: при одновременном добавлении/перемещении заявок
между разделами элементы соседних страниц могут сместиться; обновление списка
показывает текущее состояние. Это не влияет на history cursor или domain integrity.

Smoke dataset: 100 дополнительных clients, 200 requests (100 active/100 closed),
4000 messages. EXPLAIN истории использует unique index conversation/sequence;
маленький inbox может предпочесть seq scan + top-N sort. Новые индексы без признаков
bottleneck не добавлены. Local timings — диагностические, не SLA/нагрузочный benchmark.

## Ограничения v1

- Только text/photo/document; voice/video и прочие типы не поддерживаются.
- Файлы не скачиваются: сохраняются Telegram file_id/file_unique_id и caption.
  Filename/MIME не являются security boundary; unsafe/длинные имена не сохраняются.
- До 30 приложений при создании заявки; albums приходят отдельными updates.
- Нет message edit/delete, automatic retention, payments/deposits, group/web chat.
- Один мастер на deployment; нет multi-master назначения.
- Redis/FSM draft и successful UI delivery не являются durable domain data.
- Реальный Telegram аккаунт, публичные DNS/TLS и live token проверяются отдельно
  оператором; automated E2E использует настоящий dispatcher с mocked Bot API.

Результаты Phase 7.5C и RC решение: [conversations-phase75c-report.md](conversations-phase75c-report.md).
