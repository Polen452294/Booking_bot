from datetime import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.db.models import (
    Business,
    Location,
    Master,
    MasterService,
    Service,
    SpecialistProfile,
    WorkingRule,
)
from booking_bot.specialist_config import SpecialistTemplate

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


async def configure_specialist(
    session: AsyncSession,
    template: SpecialistTemplate,
    *,
    replace_schedule: bool = False,
    profile_only: bool = False,
) -> SpecialistProfile:
    deployment = await session.get(SpecialistProfile, 1)
    initialized = deployment is not None
    if profile_only and not initialized:
        raise ValueError("Profile-only configuration requires an initialized specialist")
    if deployment is not None:
        business = await session.get(Business, deployment.business_id)
        master = await session.get(Master, deployment.master_id)
    else:
        business = await session.scalar(select(Business).order_by(Business.created_at).limit(1))
        master = (
            await session.scalar(
                select(Master)
                .where(Master.business_id == business.id)
                .order_by(Master.created_at)
                .limit(1)
            )
            if business is not None
            else None
        )

    if business is None:
        business = Business(
            slug=template.profile.slug,
            name=template.profile.brand_name,
        )
        session.add(business)
        await session.flush()
    if master is None:
        master = Master(
            business_id=business.id,
            display_name=template.profile.specialist_name,
        )
        session.add(master)
        await session.flush()

    business.slug = template.profile.slug
    business.name = template.profile.brand_name
    business.timezone = template.profile.timezone
    business.locale = template.profile.locale
    business.currency = template.profile.currency
    business.is_active = True
    master.business_id = business.id
    if master.user_id is None:
        master.display_name = template.profile.specialist_name
        master.bio = template.profile.bio
    master.timezone = template.profile.timezone
    master.is_active = True

    if deployment is None:
        deployment = SpecialistProfile(
            id=1,
            business_id=business.id,
            master_id=master.id,
        )
        session.add(deployment)
    else:
        deployment.business_id = business.id
        deployment.master_id = master.id

    location = await session.scalar(
        select(Location)
        .where(Location.business_id == business.id)
        .order_by(Location.created_at)
        .limit(1)
    )
    if location is None:
        location = Location(
            business_id=business.id,
            name=template.location.name,
        )
        session.add(location)
    location.name = template.location.name
    location.address = template.location.address
    location.timezone = template.profile.timezone
    location.is_active = True
    await session.flush()

    if profile_only:
        return deployment

    configured_keys = {item.key for item in template.services}
    existing_services = list(
        (await session.scalars(select(Service).where(Service.business_id == business.id))).all()
    )
    for item in template.services:
        service = next(
            (
                candidate
                for candidate in existing_services
                if candidate.config_key == item.key
                or (candidate.config_key is None and candidate.name == item.name)
            ),
            None,
        )
        if service is None:
            service = Service(
                business_id=business.id,
                config_key=item.key,
                name=item.name,
                duration_minutes=item.duration_minutes,
            )
            session.add(service)
            await session.flush()
            existing_services.append(service)
        if not service.is_owner_managed:
            service.config_key = item.key
            service.name = item.name
            service.description = item.description
            service.duration_minutes = item.duration_minutes
            service.buffer_before_minutes = item.buffer_before_minutes
            service.buffer_after_minutes = item.buffer_after_minutes
            service.price_minor = item.price_minor
            service.pricing_mode = item.pricing_mode.value
            service.currency = template.profile.currency
            service.requires_approval = item.requires_approval
            service.requires_deposit = False
            service.deposit_minor = None
            service.is_active = True

        link = await session.scalar(
            select(MasterService).where(
                MasterService.master_id == master.id,
                MasterService.service_id == service.id,
            )
        )
        if link is None:
            session.add(
                MasterService(
                    business_id=business.id,
                    master_id=master.id,
                    service_id=service.id,
                    is_active=service.is_active,
                )
            )
        else:
            link.business_id = business.id
            if not service.is_owner_managed:
                link.is_active = True

    for service in existing_services:
        if (
            not service.is_owner_managed
            and service.config_key is not None
            and service.config_key not in configured_keys
        ):
            service.is_active = False

    existing_working_rule = await session.scalar(
        select(WorkingRule.id)
        .where(
            WorkingRule.business_id == business.id,
            WorkingRule.master_id == master.id,
        )
        .limit(1)
    )
    if replace_schedule or (not initialized and existing_working_rule is None):
        await session.execute(
            delete(WorkingRule).where(
                WorkingRule.business_id == business.id,
                WorkingRule.master_id == master.id,
            )
        )
        for day_name, weekday in WEEKDAYS.items():
            interval = template.schedule.get(day_name, "").strip()
            if not interval:
                continue
            try:
                start_raw, end_raw = interval.split("-", 1)
                start_time = time.fromisoformat(start_raw)
                end_time = time.fromisoformat(end_raw)
            except ValueError as exc:
                raise ValueError(f"Invalid schedule for {day_name}: {interval}") from exc
            if end_time <= start_time:
                raise ValueError(f"Schedule end must be after start for {day_name}")
            session.add(
                WorkingRule(
                    business_id=business.id,
                    master_id=master.id,
                    location_id=location.id,
                    weekday=weekday,
                    start_time=start_time,
                    end_time=end_time,
                )
            )
    await session.flush()
    return deployment
