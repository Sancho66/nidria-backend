"""The Email module's tables enforce their own invariants, whatever writes.

- A sent render is frozen by the database (`email_render_freeze`, SQLSTATE
  42501): only the step's deletion may set its `campaign_step_id` to NULL.
- A client profile holds one LIVE membership across campaigns, and one OPEN
  membership per campaign.
- A provider event is stored once (idempotence of webhook, job and sync), and
  a step is sent once to a membership.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import (
    LIVE_MEMBER_STATUSES,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
    EmailProviderEvent,
)
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    MakeEmailCampaign,
    MakeEmailMember,
    MakeEmailMessage,
    MakeEmailRender,
    MakeEmailStep,
)

_NOW = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)


async def _update_render(db: AsyncSession, render_id: object, **values: object) -> None:
    await db.execute(
        update(EmailMemberRender).where(EmailMemberRender.id == render_id).values(**values)
    )
    await db.commit()


# --- the freeze trigger ---------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"subject": "Another subject"},
        {"body": "Another body"},
        {"sent_at": None},
        {"resolved_variables": {"client.first_name": "Paul"}},
        {"template_hash": "f" * 64},
        {"provider_synced_at": _NOW},
    ],
)
async def test_a_sent_render_can_no_longer_change(
    db_session: AsyncSession, make_email_render: MakeEmailRender, change: dict[str, object]
) -> None:
    render_id = (await make_email_render(sent_at=_NOW)).id
    with pytest.raises(DBAPIError) as exc:
        await _update_render(db_session, render_id, **change)
    assert "EMAIL_RENDER_SENT_IS_IMMUTABLE" in str(exc.value)
    await db_session.rollback()  # expires every loaded object: ids only below
    stored = (
        await db_session.execute(
            select(EmailMemberRender.subject, EmailMemberRender.sent_at).where(
                EmailMemberRender.id == render_id
            )
        )
    ).one()
    assert stored == ("Hello Claire", _NOW)


async def test_the_freeze_raises_the_insufficient_privilege_sqlstate(
    db_session: AsyncSession, make_email_render: MakeEmailRender
) -> None:
    render = await make_email_render(sent_at=_NOW)
    with pytest.raises(DBAPIError) as exc:
        await _update_render(db_session, render.id, subject="x")
    await db_session.rollback()
    assert getattr(exc.value.orig, "sqlstate", None) == "42501"


async def test_an_unsent_render_is_refreshed_freely(
    db_session: AsyncSession, make_email_render: MakeEmailRender
) -> None:
    render = await make_email_render()
    await _update_render(db_session, render.id, subject="Refreshed", sent_at=_NOW)
    stored = await db_session.scalar(
        select(EmailMemberRender.subject).where(EmailMemberRender.id == render.id)
    )
    assert stored == "Refreshed"


async def test_deleting_a_step_detaches_its_sent_renders(
    db_session: AsyncSession,
    make_email_member: MakeEmailMember,
    make_email_step: MakeEmailStep,
    make_email_render: MakeEmailRender,
) -> None:
    member = await make_email_member()
    step = await make_email_step(campaign_id=member.campaign_id)
    render = await make_email_render(
        campaign_member_id=member.id, campaign_step_id=step.id, sent_at=_NOW
    )
    await db_session.execute(delete(EmailCampaignStep).where(EmailCampaignStep.id == step.id))
    await db_session.commit()
    stored = (
        await db_session.execute(
            select(EmailMemberRender.campaign_step_id, EmailMemberRender.subject).where(
                EmailMemberRender.id == render.id
            )
        )
    ).one()
    assert stored == (None, "Hello Claire")  # kept, auditable, detached


async def test_a_sent_render_cannot_be_moved_to_another_step(
    db_session: AsyncSession,
    make_email_member: MakeEmailMember,
    make_email_step: MakeEmailStep,
    make_email_render: MakeEmailRender,
) -> None:
    member = await make_email_member()
    first = await make_email_step(campaign_id=member.campaign_id, position=1)
    second = await make_email_step(campaign_id=member.campaign_id, position=2)
    render = await make_email_render(
        campaign_member_id=member.id, campaign_step_id=first.id, sent_at=_NOW
    )
    with pytest.raises(DBAPIError, match="EMAIL_RENDER_SENT_IS_IMMUTABLE"):
        await _update_render(db_session, render.id, campaign_step_id=second.id)
    await db_session.rollback()


# --- one live membership per person, one open membership per campaign -------------------


@pytest.mark.parametrize("live_status", LIVE_MEMBER_STATUSES)
async def test_a_client_profile_holds_one_live_membership(
    db_session: AsyncSession,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_client_profile: MakeClientProfile,
    live_status: str,
) -> None:
    first = await make_email_campaign()
    second = await make_email_campaign(agency_id=first.agency_id)
    profile = await make_client_profile(agency_id=first.agency_id)
    await make_email_member(campaign_id=first.id, client_profile_id=profile.id, status=live_status)
    with pytest.raises(IntegrityError, match="uq_email_member_live"):
        await make_email_member(
            campaign_id=second.id, client_profile_id=profile.id, status="EMAIL_READY"
        )
    await db_session.rollback()


async def test_a_terminal_membership_frees_the_person_for_another_campaign(
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_client_profile: MakeClientProfile,
) -> None:
    first = await make_email_campaign()
    second = await make_email_campaign(agency_id=first.agency_id)
    profile = await make_client_profile(agency_id=first.agency_id)
    await make_email_member(
        campaign_id=first.id, client_profile_id=profile.id, status="EMAIL_COMPLETED"
    )
    joined = await make_email_member(
        campaign_id=second.id, client_profile_id=profile.id, status="EMAIL_READY"
    )
    assert joined.status == "EMAIL_READY"


async def test_one_open_membership_per_campaign_until_it_is_removed(
    db_session: AsyncSession,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_client_profile: MakeClientProfile,
) -> None:
    campaign = await make_email_campaign()
    profile = await make_client_profile(agency_id=campaign.agency_id)
    campaign_id, profile_id = campaign.id, profile.id
    ended_id = (
        await make_email_member(
            campaign_id=campaign_id, client_profile_id=profile_id, status="EMAIL_COMPLETED"
        )
    ).id
    with pytest.raises(IntegrityError, match="uq_email_campaign_member_open"):
        await make_email_member(campaign_id=campaign_id, client_profile_id=profile_id)
    await db_session.rollback()  # expires every loaded object: ids only below
    # Once removed (never deleted), the same person may come back.
    await db_session.execute(
        update(EmailCampaignMember)
        .where(EmailCampaignMember.id == ended_id)
        .values(removed_at=_NOW, status="EMAIL_REMOVED")
    )
    await db_session.commit()
    back = await make_email_member(campaign_id=campaign_id, client_profile_id=profile_id)
    assert back.removed_at is None


# --- idempotence keys -------------------------------------------------------------------


async def test_a_provider_event_is_stored_once(
    db_session: AsyncSession, make_email_member: MakeEmailMember
) -> None:
    member = await make_email_member()

    def event() -> EmailProviderEvent:
        return EmailProviderEvent(
            provider="LEMLIST",
            provider_event_id="act_123",
            agency_id=member.agency_id,
            campaign_id=member.campaign_id,
            campaign_member_id=member.id,
            type="SENT",
            provider_type="emailsSent",
            occurred_at=_NOW,
        )

    db_session.add(event())
    await db_session.commit()
    db_session.add(event())
    with pytest.raises(IntegrityError, match="uq_email_provider_event"):
        await db_session.commit()
    await db_session.rollback()


async def test_a_step_is_sent_once_to_a_membership(
    db_session: AsyncSession,
    make_email_member: MakeEmailMember,
    make_email_message: MakeEmailMessage,
) -> None:
    member = await make_email_member()
    common = {
        "campaign_member_id": member.id,
        "campaign_id": member.campaign_id,
        "agency_id": member.agency_id,
    }
    await make_email_message(**common, step_position=1)
    with pytest.raises(IntegrityError, match="uq_email_message_outbound_step"):
        await make_email_message(**common, step_position=1)
    await db_session.rollback()
    # A reply at the same position is another message.
    reply = await make_email_message(
        **common, step_position=1, direction="INBOUND", status="REPLIED", content_state="PENDING"
    )
    assert reply.direction == "INBOUND"
