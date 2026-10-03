# Архитектура Booking_bot

Один deployment = один специалист = один Telegram-бот. На клиента постоянно
работают четыре контейнера: FastAPI API, notification worker, PostgreSQL 17,
Redis 7.4. Одноразовый admin выполняет миграции, настройку и диагностику.
На сервер общие два постоянных контейнера: Traefik и ограниченный Docker API
socket proxy; init-acme запускается однократно. Monitoring работает на host
через bookingctl и systemd timer, дополнительных постоянных контейнеров нет.

Telegram → HTTPS Traefik → API → бизнес-сервисы → PostgreSQL. Redis хранит
FSM, временные holds, leases и worker heartbeat. Записи, история, durable
webhook receipts, notification jobs и retry audit находятся в PostgreSQL.
Worker читает jobs с row locks/claim ownership и ограниченными попытками.
При потере Redis исчезают временные состояния; подтверждённые записи и
постоянная очередь сохраняются. Повторная доставка Telegram защищена
PostgreSQL receipt от повторной бизнес-операции.

Каждый клиент получает отдельные Docker project, data/egress networks,
DB/Redis volumes, пароли, Telegram token/header secret и specialist TOML.
Data network internal; PostgreSQL/Redis доступны только своему клиенту.
В общей proxy network находится только опубликованный API; worker туда не
подключён. Клиентские процессы не получают Docker socket или сеть socket proxy.
Loopback API до публикации предназначен для обслуживания; production ports —
SSH оператора, 80 и 443.

API/worker работают под UID/GID 10001 с read-only filesystem, tmpfs,
cap_drop ALL, no-new-privileges. FastAPI docs/OpenAPI в production отключены;
webhook требует header secret. Публичного metrics endpoint нет.
Traefik получает только нужные read-only API sections через socket proxy.
Container inspect при этом может читать environment: Traefik/socket proxy и
оператор остаются доверенной частью host security boundary.

Registry/backups приватны: Unix directories 0700, secret files 0600;
Windows использует private ACL. TOML mount доступен UID 10001, а родительская
host directory приватна. Docker group/socket дают полный контроль над host;
клиентские пользователи такого доступа не получают.

Update/restore сериализуются locks и пишут journal. Confirmed version меняется
после миграций и проверок. Во время update/restore API/worker выбранного клиента
остановлены: public proxy может вернуть 502/503, Telegram повторяет update.
Другие клиенты продолжают работу.

См. [deployment](production-deployment.md), [backup](backup-restore.md),
[update](updates.md), [monitoring](monitoring.md), [итоговый аудит](production-report.md).
