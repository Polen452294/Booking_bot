# Передача клиента v1

Заполняется на реальной установке. Галочки из mocked tests не переносятся сюда.
Для каждой проверки сохраните дату, deployment/version/image ID и безопасное
доказательство; secret values, телефоны и тела переписки в отчёт не включать.

- [ ] Bot создан в отдельном BotFather account/workflow; getMe соответствует клиенту.
- [ ] Domain A/AAAA и domain-check OK.
- [ ] HTTPS: production Let's Encrypt, правильный hostname, redirect, renewal config.
- [ ] Webhook: setWebhook/getWebhookInfo URL совпадает с deployment domain.
- [ ] Master invite пройден штатным Telegram flow; специалист связан.
- [ ] Services FIXED/FROM/NEGOTIABLE настроены, валюты/цены подтверждены.
- [ ] Schedule и timezone настроены, working hours/days off проверены.
- [ ] FIXED: booking, просмотр, перенос, отмена, повторная запись, calendar export.
- [ ] FROM: обычная запись без обязательного диалога и отдельный discussion flow.
- [ ] NEGOTIABLE: request, photo/document, latest price accept, slot, appointment.
- [ ] Conversation до/после записи в обе стороны, unread и закрытие проверены.
- [ ] Price negotiation: 15k → 17k; старое accept отклоняется; snapshot = 17k.
- [ ] Notifications/reminders доставлены обоим пользователям.
- [ ] Backup создан, integrity проверена и off-host copy подтверждена.
- [ ] Restore rehearsal пройден на выделенной копии; downtime объяснён оператору.
- [ ] Monitoring active: failure/recovery alerts доставляются без повторного spam.
- [ ] После reboot doctor/production-check и Telegram flow успешны.
- [ ] Только согласованные SSH/80/443 публичны; DB/Redis/API/Docker API закрыты.
- [ ] Credentials хранятся безопасно; `.env`, backups, ACME и host credentials private.
- [ ] Ограничения v1, update/recovery procedure и контакты оператора переданы.

Клиент: ______. Домен: ______. Версия/image ID: ______.
Оператор: ______. Дата: ______. Незавершённые пункты: ______.
