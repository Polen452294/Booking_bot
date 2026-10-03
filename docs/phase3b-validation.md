# Phase 3B: управление клиентскими установками

Проверка завершения: 1 октября 2026 года (Europe/Moscow).
Локальная ветка `master`, базовый HEAD `4e570a6`. Существующие незакоммиченные
Phase 1/2/3A сохранены. Reset, merge, commit и push не выполнялись.

## Что изменено

- `bookingctl doctor SLUG`: локальная конфигурация/состояние, права, Docker,
  PostgreSQL/Redis, API readiness, worker heartbeat, Alembic revision против
  heads установленного image, Python package и OCI release version, совпадение
  image ID API/worker, network isolation, установленный bind mount и ресурсы.
- `bookingctl configure SLUG --config candidate.toml`: валидация до изменения,
  остановка API/worker одного клиента, снимок изменяемых полей его БД, транзакционное
  применение профиля, атомарная замена TOML, пересоздание bind mounts и healthcheck.
  Ошибки откатывают файл и эти поля БД, без изменения услуг/графика/записей.
- `configure --resume` восстанавливает незавершённую операцию по закрытому журналу.
  Незавершённая операция не отображается как READY. Полностью остановленная
  установка остаётся остановленной после configure.
- Создание сохраняет исходные файлы/секреты в приватном `creation.json`, поэтому
  `create --resume` может завершить прерванную генерацию без смены паролей.
- `list` показывает повреждённые/неполные записи как FAILED/state; `doctor`
  возвращает понятный отчёт для отсутствующих и остановленных установок.
  Проверяются типы полей state; start/configure обнаруживают прямые изменения TOML.
- `logs` сохраняет доступ к исторической диагностике, когда Docker недоступен.
- Обновлены README и [эксплуатационная инструкция](client-deployment.md).

## Найденные и исправленные проблемы

1. В Phase 3A повторный configure мог восстановить пустой график владельца из
   исходного TOML. Теперь первоначальное заполнение зависит от отсутствия
   SpecialistProfile; уже созданный пустой график остаётся пустым. Явный
   административный reset-schedule продолжает работать.
2. Одной замены файла недостаточно: single-file Docker bind mount может держать
   старый inode. Configure пересоздаёт API/worker с новым mount, doctor сравнивает
   digest внутри API с файлом на хосте.
3. Первый smoke Phase 3B выявил неподдерживаемый `create --no-deps`. Для
   остановленного клиента используется `up --no-start --force-recreate --no-deps`.
   Сохранённая неудачная операция восстановлена `configure --resume` без удаления
   файлов, смены паролей или очистки PostgreSQL volumes.
