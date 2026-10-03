from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.db.models import PriceProposal
from booking_bot.domain.conversations import (
    MAX_COMMENT_LENGTH,
    InvalidRequestTransitionError,
    ProposalNotPendingError,
    validate_page,
    validate_text,
)
from booking_bot.domain.enums import BookingRequestStatus, PriceProposalStatus
from booking_bot.services.conversation_context import (
    enqueue_notification,
    get_request,
    latest_proposal,
    record_event,
    require_client,
    require_master,
    transition,
)


class PriceProposalService:
    async def decide_by_id(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        actor_user_id: UUID,
        proposal_id: UUID,
        accept: bool,
    ) -> PriceProposal:
        from booking_bot.domain.conversations import ConversationAccessError

        request_id = await session.scalar(
            select(PriceProposal.booking_request_id).where(
                PriceProposal.id == proposal_id,
            )
        )
        if request_id is None:
            raise ConversationAccessError("Request is unavailable")
        return await self._decide(
            session,
            business_id=business_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
            proposal_id=proposal_id,
            accept=accept,
        )

    async def propose(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        amount_minor: int,
        currency: str = "RUB",
        comment: str | None = None,
    ) -> PriceProposal:
        if type(amount_minor) is not int or not 0 <= amount_minor <= 2_000_000_000:
            raise ValueError("Invalid price amount")
        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or not all("A" <= letter <= "Z" for letter in currency)
        ):
            raise ValueError("Currency must contain three uppercase ASCII letters")
        if comment is not None:
            comment = validate_text(comment, maximum=MAX_COMMENT_LENGTH, field="comment")
        async with session.begin_nested():
            request = await get_request(
                session,
                business_id=business_id,
                request_id=request_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            require_master(request, actor_user_id)
            if request.status not in {
                BookingRequestStatus.WAITING_MASTER.value,
                BookingRequestStatus.WAITING_CLIENT.value,
                BookingRequestStatus.TERMS_PROPOSED.value,
                BookingRequestStatus.TERMS_ACCEPTED.value,
            }:
                raise InvalidRequestTransitionError("Request does not allow new terms")
            previous = await latest_proposal(session, request.id)
            if previous is not None and previous.status == PriceProposalStatus.PENDING.value:
                previous.status = PriceProposalStatus.SUPERSEDED.value
                previous.superseded_at = datetime.now(UTC)
                # Flush withdrawal before inserting under the partial unique index.
                await session.flush()
                await record_event(
                    session,
                    request=request,
                    actor_user_id=actor_user_id,
                    action="price_superseded",
                    details={"proposal_id": str(previous.id)},
                )
            proposal = PriceProposal(
                booking_request_id=request.id,
                created_by_user_id=actor_user_id,
                revision=previous.revision + 1 if previous else 1,
                amount_minor=amount_minor,
                currency=currency,
                comment=comment,
            )
            session.add(proposal)
            if request.status != BookingRequestStatus.TERMS_PROPOSED.value:
                transition(request, BookingRequestStatus.TERMS_PROPOSED)
            await session.flush()
            await record_event(
                session,
                request=request,
                actor_user_id=actor_user_id,
                action="price_proposed",
                details={
                    "proposal_id": str(proposal.id),
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "comment": comment,
                    "previous_proposal_id": str(previous.id) if previous else None,
                    "previous_amount_minor": previous.amount_minor if previous else None,
                },
            )
            await enqueue_notification(
                session,
                request=request,
                kind="client_price_proposal",
                event_id=proposal.id,
            )
            await session.flush()
            return proposal

    async def accept(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        proposal_id: UUID,
    ) -> PriceProposal:
        return await self._decide(
            session,
            business_id=business_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
            proposal_id=proposal_id,
            accept=True,
        )

    async def reject(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        proposal_id: UUID,
    ) -> PriceProposal:
        return await self._decide(
            session,
            business_id=business_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
            proposal_id=proposal_id,
            accept=False,
        )

    async def _decide(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        proposal_id: UUID,
        accept: bool,
    ) -> PriceProposal:
        async with session.begin_nested():
            request = await get_request(
                session,
                business_id=business_id,
                request_id=request_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            require_client(request, actor_user_id)
            proposal = await latest_proposal(session, request.id)
            if (
                request.status != BookingRequestStatus.TERMS_PROPOSED.value
                or proposal is None
                or proposal.id != proposal_id
                or proposal.status != PriceProposalStatus.PENDING.value
            ):
                raise ProposalNotPendingError("Proposal is no longer pending")
            proposal.status = (
                PriceProposalStatus.ACCEPTED.value if accept else PriceProposalStatus.REJECTED.value
            )
            if accept:
                proposal.accepted_at = datetime.now(UTC)
            else:
                proposal.rejected_at = datetime.now(UTC)
            transition(
                request,
                BookingRequestStatus.TERMS_ACCEPTED
                if accept
                else BookingRequestStatus.WAITING_MASTER,
            )
            await record_event(
                session,
                request=request,
                actor_user_id=actor_user_id,
                action="price_accepted" if accept else "price_rejected",
                details={
                    "proposal_id": str(proposal.id),
                    "amount_minor": proposal.amount_minor,
                    "currency": proposal.currency,
                },
            )
            await enqueue_notification(
                session,
                request=request,
                event_id=proposal.id,
                kind="master_price_proposal_accepted"
                if accept
                else "master_price_proposal_rejected",
            )
            await session.flush()
            return proposal

    async def list_proposals(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        limit: int = 50,
        before_revision: int = 0,
    ) -> list[PriceProposal]:
        validate_page(limit, before_revision)
        await get_request(
            session,
            business_id=business_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
        )
        statement = select(PriceProposal).where(PriceProposal.booking_request_id == request_id)
        if before_revision:
            statement = statement.where(PriceProposal.revision < before_revision)
        return list(
            (
                await session.scalars(
                    statement.order_by(PriceProposal.revision.desc()).limit(limit)
                )
            ).all()
        )
