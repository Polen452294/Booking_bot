# Shared reverse proxy (Phase 4)

Phase 7 использует дополнительный versioned socket-proxy image (см.
[production-deployment.md](production-deployment.md)). Traefik больше не
монтирует Docker socket: read-only API sections доступны через приватную
сеть booking-proxy_docker-api. Socket proxy не имеет host port или подключения
к client proxy network; POST/EXEC/IMAGES/VOLUMES отключены. Ограничения основаны
на [upstream Docker socket proxy](https://github.com/Tecnativa/docker-socket-proxy).
Оба постоянных proxy services используют Docker log rotation 10m × 3.

`bookingctl` использует один реестр клиентов (`--root`) на Docker-хосте. Рядом с
ним создаётся `infra/proxy`: для стандартного `~/.booking-bot/deployments` это
`~/.booking-bot/infra/proxy`. Для `/opt/booking/clients` получится
`/opt/booking/infra/proxy`. Не создавайте два независимых реестра на одном VPS.

Проверена версия Traefik `v3.7.13`. Отдельный Compose project `booking-proxy`
публикует только `80:80` и `443:443`, использует Docker provider с
`exposedByDefault=false` и перенаправляет HTTP на HTTPS. Dashboard наружу не
публикуется. Сеть `booking-proxy` создаётся один раз независимо от клиентов.
Только Traefik и публичные API подключены к ней. У каждого клиента своя Compose
`data` network с `internal: true` для PostgreSQL/Redis/API/worker и своя `egress`
network для приложения. PostgreSQL, Redis и worker не имеют host bindings.
Приватный API имеет только динамический loopback binding; после `expose` он
удаляется.

```sh
bookingctl --root /opt/booking/clients proxy init \
  --email ops@example.ru --server-ipv4 203.0.113.10
bookingctl --root /opt/booking/clients proxy start
bookingctl --root /opt/booking/clients proxy status
bookingctl --root /opt/booking/clients proxy logs
bookingctl --root /opt/booking/clients proxy restart
```

`--server-ipv4` и необязательный `--server-ipv6` задают **публичные адреса VPS**.
Автоматическое определение адреса может ошибиться за NAT, поэтому IP вводится
оператором. Перед `expose` все опубликованные A/AAAA записи должны указывать
только на заданные адреса. Если IPv6 не используется, не создавайте AAAA запись.
Проверка: `bookingctl --root /opt/booking/clients domain-check anna`.

Traefik выпускает Let's Encrypt certificate через HTTP-01 на порту 80.
ACME state хранится в постоянном Docker volume `booking-proxy_letsencrypt`.
Одноразовый `init-acme` создаёт `acme-production.json` и `acme-staging.json`
с правами 0600 внутри volume. Эти файлы разделены и сохраняются при restart.
`.env` не входит в Git или Docker image; ACME private material остаётся в volume.
Перед запуском убедитесь, что домен резолвится на VPS, порты
80/443 доступны извне и другая служба не занимает их.

Для тестирования лимитов Let's Encrypt:

```sh
bookingctl --root /opt/booking/clients proxy init \
  --email ops@example.ru --server-ipv4 203.0.113.10 --staging
# Позже, после проверки маршрутизации:
bookingctl --root /opt/booking/clients proxy mode --production
```

`mode` пересоздаёт только Traefik. Staging certificate не доверен Telegram;
`expose` в staging не устанавливает webhook. После перехода на production
дождитесь доверенного сертификата, затем `bookingctl webhook set anna`.
Файлы ACME разных режимов сохраняются отдельно. Не копируйте staging state в
production. `proxy restart` не меняет storage и должен восстановить маршруты
Docker provider и ранее выданные сертификаты.

Docker socket смонтирован read-only **только** в Traefik. Это ограничивает
запись в файл сокета, но само по себе **не ограничивает Docker API**:
компрометация Traefik может дать контроль над Docker-хостом. Запускайте proxy
на выделенном доверенном VPS, ограничьте доступ к Docker оператором; при более
строгой модели угроз поставьте ограничивающий Docker socket proxy. Клиентским
контейнерам сокет не передаётся.

Host firewall: снаружи нужны SSH (обычно 22, не меняйте его автоматически),
80/tcp и 443/tcp. Пример UFW после проверки собственного SSH-порта:

```sh
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw status verbose
```

Правила firewall не применяются `bookingctl`. Docker port publishing может
обходить часть правил UFW; проверьте правила Docker/firewall провайдера и
`docker ps --format '{{.Names}} {{.Ports}}'`. Не публикуйте API, 5432, 6379 или
worker на интерфейсе VPS. Rate limit webhook на proxy не задан: случайный
лимит может заблокировать bursts и повторные доставки Telegram. Подбирайте
его только по наблюдаемому трафику. HSTS не включён для staging.

Диагностика: `proxy logs` для ACME/маршрутов; `domain-check` для DNS;
`doctor SLUG` для сети, HTTPS и Telegram. Ошибка Let's Encrypt не удаляет
клиентские файлы/volumes. При остановке Traefik клиентские контейнеры продолжают
работать. Не используйте `docker compose down -v`.

При ошибке Docker `all predefined address pools have been fully subnetted`
расширьте `default-address-pools` Docker daemon или выделите безопасные
непересекающиеся подсети для Compose на этом VPS; не удаляйте занятые сети
других проектов. У каждого клиента создаются `data` и `egress` сети.
