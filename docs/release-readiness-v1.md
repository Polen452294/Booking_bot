# Release readiness v1 — Phase 8

Дата: **3 октября 2026, Europe/Moscow**. Рекомендация: **NOT READY FOR v1.0.0**.
Кандидат: `1.0.0-rc.1`. Final tag `v1.0.0` не создан; Phase 9 не начата.

Scope: текущая незакоммиченная ветка `master`, реальные локальные Docker deployments,
PostgreSQL/Redis и отдельный свежий Ubuntu 24.04 container. VPS, два настоящих
тестовых Telegram token/domain и off-host credentials не предоставлены.
Поэтому full production qualification не объявляется завершённой. Код и тесты
Phase 1–7.5 присутствуют; прежние внешние acceptance gates этим фактом не закрываются.
Созданные для Phase 8 test deployments остановлены после замеров; их volumes,
backups и evidence сохранены. Предшествующие test services и чужие deployments не затронуты.

## PASSED

| Проверка | Доказательство и границы |
|---|---|
| Baseline | Ruff; 373 unit; 124 integration; одна Alembic head c75a01d29f10 |
| Финальный Windows suite | Ruff; 396 unit; 124 integration на реальных PostgreSQL/Redis |
| Свежий Linux test environment | Ubuntu 24.04: Ruff, 396 unit, 124 integration; без repo `.env`/PYTHONPATH |
| Дополнительные проверки | compileall, pip check, git diff --check, Actionlint (без shellcheck/pyflakes) |
| Docker build | RC application, hardened PostgreSQL/Redis/socket proxy; runtime non-root |
| Versioning | Canonical rc.N; numeric SemVer order; repeated/older RC и stable → RC отклоняются |
| Release pipeline | RC не меняет stable minor alias; prerelease/--latest=false; guard всех четырёх packages |
| Fresh и old DB | Реальный old pre-conversations image → populated DB → RC image; FIXED default/backfill; повторный upgrade |
| Telegram flow harness | Реальный dispatcher + PostgreSQL/Redis, mocked Bot API; FIXED/FROM/NEGOTIABLE и master regressions |
| Negotiation | 15k → 17k; stale 15k rejected; 17k accepted → slot → appointment; price snapshot = 1 700 000 minor units |
| Conversation | Text/photo/document до/после записи; unsupported media, unread, pagination, cancel/close/privacy |
| Concurrency/stale | Double accept, proposal replacement, последняя slot, stale request/proposal/buttons; одно событие/запись |
| Backup/restore | Реальные dump/restore, archive verification, controlled volume loss, safety backup; точные business rows и peer сохранены |
| Реалистичная backup fixture | 154 messages на deployment, photo/document IDs, proposals/history, accepted price, appointment/read pointers, schedule changes |
| Update | rc.1 → synthetic rc.2 через disposable registry, настоящий push/pull и additive migration; doctor и данные сохранены |
| Broken update | Injected validation failure и отдельный rc.3 image с API exit 42: backup, FAILED, peer alive, exact image/DB recovery |
| Restart/outage | API/worker/Redis/PostgreSQL; worker kill/auto-restart; Redis runtime loss; readiness failure/recovery |
| Proxy | Routing/redirect, synthetic TLS, Docker API ACL, изоляция, Traefik outage/restart; public ACME не тестировался |
| Monitoring | Реальные локальные outages/backup failure; captured failure/dedup/recovery alerts; доставка Telegram не тестировалась |
| Ubuntu CLI installation | Штатный bootstrap --prepare-only на новой Ubuntu 24.04; wheel, launcher, version/doctor без PYTHONPATH; два запуска, hashes сохранены |
| Redis final runtime | Два новых synthetic release-test-1/2; PID1 UID 999/caps 0; AOF persistence после restart/recreate; backup/restore A при работе B |
| Logs | stdout+stderr последних 2000 строк каждого выбранного container; raw secrets/fixtures phone/body matches = 0, tracebacks = 0 |

