# Production hardening — Phase 1 + Phase 2

Для нескольких клиентов на одном VPS используйте `bookingctl` и общую
инфраструктуру из [reverse-proxy.md](reverse-proxy.md) и
[https-webhook.md](https-webhook.md). Ниже описан прежний Compose одного
deployment; он не заменяет Phase 4 proxy workflow.

Один deployment обслуживает одного специалиста и одного Telegram-бота.
PostgreSQL, Redis, токен, TOML и данные принадлежат только этой копии.
Booking-ядро и существующие сценарии кабинета сохранены.

## Конфигурация

Нужны Python 3.12 и Docker Compose 2.24.4 или новее. Создайте `.env.production`
вне Git, с правами чтения только у оператора. Ниже — шаблон, а не рабочие secrets:

```dotenv
APP_ENV=production
COMPOSE_PROJECT_NAME=specialist-example
API_PORT=8000
POSTGRES_DB=booking
POSTGRES_USER=booking_owner
POSTGRES_PASSWORD=REPLACE_WITH_RANDOM_DATABASE_PASSWORD
REDIS_PASSWORD=REPLACE_WITH_RANDOM_REDIS_PASSWORD
DATABASE_URL=postgresql+asyncpg://booking_owner:REPLACE_WITH_RANDOM_DATABASE_PASSWORD@postgres:5432/booking
REDIS_URL=redis://:REPLACE_WITH_RANDOM_REDIS_PASSWORD@redis:6379/0
TELEGRAM_BOT_TOKEN=REPLACE_WITH_BOTFATHER_TOKEN
TELEGRAM_WEBHOOK_BASE_URL=https://booking.example.com
TELEGRAM_WEBHOOK_HEADER_SECRET=REPLACE_WITH_RANDOM_WEBHOOK_SECRET
SPECIALIST_CONFIG_PATH=./specialist.toml
LOG_LEVEL=INFO
BOOKING_IMAGE=ghcr.io/polen452294/booking-bot:1.0.0-rc.1
```

Сгенерируйте три независимых значения, например `python -c "import secrets;
print(secrets.token_hex(32))"` для каждого. Пароли PostgreSQL и Redis в URL
должны совпадать с соответствующими `*_PASSWORD`. Hex не требует URL-encoding;
если используете другие символы, закодируйте пароль в URL percent-encoding.
Не вставляйте secrets в Git, логи, команды диагностики или build arguments.
`docker compose config` без `--quiet` показывает итоговые secrets — не публикуйте его вывод.

Production отвергает отсутствующие обязательные настройки, HTTP webhook,
credentials/query в webhook URL, пустые/очевидные secrets, неверный формат
токена, PostgreSQL без пароля и Redis без пароля. Минимум для паролей — 24
символа, для webhook secret — 32; webhook допускает только `A-Z a-z 0-9 _ -`
и максимум 256 символов. Это проверка конфигурации, а не подтверждение
подлинности токена Telegram или криптографической случайности строки.

`SPECIALIST_CONFIG_PATH` в production Compose — путь **на хосте**; файл
монтируется read-only в `/app/specialist.toml`. Его должен читать UID 10001.
Для запуска Python непосредственно переменная указывает локальный путь.
Проверяются структура TOML, типы полей, профиль, timezone, валюта, услуги и
интервалы расписания. Услуги, изменённые владельцем в боте, и его рабочие часы
не перезаписываются обычным `configure`.

## Development и production

Development: скопируйте `.env.example` в `.env`, задайте токен, выполните
`APP_VERSION=$(python -c 'from booking_bot.version import __version__; print(__version__)') docker compose up --build -d`.
Для polling запустите только
`docker compose up -d postgres redis`, затем локально примените Alembic,
`booking-admin configure`, `booking-admin run-polling` и `booking-admin run-worker`.
Не запускайте polling одновременно с webhook той же копии.

Development публикует API, PostgreSQL и Redis только на loopback. В нём
разрешены удобные credentials. Compose строит URL PostgreSQL из `POSTGRES_*`,
если `DOCKER_DATABASE_URL` не задан. Для локального Python отдельно обновите
`DATABASE_URL`, если меняете credentials или опубликованный порт.

Production:

```sh
docker compose --env-file .env.production -f compose.yaml -f compose.prod.yaml config --quiet
docker compose --env-file .env.production -f compose.yaml -f compose.prod.yaml pull
docker compose --env-file .env.production -f compose.yaml -f compose.prod.yaml up -d --wait
```

