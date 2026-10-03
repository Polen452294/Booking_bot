# Release gate v1

RC: **1.0.0-rc.1**. Final tag `v1.0.0` не создаётся автоматически.
Текущие доказательства и blockers: [release-readiness-v1.md](release-readiness-v1.md).
Этот checklist является production gate: локальный тест подтверждает лишь
указанный scope, поэтому live пункты остаются пустыми до реального испытания.

- [ ] CI green на commit, из которого собран неизменяемый RC.
- [x] Ruff green на финальном локальном snapshot.
- [x] Unit tests green: Windows/Linux, 396 tests.
- [x] Integration tests green на PostgreSQL/Redis: Windows/Linux, 124 tests.
- [x] Локальный Docker build green; package/OCI version совпадают.
- [x] Локальный security scan reviewed для точных application/storage/proxy image IDs.
- [x] Alembic single head.
- [x] Fresh DB migration tested локально.
- [x] Populated pre-conversations DB upgrade tested локально; old services behave as FIXED.
- [ ] Clean VPS install tested по документации без generated Compose/SQL/source patch.
- [ ] Bootstrap repeat tested: secrets/clients/volumes/ACME не изменены.
- [ ] Telegram tested: отдельные master/client users, getMe, invite, /start.
- [ ] FIXED booking/operations tested.
- [ ] FROM booking и discussion tested.
- [ ] NEGOTIABLE request → agreed terms → appointment tested.
- [ ] Conversation text/photo/document, unsupported media, unread tested.
- [ ] Price negotiation tested; accepted price snapshot не изменяется с Service.
- [ ] Stale price/request/slot buttons и concurrent accept/proposal/booking tested.
- [ ] Specialist/manual booking/exports/reminders regression tested.
- [x] Локальный backup tested с 154 messages, media, price/read/history/appointment relations.
- [x] Локальный restore tested; post-backup data исчезает, stale buttons проверяются по DB.
- [x] Локальный update rc.1 → synthetic rc.2 tested; backup/migration/doctor/business data preserved.
- [x] Локальный broken update → FAILED → rollback/recovery tested.
- [x] Локальная two-client isolation tested, включая backup/restore/restart/update A при работе B.
- [ ] HTTPS tested: production Let's Encrypt, redirect, hostname, renewal.
- [ ] Webhook tested с реальным Telegram getWebhookInfo.
- [ ] API/worker/Redis/PostgreSQL/Traefik restart tested.
- [ ] Full VPS reboot tested; Docker/services/timers auto-start.
- [ ] Monitoring tested: API/worker/DB/old backup/bad webhook, dedup и recovery delivery.
- [ ] Production log audit: no secrets/credentials/phone/private bodies/unexpected PII.
- [ ] Public ports, private filesystem permissions и container security checked.
- [x] Локальный performance/resource baseline measured на двух deployments; VPS sizes не придуманы.
- [ ] production-check passed после reboot и end-to-end flow.
- [ ] BLOCKERS = 0; security/integrity/privacy/backup HIGH закрыты.

После прохождения всех gates рекомендация может стать READY FOR v1.0.0.
Не превращайте отсутствие воспроизведённой ошибки в доказательство unrun check.
