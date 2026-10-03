# Публичный HTTPS и Telegram webhook

Модель: один deployment, один специалист, один бот. Создание клиента остаётся
приватным, даже если введён домен. Домен можно указать позднее:

```sh
bookingctl --root /opt/booking/clients create anna --image booking-bot:VERSION
bookingctl --root /opt/booking/clients domain-check anna
bookingctl --root /opt/booking/clients expose anna --domain anna.booking.example.ru
bookingctl --root /opt/booking/clients doctor anna
```

Если домен был введён при `create`, у `expose anna` флаг `--domain` не нужен.
`expose` проверяет hostname и конфликт с другими клиентами в реестре,
сравнивает A/AAAA с IP VPS, убеждается, что Traefik и сеть работают. Затем
меняет Compose **только этого клиента**: у API удаляется loopback binding,
добавляется внешняя `booking-proxy` network и Docker labels с уникальным
именем router/service на основе случайного Compose project ID.
Маршрут `Host(...)` ведёт к порту API 8000. PostgreSQL/Redis остаются на
внутренней сети. Запускается только API этого клиента; другие deployments
не пересоздаются.

После подключения `expose` ждёт доверенный TLS (в production), `GET /live`
и `GET /ready` через `https://<domain>`. Эти же проверки нужны для
`/api/v1/health/live` и `/api/v1/health/ready` в ручном smoke test.
Затем запускается `booking-admin set-webhook`: Telegram получает URL
`https://<domain>/api/v1/webhooks/telegram` и существующий секретный header.
Команда вызывает `getWebhookInfo` и требует точного совпадения URL. Ни IP,
ни HTTP, ни публичный API-порт не используются. Повторная регистрация:

```sh
bookingctl --root /opt/booking/clients webhook set anna
```

Если DNS или proxy не готовы, приватный deployment остаётся работающим.
Если HTTPS не поднялся, Compose API возвращается к предыдущему варианту;
остальные клиенты не затрагиваются. Если Telegram отказал после готового HTTPS,
публичный API остаётся доступным: устраните проблему Telegram и повторите
`webhook set`. Секреты не передаются в Docker command line.

Неверный DNS: проверьте A/AAAA, отсутствие старой AAAA, TTL и IP в `proxy init`.
TLS failure: убедитесь, что 80/443 открыты, домен публично доступен и
Let's Encrypt не достиг rate limit; смотрите `proxy logs`.
Staging certificate ожидаемо не доверен клиентам; `expose` в staging проверяет
TLS транспорт без цепочки доверия и не устанавливает webhook.
Для production выполните `proxy mode --production`, дождитесь сертификата,
затем `webhook set`.

Ручной smoke test на VPS с **двумя реальными доменами и двумя тестовыми ботами**:

```sh
curl -fsS https://anna.booking.example.ru/live
curl -fsS https://anna.booking.example.ru/ready
curl -fsS https://ivan.booking.example.ru/live
curl -fsS https://ivan.booking.example.ru/ready
curl -i https://anna.booking.example.ru/docs
curl -i https://ivan.booking.example.ru/openapi.json
bookingctl --root /opt/booking/clients doctor anna
bookingctl --root /opt/booking/clients doctor ivan
docker ps --format '{{.Names}} {{.Ports}}'
```

Проверьте 404 для `/docs`, `/redoc`, `/openapi.json` и 200 для health.
Отправьте каждому **своему** боту тестовое сообщение; убедитесь, что
запись/ответ видны только в соответствующем deployment. Проверка чужим
секретом webhook должна быть отвергнута API. Реальную доставку Telegram,
сертификат и изоляцию данных на двух публичных доменах нельзя подтвердить
без DNS, доступного VPS и двух действующих bot tokens.

Локальный Docker-эквивалент для двух тестовых deployments проверяет Host routes,
`/ready`, раздельные webhook secrets, записи в отдельных PostgreSQL и
восстановление маршрутов после restart Traefik. Он использует HTTP на loopback
и **не** проверяет Let's Encrypt или Telegram API:

```sh
python tests/smoke_deployments.py --image booking-bot:VERSION --root tmp/phase4-smoke
python tests/smoke_proxy.py --root tmp/phase4-smoke
```

При исчерпанном Docker address pool можно задать свободную подсеть
`--subnet 10.251.0.16/28` для временной proxy network; подсеть выбирайте
только после проверки пересечений с действующими сетями/VPN.
