# Phase 7.5C: hardening и regression report

Дата: 2 октября 2026, Europe/Moscow. Работа выполнена в текущей ветке master с
сохранением существующих незакоммиченных изменений Phase 1–7.5B. Новые пользовательские
сценарии не добавлялись. Phase 8 не запускалась.

Baseline: Ruff — PASS; pytest — 371 passed; integration — 98 passed. Для integration
созданы отдельные PostgreSQL 17/Redis 7.4 containers на loopback 55432/56379. Первый
запуск без этого профиля не подключился к БД; после подготовки isolated profile
baseline прошёл. Существующие БД и deployments других проектов не изменялись.

## Исправленные проблемы

| Уровень | Проблема | Исправление и доказательство |
|---|---|---|
| HIGH | Telegram показывал отправку/принятие цены/создание записи до commit; отказ commit мог оставить ложный успех | Transport persistence boundary после полной mutation и до UI. Шесть webhook regressions: create, reply, propose, accept, cancel, book. Rollback сохраняет FSM и позволяет retry того же update |
| MEDIUM | GiST exclusion concurrency мог вернуть deadlock вместо обычного slot conflict | Обработка SQLSTATE 40P01 после rollback существующего hold savepoint. Connection outage не маскируется; unit tests и два согласовавших цену клиента на один slot |
| MEDIUM | Cached SQLAlchemy read state мог уменьшить прочитанный cursor | populate_existing после request lock; regression с двумя сессиями и удерживаемым старым ORM object |
| MEDIUM | Inbox делал N+1 запросы conversation/unread | Один authorized unread_by_request запрос для всей страницы. Проверены query count, 100 active requests и отсутствие доступа чужого клиента |
| MEDIUM | Старый proposal job мог повторно отправлять latest цену | Worker сверяет event_id с latest PENDING proposal; superseded job CANCELLED, актуальный job SENT, новый worker не отправляет SENT снова |
| MEDIUM | Недостаточная audit трассировка и копирование приватного comment в audit | message_created и price_superseded events; comment остаётся только в domain history, audit содержит IDs и структурированные коммерческие поля |
| MEDIUM | Закрытие переписки не ставило уведомление клиенту | client_booking_request_closed в прежнем transactional outbox; повторный close не дублирует event/job |
| LOW | Подделанный чрезмерный integer cursor мог вызвать DB range error | Проверка nonnegative 32-bit cursor/offset до SQL |

Оставшихся BLOCKER/HIGH по data integrity, access control и booking consistency
не обнаружено. Blocker или HIGH нельзя считать устранённым без перечисленных
executable regressions.

## Результаты по критериям запроса

1. **State machine.** Переходы централизованы в service layer. CANCELLED/CLOSED/BOOKED
   terminal; нет CANCELLED→BOOKED, CLOSED→TERMS_ACCEPTED или BOOKED→DRAFT.
   TERMS_ACCEPTED→BOOKED требует актуальной принятой revision и существующего hold.
   BOOKED conversation остаётся доступной, её закрытие не меняет request lifecycle.
2. **Concurrency.** Request row lock сериализует propose/accept/message/read/booking.
   Проверены десять concurrent proposals (один PENDING), ожидание accept за заменой
   15k→17k, double accept без повтор events/jobs, final booking одной заявки и два
   клиента на один slot. Partial unique PENDING index и unique appointment/request
   проверены; exclusion constraint остаётся прежним механизмом slot protection.
3. **Stale callbacks.** Старые proposal IDs, завершённая заявка, закрытый диалог,
   повторная запись/confirm и занятый slot безопасно отклоняются. Сервис не доверяет
   Telegram message. После restore проверены stale accept и отсутствие изменений строк.
4. **Notification reliability.** Все request kinds строятся существующим worker;
   closing kind добавлен в него же. Retry/permanent failure/reclaim/claim ownership
   и graceful shutdown покрыты общим worker integration. Proposal переживает
   временную недоступность Telegram. Superseded jobs пропускаются, SENT не redeliver
   при обычном restart. Exactly-once при потере acknowledgement не гарантируется.
