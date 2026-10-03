# Phase 7.5A–C: заявки, переписка и согласование условий

Backend расширяет существующий single-specialist Booking_bot. Telegram adapter Phase
7.5B описан в [conversations-telegram-flow.md](conversations-telegram-flow.md).
REST CRUD и платежи в эти фазы не входят.

## Услуги и совместимость

`Service.pricing_mode`: `fixed`, `from`, `negotiable` (`PricingMode`). Старые услуги
и старые конфигурации получают `fixed`; `price_minor` сохраняет прежний смысл.
`ServiceConfig.pricing_mode` можно задать в specialist.toml. Настройка через
`SpecialistServiceCatalog.set_pricing_mode` сохраняется при повторном configure.

* FIXED: прежний выбор услуги → даты → времени → телефона → подтверждения.
* FROM: разрешены обычная запись с snapshot начальной цены и заявка на обсуждение.
* NEGOTIABLE: создаётся заявка; обычный `confirm_hold` без принятой заявки запрещён.

В 7.5B handlers выбирают сценарий по режиму услуги, отображают «от» и
«Стоимость после обсуждения». FIXED использует прежний календарный flow.

## Сущности и связь с записью

* `BookingRequest`: business, назначенный master, клиент, услуга, исходное описание,
  snapshots названия услуги, имени и телефона, статус и timestamps.
* `Conversation`: одна на заявку (unique request FK), `open`/`closed`, последовательность сообщений.
* `ConversationMessage`: append-only текст, photo/document metadata или структурированное
  system event (`event_type`, `event_payload`), автор, роль, время и номер в переписке.
* `ConversationReadState`: отдельный курсор для каждого пользователя/переписки.
* `PriceProposal`: неизменяемые сумма в minor units, currency и комментарий,
  автор, номер revision, статус и timestamps решения.

`Appointment.booking_request_id` nullable и unique: старые записи не требуют заявки,
одна заявка создаёт максимум одну запись. Связь request → conversation сохраняется
после booking. `Appointment.price_minor` и `currency` являются snapshot **последнего
принятого** предложения. Изменение услуги и перенос записи не меняют согласованную цену.
Snapshots контакта берутся из заявки. Request остаётся `booked`, даже если запись
позже завершена/отменена: история договорённостей и lifecycle записи независимы.

## State machines

```text
draft → waiting_master
waiting_master ↔ waiting_client
waiting_master / waiting_client → terms_proposed
terms_proposed → terms_accepted              (client accept)
terms_proposed → waiting_master              (client reject)
terms_accepted → terms_proposed              (master: новое предложение)
terms_accepted → booked                      (confirm существующего hold)
любое состояние до booked → cancelled       (client/assigned master)
любое состояние до booked → closed          (assigned master)
```

`cancelled`, `closed`, `booked` — terminal request states. Только draft можно
submit. Новое предложение в `terms_proposed` сохраняет статус заявки.
Обычные сообщения меняют waiting states по отправителю; сообщения после принятия
цены сохраняют принятие до явного нового предложения мастера.

```text
pending → accepted / rejected / superseded
accepted / rejected / superseded → terminal
```

Новое предложение supersedes старое PENDING. Прежнее ACCEPTED остаётся ACCEPTED
как историческое решение; новая revision требует нового согласия. Решение принимается
только для последней PENDING revision при `terms_proposed`; старая кнопка и повторное
принятие дают `ProposalNotPendingError`. Сумма не перезаписывается. Частичный unique
index запрещает две PENDING revisions. PostgreSQL triggers запрещают UPDATE сообщений,
изменение условий proposal и изменение уже resolved proposal.

## Внутренний service API

* `BookingRequestService`: create (submit по умолчанию, либо draft), submit, get,
  list_requests (statuses/limit/offset), cancel, close, list_slots, create_hold, book.
* `ConversationService`: get по request, send_message, list_messages, unread_count,
  mark_read, close. Работа с сообщениями использует conversation_id.
