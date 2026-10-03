# Клиентские установки: Phase 3A/3B

Для нового RC v1 основной маршрут — [new-client.md](new-client.md).
Примеры локальных Phase 3 tags и сведения о прежнем CI ниже исторические;
актуальные versioning/storage/release gates — в [releases.md](releases.md)
и [release-readiness-v1.md](release-readiness-v1.md).

Публичная эксплуатация нескольких клиентов описана в
[reverse-proxy.md](reverse-proxy.md) и [https-webhook.md](https-webhook.md).
Разделы ниже фиксируют приватный Phase 3 workflow до команды `expose`.

Один deployment — один специалист и один Telegram-бот. Исходники и image общие;
PostgreSQL, Redis, API, worker, сети, volumes и конфигурация отдельные. Реестр
`bookingctl` локальный, без server daemon или общей клиентской БД.

## Подготовка

Нужны Python 3.12+, Docker Engine с Linux containers и Docker Compose v2 с
поддержкой `up --wait` (рекомендуется 2.24.4+). Docker Desktop поддерживается.
Все команды одного Docker-хоста выполняйте одним оператором с **одним реестром**.
Удалённый Docker daemon и запуск нескольких операторов с разными реестрами
для одного набора ботов не поддерживаются.

```sh
python -m pip install -e '.[dev]'
docker build --build-arg APP_VERSION=0.3.0-phase3b-r2 \
  --build-arg VCS_REF=YOUR_COMMIT -t booking-bot:0.3.0-phase3b-r2 .
```

В PowerShell выполните build одной строкой. Образ содержит приложение, CLI и
Alembic, не содержит клиентские файлы. `.dockerignore` исключает `.env`,
`specialist.toml`, `deployments/`, `tmp/` и `output/`. Собирайте образ один раз
и используйте одну release-версию для всех создаваемых клиентов. Новый build
получает новый tag: не переиспользуйте опубликованные release-теги.

`bookingctl create` требует явный tag или `repository@sha256:...`; `latest`,
`stable`, `main`, `master`, `dev` и ссылки без версии отклоняются. Локальный
image используется сразу, отсутствующий скачивается. После получения ссылка
разрешается в локальный `sha256` image ID: именно он сохраняется в `.env` и
используется Compose с `pull_policy: never`. Даже переназначение исходного тега
не меняет уже созданного клиента. PostgreSQL 17 и Redis 7.4 также фиксируются
по ID доступных на момент создания образов.

Для будущего GHCR достаточно публиковать этот же image вне bookingctl и передать:

```sh
bookingctl create anna --image ghcr.io/polen452294/booking_bot@sha256:YOUR_DIGEST
```

Авторизацию приватного registry выполните заранее через `docker login`.
CI/CD, публикация в GHCR и массовые обновления здесь не реализованы. Source build
пока не является побитово воспроизводимым: Docker base tags и часть Python
зависимостей используют диапазоны версий. Изоляция release обеспечивается
зафиксированным готовым image ID, а не повторной сборкой по тому же tag.

## Создание

```sh
bookingctl create anna --image booking-bot:0.3.0-phase3b-r2 --config specialist.toml
bookingctl create maria --image booking-bot:0.3.0-phase3b-r2 --config specialist.toml
```

Команда спрашивает название бизнеса, имя и специализацию специалиста, timezone,
валюту, адрес, необязательный домен и Telegram token. Токен вводится без отображения;
его нельзя передать аргументом командной строки. Нужен интерактивный терминал.
`getMe` проверяет реальную идентичность бота. Недоступный Telegram или отвергнутый
токен останавливают создание до записи клиентских файлов. Проверка не меняет
webhook и не отправляет сообщения.

Slug: 1–40 символов, начинается с латинской строчной буквы, далее строчные
буквы/цифры и одиночные внутренние дефисы; `anna-tattoo` допустим. Пути,
underscore, uppercase и зарезервированные Windows имена запрещены. Один bot ID
нельзя зарегистрировать дважды в одном реестре, в том числе после ошибки создания.

Основа — существующий `specialist.toml`: сохраняются услуги, цены, график,
тексты и кнопки; заменяются профиль и адрес, bio соответствует специализации.
**Перед созданием подготовьте услуги, цены и тексты для этого специалиста**:
пример репозитория содержит услуги тату-мастера, они не переводятся автоматически
в другую специализацию. Timezone проверяется по IANA, валюта — три uppercase
буквы; это проверка формата, не каталог поддерживаемых денежных единиц.

По умолчанию приватный реестр находится в `~/.booking-bot/deployments`:

```text
deployments/
  .lock
  anna/
    .env
    specialist.toml
    compose.yaml
    state.json
    master-invite.txt
    last-error.log       # только после ошибки Docker
```

Другой путь задаётся **перед подкомандой**:
`bookingctl --root /srv/booking/deployments create anna --image ...`.
В следующих командах используйте тот же root. Не размещайте реестр в Git или
публичной/синхронизируемой папке; произвольные дополнительные папки внутри
реестра не поддерживаются. Не изменяйте вручную project, image ID, bot ID и
пароли существующей БД. Это не механизм ротации credentials или обновления image.

Порядок создания:

1. Валидация ввода, Docker/Compose, `getMe`, уникальности бота и разрешение image ID.
2. Запись `CREATING`, файлов и независимых случайных DB/Redis/webhook secrets.
3. Запуск и readiness PostgreSQL/Redis; остановка API/worker при восстановлении.
4. Один admin container: `alembic upgrade head`, затем `booking-admin configure`.
5. Запуск API и worker; ожидание здоровья всех четырёх сервисов.
6. Создание настоящего master invite в БД, запись ссылки в приватный файл; `READY`.

Master invite живёт 24 часа и не выводится в обычный stdout/status/logs. Его
файл — credential владельца; открывайте локально. Telegram username передаётся
в `create-master-invite --bot-username` из уже проверенного getMe, без второго
сетевого вызова. Ссылка не работает как onboarding до подключения Telegram
доставки updates в Phase 4; после истечения срока понадобится новый invite
через существующую административную команду. Повторный `create` его не обновляет.

## Состояния и ошибки

- `CREATING`: создание идёт или процесс прерван; `stage` указывает последний этап.
- `READY`: начальная подготовка завершена. Это не постоянный мониторинг здоровья.
- `FAILED`: сохранены файлы, пароли, volumes и этап сбоя. Данные не удаляются.

```sh
bookingctl status anna
bookingctl logs anna --tail 100
bookingctl logs anna --service api --tail 100
bookingctl create anna --resume
```

Повторный `create` для `READY` — no-op, без запросов и перегенерации конфигурации.
Для `FAILED/CREATING` нужен `--resume`; новый ввод и новый image не применяются.
Восстановление использует сохранённые настройки, повторяет idempotent Alembic и
configure, сохраняет график владельца. Сначала исправьте причину: неверный TOML,
недоступный image, Docker, ресурсы хоста или миграцию. Ошибка Docker сохраняется
в `last-error.log` с маскированием credentials; `logs` добавляет её как историческую.
Не запускайте `docker compose config` без `--quiet`: полный вывод содержит secrets.

Ошибка до создания клиентской папки не оставляет фиктивный `FAILED` deployment.
Для новых установок Phase 3B до генерации файлов сохраняется закрытый
`creation.json` с первоначальными значениями. При прерывании записи файлов
`create SLUG --resume` дописывает отсутствующие файлы с теми же паролями;
существующие отличающиеся файлы не перезаписываются. Установка Phase 3A без
manifest на этапе `files` требует ручной проверки сохранённых файлов. При
недостатке места даже запись `FAILED` может не состояться; предыдущий `CREATING`
показывает последний успешно сохранённый этап. Это fail-safe, без удаления данных.

Операции изменения сериализованы OS file lock, освобождаемым при завершении
процесса. Зависшая или оставшаяся после убийства CLI admin-команда блокирует
повторный provisioning: дождитесь завершения и проверьте контейнер. Bookingctl
не убивает выполняющуюся миграцию автоматически. Если процесс умер между
commit invite и записью файла, повтор создаст ещё один invite с обычным TTL;
данные клиента не сбрасываются. Если файл invite уже существует, он сохраняется.

## Управление и сети

```sh
bookingctl list
bookingctl status anna
bookingctl start anna
bookingctl stop anna
bookingctl restart anna
bookingctl logs anna --service worker
```

`list` читает локальные состояния без Docker. `status` отдельно показывает
состояние provisioning и фактические состояния/health/порты Docker; exit 0
только для `READY` и четырёх healthy-сервисов, иначе 1. При остановке статус
подготовки остаётся READY, runtime показывает exited. `stop` останавливает
worker/API до хранилищ; `start/restart` ждут health, не выполняют миграции и
configure повторно. Политики restart контейнеров не являются monitoring.