5. **Unread.** Read pointer монотонен; latest page отмечается только после доставки.
   Одновременное новое сообщение остаётся unread в обе стороны. Cached-session
   regression исключает уменьшение cursor. Failed history delivery не отмечает read.
6. **Access control.** Negative domain и crafted callback tests проверяют client,
   assigned master, revoked membership и business mismatch. UUID берётся из
   callback только как object ID; actor identity — из authenticated Telegram update.
   Batch unread сохраняет тот же SQL access predicate. Неавторизованная заявка и
   отсутствующий ID имеют одинаковый отказ.
7. **Pagination/performance.** History 10/100/1000 сообщений с одинаковыми timestamps
   прочитана без пропусков и дублей; SQL LIMIT и ORM object count ограничены страницей.
   Inbox — 100 active плюс 100 closed requests; dataset — 100 clients/4000 messages.
   Финальный локальный smoke: пять inbox pages ~77 ms, history page ~21 ms,
   total unread ~12 ms, proposal ~53 ms, hold + booking + commit ~215 ms.
   EXPLAIN history использует conversation/sequence unique index; маленький inbox
   предпочитает seq scan/top-N sort (~0.27 ms). Фактический SQL total_unread занял
   ~2.4 ms. При параллельных infrastructure drills был одиночный latency spike
   ~1 s; повтор после завершения нагрузки и EXPLAIN не выявили SQL bottleneck.
   Эти локальные замеры не являются production SLA. Новых индексов не добавлено.
8. **Media security.** Photo/document, albums, unsupported types и oversize text
   проверены. Сохраняются только Telegram IDs/caption. Filename/MIME не сохраняются,
   произвольные файлы не скачиваются/исполняются. Входные лимиты не truncates текст.
9. **Backup/restore.** E2E pg_dump/pg_restore сравнивает полные строки связанных
   таблиц и проверяет старые кнопки на restored state. Отдельный deployment DR seed
   содержит 54 messages, photo/document, две proposals, accepted price, appointment
   и unread pointer; controlled test volume loss использует прежний BackupManager.
10. **Old-version upgrade.** UUID test DB создаётся старым pre-conversations image
    на b62f3d910ea4 и обновляется текущим image до head. Существующие clients,
    services, appointments и notification rows сохранены; FIXED backfill/default
    подтверждён; повторный upgrade безопасен. Application rollback не делает Alembic downgrade.
11. **FIXED/FROM/NEGOTIABLE.** Прежний быстрый FIXED flow сохранён; FROM поддерживает
    обычную запись и обсуждение; NEGOTIABLE запрещает booking без согласия. Telegram
    dispatcher/harness и domain tests покрывают эти пути.
12. **Appointment regression.** Request appointment поддерживает notes, reschedule,
    calendar export, cancellation by client/master, complete/no-show и прежние
    notifications/reminders. Цена остаётся snapshot принятого proposal, история
    диалога и request BOOKED сохраняются при дальнейших операциях appointment.
13. **Telegram escaping/security.** HTML escaping, длинные descriptions/comments,
    history batching, media captions и callback byte limits покрыты тестами.
    Пользовательский content не меняет системную разметку. Logs новой функции не
    содержат body/phone/token/files; SQL parameters скрыты. Payment/API endpoints
    или новые privileges не добавлены.
14. **Full two-user E2E.** Реальный dispatcher + PostgreSQL/Redis, mocked Bot API:
    request/photo/document → master read/reply → client reply/discussion → 15k →
    17k → stale accept 15k rejected → accept 17k → slot → appointment → master view
    → дальнейший диалог. Повторные updates не повторяют domain writes/jobs.
15. **Two-deployment isolation.** Domain business mismatch tests, раздельные A/B
    DR datasets и infrastructure smoke проверяют сохранность peer deployment,
    раздельные credentials/networks/volumes и невозможность foreign DB connection.
16. **Full checks.** Финальные результаты и infrastructure drills приведены ниже.
    Статическая type-check команда в проекте не настроена; Ruff, compileall и pip
    check выполнены. Live Telegram/public DNS/TLS не входят в mocked E2E.
