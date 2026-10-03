# Ежедневная эксплуатация

Используйте постоянный root, например bookingctl --root /opt/booking/clients;
ниже root опущен. Release images должны быть опубликованы до create/update.

| Действие | Команды |
| --- | --- |
| Новый клиент | create SLUG --image ghcr.io/polen452294/booking-bot:1.0.0-rc.1 --config TEMPLATE; затем domain/expose/webhook по deployment guide |
| Проверка | bookingctl status; bookingctl doctor; bookingctl resources |
| Приёмка | bookingctl production-check SLUG и real Telegram scenarios |
| Логи | bookingctl logs SLUG --service api --tail 200; worker аналогично; --follow для stream |
| Backup | bookingctl backup SLUG; backup verify SLUG ID; backup-all --retention |
| Версии | bookingctl versions; BOOKING_LATEST_VERSION после release review |
| Canary update | bookingctl update SLUG --version X.Y.Z --dry-run; затем без dry-run; production-check |
| Rollout | bookingctl update-all --version X.Y.Z после здорового canary |
| Recovery | history SLUG; rollback SLUG; при изменённой schema reviewed --restore-database --yes |
| Restore | restore SLUG BACKUP_ID --yes после проверки identity/version/checksum и потери новых записей |
| Alerts | bookingctl monitor; journalctl -u booking-monitor.service; сохранять alert state |
| Failed jobs | notifications failed SLUG; explicit safe retry после устранения причины |

Ежедневно проверяйте ERROR/CRITICAL, freshness backups, pending/failed и disk.
После change проверяйте actual runtime и Telegram. Планово проверяйте off-host
read-back и restore в отдельном deployment. Перед update backup обязателен.
Backup/monitor timers должны быть enabled и реально успешно завершаться.

Не удаляйте PostgreSQL volumes. Ручные Docker/SQL обходят locks; не запускайте
их одновременно с bookingctl. Несколько независимых registry для одного
deployment не поддерживаются. Restore/update временно отключают выбранного
клиента; другие продолжают работу.

См. [incidents](troubleshooting.md), [DR](disaster-recovery.md),
[результаты и ограничения](production-report.md).