Ubuntu `doctor` в CLI-only environment завершился с exit 1 и явным CRITICAL
для отсутствующих Docker/proxy. Это корректная диагностика неполной установки,
а не успешный production doctor. Bootstrap/systemd/proxy повтор полностью на VPS не проверен.
В рабочие source/SQL/generated Compose на сервере ручные исправления не вносились;
исправления делались в repository automation и повторялись соответствующие проверки.
SQL seeding и proxy/network overrides в лабораторных harness — synthetic fixtures,
они не являются доказательством штатного live onboarding.

### Исправленные qualification findings

| Finding | Исправление и повтор |
|---|---|
| RC не поддерживался release/update policy | parse_release_version/version_order, preflight tests, prerelease publish rules |
| Отсутствовал clean-server bootstrap | scripts/bootstrap-server.sh + bootstrap_server.py; CLI clean install/repeat, preservation/recovery unit tests |
| Fresh upstream PostgreSQL: 21 HIGH / 1 CRITICAL в gosu Go runtime | Pinned official PG17, удалён vulnerable binary; su-exec wrapper для единственного privilege-drop call; scan + fresh/restore/restart |
| Fresh upstream Redis: 4 HIGH в OpenSSL | Pinned Redis7.4, libcrypto/libssl 3.3.7-r2; exact image scan |
| Socket proxy: 4 HIGH в PCRE2 | PCRE2 10.49-r0; exact image scan и ACL/proxy restart |
| **HIGH: Redis сервер фактически работал root** | Generated shell command bypassed official drop branch; compatibility entrypoint теперь передаёт redis-server в штатный entrypoint; UID999/caps0 + recreate/restore regression |
| Registry3 debug port конфликтовал в Windows test harness | Loopback ephemeral debug port; успешный повтор, включая реально broken API image |
| Linux unit fixture возвращала C:/ Docker root | Real fixture directory вместо Windows literal; полный Linux suite |
| Performance artifact требовал существующий ignored tmp/ | Создание parent в тесте; full integration на свежем Linux checkout |

Полные scan artifacts перечислены с hashes в [phase8-evidence.json](phase8-evidence.json).
На **пяти финальных проверенных images**: application, socket proxy, PostgreSQL,
Redis, Traefik — **HIGH = 0, CRITICAL = 0**. Образы собирались/проверялись локально;
registry publication не выполнялась. Это не security approval ещё не установленного VPS.
PostgreSQL использует официальный entrypoint/volume layout и UID70, Redis — UID999;
effective capabilities обоих PID1 = 0. API/worker: UID10001, cap_drop ALL,
no privileged/host network/Docker socket. Root initialization storage остаётся штатной.
Старые pinned storage IDs существующих deployments автоматически не заменяются.

### Performance/resource measurements

Dataset: 100 clients, 200 requests, 4000 messages. Локальные timings: inbox пяти
страниц ~58.2 ms, history page ~13.3 ms, unread ~9.0 ms, proposal ~21.3 ms,
hold + booking + commit ~98.6 ms. Query-count/limit/index regressions прошли;
очевидный N+1/full-history scan не выявлен. Infrastructure smoke: 96 requests,
concurrency 32, все HTTP200, p95 ~0.846 s, max ~1.023 s.

Финальная выборка двух deployments: **8 containers**, shared Docker Desktop
**20 CPU / 8 169 623 552 bytes RAM**. Это не VPS baseline и не SLA.

| Deployment | RAM snapshot | CPU snapshot (Docker %) | DB bytes | PG data disk |
|---|---:|---:|---:|---:|
| release-test-1 | ~436.6 MiB | ~3.8% | 10 344 115 | 66 164 KiB |
| release-test-2 | ~423.1 MiB | ~4.5% | 10 385 075 | 66 204 KiB |

Всего RAM ~859.8 MiB; физические PG directories ~129.3 MiB. Backup directories:
A 215 158 bytes, B 107 561 bytes в этой выборке. Docker-reported image sizes:
application ~58.3 MiB, socket ~22.3 MiB, PG ~111.8 MiB, Redis ~17.7 MiB,
Traefik ~52.7 MiB; это не суммарное физическое использование слоёв Docker.
Workspace disk/free и точные individual stats сохранены в evidence; shared images,
VM overhead, cache и другие проекты не выдаются за расход этого продукта.

