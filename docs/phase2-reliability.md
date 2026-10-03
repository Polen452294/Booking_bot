# Phase 2 — надёжность webhook и notification worker

## Webhook

Endpoint: `POST /api/v1/webhooks/telegram`. Header
`X-Telegram-Bot-Api-Secret-Token` проверяется через `hmac.compare_digest`.
Secret и тело update не попадают в диагностические сообщения.
Aiogram/Pydantic валидируют Telegram schema; `update_id` дополнительно требует
настоящий неотрицательный integer (не строку и не boolean).

| Ситуация | HTTP |
| --- | --- |
| Успешная обработка и commit | 200 |
| Повтор успешно обработанного update | 200 |
| Первый запрос ещё обрабатывает этот update | 503, Retry-After: 2 |
| Redis недоступен | 503; бизнес-обработчик не запускается |
| PostgreSQL/commit недоступны, handler завершился ошибкой | 5xx; update не подтверждается |
| Неверный или отсутствующий secret при корректном JSON body | 403 |
| Пустое тело, неверный JSON/schema/update_id | 422 |
| Неизвестный новый тип update с корректным update_id | 200, безопасно игнорируется |

Namespace: `booking:<bot_id>`; bot ID — публичная числовая часть токена, secret
не используется в ключах. Каждая установка по-прежнему имеет собственные PostgreSQL
и Redis. Не запускайте две независимые установки с одним Telegram-ботом.

Ключ `booking:<bot_id>:telegram:update:<update_id>` захватывается атомарным Lua:
отсутствует → `processing:<random-owner-token>`, успешно завершён → `processed`.
Завершить или удалить lease может только его владелец. Конкурентному запросу
нельзя отвечать 200 до результата первого: тот ещё может откатиться.

Processing lease живёт **120 секунд**, обработка с commit ограничена **60 секундами**.
Ошибка откатывает транзакцию и освобождает lease. При аварии процесса или недоступном
Redis lease истекает самостоятельно. Redis failure никогда не включает обход защиты.

