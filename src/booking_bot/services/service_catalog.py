from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.db.models import Business, MasterService, Service
from booking_bot.domain.enums import PricingMode

MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 24 * 60
MAX_BUFFER_MINUTES = 24 * 60
MAX_PRICE_MINOR = 2_000_000_000


class ServiceCatalogError(RuntimeError):
    pass


class ServiceNotFoundError(ServiceCatalogError):
    pass


class DuplicateServiceNameError(ServiceCatalogError):
    pass


class InvalidServiceValueError(ServiceCatalogError):
    pass


class SpecialistServiceCatalog:
    async def set_pricing_mode(
        self, session: AsyncSession, *, business_id: UUID, master_id: UUID,
        service_id: UUID, pricing_mode: PricingMode,
    ) -> Service:
        try:
            mode = PricingMode(pricing_mode)
        except ValueError as exc:
            raise InvalidServiceValueError("Invalid pricing mode") from exc
        service = await self.get_service(
            session, business_id=business_id, master_id=master_id, service_id=service_id,
        )
        service.pricing_mode = mode.value
        service.is_owner_managed = True
        await session.flush()
        return service

    async def list_services(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
    ) -> list[Service]:
        return list(
            (
                await session.scalars(
                    select(Service)
                    .join(MasterService, MasterService.service_id == Service.id)
                    .where(
                        Service.business_id == business_id,
                        MasterService.business_id == business_id,
                        MasterService.master_id == master_id,
                    )
                    .order_by(Service.is_active.desc(), Service.name)
                )
            ).all()
        )

    async def get_service(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
    ) -> Service:
        service, _link = await self._get_linked_service(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
        )
        return service

    async def create_service(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        name: str,
    ) -> Service:
        normalized_name = self._validated_name(name)
        await self._ensure_name_available(
            session,
            business_id=business_id,
            name=normalized_name,
        )
        business = await session.get(Business, business_id)
        if business is None:
            raise ServiceCatalogError("Specialist profile not found")
        service = Service(
            business_id=business_id,
            name=normalized_name,
            duration_minutes=60,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            currency=business.currency,
            requires_approval=False,
            requires_deposit=False,
            is_owner_managed=True,
            is_active=False,
        )
        session.add(service)
        await session.flush()
        session.add(
            MasterService(
                business_id=business_id,
                master_id=master_id,
                service_id=service.id,
                is_active=False,
            )
        )
        await session.flush()
        return service

    async def set_name(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
        name: str,
    ) -> Service:
        service = await self.get_service(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
        )
        normalized_name = self._validated_name(name)
        await self._ensure_name_available(
            session,
            business_id=business_id,
            name=normalized_name,
            exclude_service_id=service.id,
        )
        service.name = normalized_name
        service.is_owner_managed = True
        await session.flush()
        return service

    async def set_description(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
        description: str | None,
    ) -> Service:
        service = await self.get_service(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
        )
        normalized = description.strip() if description else None
        if normalized and len(normalized) > 2000:
            raise InvalidServiceValueError("Description is too long")
        service.description = normalized or None
        service.is_owner_managed = True
        await session.flush()
        return service

    async def set_duration(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
        duration_minutes: int,
    ) -> Service:
        if not MIN_DURATION_MINUTES <= duration_minutes <= MAX_DURATION_MINUTES:
            raise InvalidServiceValueError("Invalid duration")
        service = await self.get_service(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
        )
        service.duration_minutes = duration_minutes
        service.is_owner_managed = True
        await session.flush()
        return service

    async def set_price(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
        price_minor: int | None,
    ) -> Service:
        if price_minor is not None and not 0 <= price_minor <= MAX_PRICE_MINOR:
            raise InvalidServiceValueError("Invalid price")
        service = await self.get_service(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
        )
        service.price_minor = price_minor
        service.is_owner_managed = True
        await session.flush()
        return service

    async def set_buffers(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
        buffer_before_minutes: int,
        buffer_after_minutes: int,
    ) -> Service:
        if not (
            0 <= buffer_before_minutes <= MAX_BUFFER_MINUTES
            and 0 <= buffer_after_minutes <= MAX_BUFFER_MINUTES
        ):
            raise InvalidServiceValueError("Invalid service buffers")
        service = await self.get_service(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
        )
        service.buffer_before_minutes = buffer_before_minutes
        service.buffer_after_minutes = buffer_after_minutes
        service.is_owner_managed = True
        await session.flush()
        return service

    async def toggle_approval(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
    ) -> Service:
        service = await self.get_service(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
        )
        service.requires_approval = not service.requires_approval
        service.is_owner_managed = True
        await session.flush()
        return service

    async def toggle_active(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
    ) -> Service:
        service, link = await self._get_linked_service(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
            for_update=True,
        )
        active = not service.is_active
        service.is_active = active
        service.is_owner_managed = True
        link.is_active = active
        await session.flush()
        return service

    async def _get_linked_service(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
        for_update: bool = False,
    ) -> tuple[Service, MasterService]:
        statement = (
            select(Service, MasterService)
            .join(MasterService, MasterService.service_id == Service.id)
            .where(
                Service.id == service_id,
                Service.business_id == business_id,
                MasterService.business_id == business_id,
                MasterService.master_id == master_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise ServiceNotFoundError
        return row[0], row[1]

    async def _ensure_name_available(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        name: str,
        exclude_service_id: UUID | None = None,
    ) -> None:
        statement = select(Service.id).where(
            Service.business_id == business_id,
            func.lower(Service.name) == name.lower(),
        )
        if exclude_service_id is not None:
            statement = statement.where(Service.id != exclude_service_id)
        if await session.scalar(statement) is not None:
            raise DuplicateServiceNameError

    @staticmethod
    def _validated_name(name: str) -> str:
        normalized = " ".join(name.split())
        if not 2 <= len(normalized) <= 160:
            raise InvalidServiceValueError("Invalid service name")
        return normalized