* `PriceProposalService`: propose, accept, reject, list_proposals.

Все пользовательские операции требуют business_id и actor_user_id (внутренний UUID
из аутентифицированного Telegram update; его нельзя брать из callback payload).
`book`, `create_hold`, `list_slots` принимают Settings, request_id и client actor.
Сначала требуется принятие условий, затем переиспользуются AvailabilityService и
BookingService. Проверяется соответствие hold по business, master, service, client;
неподходящее удержание не конвертируется. Новой календарной логики нет.

## Доступ и транзакции

SQL-фильтр проверяет business заявки и одного из участников:
client_user_id, либо назначенный активный Master с соответствующим user_id и активным
BusinessMember (`master`/`owner`) в **той же** business. Любой другой специалист,
manager и другой клиент не получают доступ. Чужой и отсутствующий ID возвращают
одинаковую `ConversationAccessError`. Предложения создаёт только мастер, решения
принимает только клиент, закрывает переписку мастер. Worker повторно проверяет доступ
получателя перед уведомлением (в том числе после отвязки мастера).

Services используют переданный AsyncSession и **не commit**. Caller отвечает за
outer transaction и commit/rollback. Критичные операции выполняются в savepoint:
domain state, message/event, AuditLog и NotificationJob откатываются вместе даже
при перехваченной caller ошибке. После rollback ORM-объекты могут быть expired;
caller должен держать UUID отдельно и повторно читать через async service.

Изменения одной заявки сериализуются `SELECT ... FOR UPDATE` на BookingRequest.
Порядок: request → conversation/counters/proposals → SlotHold/calendar.
Запросы после блокировки обновляют ORM identity map (`populate_existing`).
Номера сообщений/revisions выделяются внутри этой блокировки. При финальном
booking повторно проверяются статус и latest proposal. PostgreSQL exclusion
constraint календаря и существующие проверки hold сохраняют защиту от пересечений.

Схема не использует PostgreSQL RLS: доверенный внутренний service API проверяет
права в SQL; прямой доступ к БД требует операторских полномочий.

## История, pagination, unread и media

Сообщения — TEXT, PHOTO, DOCUMENT, SYSTEM. API запрещает пользовательский SYSTEM,
VIDEO/VOICE и несовместимые payloads. Limits: description/text 4000 символов,
proposal comment 2000, name 160, file_id 512, file_unique_id 256; цена 0–2 млрд
minor units, currency три ASCII uppercase символа. Контакт нормализуется.

PHOTO/DOCUMENT сохраняют `telegram_file_id`, `telegram_file_unique_id` и caption;
файлы не скачиваются и не исполняются. Нет зависимости domain services от aiogram
Bot. Будущий adapter может отправлять metadata через Telegram API; для другого
канала можно добавить media reference без изменения lifecycle заявки.

`list_messages(limit=1..100, after_sequence=0)` возвращает сообщения по возрастанию.
Следующий cursor — sequence последнего сообщения. Unique `(conversation_id, sequence)`
обслуживает историю и unread range; index `(conversation_id, created_at)` обслуживает
временной поиск. `list_proposals` возвращает revisions убывающе, cursor before_revision.

Unread: сообщения с sequence выше user cursor, отправленные другим участником
(включая system events другого участника). Собственные сообщения/event не увеличивают
unread. Чтение истории само по себе не помечает её прочитанной. `mark_read` принимает
последнюю **показанную** sequence, никогда не уменьшает cursor и отвергает будущее значение.
Сериализация с отправкой сообщений предотвращает потерю unread в гонке.

`price_proposed`, `price_accepted`, `price_rejected`, `request_cancelled`,
`appointment_created` и lifecycle events записываются в сообщения с JSON payload и
существующий AuditLog. AppointmentHistory создаётся прежним BookingService.
Нет методов редактирования/удаления истории и автоматического retention.

Close до booking закрывает request и withdraws pending proposal; cancel делает то же
со статусом cancelled. Close после booking закрывает только conversation и оставляет
request booked. Закрытая история доступна участникам; новые сообщения запрещены.
Автоматическое закрытие после appointment lifecycle пока не подключено.

