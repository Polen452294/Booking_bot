"""Real Docker DR acceptance. Creates NEW dedicated registry; deletes ONLY its test PG volume.

python tests/disaster_recovery_smoke.py --root tmp/phase5-dr --image booking-bot:0.5.0-phase5
Telegram getMe alone is stubbed; never uses a real token or sends messages.
"""

import argparse
import ipaddress
import json
import secrets
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from booking_bot.deployment.backup import BackupManager
from booking_bot.deployment.files import DeploymentError
from booking_bot.deployment.manager import BotIdentity, CreateRequest, DeploymentManager, run_docker
from booking_bot.specialist_config import load_specialist_template

SEED = """
import asyncio
from datetime import UTC, datetime, date, timedelta
from sqlalchemy import select
from booking_bot.db.session import async_session_factory, engine
from booking_bot.db.models import (Business, Master, Service, TelegramUser, CalendarEntry,
    Appointment, AppointmentHistory, ScheduleException, BookingRequest, Conversation,
    ConversationMessage, ConversationReadState, PriceProposal)
async def seed():
    async with async_session_factory() as s, s.begin():
        b = await s.scalar(select(Business))
        m = await s.scalar(select(Master))
        service = Service(business_id=b.id, name='DR service', duration_minutes=60,
            price_minor=12345, is_owner_managed=True)
        client = TelegramUser(telegram_user_id=987654321, first_name='DR client',
                              phone='+79990000123')
        s.add_all([service, client])
        await s.flush()
        request = BookingRequest(business_id=b.id, master_id=m.id, service_id=service.id,
            client_user_id=client.id, status='booked', service_name_snapshot=service.name,
            client_name_snapshot=client.first_name, client_phone_snapshot=client.phone,
            description='DR individual requirements')
        s.add(request)
        await s.flush()
        conversation = Conversation(booking_request_id=request.id, last_message_sequence=154)
        previous = PriceProposal(booking_request_id=request.id,
            created_by_user_id=m.user_id or client.id, revision=1, amount_minor=10000,
            currency='RUB', status='superseded', superseded_at=datetime.now(UTC))
        proposal = PriceProposal(booking_request_id=request.id,
            created_by_user_id=m.user_id or client.id, revision=2, amount_minor=12345,
            currency='RUB', comment='DR agreed terms', status='accepted',
            accepted_at=datetime.now(UTC))
        s.add_all([conversation, previous, proposal])
        await s.flush()
        s.add_all([
            ConversationMessage(conversation_id=conversation.id, sequence=1,
                sender_user_id=client.id, sender_role='client', message_type='photo',
                text='DR reference', telegram_file_id='dr-telegram-file',
                telegram_file_unique_id='dr-telegram-unique'),
            ConversationMessage(conversation_id=conversation.id, sequence=2,
                sender_user_id=client.id, sender_role='system', message_type='system',
                event_type='price_accepted', event_payload={'proposal_id': str(proposal.id)}),
            ConversationMessage(conversation_id=conversation.id, sequence=3,
                sender_user_id=client.id, sender_role='client', message_type='text',
                text='DR additional requirements'),
            ConversationReadState(conversation_id=conversation.id, user_id=client.id,
                last_read_sequence=51),
            ConversationMessage(conversation_id=conversation.id, sequence=4,
                sender_user_id=client.id, sender_role='client', message_type='document',
                telegram_file_id='dr-document', telegram_file_unique_id='dr-document-unique'),
        ])
        s.add_all([ConversationMessage(conversation_id=conversation.id, sequence=i,
            sender_user_id=client.id, sender_role='client', message_type='text',
            text=f'DR history {i}') for i in range(5, 155)])
        start = datetime(2030, 1, 2, 9, tzinfo=UTC)
        entry = CalendarEntry(business_id=b.id, master_id=m.id, starts_at=start,
            ends_at=start+timedelta(hours=1), kind='appointment', state='active')
        s.add(entry)
        await s.flush()
        a = Appointment(business_id=b.id, calendar_entry_id=entry.id, service_id=service.id,
            client_id=client.id, service_name_snapshot=service.name, service_starts_at=start,
            service_ends_at=entry.ends_at, duration_minutes=60, price_minor=12345,
            status='confirmed', internal_note='DR important note', booking_request_id=request.id)
        s.add(a)
        await s.flush()
        s.add(AppointmentHistory(business_id=b.id, appointment_id=a.id,
            event_type='confirmed', to_status='confirmed'))
        s.add(ScheduleException(business_id=b.id, master_id=m.id,
            exception_date=date(2030,1,3), kind='day_off', reason='DR schedule change'))
    await engine.dispose()
asyncio.run(seed())
"""

TABLES = (
    "businesses",
    "masters",
    "locations",
    "services",
    "master_services",
    "telegram_users",
    "calendar_entries",
    "appointments",
    "appointment_history",
    "working_rules",
    "schedule_exceptions",
    "master_invites",
    "booking_requests",
    "conversations",
    "conversation_messages",
    "conversation_read_states",
    "price_proposals",
    "alembic_version",
)


def snapshot(backups, slug):
    return {
        table: backups.sql(
            slug,
            f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY "
            f"to_jsonb(t)::text), '[]'::jsonb) FROM {table} t",
        )
        for table in TABLES
    }