Processed TTL — **7 суток**. Это запас к 24-часовому хранению входящих updates,
описанному в [Telegram Bot API](https://core.telegram.org/bots/api#getting-updates).
За пределами окна хранения повторная обработка старого update не исключается.

Одна Redis-метка не закрывает окно «DB commit прошёл, процесс умер до processed».
Поэтому таблица `telegram_update_receipts` содержит составной ключ
`(namespace, update_id)` и `expires_at`; receipt записывается **в одной транзакции
с бизнес-изменениями**. `INSERT ... ON CONFLICT` блокирует конкурентную транзакцию
даже при утрате Redis lease. При rollback receipt исчезает; после commit повтор
видит receipt и не запускает handler. Redis здесь — быстрый фильтр и lease,
PostgreSQL — подтверждение сохранённого результата.

Receipt имеет такое же окно 7 суток. На updates с `update_id % 100 == 0`
удаляется не более 1000 истёкших receipts через индекс и SKIP LOCKED.
Без трафика оставшиеся истёкшие строки не растут и очищаются при новых updates.
Redis удаляет свои ключи по TTL самостоятельно.

Гарантия относится к транзакционным изменениям PostgreSQL в пределах этого окна.
Webhook буферизует изменения FSM в памяти до успешного DB commit. Per-chat lock
aiogram удерживается до commit и атомарного применения state/data в Redis:
ошибка handler/commit не уничтожает контекст подтверждения перед повтором.
Ответы Telegram и Redis FSM всё же не участвуют в DB-транзакции: при аварии
**после** commit, но до применения FSM, пользователь может увидеть прежнее меню;
нужно снова открыть /start. Receipt при этом защищает сохранённую бизнес-операцию.
Отправленный до rollback ответ Telegram также может повториться. Это не общая
exactly-once гарантия внешних эффектов.
Development polling сохраняет прежний путь aiogram; webhook lease, receipt и
буферизация FSM на polling не накладываются. Per-chat isolation и DB-защита
повторного confirm действуют в обоих режимах.

## Диагностика webhook

```sh
booking-admin set-webhook
booking-admin webhook-status
```

`set-webhook` сохраняет pending updates, затем вызывает `getWebhookInfo` и `getMe`.
Обе команды показывают username, ожидаемый и фактический URL, размер очереди,
последнюю ошибку/дату, max connections и allowed updates. Токен/secret скрываются.
Различие URL — `Status: ERROR` и exit 1. Совпавший URL без очереди/ошибок — OK.
Pending updates или сохранённая историческая ошибка — WARNING: поле last error
само по себе не доказывает текущую недоступность. Сетевые/API ошибки дают exit 1.

## Worker: claim, отправка, retry

Существующая очередь и `FOR UPDATE SKIP LOCKED` сохранены. Каждая захваченная
job получает `claim_token` процесса. Перед отправкой worker заново проверяет
владельца и `processing`, удерживая row lock до завершения send и commit.
Stale recovery тоже использует SKIP LOCKED: активная отправка не перехватывается.
Прежний владелец не может отправить или освободить job после смены владельца.
Несколько worker-контейнеров могут читать одну очередь.

Состояния:

```text
pending → processing → sent
                     → pending (отложенный retry)
                     → failed (permanent / max attempts)
                     → cancelled (настройки получателя / отменённая запись)
```

- Forbidden/BadRequest/NotFound/Unauthorized aiogram, неподдерживаемый kind и
  неполный recipient/context → failed без retry.
- Network/server/timeout и прочие ошибки → ограниченный retry: 15, 60, 300,
  900, 3600 секунд. Задержка считается от окончания неудачной попытки;
  `TelegramRetryAfter.retry_after` увеличивает её при необходимости.
- `NOTIFICATION_MAX_ATTEMPTS` по умолчанию 5. Счётчик отражает захваты job:
  авария после claim тоже расходует попытку. Stale processing старше 5 минут
  возвращается в pending либо становится failed при исчерпанном лимите.
  Исчерпанные pending jobs не отправляются ещё раз.
- Вся операция доставки ограничена 60 секундами; Telegram HTTP timeout — 20 секунд.
  При неожиданной ошибке БД worker завершится с ошибкой, Compose применяет
  существующий `on-failure:5`. Неподтверждённый результат восстанавливается как stale.

Практическая стратегия — ограниченная доставка с повторными попытками, с приоритетом
не потерять сообщение при неоднозначном сетевом результате. Telegram `sendMessage`
не предоставляет ключ идемпотентности. **Если Telegram принял сообщение, а процесс
умер или потерял DB commit, повторная отправка остаётся возможной.** Row locks и
claim tokens предотвращают обычную параллельную отправку, но не устраняют это окно.
При разрыве DB-соединения блокировка также может исчезнуть раньше окончания внешнего
запроса. Exactly-once не заявляется; `failed` после неоднозначной попытки не доказывает,
что получатель ничего не получил. Ручной повтор такой job требует учёта этого факта.

Логи содержат started/shutdown, job claimed/sent/retry/permanently failed/stale recovered,
ID job и номер попытки. Тексты сообщений, recipient и пользовательские данные не пишутся.

## Heartbeat и graceful shutdown

Ключ `booking:<bot_id>:worker:heartbeat:<hostname>` содержит UTC timestamp с TTL
**120 секунд**. Worker обновляет его в основном цикле и после завершения job,
на простое — не реже одного раза в 30 секунд. Отдельной фоновой задачи, способной
маскировать зависший цикл, нет. После штатного завершения ключ удаляется.

```sh
booking-admin worker-health
booking-admin worker-health --worker-id <hostname-worker>
```

Проверяются PostgreSQL (`SELECT 1`), Redis (`PING`) и свежесть heartbeat.
Exit 0 — всё исправно, exit 1 — ошибка; выводится время последнего heartbeat,
если ключ ещё существует. После TTL время уже недоступно, выводится missing/expired.
Команду удобнее выполнять **в worker-контейнере**, через `docker compose exec worker
booking-admin worker-health`; отдельный `run --rm` имеет другой hostname.
Каждый контейнер проверяет собственный ключ, здоровая реплика не скрывает зависшую.
На одном хосте без контейнеров предполагается один CLI worker на hostname.

Docker healthcheck: interval 30s, timeout 15s, start_period 30s, retries 3.
Два сетевых этапа CLI имеют deadline по 4 секунды. TTL оставляет запас для
60-секундной job; зависший цикл теряет heartbeat и становится unhealthy примерно
через 3–4 минуты с учётом retries. Сам статус unhealthy не перезапускает контейнер.

SIGTERM/SIGINT выставляет stop event. Worker не начинает следующую job,
заканчивает текущую, сохраняет её результат, освобождает не начатую часть пачки
без расходования попыток и закрывает Bot session, heartbeat Redis, FSM и DB engine.
Compose даёт worker **120 секунд**: время текущей операции, освобождения пачки и cleanup.
При принудительном SIGKILL/падении зависимости остаётся stale recovery.

## Миграция и границы

Новая Alembic head: `b62f3d910ea4`, после `a41d2c9e7b63`. Добавлены receipt table
и nullable `notification_jobs.claim_token`; существующие jobs сохраняются.
Остановите старые API/worker перед обновлением, затем выполните существующий init:
совместная работа старого worker, не проверяющего ownership, с новым не поддерживается.
Downgrade удаляет receipts и колонку ownership, поэтому теряет накопленную защиту от replay.

Регрессионные DB-тесты покрывают два confirm одного hold и конкуренцию двух
клиентов за один слот. Повторное использование hold больше не переводит активную
запись в expired; проверка добавлена также в ручную запись и оба пути переноса.
Существующие exclusion/unique constraints и транзакции booking-ядра сохранены.

Integration-тестам теперь нужны **PostgreSQL и Redis** с изолированными тестовыми
данными и применёнными миграциями. Реальные вызовы Telegram в тестах заменяются
mock или локальным HTTP stub. Публичный HTTPS и настоящий Telegram round trip
не входят в эти проверки.

Phase 3 не реализована. Deployment manager, TLS/proxy automation, backup/restore,
CI/CD, массовые обновления и централизованный monitoring остаются отдельными задачами.
