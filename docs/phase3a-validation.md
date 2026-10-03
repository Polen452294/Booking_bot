# Phase 3A: реализация и проверка

Дата: 25 сентября 2026. Локальная ветка `master`, HEAD `4e570a6`.
Незакоммиченные изменения Phase 1/2 сохранены; reset, checkout старого master,
merge, commit и push не выполнялись. Проверка перед изменениями: Ruff успешно,
116 unit-тестов. Существующая Alembic head `b62f3d910ea4` сохранена.

## Изменения

- Добавлен host CLI `bookingctl`: create/list/status/start/stop/restart/logs.
- Deployment manager отделён от runtime singletons приложения. Использует
  существующие specialist TOML, configure, Alembic и master invite service.
- Один image с явной версией для клиентов, фиксация по immutable image ID;
  поддерживаются GHCR references/digests без реализации публикации/CI/CD.
- Отдельные project, config, DB/Redis, API/worker, volumes и сети. Нет
  published storage ports; API — автоматически выделяемый loopback port.
- Private registry, случайные пароли, маскирование диагностики, OS lock,
  атомарная замена файлов состояния, CREATING/READY/FAILED и explicit resume.
- Public production по умолчанию продолжает требовать HTTPS; явный internal
  mode разрешает локальный запуск и запрещает регистрацию webhook.
- README и инструкция client-deployment описывают создание, диагностику,
  восстановление и ограничения.

## Выполненные проверки

| Проверка | Результат |
| --- | --- |
| `ruff check .` | успешно |
| Ruff format новых deployment модулей и тестов | успешно |
| `pytest` | **157 passed, 43 integration deselected** |
| `pytest -m integration` | **43 passed**, отдельные PostgreSQL 17 / Redis 7.4 |
| Alembic на пустой integration DB | полная цепочка до `b62f3d910ea4` |
| Versioned Docker build | успешно, runtime smoke image `booking-bot:0.3.0-phase3a-r1` |
| Установка console script / `bookingctl --help` | успешно |
| Docker smoke двух клиентов | успешно, два прогона; финальный включает межсетевые проверки |
| Windows `.env` ACL | только текущий оператор, наследуемый FullControl |
| POSIX permissions / OS lock | отдельный Linux container: 0700/0600, config 0644 под 0700, второй lock отклонён |
| `git diff --check` | успешно |

Unit покрывают неправильные slug/image/domain, недоступный Docker,
неправильный token/getMe identity, неверный TOML/timezone, повторный bot ID,
повторный slug, отказ и resume миграции, env isolation, маскирование secrets,
зависший admin container, runtime commands и unhealthy status exit code.
Регрессионный тест подтверждает запрет set-webhook в internal mode.

Smoke использует настоящие Docker, Alembic, PostgreSQL, Redis, configure,
master invites, API и workers. Только HTTP getMe заменён искусственными
ответами для двух вымышленных токенов. Каждый клиент содержит свой профиль и
один invite; все четыре контейнера healthy, приложения работают под UID 10001.
Одинаковый image ID подтверждён через Docker inspect; контейнеры, сети и volumes
не пересекаются. Redis marker клиента A отсутствует у B и сохраняется после
перезапуска A. Соединения A→B и B→A к PostgreSQL/Redis по IP блокируются.
Остановка A не нарушает readiness и heartbeat B. Повторное создание не меняет
файлы или invite. Намеренная реальная ошибка Alembic приводит к FAILED/migration;
resume успешно продолжает создание с теми же secrets. В terminal/logs/state
исходные значения credentials не обнаружены тестовыми assertions.

## Сохранённые тестовые данные и ограничения

Финальный smoke registry: `tmp/phase3-smoke-final`, клиенты `smoke-alice` и
`smoke-bob`; журнал `tmp/phase3-smoke-final.log`. Предварительный прогон —
`tmp/phase3-smoke-a`. Integration log: `tmp/phase3-integration/result.log`.
Эти пути игнорируются Git. После проверок все созданные для этой задачи
тестовые стеки **остановлены**, конфигурация и volumes сохранены.

Реальные BotFather токены, доставка Telegram updates, публичный HTTPS и
сертификаты не проверялись. READY означает готовность локальной установки,
а не доступность бота из Telegram. Invite имеет обычный TTL 24 часа.
Один оператор/реестр на Docker-хост; root/Docker administrator остаётся
доверенной стороной. Полный cold build пока использует некоторые dependency
version ranges; развёрнутые клиенты фиксируются на готовом image ID.

При аварии непосредственно во время генерации файлов нужна ручная проверка
сохранённой папки. Сбой после commit invite до записи файла может оставить
дополнительный неиспользованный invite с обычным TTL. Автоматического удаления
данных нет. Подробная эксплуатационная инструкция: [client-deployment.md](client-deployment.md).

Backup/restore, mass updates, CI/CD, HTTPS automation, monitoring, web-панель,
новые booking-сценарии и Phase 3B не реализовывались.