У каждого project отдельная `data` network с `internal: true`; PostgreSQL и
Redis подключены только к ней и **не имеют published ports**. API/worker/admin
дополнительно имеют собственную egress network для Telegram. API публикуется
только на `127.0.0.1` с автоматически назначенным Docker свободным портом:
конфликта при параллельных клиентах нет. Порт виден в `status` и может измениться
после пересоздания контейнера. Внутри project адрес API — `http://api:8000`.

```sh
curl http://127.0.0.1:PORT/ready
```

Production `TELEGRAM_WEBHOOK_MODE=internal` разрешает отсутствие публичного URL,
сохраняет требования к DB/Redis/token/webhook secret и проверку secret на endpoint.
Default mode по-прежнему `public` с обязательным HTTPS URL. `set-webhook` запрещён
в internal mode. При `create` домен сохраняется в state, но DNS, reverse proxy,
сертификаты и регистрация webhook не выполняются до `bookingctl expose`.
Существующий внешний webhook у переданного бота также не удаляется автоматически:
используйте отдельного, свободного бота для нового клиента.

`bookingctl expose` подключает к общей proxy network **только API** и убирает
его loopback binding; PostgreSQL/Redis остаются на data. См.
[reverse-proxy.md](reverse-proxy.md).

## Секреты и права

Secrets генерируются `secrets.token_urlsafe(36)` независимо для каждого назначения.
Они находятся в `.env`; в compose сохраняются только placeholders. В аргументы
Docker credentials не передаются. Compose получает явно выбранные файлы и project;
чужие `COMPOSE_*` и клиентские env vars исключаются из окружения запуска.

POSIX: root/client directories 0700, секретные файлы 0600. Только `specialist.toml`
имеет 0644 **внутри закрытой 0700 папки**, чтобы единственный read-only bind mount
читался runtime UID 10001. Windows: закрытый ACL с полным доступом текущему
оператору, наследуемый файлами. Права других пользователей удаляются у папки
реестра; поэтому передавайте только специально созданную для bookingctl папку.
Docker/root/администратор хоста по определению имеют доступ к container env;
это разделение клиентов, не защита от администратора Docker.

## Проверки

```sh
python -m ruff check .
python -m pytest
# Только отдельные тестовые PostgreSQL/Redis с применёнными миграциями:
python -m pytest -m integration
python tests/smoke_deployments.py --image booking-bot:0.3.0-phase3b-r2 --root tmp/phase3-smoke
```

Smoke требует новый root. Реальные Docker, PostgreSQL/Redis, миграции, configure,
invite, API и worker; подменён только транспорт getMe с двумя искусственными
токенами. Проверяются реальная ошибка Alembic и resume, два healthy project,
раздельные БД/Redis/volumes/сети/контейнеры, одинаковый image ID, localhost ports,
маскирование secrets, повторный create и независимые stop/start/restart.
Файлы и volumes сохраняются, в том числе при ошибке. Тест не доказывает реальную
доставку Telegram и публичный HTTPS. После проверки тестовые установки можно
остановить `bookingctl --root tmp/phase3-smoke stop smoke-alice` и аналогично bob.

Backup/restore, mass updates, CI/CD, monitoring, web-панель и новый
booking-функционал не входят в Phase 3A/3B. HTTPS workflow описан отдельно.

## Phase 3B: диагностика и обслуживание

Соберите новый образ с отдельным tag, например `booking-bot:0.3.0-phase3b-r2`.
Команды configure и полная runtime-диагностика требуют такого образа: старые
зафиксированные images Phase 3A не получают код автоматически. Автоматическое
обновление существующих клиентских images не реализовано. Обычные команды
start/stop/status/logs продолжают работать с прежними установками.

```sh
bookingctl doctor anna
bookingctl logs anna --service worker --tail 100
```

`doctor` показывает readable checks (JSON через --json) и exit 1 при ERROR/CRITICAL
или deferred checks. WARNING видны и не блокируют приёмку. Проверяет state/TOML/.env, digest
применённого TOML, приватные права, Docker, четыре контейнера, SQL `SELECT 1`,
Redis PING с auth, API `/ready`, worker heartbeat. Кратковременный admin-контейнер
дополнительно проверяет DB/Redis с credentials приложения, текущую Alembic revision
против heads из **установленного образа**, профиль в БД и Python package version.
Отдельно сравниваются pinned image ID у API/worker, OCI release version,
loopback-порты, private data network и digest TOML внутри работающего API.