## Уведомления

Используются прежние NotificationJob, worker, claim_token, retry/backoff и preferences:
master_new_booking_request, master_new_conversation_message,
client_new_conversation_message, client_price_proposal,
master_price_proposal_accepted, master_price_proposal_rejected,
master_booking_request_cancelled, client_booking_request_closed.

Новое nullable `booking_request_id` даёт контекст до appointment. Nullable unique
`event_key = kind:event_id:recipient` обеспечивает deduplication при отсутствующем
appointment_id. Повторные proposal callbacks не создают новых jobs. Telegram update
receipts Phase 2/transactional webhook остаются механизмом дедупликации входящих updates:
7.5B должен вызывать сервисы внутри этой транзакции. Вне webhook caller сам обеспечивает
идемпотентность повторных create/send (одинаковый текст может быть легитимным сообщением).

Worker отправляет краткое уведомление с контекстом заявки и кнопкой её открытия.
Предложение содержит текущую цену, комментарий и кнопки принятия/обсуждения.
NotificationJob не отмечает историю прочитанной. HTML escaping сохраняется.
Exactly-once отправка в Telegram не гарантируется, как и в существующем outbox.

7.5C: jobs заменённых/уже принятых предложений пропускаются worker по latest
proposal и event_id. Уже отправленная кнопка всё равно повторно проверяется в
service layer. Domain mutation и outbox сохраняются до отправки успешного UI;
ошибка Telegram после commit не отменяет согласие, сообщение или запись. Повтор
update блокируется прежним PostgreSQL receipt, даже после потери Redis.

Inbox использует один `unread_by_request` запрос для всей страницы вместо запросов
на каждую заявку. Он проверяет тот же business/participant predicate. История
ограничена SQL LIMIT; порядок — transactional sequence. `message_created` и
`price_superseded` фиксируются в AuditLog; комментарии к цене остаются в истории,
но не копируются в audit details. Cursor/offset ограничен диапазоном PostgreSQL INTEGER.

Конкурентные GiST exclusion inserts иногда завершаются PostgreSQL deadlock
(`40P01`). Существующий BookingService откатывает savepoint и переводит именно
эту ошибку в SlotUnavailableError. Остальные DBAPI ошибки не маскируются.

Phase 7.5B добавляет `BookingRequestService.for_appointment`, фильтр
`list_requests(unopened_by_actor=True)`, `ConversationService.total_unread` и
`PriceProposalService.decide_by_id`. Последний находит заявку по proposal и затем
проходит те же authorization/lock/latest-revision проверки, что accept/reject.
Отмена заявки атомарно создаёт job для мастера. Событие нового предложения сохраняет
предыдущую сумму, чтобы показать изменение цены без изменения старых сообщений.

## Миграция, backup и update

`c75a01d29f10` после `b62f3d910ea4`: пять новых таблиц, pricing_mode с server default
fixed, nullable FK appointment/request и notification/request, unique event_key,
indexes/checks и два immutable triggers. Upgrade ничего не удаляет и не меняет цены.
Повторный upgrade безопасен. Downgrade запрещён при наличии заявок, чтобы не терять
коммерческую историю; rollback production делается существующим backup/restore.

Phase 5 делает полный PostgreSQL dump, поэтому новые таблицы включаются автоматически.
DR acceptance seed/snapshot теперь включает request, conversation, text/media/system
metadata, read cursor, proposal и связь appointment. Phase 6 rollout smoke продолжает
новый Alembic head, сохраняет новые business rows и проверяет restore.

Проверки: `ruff check .`, `pytest`, `pytest -m integration`. Migration integration
создаёт отдельные disposable БД для empty → head и populated previous → head
(нужны CREATEDB права в тестовом профиле). `tests/disaster_recovery_smoke.py` проверяет
настоящий dump/restore через BackupManager на двух отдельных deployment.