def smoke(root: Path, image: str, network_pool: str | None = None, *, test_domains=False):
    if root.exists() or root.with_name(root.name + "-backups").exists():
        raise RuntimeError("Use a NEW dedicated DR registry and backup directory")
    manager = DeploymentManager(root)
    backups = BackupManager(manager, root.with_name(root.name + "-backups"))
    template = load_specialist_template(Path(__file__).resolve().parents[1] / "specialist.toml")
    slugs = ("dr-test", "dr-test-b")
    ids, before = {}, {}
    subnets = []
    if network_pool:
        pool = ipaddress.ip_network(network_pool)
        if pool.version != 4 or pool.prefixlen != 22 or not pool.is_private:
            raise ValueError("Test network pool must be a private IPv4 /22")
        subnets = list(pool.subnets(new_prefix=24))
    provision = manager.provision

    def test_provision(slug, state):
        if subnets:
            for name in ("data", "egress"):
                run_docker(
                    [
                        "network",
                        "create",
                        "--subnet",
                        str(subnets.pop(0)),
                        "--label",
                        f"com.docker.compose.project={state['project']}",
                        "--label",
                        f"com.docker.compose.network={name}",
                        *(["--internal"] if name == "data" else []),
                        f"{state['project']}_{name}",
                    ]
                )
        return provision(slug, state)

    manager.provision = test_provision
    for index, slug in enumerate(slugs):
        bot_id = 933333330 + index
        request = CreateRequest(
            replace(template, profile=replace(template.profile, slug=slug)),
            f"{bot_id}:{secrets.token_urlsafe(36)}",
            image,
            domain=f"{slug}.ops.invalid" if test_domains else "",
        )
        with patch(
            "booking_bot.deployment.manager.get_bot_identity",
            return_value=BotIdentity(bot_id, f"dr_{bot_id}_bot"),
        ):
            manager.create(slug, request)
        manager.compose(slug, "run", "--rm", "--no-deps", "-T", "admin", "python", "-c", SEED)
        before[slug] = snapshot(backups, slug)
        ids[slug] = backups.create(slug)
        backups.verify(slug, ids[slug])
        print(f"{slug}: created, business data seeded, backup verified", flush=True)

    exercise(manager, backups, ids, before)


def exercise(manager, backups, ids, before):
    slugs = ("dr-test", "dr-test-b")
    for slug, other in ((slugs[0], slugs[1]), (slugs[1], slugs[0])):
        # Prove wrong-client restore is rejected before touching services.
        try:
            backups.restore(other, ids[slug], yes=True)
        except DeploymentError:
            pass
        else:
            raise AssertionError("Wrong-client restore was accepted")
        peer_containers = manager.compose(other, "ps", "-q")
        state = manager.state(slug)
        container = manager.compose(slug, "ps", "-q", "postgres").strip()
        inspected = json.loads(run_docker(["inspect", container]))[0]
        volume = next(
            m["Name"] for m in inspected["Mounts"] if m["Destination"] == "/var/lib/postgresql/data"
        )
        volume_info = json.loads(run_docker(["volume", "inspect", volume]))[0]
        assert slug in slugs and state["project"].startswith(f"booking-{slug}-")
        assert volume_info["Labels"]["com.docker.compose.project"] == state["project"]
        assert volume_info["Labels"]["com.docker.compose.volume"] == "postgres_data"
        assert volume == state["project"] + "_postgres_data"
        manager.compose(slug, "stop", "api", "worker", "postgres")
        manager.compose(slug, "rm", "-f", "postgres")
        run_docker(["volume", "rm", volume])
        print(f"{slug}: dedicated PostgreSQL volume destroyed", flush=True)
        # An unavailable DB must prevent the ordinary restore before any destructive work.
        try:
            backups.restore(slug, ids[slug], yes=True)
        except DeploymentError:
            pass
        else:
            raise AssertionError("Missing safety backup did not abort")
        backups.restore(slug, ids[slug], yes=True, dangerous_skip_safety=True)
        assert snapshot(backups, slug) == before[slug]
        assert snapshot(backups, other) == before[other]
        assert manager.compose(other, "ps", "-q") == peer_containers
        assert manager.doctor(slug)["ok"]
        assert (manager.directory(slug) / "specialist.toml").read_bytes() == (
            backups.directory(slug, ids[slug]) / "specialist.toml"
        ).read_bytes()
        print(f"{slug}: restored exact rows/config, doctor OK, {other} unchanged", flush=True)

    # Ordinary restore must create a separately verified safety backup.
    backups.restore(slugs[0], ids[slugs[0]], yes=True)
    safety = manager.state(slugs[0])["safety_backup"]
    assert safety.startswith("pre-restore-")
    backups.verify(slugs[0], safety)
    assert snapshot(backups, slugs[0]) == before[slugs[0]]
    print(
        "DR ACCEPTANCE PASSED: A/B isolation, volume loss, safety backup, exact business rows",
        flush=True,
    )
    print(
        "Telegram: syntactically valid fake tokens; getMe stubbed; live public webhook not tested",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--network-pool", help="Optional unused private IPv4 /22 for test networks")
    args = parser.parse_args()
    smoke(args.root.absolute(), args.image, args.network_pool)