Остановленная установка диагностируется без её запуска. Если storage уже работает
и config корректен, doctor может запустить только одноразовый admin probe
`run --rm --no-deps`; отдельные host checks вызывают Telegram getMe/webhook.
При active lock checks откладываются; configure storage probe пропускается.
Периодический monitoring запускает одноразовые checks через systemd timer;
см. [monitoring.md](monitoring.md).
Битая папка в registry отображается в `list` как FAILED/state; она не делает
остальные строки списка недоступными. Новый create блокируется до восстановления
нечитаемой записи, чтобы не допустить повторной регистрации одного bot ID.

Ресурсы в `resources`: Docker MemUsage, CPU%, PIDs для работающих контейнеров,
а также отдельное число процессов `Processes` из `docker top -eo pid,comm`.
Это моментальный снимок; PIDs включает процессы/потоки по учёту Docker.
На клиента постоянно работают четыре контейнера, временный admin удаляется
после операции. Показания не являются нагрузочным тестом или гарантией ёмкости.

## Безопасное изменение профиля

1. Скопируйте **установленный** `specialist.toml` в отдельный `candidate.toml`.
2. Измените бренд, специализацию/bio, timezone, locale, адрес, тексты или кнопки.
3. Выполните `bookingctl configure anna --config candidate.toml`.
4. Проверьте `bookingctl doctor anna` и readiness.

Услуги, цены и график продолжайте изменять через кабинет Telegram. `configure`
проверяет, что секции services/schedule кандидата совпадают с установленным TOML,
и **не пишет их в БД вообще**, в том числе строки без owner-managed marker.
Удалённый владельцем весь график остаётся пустым при повторном первоначальном
configure/resume тоже. Изменение slug, token/image/паролей этим механизмом
не поддерживается. Смена currency отклоняется, поскольку потребовала бы отдельной
миграции цен. Имя и bio уже привязанного Telegram-владельца сохраняются по
существующему правилу configure; чтобы изменить их, используйте кабинет.
Timezone меняет интерпретацию локальных часов расписания: существующие абсолютные
времена записей не переносятся. Перед такой сменой учитывайте записи клиента.

Новый TOML полностью валидируется до остановки приложения. Записывается закрытый
`configure-journal.json`, затем API/worker останавливаются. После завершения их
активных транзакций снимается snapshot **только изменяемых полей профиля/адреса**
из реальной БД; он может отличаться от старого TOML. Новый файл заменяется атомарно,
профиль меняется одной DB-транзакцией. API/worker пересоздаются, чтобы single-file
bind mount увидел новый inode, и проверяется health. Это короткое обслуживание
одного клиента. Остальные клиенты продолжают работать.

При ошибке применяется прежний TOML и точный snapshot этих полей БД, services,
график и записи не затрагиваются. Если исходно установка полностью остановлена,
storage запускается только на время применения; API/worker остаются остановлены,
storage затем тоже останавливается. Частично работающая или unhealthy установка
требует сначала `doctor` и восстановления через `start`.

Внезапное завершение между filesystem/DB/Docker шагами не является общей
транзакцией этих систем. Durable journal и CREATING/FAILED позволяют продолжить
откат после устранения причины:

```sh
bookingctl configure anna --resume
```

Эта команда восстанавливает предыдущую рабочую версию незавершённого изменения;
если commit операции уже отмечен в журнале, завершает запись итогового состояния.
`create --resume` откажется обрабатывать незавершённый configure. При оставшемся
активном admin-контейнере сначала дождитесь его завершения. Journal не является
backup системы: он содержит только один последний конфигурационный переход.
Не редактируйте установленный TOML непосредственно: digest mismatch блокирует
start/configure. Верните прежний файл и примените изменения через candidate.

| Симптом | Действие |
| --- | --- |
| Deployment does not exist | Проверьте slug и тот же `--root`, затем `list` |
| FAILED/migration | `doctor`, `logs`; устраните причину и `create SLUG --resume` |
| FAILED/configure_recovery | Восстановите Docker/DB, `configure SLUG --resume` |
| Контейнеры stopped/missing | Для READY выполните `start`, затем `doctor` |
| Worker heartbeat failed | `logs --service worker`, проверьте DB/Redis, затем restart |
| Migrations mismatch | Сверьте image/revision; start и configure миграции не выполняют |
| Permissions failed | Восстановите 0700/0600 или закрытый Windows ACL в выделенном registry |
| Mounted config mismatch | Проверьте прямое редактирование и завершите configure recovery |
| Docker unavailable | Запустите Docker Linux engine; клиентские данные не удаляйте |

Протоколы: [Telegram getMe](https://core.telegram.org/bots/api#getme),
[Docker Compose services](https://docs.docker.com/reference/compose-file/services/),
[Docker networks](https://docs.docker.com/compose/how-tos/networking/).
