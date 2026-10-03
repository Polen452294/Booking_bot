from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from booking_bot.db.models import Business, Service, SpecialistProfile, WorkingRule
from booking_bot.db.session import async_session_factory
from booking_bot.deployment import runtime
from booking_bot.services.specialist_setup import configure_specialist
from booking_bot.specialist_config import load_specialist_template

pytestmark = pytest.mark.integration
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "specialist.toml"


@pytest_asyncio.fixture(loop_scope="session")
async def configured_profile():
    template = load_specialist_template(TEMPLATE_PATH)
    template = replace(
        template, profile=replace(template.profile, slug="profile-" + uuid4().hex[:8])
    )
    async with async_session_factory() as session:
        assert await session.get(SpecialistProfile, 1) is None
        profile = await configure_specialist(session, template)
        await session.commit()
        business_id = profile.business_id
    yield template, business_id
    async with async_session_factory() as session:
        await session.execute(delete(SpecialistProfile).where(SpecialistProfile.id == 1))
        await session.execute(delete(Business).where(Business.id == business_id))
        await session.commit()


async def test_reconfigure_never_recreates_owner_cleared_schedule(configured_profile):
    template, business_id = configured_profile
    async with async_session_factory() as session:
        await session.execute(delete(WorkingRule).where(WorkingRule.business_id == business_id))
        await session.commit()
        await configure_specialist(session, template)
        await session.commit()
        assert not list(
            (
                await session.scalars(
                    select(WorkingRule).where(WorkingRule.business_id == business_id)
                )
            ).all()
        )


async def test_profile_change_and_exact_rollback_preserve_owner_data(
    configured_profile, monkeypatch
):
    template, business_id = configured_profile
    async with async_session_factory() as session:
        services = list(
            (await session.scalars(select(Service).where(Service.business_id == business_id))).all()
        )
        for i, service in enumerate(services):
            service.name = f"Owner service {i}"
            service.price_minor = 12345 + i
            service.is_owner_managed = bool(i)  # Both managed and unmarked rows must stay intact.
        await session.execute(delete(WorkingRule).where(WorkingRule.business_id == business_id))
        business = await session.get(Business, business_id)
        business.name = "Actual DB name differs from old TOML"
        await session.commit()
    snapshot = await runtime.profile_operation("snapshot")
    candidate = replace(template, profile=replace(template.profile, brand_name="New brand"))
    monkeypatch.setattr(runtime, "get_specialist_template", lambda: candidate)
    assert await runtime.profile_operation("apply") == {"ok": True}
    async with async_session_factory() as session:
        assert (await session.get(Business, business_id)).name == "New brand"
    await runtime.profile_operation("restore", snapshot)
    assert await runtime.profile_operation("snapshot") == snapshot
    async with async_session_factory() as session:
        services = list(
            (await session.scalars(select(Service).where(Service.business_id == business_id))).all()
        )
        assert {s.name for s in services} == {"Owner service 0", "Owner service 1"}
        assert {s.price_minor for s in services} == {12345, 12346}
        assert not list(
            (
                await session.scalars(
                    select(WorkingRule).where(WorkingRule.business_id == business_id)
                )
            ).all()
        )


async def test_snapshot_identity_failure_rolls_back_all_fields(configured_profile):
    before = await runtime.profile_operation("snapshot")
    wrong = deepcopy(before)
    wrong["business"]["values"]["name"] = "Must roll back"
    wrong["master"]["id"] = str(uuid4())
    with pytest.raises(ValueError, match="another profile"):
        await runtime.profile_operation("restore", wrong)
    assert await runtime.profile_operation("snapshot") == before