17. **BLOCKER/HIGH.** Найденный HIGH — success UI до commit — устранён. Двойная цена,
    двойная запись, доступ чужого клиента или повреждение history не воспроизведены.
    Дополнительные MEDIUM/LOW исправления перечислены выше.
18. **Known limitations.** Text/photo/document only; нет edit/delete, voice/video,
    payments/deposits/group/web chat; один мастер на deployment. Черновой FSM ввод
    может потеряться при Redis loss. Inbox offset pages могут сместиться при
    concurrent новых заявках. Telegram delivery acknowledgement не атомарен с БД.
19. **RC decision.** Conversations готовы к Release Candidate Phase 8 по выполненным
    automated/mocked критериям. Переход к Phase 8 не выполнялся. Production release
    с реальными Telegram accounts, DNS/TLS и операторскими credentials проверяется
    отдельно; этот отчёт не утверждает live Telegram delivery или host reboot.

## Финальные executable checks

| Проверка | Результат |
|---|---|
| Ruff check . | PASS |
| pytest (полный обычный набор) | 373 passed, integration deselected |
| pytest -m integration (полный набор) | 124 passed, unit deselected |
| compileall и pip check | PASS |
| Production Docker image build | PASS, booking-bot:phase75c-rc, version 0.7.0 |
| Старый image → populated old DB → новый RC image → Alembic | PASS, preserved clients/services/schedule/appointments/notifications, FIXED default |
| Dump/restore + старые proposal buttons | PASS, exact rows, stale actions rejected |
| operations_smoke.py | PASS: A/B DR, dedicated volume loss, safety backup, API/worker/Redis/PostgreSQL restart, worker kill, outages/alerts, Redis loss, injected backup failure, network/data isolation |
| Infrastructure load smoke | PASS: 96 requests, concurrency 32, all HTTP 200, p95 ~1.225 s |
| proxy_operations_smoke.py | PASS: routing, Docker API ACL, network isolation, outage/restart, backend alive |
| release_rollout_smoke.py | PASS: two-client update, pre-update backup, real pull/migration, doctor, failed rollout stop, exact image/DB recovery, no downgrade |
| Performance smoke + SQL EXPLAIN | PASS: 100 additional clients, 200 requests, 4000 messages, price proposal и appointment creation |

Основные infrastructure drills выполнялись на `phase75c-final` image; после финальной
правки refresh read pointer собран `phase75c-rc`. Эта правка покрыта двухсессионной
integration regression; migration drill повторён через RC image. Docker packaging,
infrastructure configuration и dependencies между этими образами не менялись.

Первый proxy smoke при параллельных Docker drills остановился из-за отсутствующего
proxy check в неполном host report. Повтор без изменения кода прошёл; начальная
причина отдельно не воспроизведена. Это оставшийся LOW риск диагностического
snapshot, не найденный дефект conversations или access control. При неполном
inventory следует повторить doctor и сопоставить текущий runtime.

Не выполнялись реальный host reboot, физическое заполнение диска, реальные Telegram
отправки и public TLS/DNS acceptance. Disk-full alert проверен injected thresholds;
notification/alert sending — mocks/capturing sender. Это границы выполненного
тестирования, а не утверждение об exactly-once delivery.

Артефакты текущего запуска сохранены в `tmp/phase75c-*.txt`,
`tmp/phase75c-performance.json`, `tmp/phase75c-ops/clients-results.json`,
`tmp/phase75c-proxy/proxy-results.json`; deployment data/backups — в изолированных
`tmp/phase75c-ops` и `tmp/phase75c-rollout*`. Эти каталоги исключены из Git.
Четыре созданных smoke deployments остановлены; их volumes и backups сохранены.
Отдельные integration PostgreSQL/Redis containers оставлены для повторного запуска
тестов на loopback 55432/56379.

См. [operations runbook](conversations-operations.md), [domain contract](conversations.md)
и [Telegram flow](conversations-telegram-flow.md).
