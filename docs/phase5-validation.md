# Phase 5: результаты проверки, 1 октября 2026

Работа выполнена поверх текущих незакоммиченных Phase 1–4 в `master`.
Состояние GitHub master не использовалось для сброса файлов.

## Автоматические проверки

- Baseline Ruff и обычные тесты прошли.
- Начальный integration-запуск выявил отсутствие локальных PostgreSQL/Redis.
  Созданы отдельные `booking-phase5-tests-postgres` / `booking-phase5-tests-redis`;
  миграции применены, повторный baseline integration прошёл.
- Финальный `ruff check .`: passed.
- Финальный `pytest`: **217 passed**, 46 integration deselected.
- Последний `pytest -m integration` на реальных PostgreSQL/Redis: **46 passed**.
  После этого изменялись только host backup validation и тесты; повторить integration
  после восстановления локального Docker engine.
- `compileall` для deployment-модулей и DR smoke: passed.
- Docker image `booking-bot:0.5.0-phase5-final` успешно собран до последних host validation
  исправлений. Финальная повторная сборка сейчас заблокирована неработающим Docker Desktop.
- Отдельного настроенного type checker в проекте нет.

Integration environment:

```powershell
$env:DATABASE_URL='postgresql+asyncpg://booking:booking@127.0.0.1:55432/booking'
$env:REDIS_URL='redis://127.0.0.1:6379/0'
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\pytest.exe -m integration
```

Credentials выше относятся исключительно к disposable test services.

## Реальный disaster recovery

Тестовый образ: `booking-bot:0.5.0-phase5`.
Registry: `tmp/phase5-dr`; backup root: `tmp/phase5-dr-backups`.
Только новые deployments `dr-test` и `dr-test-b`, синтетические Telegram tokens.

Проверено в обе стороны:

1. Provision реальных API/worker/PostgreSQL/Redis и Alembic single head.
2. Seed услуги, клиента, подтверждённой записи/статусов/истории и schedule exception.
3. Backup обоих клиентов через настоящий `pg_dump -Fc`; checksum и `pg_restore --list`.
4. Отказ wrong-client restore.
5. Удаление PostgreSQL volume только соответствующего тестового project, после проверки
   имени volume и Docker Compose labels.
6. Отказ обычного restore при невозможности safety backup.
7. Explicit disaster restore с dangerous flag и настоящим `pg_restore`.
8. Полное совпадение JSON rows 13 business/schema tables и specialist.toml.
9. `doctor` успешен для восстановленного клиента.
10. Данные и container IDs второго клиента не изменились.
11. Обычный restore создаёт отдельную проверенную `pre-restore-*` копию.

Результат: **DR ACCEPTANCE PASSED**.

Первый запуск столкнулся с исчерпанным автоматическим Docker address pool, оставшимся
от предыдущих smoke tests. Для тестового egress network второго клиента задан свободный
`10.254.5.0/24`; тест продолжен с сохранённым registry. Чужие сети/containers/volumes
не удалялись. Новый DR script поддерживает optional `--network-pool PRIVATE_IPV4/22`
для воспроизводимого запуска при этом ограничении.

Локальное доказательство: `tmp/phase5-dr-resumed.log` содержит:

```text
dr-test: restored exact rows/config, doctor OK, dr-test-b unchanged
dr-test-b: restored exact rows/config, doctor OK, dr-test unchanged
DR ACCEPTANCE PASSED: A/B isolation, volume loss, safety backup, exact business rows
```

Логи и private test registry остаются в ignored `tmp/`, не публикуются в Git.

## Ограничения и оставшаяся проверка

- Финальный повтор DR на последней версии кода пока не выполнен: Docker Desktop
  при запуске падает на служебном сокете `dockerInference`; Linux engine недоступен.
  Штатный `docker desktop restart` не восстановил engine. Factory reset не выполнялся.
- Последние host validation изменения покрыты unit-тестами: corrupted TOML, empty doctor,
  сохранение recovery webhook flag при ошибке и повторная регистрация при успешном restore.
- Live Telegram getMe/public DNS/TLS/webhook не проверены: нет выделенного реального
  Telegram bot/domain. GetMe при тестовом provisioning подменён, остальные DR операции реальные.
- S3 adapter upload/read-back/pull/corruption/private/SSE покрыт unit-тестами; реальный
  private bucket/credentials не предоставлены. Providers без PublicAccessBlock API
  отклоняются. Client-side encryption не реализовано; обязательна SSE.
- Systemd unit generation/time/path escaping проверены unit-тестами; live timer install
  требует Linux host, текущая host OS — Windows.
- Retention сохраняет safety backups; их ручная очистка и remote lifecycle — обязанности оператора.
- Restore требует точного app image, PostgreSQL 17 и совпадающей Alembic head.
  Forward upgrade старого backup под новым image относится к будущей update policy.

## Решение о переходе

Основной критерий восстановления после потери клиентского PostgreSQL volume доказан
реальным тестом для двух deployments. Перед массовыми production обновлениями требуется
восстановить Docker engine и повторить сборку/DR на окончательном коде; проверить выбранные
production Telegram/domain, systemd и off-host storage. **Phase 6 не начата.**
