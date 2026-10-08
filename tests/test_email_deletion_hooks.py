"""The deletion hooks (spec 8.11, decision A2, gaps E2 and E3): before a
client record is deleted (alone, in bulk, or merged away) or an agency is,
its leads that carry a real Lemlist id are removed there; Lemlist refusing
one fails the deletion (409 `email.provider_cleanup_failed`) and NOTHING is
deleted, in bulk too. Lemlist's deduplication sees ended leads as well: a
lead left behind would refuse that address in every later campaign.

Nothing to do, and no call at all, in a dry run, with the module off for
the agency, or without a real lead; another Lemlist team's lead is left
alone (the key cannot reach it), and so is a lead a removal already took
out there.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.bulk_deletion import BulkDeletionLog
from shared.models.client_profile import ClientProfile
from shared.models.email_campaign import EmailCampaign
from shared.models.rbac import Role
from src.email_campaigns import email_manager
from src.email_campaigns.providers.base import ProviderError
from tests.email_fakes import FakeProvider
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import EmailWorld, MakeEmailCampaign, MakeEmailMember
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


async def _campaign(
    make_email_campaign: MakeEmailCampaign,
    world: EmailWorld,
    provider_id: str | None,
    *,
    team: str | None = "tea_current",
    status: str = "ACTIVE",
    name: str = "Live",
) -> uuid.UUID:
    campaign = await make_email_campaign(
        agency_id=world.agency_id,
        name=name,
        status=status,
        provider_campaign_id=provider_id,
        provider_sequence_id="seq" if provider_id else None,
        provider_metadata={"team_id": team} if team and provider_id else {},
    )
    return campaign.id


async def _lead(
    make_email_member: MakeEmailMember,
    world: EmailWorld,
    campaign_id: uuid.UUID,
    profile_id: uuid.UUID,
    lead_id: str | None,
    **values: Any,
) -> uuid.UUID:
    member = await make_email_member(
        campaign_id=campaign_id,
        client_profile_id=profile_id,
        agency_id=world.agency_id,
        provider_lead_id=lead_id,
        **{"status": "EMAIL_ACTIVE", **values},
    )
    return member.id


async def _exists(db: AsyncSession, profile_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(ClientProfile.id)
        .where(ClientProfile.id == profile_id)
        .execution_options(populate_existing=True)
    )
    return found is not None


# ── one client record ─────────────────────────────────────────────────────────


async def test_deleting_a_client_record_removes_its_real_leads_at_lemlist_first(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    ana = await make_client_profile(agency_id=email_world.agency_id, email="ana@x.io")
    live = await _campaign(make_email_campaign, email_world, "cam_live")
    ended = await _campaign(make_email_campaign, email_world, "cam_ended", name="Ended")
    dry = await _campaign(make_email_campaign, email_world, "dry_cam", name="Dry")
    old_team = await _campaign(
        make_email_campaign, email_world, "cam_old", team="tea_old", name="Old team"
    )
    removed = await _campaign(make_email_campaign, email_world, "cam_removed", name="Removed")
    # Live, ended (Lemlist still holds an ended lead), a dry run's, another
    # team's, and one a removal already took out of Lemlist.
    await _lead(make_email_member, email_world, live, ana.id, "lea_live", status="EMAIL_PAUSED")
    await _lead(make_email_member, email_world, ended, ana.id, "lea_ended", status="EMAIL_REPLIED")
    await _lead(make_email_member, email_world, dry, ana.id, "dry_lea", status="EMAIL_COMPLETED")
    await _lead(
        make_email_member, email_world, old_team, ana.id, "lea_old", status="EMAIL_COMPLETED"
    )
    await _lead(
        make_email_member,
        email_world,
        removed,
        ana.id,
        "lea_gone",
        status="EMAIL_REMOVED",
        removed_at=datetime.now(UTC),
    )

    response = await client.delete(f"/client-profiles/{ana.id}", headers=email_world.headers)
    assert response.status_code == 204, response.text
    assert sorted(kw["lead"] for name, kw in fake_email_provider.ops) == ["lea_ended", "lea_live"]
    assert set(fake_email_provider.names()) == {"remove_contact"}
    assert not await _exists(db_session, ana.id)


async def test_a_refused_removal_deletes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    ana = await make_client_profile(agency_id=email_world.agency_id, email="ana@x.io")
    live = await _campaign(make_email_campaign, email_world, "cam_live")
    await _lead(make_email_member, email_world, live, ana.id, "lea_ana")
    fake_email_provider.failures[("remove_contact", "lea_ana")] = ProviderError(
        "Lemlist did not answer in time.", code="TIMEOUT"
    )
    response = await client.delete(f"/client-profiles/{ana.id}", headers=email_world.headers)
    assert response.status_code == 409
    assert response.json()["code"] == "email.provider_cleanup_failed"
    assert response.json()["params"] == {
        "client_profile_id": str(ana.id),
        "code": "email.provider_unreachable",
    }
    assert await _exists(db_session, ana.id)

    # The agent tries again once Lemlist answers.
    fake_email_provider.failures.clear()
    again = await client.delete(f"/client-profiles/{ana.id}", headers=email_world.headers)
    assert again.status_code == 204
    assert not await _exists(db_session, ana.id)


@pytest.mark.parametrize("why", ["dry_run", "module_off", "no_real_lead"])
async def test_nothing_to_do_means_no_call_at_all(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    monkeypatch: pytest.MonkeyPatch,
    why: str,
) -> None:
    asked: list[str] = []

    def counted(_settings: Any, _row: Any) -> FakeProvider:
        asked.append("provider_for")
        return fake_email_provider

    monkeypatch.setattr(email_manager, "provider_for", counted)
    ana = await make_client_profile(agency_id=email_world.agency_id, email="ana@x.io")
    if why == "no_real_lead":
        dry = await _campaign(make_email_campaign, email_world, "dry_cam")
        await _lead(make_email_member, email_world, dry, ana.id, "dry_lea")
    else:
        live = await _campaign(make_email_campaign, email_world, "cam_live")
        await _lead(make_email_member, email_world, live, ana.id, "lea_ana")
    if why == "dry_run":
        fake_email_provider.external = False
    elif why == "module_off":
        await db_session.execute(
            update(AgencyEmailModule)
            .where(AgencyEmailModule.agency_id == email_world.agency_id)
            .values(enabled=False)
        )
        await db_session.commit()
    response = await client.delete(f"/client-profiles/{ana.id}", headers=email_world.headers)
    assert response.status_code == 204, response.text
    assert fake_email_provider.ops == []
    # The provider is not even chosen when there is nothing real to remove.
    assert asked == (["provider_for"] if why == "dry_run" else [])
    assert not await _exists(db_session, ana.id)


# ── in bulk ──────────────────────────────────────────────────────────────────


async def test_a_bulk_deletion_cleans_every_deletable_record_before_the_first_batch(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One record per batch: the engine commits each batch for good (E3), so
    # a cleanup done batch by batch would leave a partial deletion behind.
    monkeypatch.setattr("src.core.bulk_delete.BATCH_SIZE", 1)
    agency = email_world.agency_id
    live = await _campaign(make_email_campaign, email_world, "cam_live")
    ana = await make_client_profile(agency_id=agency, email="ana@x.io")
    ben = await make_client_profile(agency_id=agency, email="ben@x.io")
    # A record a case protects stays, and so does its lead.
    account = await make_expat_user(email="kept@x.io")
    kept = await make_client_profile(agency_id=agency, expat_user_id=account.id)
    await make_client_case(agency_id=agency, principal_expat_user_id=account.id)
    for profile, lead in ((ana, "lea_ana"), (ben, "lea_ben"), (kept, "lea_kept")):
        await _lead(make_email_member, email_world, live, profile.id, lead)
    ids = [str(ana.id), str(ben.id), str(kept.id)]

    # The dry run counts, and asks Lemlist nothing.
    counted = await client.post(
        "/client-profiles/bulk-delete",
        json={"ids": ids, "dry_run": True},
        headers=email_world.headers,
    )
    assert counted.status_code == 200, counted.text
    assert (counted.json()["deletable"], counted.json()["protected"]) == (2, 1)
    assert fake_email_provider.ops == []

    # Lemlist refuses Ben's (the second record): nothing at all is deleted,
    # Ana included.
    fake_email_provider.failures[("remove_contact", "lea_ben")] = ProviderError(
        "Lemlist: server error", code="PROVIDER_ERROR"
    )
    refused = await client.post(
        "/client-profiles/bulk-delete", json={"ids": ids}, headers=email_world.headers
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "email.provider_cleanup_failed"
    assert refused.json()["params"]["client_profile_id"] == str(ben.id)
    assert all([await _exists(db_session, p.id) for p in (ana, ben, kept)])
    assert await db_session.scalar(select(func.count()).select_from(BulkDeletionLog)) == 0

    fake_email_provider.failures.clear()
    fake_email_provider.ops.clear()
    done = await client.post(
        "/client-profiles/bulk-delete", json={"filter": {}}, headers=email_world.headers
    )
    assert done.status_code == 200, done.text
    assert (done.json()["deleted"], done.json()["protected"]) == (2, 1)
    removed = sorted(kw["lead"] for name, kw in fake_email_provider.ops)
    assert removed == ["lea_ana", "lea_ben"]  # never the protected record's
    assert [await _exists(db_session, p.id) for p in (ana, ben, kept)] == [False, False, True]


# ── merged away ──────────────────────────────────────────────────────────────


async def test_merging_removes_the_sources_leads_and_a_refusal_merges_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    agency = email_world.agency_id
    target = await make_client_profile(agency_id=agency, email="ana@x.io", phone=None)
    source = await make_client_profile(agency_id=agency, email="ana.old@x.io", phone="+33600000000")
    live = await _campaign(make_email_campaign, email_world, "cam_live")
    await _lead(make_email_member, email_world, live, source.id, "lea_source")
    fake_email_provider.failures[("remove_contact", "lea_source")] = ProviderError(
        "Lemlist did not answer in time.", code="TIMEOUT"
    )
    body = {"source_profile_id": str(source.id)}
    refused = await client.post(
        f"/client-profiles/{target.id}/merge", json=body, headers=email_world.headers
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "email.provider_cleanup_failed"
    assert await _exists(db_session, source.id)
    phone = await db_session.scalar(
        select(ClientProfile.phone)
        .where(ClientProfile.id == target.id)
        .execution_options(populate_existing=True)
    )
    assert phone is None  # nothing of the merge was written

    fake_email_provider.failures.clear()
    merged = await client.post(
        f"/client-profiles/{target.id}/merge", json=body, headers=email_world.headers
    )
    assert merged.status_code == 200, merged.text
    assert ("remove_contact", {"lead": "lea_source"}) in fake_email_provider.ops
    assert not await _exists(db_session, source.id)


# ── an agency ────────────────────────────────────────────────────────────────


async def test_deleting_an_agency_pauses_its_campaigns_and_removes_their_leads_first(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"], email="root@platform.io")
    root = agent_headers(superadmin)
    live = await _campaign(make_email_campaign, email_world, "cam_live")
    archived = await _campaign(
        make_email_campaign, email_world, "cam_archived", status="ARCHIVED", name="Archived"
    )
    dry = await _campaign(make_email_campaign, email_world, "dry_cam", name="Dry")
    ana = await make_client_profile(agency_id=email_world.agency_id, email="ana@x.io")
    ben = await make_client_profile(agency_id=email_world.agency_id, email="ben@x.io")
    await _lead(make_email_member, email_world, live, ana.id, "lea_ana")
    await _lead(make_email_member, email_world, archived, ben.id, "lea_ben", status="EMAIL_REPLIED")
    await _lead(make_email_member, email_world, dry, ben.id, "dry_lea", status="EMAIL_COMPLETED")
    # The other agency's campaign at Lemlist is never touched.
    other = await make_email_campaign(
        agency_id=email_world.other_agency_id,
        name="Elsewhere",
        status="ACTIVE",
        provider_campaign_id="cam_theirs",
        provider_metadata={"team_id": "tea_current"},
    )
    body = {"confirm_name": "Relocation Lisboa"}

    fake_email_provider.failures[("pause_campaign", "cam_archived")] = ProviderError(
        "Lemlist: server error", code="PROVIDER_ERROR"
    )
    refused = await client.request(
        "DELETE", f"/agencies/{email_world.agency_id}", json=body, headers=root
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "email.provider_cleanup_failed"
    assert refused.json()["params"] == {
        "campaign_id": str(archived),
        "code": "email.provider_error",
    }
    db_session.expunge_all()
    assert await db_session.get(Agency, email_world.agency_id) is not None

    fake_email_provider.failures.clear()
    fake_email_provider.ops.clear()
    deleted = await client.request(
        "DELETE", f"/agencies/{email_world.agency_id}", json=body, headers=root
    )
    assert deleted.status_code == 200, deleted.text
    assert fake_email_provider.ops == [
        ("pause_campaign", {"campaign": "cam_live"}),
        ("remove_contact", {"lead": "lea_ana"}),
        ("pause_campaign", {"campaign": "cam_archived"}),
        ("remove_contact", {"lead": "lea_ben"}),
    ]
    db_session.expunge_all()
    assert await db_session.get(Agency, email_world.agency_id) is None
    assert await db_session.get(EmailCampaign, other.id) is not None