Production override явно задаёт `APP_ENV=production` и не читает development
`.env` в контейнеры. Значения берутся из `--env-file` и окружения оболочки
(окружение имеет приоритет). `DOCKER_DATABASE_URL` и `DOCKER_REDIS_URL` здесь
не используются; `DOCKER_TELEGRAM_PROXY_URL` остаётся необязательным.

API/worker/init работают от UID/GID 10001, с read-only filesystem и `/tmp`
в tmpfs. Код root-owned, settings и secrets в image отсутствуют. Redis
работает от пользователя `redis`; PostgreSQL использует штатный entrypoint.
PostgreSQL и Redis сохраняют данные в отдельных named volumes; Redis включает AOF.
PostgreSQL/Redis/worker не публикуют порты, API публикуется только на loopback.
Доступ контейнеров идёт через отдельную Compose network этого проекта.

Первым выполняется одноразовый `init`: Alembic, затем `configure`.
При ошибке init API/worker не стартуют. API не выполняет миграции, запускается
с одним Uvicorn worker. По умолчанию используются один API и один notification worker;
Phase 2 допускает несколько worker-контейнеров одной очереди. Не запускайте
одновременно несколько init или команд миграции.
При обновлении останавливайте процессы, использующие изменяемую схему;
автоматизация обновлений в эту фазу не входит.

API/worker имеют `on-failure:5`, хранилища — `unless-stopped`, init — без
автоматического перезапуска. Статус Docker `unhealthy` сам по себе не вызывает
рестарт. Временный сбой зависимости делает API неготовым, но не убивает его.
После исчерпания рестартов worker устраните причину и перезапустите его вручную.

Не используйте `down -v` для клиентского deployment: команда удалит данные.
Изменение `POSTGRES_PASSWORD` в env **не меняет пароль существующей роли** в
уже созданном volume. Не заменяйте credentials без согласованной смены пароля в БД.

## Health и завершение

- `/live` и `/api/v1/health/live`: лёгкая проверка процесса, `200`.
- `/ready` и `/api/v1/health/ready`: PostgreSQL, Redis, TOML и активный профиль
  с соответствующим slug и мастером; `200` либо обезличенный `503`.
- Telegram API не участвует в health. Init/API/пустой worker могут стартовать
  без доступа к Telegram. Действительность токена проверяется при обращении к API Telegram.

Readiness имеет общий deadline 3 с для сетевых проверок; Docker healthcheck
использует `/ready`. Приложение обнаруживает неверный TOML при старте и при
проверках готовности. Нет зависимости от персональных имён, изменённых в кабинете.

SIGTERM/SIGINT закрывает Telegram sessions, FSM/Redis и DB engine. Worker
заканчивает текущую отправку, сохраняет результат, возвращает оставшуюся
часть захваченной пачки в pending без расходования попыток и выходит. Compose
даёт API 90 с, worker 120 с. При SIGKILL или аварии остаётся восстановление
processing-заданий через 5 минут. Подтверждение Telegram и commit в БД не
атомарны: при аварии между ними возможен повтор уведомления; exactly-once не обещается.

## Пулы и логи

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `DB_POOL_SIZE` | 3 | Постоянные соединения на процесс |
| `DB_MAX_OVERFLOW` | 2 | Временные дополнительные соединения |
| `DB_POOL_TIMEOUT` | 5 | Ожидание свободного соединения, секунды |
| `DB_POOL_RECYCLE` | 1800 | Возраст соединения перед переоткрытием, секунды |

Для API + worker верхняя граница — 10 соединений, init использует NullPool.
`pool_pre_ping` сохранён; соединение ограничено 5 с, DB-команда — 30 с.
SQLAlchemy не выводит значения SQL-параметров в исключениях.

Development выводит читаемые логи, production — JSON: timestamp, level, logger,
message, specialist_slug. Маскируются токен, webhook secret, пароли, URL и
Authorization. Traceback в production содержит тип исключения и позиции кода,
без сообщения исключения, исходных строк и локальных переменных с данными клиентов.
HTTP access/debug SQL/HTTP payload logs в production отключены. Неожиданные
HTTP ошибки дают `500 {"detail":"Internal server error"}`, намеренные
`HTTPException` сохраняют status/detail.

Подробности heartbeat, worker-health, retry и webhook idempotency:
[Phase 2](phase2-reliability.md).

## Границы текущей реализации

Публичный HTTPS-вход ещё не поставляется; до его подготовки Telegram webhook
недоступен из интернета. Также здесь нет deployment manager, backups, CI/CD,
массовых обновлений и централизованного monitoring. Локальный worker heartbeat
и идемпотентность webhook реализованы в Phase 2.
Не выставляйте API непосредственно в интернет вместо завершения следующей фазы.