Краткие пики worker-health достигали ~100% одного core и повышали worker RSS
до ~285 MiB. Docker top показал worker-health ~92.1% при основном worker ~1%.
Нужны измерения этой периодической нагрузки на целевом VPS.

| Fleet | Рекомендация VPS |
|---|---|
| 1–3 clients | Недостаточно VPS measurements для обоснованных CPU/RAM/disk чисел |
| 4–10 clients | Не измерено на VPS; не экстраполировать линейно shared Desktop snapshot |
| 10+ clients | Не измерено; проверить Docker network pools, ресурсы, backup windows и peak load |

### Ответы по 24 пунктам финального отчёта

| № | Пункт | Результат |
|---:|---|---|
| 1 | Clean VPS по документации | Не проведён; clean Ubuntu CLI-only installation прошла |
| 2 | Ручные действия | 0 ручных source/generated Compose/DB repair на тестовых deployments; число действий полного VPS onboarding не измерено |
| 3 | FIXED | Automated flow/operations regression PASS; live Telegram pending |
| 4 | FROM | Booking и discussion harness PASS; live pending |
| 5 | NEGOTIABLE | Полный dispatcher/domain E2E PASS; live pending |
| 6 | Conversation | History/media/unread/close, до/после appointment PASS в harness/DB |
| 7 | Price negotiation | 15k superseded, 17k accepted/snapshot preserved PASS |
| 8 | Stale/concurrency | PASS, включая restored-state stale buttons и последнюю slot |
| 9 | Webhook/HTTPS | Local secret/routing/ACL/synthetic TLS PASS; real getWebhookInfo/Let's Encrypt/DNS pending |
| 10 | Reboot | Full VPS reboot/systemd timers не проверены; отдельные services restart PASS |
| 11 | Backup/restore | PASS локально, 154 messages, metadata/history/pointers/relations; off-host pending |
| 12 | Old DB upgrade | PASS, old image → populated DB → RC; данные/FIXED backfill сохранены |
| 13 | RC update | PASS, rc.1 → synthetic rc.2, backup/pull/migration/doctor |
| 14 | Broken update/recovery | PASS, injected failure и реально broken API image; FAILED/peer/verified recovery |
| 15 | Two-client isolation | PASS для данных, networks, credentials, backup/restore/update/restart; real two bots pending |
| 16 | Monitoring | Failure/dedup/recovery capture PASS; live alerts и VPS timer execution pending |
| 17 | Exposed ports | У двух выбранных deployments API только loopback; DB/Redis/worker/Docker API не опубликованы; VPS ss/firewall pending |
| 18 | FS/container security | Ubuntu dirs0700/state/template0600; API/worker10001, PG70, Redis999/caps0; live secrets/ACME/host audit pending |
| 19 | Performance/resources | Измерения выше; обоснованных VPS recommendations пока нет |
| 20 | Ruff/unit/integration | PASS: Ruff, 396 unit, 124 integration; также повтор на Ubuntu |
| 21 | BLOCKERS | B1–B4 ниже; release gate не закрыт |
| 22 | HIGH issues | Найденные local HIGH исправлены; открытых HIGH на проверенном финальном scope не осталось |
| 23 | Known limitations | Ниже; production support ещё не квалифицирована |
| 24 | Итог | **NOT READY FOR v1.0.0** |

## WARNINGS

- Worktree изначально содержала незакоммиченные Phase 1–7.5 изменения. Они сохранены.
  OCI revision у локального кандидата — baseline HEAD, а не commit полного snapshot;
  source hashes/IDs сохранены. Production provenance требует commit + CI + release build.
- `1.0.0-rc.1` — локальная подготовка. Staging tags не являются публикацией;
  deployments фиксируют immutable image IDs. GHCR digest/permissions/guard при реальной
  публикации и green CI для этого snapshot ещё не подтверждены.
- Старый единственный доступный GitHub run — Dependency Graph update, не новый CI;
  он не доказывает readiness текущих незакоммиченных workflows.
- Live Telegram, публичные DNS/TLS, VPS host ports и credentials audit не заменяются
  synthetic fixtures. Ни один настоящий клиентский bot/VPS не использовался.