4. Docker Desktop дважды не запускался из-за оставшихся Windows AF_UNIX sockets
   dockerInference / secrets engine. После остановки неисправного Desktop
   служебные каталоги `Docker/run` и `docker-secrets-engine` переименованы в
   соседние `.stale-phase3b-TIMESTAMP`; исходные файлы сохранены. Docker images,
   volumes, settings и данные клиентов не сбрасывались. Проблема окружения
   соответствует [описанию Docker](https://github.com/docker/desktop-feedback/issues/448).

## Проверки

| Проверка | Результат |
| --- | --- |
| Baseline Phase 3A | Ruff, 157 unit, 43 integration успешно |
| Итоговый Ruff | успешно |
| Итоговый unit suite | **166 passed, 46 deselected** |
| Итоговый integration suite | **46 passed, 166 deselected**, отдельные PostgreSQL/Redis |
| Alembic на пустой БД | вся цепочка до `b62f3d910ea4`; новых миграций нет |
| Docker build | `booking-bot:0.3.0-phase3b-r2` успешно |
| Linux permissions | private modes, обнаружение 0644 у secret и exclusive lock успешно |
| Docker smoke двух клиентов | **PASS Phase 3A + PASS Phase 3B**, оба doctor: 18/18 успешно |

DB integration-тесты отдельно проверяют сохранение пустого графика, услуг с
owner-managed marker и без него, применение профиля, точный откат к значениям
БД, отличающимся от прежнего TOML, и rollback всей транзакции при чужом ID snapshot.
Unit-тесты проверяют ошибки Docker/config/token, состояния, file manifest resume,
configure recovery, corrupt state, stopped installations и редактирование services.

## Две установки на одном хосте

Финальный запуск `tests/smoke_deployments.py --image booking-bot:0.3.0-phase3b-r2
--root tmp/phase3b-final-smoke` создал `smoke-alice` и `smoke-bob`. Обе установки
достигли READY/complete, API и workers здоровы. Общий неизменяемый image ID:
`sha256:93a5f7352395f1d4eaadefe14b64c95b1373dc4a1680e0797ebb81096f02581f`.
OCI release — `0.3.0-phase3b-r2`; версия Python package пока `0.1.0`, doctor
показывает оба значения отдельно.

Подтверждены разные TOML, тестовые Telegram tokens, DB/Redis credentials,
PostgreSQL/Redis данные, volumes, networks, процессы и loopback API ports.
Межклиентский доступ к хранилищам проверен сетевыми попытками подключения.
Остановка, restart, применение профиля и ошибки Alice не изменили контейнеры Bob.
Сценарий покрывает повторный slug, плохой token/config, сбой миграции и resume,
откат после уже выполненного изменения БД, недоступность Docker при восстановлении,
ошибочную Alembic revision, остановку Redis/worker и configure остановленного клиента.
Проверены сохранение изменённых владельцем услуг и пустого графика, приватные права
и отсутствие секретов в собранных логах/отчётах. Telegram getMe замокан;
контейнеры, migrations, БД, Redis, API, workers и invites настоящие.

Повторный замер без клиентской нагрузки после smoke:

| Клиент / Compose project | Контейнеры | Память | Постоянные процессы |
| --- | --- | --- | --- |
| smoke-alice / booking-smoke-alice-da5f18cd | 4 | ~383 MiB | 11 |
| smoke-bob / booking-smoke-bob-cd879ea7 | 4 | ~401 MiB | 11 |

Суммарно около 783 MiB (округление по клиентам независимо), без памяти Docker VM.
При healthcheck появляется дополнительный процесс; в итоговом doctor зафиксировано
по 12 процессов. Docker PIDs включает потоки, поэтому это отдельное число.
Worker healthcheck даёт краткий пик около одного CPU core и поднимает память worker
с ~171 MiB до ~287 MiB. Это ограничение текущей проверки, а не оценка CPU под
пользовательской нагрузкой; тяжёлая инфраструктура не добавлялась.

Локальные доказательства (каталог `tmp` исключён из Git):
`tmp/phase3b-final-smoke.log`, `tmp/phase3b-final-smoke-doctor.json`,
`tmp/phase3b-unit-final.log`, `tmp/phase3b-final-verify/result.log`.
Реестр `tmp/phase3b-final-smoke` содержит тестовые секреты и не предназначен для
публикации. После проверки тестовые установки и отдельные integration-хранилища
остановлены; файлы, контейнеры и volumes сохранены для повторного запуска.

## Границы

Для услуг и графика источник истины — Telegram/БД. Их изменения в candidate TOML
отклоняются. Смена валюты также отклоняется, поскольку требует отдельного изменения
цен. Секреты, token и image через configure не меняются. Имя/bio уже привязанного
владельца сохраняются по прежним правилам. Timezone не переносит существующие
абсолютные времена записей.

Журнал конфигурации содержит только поля текущей операции; это не backup/restore
клиентской БД. При потере Docker или аварии процесса между системами восстановление
выполняется явно через resume. Установка Phase 3A со старым image не получает
новые runtime-команды автоматически; массовые/централизованные обновления не входят
в работу. Полная сборка по-прежнему использует отдельные dependency ranges,
а установленные клиенты фиксируются на готовом image ID.

Тесты используют искусственные токены и mocked getMe; реальные Telegram updates,
публичный HTTPS, сертификаты и webhook registration не проверялись. На клиента
остаются четыре постоянных контейнера и временный admin на время операции.
Ресурсы измеряются в простое, это не нагрузочная проверка максимальной ёмкости.

HTTPS, Traefik/Caddy, backup, централизованные обновления, CI/CD и monitoring
не реализованы. Booking-ядро не переписывалось. Phase 4 не выполнялась.