- На shared Desktop исчерпался стандартный Docker address pool. Dedicated harness
  повторён с собственными private test CIDRs; unrelated networks не удалялись.
  Это требует capacity planning перед fleet deployment, не ручного network repair.
- Первые попытки имели image-build ordering error, registry debug conflict,
  Linux fixture failures и network retries. После исправлений соответствующие проверки
  повторены; эти попытки не засчитывались как PASS. Windows suspend/time jump timeout
  также закрыт повтором suite. pip/apt доступность остаётся внешней зависимостью bootstrap.
- Type checker в проекте не настроен; Actionlint не запускал shellcheck/pyflakes.
  Trivy 0.74.0 предупреждает, что Alpine3.24 отсутствует в его EOL list;
  zero HIGH/CRITICAL не является доказательством отсутствия всех vulnerabilities.
- Healthcheck resource bursts требуют VPS acceptance; Desktop measurements не обещают SLA.
- Off-host disaster recovery и реальные monitoring notifications остаются непроверенными.

## KNOWN LIMITATIONS

- Один специалист/Telegram bot/PostgreSQL/Redis на deployment.
- Нет payments/deposits, CRM, web admin, multi-master, SaaS и AI.
- Conversation: text/photo/document; нет voice/video/sticker, group/web chat,
  редактирования/удаления сообщений. Media сохраняется как Telegram metadata/IDs.
- Redis/FSM черновики могут теряться; durable requests/history/terms находятся в PostgreSQL.
- Telegram exactly-once delivery не гарантируется при потере acknowledgement/restore.
- Restore требует controlled downtime и возвращает данные к backup; внешние Telegram
  сообщения не откатываются, старые кнопки проверяются по восстановленному DB state.
- Application update не заменяет pinned storage images автоматически и не делает downgrade.
- Windows/ARM64/Debian production support не заявляется. Ubuntu24.04 x86_64 выбрана
  первой целевой платформой, её полная production qualification ещё не пройдена.

## BLOCKERS

**B1 — Full clean VPS acceptance.** Нужен отдельный Ubuntu24.04 x86_64 VPS:
full bootstrap/repeat, Docker/systemd/timers, реальные onboarding/permissions/ports,
reboot и production-check после reboot. Shared Desktop/CLI container этого не доказывают.

**B2 — Real Telegram/DNS/HTTPS.** Нужны два отдельных test bots, два домена и DNS:
BotFather/getMe/invite/два user E2E, production Let's Encrypt/redirect/renewal,
setWebhook/getWebhookInfo, notifications и delivery/recovery alerts.

**B3 — Frozen release provenance/publication.** Нужен проверенный commit текущего
snapshot, green GitHub CI, scanned immutable RC artifacts в GHCR, package access
и проверенные published digests. Local labels/digests не заменяют этот gate.

**B4 — Off-host recovery.** Нужна реальная защищённая remote copy и restore rehearsal
на отдельном clean target; подтвердить credentials permissions, integrity и данные.

BLOCKERS ≠ 0. Финальный release не рекомендован. После закрытия B1–B4 обновить
[release-checklist.md](release-checklist.md) и этот отчёт; final tag не создавать автоматически.
Новый клиент: [new-client.md](new-client.md). Передача: [client-handover-checklist.md](client-handover-checklist.md).

Основные локальные artifacts: `tmp/phase8-linux-ci-confirmed.log`,
`tmp/phase8-final-integration-confirmed.log`, `tmp/phase8-final-unit-confirmed.log`,
`tmp/phase8-final-operations/clients-results.json`, `tmp/phase8-verified-rollout.log`,
`tmp/phase8-final-proxy.log`, `tmp/phase8-final-clean-cli.log`,
`tmp/phase8-redis-runtime-verified/redis-runtime-result.json`,
`tmp/phase8-scan-final-app.json`, `tmp/phase8-scan-fixed-postgres.json`,
`tmp/phase8-scan-final-redis.json`, `tmp/phase8-scan-fixed-socket-proxy.json`,
`tmp/phase8-scan-traefik.json`. Machine-readable summary: [phase8-evidence.json](phase8-evidence.json).
