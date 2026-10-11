"""The platform job `email_auto_import` (Alexandre's decision of 11/10/2026:
« import auto via un script cron »): the campaigns created in Lemlist come
into Nidria by themselves, by the import's own rules, for no agent; a run
with nothing new reads only the team's listing; drafts, ended campaigns and
Nidria's own are never imported; an imported campaign with leads Nidria does
not know is imported again, never an archived one; nothing is read without
real effects, a module on and the agency bound to the key's team.

Lemlist is `FakeProvider`, scripted as the import's tests script it (the
`team_scenario`: A paused and B running importable, a draft, an ended, an
archived, an erroneous campaign and one Nidria made). No test reaches it.
"""

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agency_email_module import AgencyEmailModule
from shared.models.client_profile import ClientProfile
from shared.models.email_campaign import EmailCampaign, EmailCampaignMember
from shared.models.job import JobConfig, JobRun
from src.core.enums import JobTriggeredBy
from src.core.job_wrapper import run_job
from src.core.scheduler import JOB_REGISTRY
from src.email_campaigns.email_manager import IMPORT_METADATA_KEY
from src.email_campaigns.providers.base import (
    ProviderError,
    ProviderLead,
    ProviderLeadIdentity,
)
from src.jobs.jobs_baseline import seed_job_configs
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    MakeCompanyLink,
    MakeEmailCampaign,
    MakeEmailMember,
    email_env,
)
from tests.test_email_import import (
    LEMLIST_A,
    LEMLIST_B,
    Team,
    campaign_of,
    import_,
    members_by_lead,
    plan_of,
    set_status,
    team_scenario,
)
from tests.test_email_sync_engine import (  # noqa: F401 (fixtures)
    Live,
    _bind,
    _run,
    job_db,
    live,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

#: What reading the team's listing alone calls at Lemlist.
LISTING_ONLY = ["list_campaigns"]


async def _enable_auto(db: AsyncSession) -> None:
    """The seeded row is disabled: switched on as go-live does it."""
    await seed_job_configs(db)
    await db.execute(
        update(JobConfig).where(JobConfig.job_id == "email_auto_import").values(is_enabled=True)
    )
    await db.commit()


async def _auto(sessions: sessionmaker[Session], *, dry_run: bool = False) -> JobRun:
    """One run of the automatic import, through the job wrapper, in a worker
    thread like the scheduler's."""

    def go() -> JobRun:
        with sessions() as db:
            return run_job(
                db,
                job_id="email_auto_import",
                pipeline=JOB_REGISTRY["email_auto_import"],
                triggered_by=JobTriggeredBy.MANUAL,
                dry_run=dry_run,
            )

    return await asyncio.to_thread(go)


async def scenario(
    env: Live,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> Team:
    team = await team_scenario(
        env.world,
        env.provider,
        make_client_profile,
        make_email_campaign,
        make_email_member,
        make_company_link,
    )
    env.provider.ops.clear()
    return team


async def count(db: AsyncSession, model: Any, *where: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def reads_beyond_listing(env: Live) -> list[str]:
    return [name for name in env.provider.names() if name != "list_campaigns"]


async def test_the_campaigns_created_in_lemlist_are_imported_by_the_job(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> None:
    team = await scenario(
        live, make_client_profile, make_email_campaign, make_email_member, make_company_link
    )
    await _enable_auto(db_session)
    # What the route's plan says an import would do, read before the job.
    plan = await plan_of(client, team.world.headers)
    planned = {c["provider_campaign_id"]: c["leads"] for c in plan["campaigns"]}
    live.provider.ops.clear()

    run = await _auto(live.sessions)
    assert run.status == "success", run.log_output
    stats = run.stats
    assert (stats["agencies"], stats["campaigns_listed"], stats["campaigns_new"]) == (1, 7, 2)
    assert (stats["campaigns_imported"], stats["campaigns_failed"], stats["errors"]) == (2, 0, 0)
    # Exactly what the plan announced, nothing more: the route's own rules.
    assert stats["members_created"] == sum(p["to_create"] for p in planned.values())
    assert stats["profiles_created"] == sum(p["profiles_to_create"] for p in planned.values())
    assert stats["conflicts"] == sum(
        p["conflicts"] + p["conflicts_in_plan"] for p in planned.values()
    )
    assert stats["calls"] > 1
    assert "2 imported" in (run.log_output or "") and "@" not in (run.log_output or "")

    a = await campaign_of(db_session, LEMLIST_A)
    b = await campaign_of(db_session, LEMLIST_B)
    for campaign in (a, b):
        # Imported for no agent, said so.
        noted = campaign.provider_metadata[IMPORT_METADATA_KEY]
        assert (campaign.origin, campaign.created_by_agent_id) == ("LEMLIST_IMPORT", None)
        assert (noted["imported_by"], noted["imported_by_agent_id"]) == ("auto", None)
    added_by = await db_session.scalars(
        select(EmailCampaignMember.added_by_agent_id).where(
            EmailCampaignMember.campaign_id.in_([a.id, b.id])
        )
    )
    assert set(added_by.all()) == {None}
    # Never Nidria's own, a draft, an ended, an archived nor an erroneous one.
    for provider_id in ("cam_draft", "cam_ended", "cam_archived", "cam_errors"):
        assert (
            await count(
                db_session, EmailCampaign, EmailCampaign.provider_campaign_id == provider_id
            )
            == 0
        )
    nidria = await db_session.get(EmailCampaign, team.nidria_campaign)
    assert nidria is not None and nidria.origin == "NIDRIA"


async def test_a_run_with_nothing_new_reads_only_the_listing(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> None:
    await scenario(
        live, make_client_profile, make_email_campaign, make_email_member, make_company_link
    )
    await _enable_auto(db_session)
    first = await _auto(live.sessions)
    assert first.stats["campaigns_imported"] == 2, first.log_output
    profiles = await count(db_session, ClientProfile)
    members = await count(db_session, EmailCampaignMember)
    live.provider.ops.clear()

    second = await _auto(live.sessions)
    assert second.status == "success", second.log_output
    assert (second.stats["campaigns_new"], second.stats["campaigns_stale"]) == (0, 0)
    assert (second.stats["campaigns_imported"], second.stats["calls"]) == (0, 1)
    assert live.provider.names() == LISTING_ONLY
    assert "nothing to import" in (second.log_output or "")
    assert await count(db_session, ClientProfile) == profiles
    assert await count(db_session, EmailCampaignMember) == members


async def test_new_leads_of_an_imported_campaign_come_in_never_an_archived_one(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> None:
    team = await scenario(
        live, make_client_profile, make_email_campaign, make_email_member, make_company_link
    )
    await _enable_auto(db_session)
    await import_(client, team.world.headers, LEMLIST_A, LEMLIST_B)
    a = await campaign_of(db_session, LEMLIST_A)
    # A lead added at Lemlist since: the Email Sync job counts it unknown.
    live.provider.listed[LEMLIST_A].append(
        ProviderLead("lea_new", None, False, state="emailsSent", raw={"state": "emailsSent"})
    )
    live.provider.exports[LEMLIST_A].append(
        ProviderLeadIdentity("lea_new", "new@acme.io", "New", "Lead", None, "running", "emailsSent")
    )
    synced = await _run(live.sessions)
    assert synced.stats["unknown_leads"] == 1, synced.log_output
    live.provider.ops.clear()

    run = await _auto(live.sessions)
    assert (run.stats["campaigns_new"], run.stats["campaigns_stale"]) == (0, 1), run.log_output
    assert (run.stats["campaigns_imported"], run.stats["members_created"]) == (1, 1)
    assert run.stats["unknown_leads"] == 0
    assert "lea_new" in await members_by_lead(db_session, a.id)
    noted = (await campaign_of(db_session, LEMLIST_A)).provider_metadata[IMPORT_METADATA_KEY]
    assert noted["unknown_leads"] == 0

    # Archived in Nidria: left alone, even with leads Nidria does not know.
    await set_status(client, team.world.headers, str(a.id), "ARCHIVED")
    archived = await campaign_of(db_session, LEMLIST_A)
    archived.provider_metadata = {
        **archived.provider_metadata,
        IMPORT_METADATA_KEY: {
            **archived.provider_metadata[IMPORT_METADATA_KEY],
            "unknown_leads": 4,
        },
    }
    await db_session.commit()
    live.provider.ops.clear()
    again = await _auto(live.sessions)
    assert (again.stats["campaigns_stale"], again.stats["campaigns_imported"]) == (0, 0)
    assert reads_beyond_listing(live) == []


async def test_nothing_is_read_without_effects_a_module_or_a_bound_team(
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> None:
    await scenario(
        live, make_client_profile, make_email_campaign, make_email_member, make_company_link
    )
    await _enable_auto(db_session)
    # Asked as a dry run: the pipeline's own skip, no call.
    dry = await _auto(live.sessions, dry_run=True)
    assert dry.stats == {"skipped": True, "reason": "dry_run"}
    # Real effects off on the platform: the same.
    with email_env(EMAIL_PROVIDER_EXTERNAL_EFFECTS="false"):
        off = await _auto(live.sessions)
    assert off.stats == {"skipped": True, "reason": "dry_run"}
    # The agency bound to another team: skipped, said, nothing read.
    await _bind(db_session, live.world.agency_id, "tea_other")
    other = await _auto(live.sessions)
    assert other.stats["agencies"] == 0
    assert f"agency {live.world.agency_id}: skipped (not bound to the key's Lemlist team)" in (
        other.log_output or ""
    )
    # The module off for the agency: not even listed.
    await _bind(db_session, live.world.agency_id)
    await db_session.execute(
        update(AgencyEmailModule)
        .where(AgencyEmailModule.agency_id == live.world.agency_id)
        .values(enabled=False)
    )
    await db_session.commit()
    closed = await _auto(live.sessions)
    assert closed.stats["agencies"] == 0
    assert str(live.world.agency_id) not in (closed.log_output or "")
    assert live.provider.names() == []
    assert await count(db_session, EmailCampaign, EmailCampaign.origin == "LEMLIST_IMPORT") == 0


async def test_a_campaign_failing_is_counted_by_its_code_the_others_go_on(
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> None:
    await scenario(
        live, make_client_profile, make_email_campaign, make_email_member, make_company_link
    )
    await _enable_auto(db_session)
    live.provider.failures[("read_activities", LEMLIST_A)] = ProviderError(
        "Lemlist could not be reached.", code="UNREACHABLE"
    )
    run = await _auto(live.sessions)
    assert run.status == "success", run.log_output
    assert (run.stats["campaigns_imported"], run.stats["campaigns_failed"]) == (1, 1)
    assert run.stats["error_messages"] == [
        {
            "code": "email.provider_unreachable",
            "agency_id": str(live.world.agency_id),
            "provider_campaign_id": LEMLIST_A,
        }
    ]
    assert (
        await count(db_session, EmailCampaign, EmailCampaign.provider_campaign_id == LEMLIST_A) == 0
    )
    # The next run brings it in: it is still new.
    del live.provider.failures[("read_activities", LEMLIST_A)]
    again = await _auto(live.sessions)
    assert (again.stats["campaigns_new"], again.stats["campaigns_imported"]) == (1, 1)


async def test_a_listing_refused_by_lemlist_is_counted_never_raised(
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> None:
    await scenario(
        live, make_client_profile, make_email_campaign, make_email_member, make_company_link
    )
    await _enable_auto(db_session)
    live.provider.failures[("list_campaigns", "team")] = ProviderError(
        "Lemlist rate limit persisted.", code="RATE_LIMITED", status=429
    )
    run = await _auto(live.sessions)
    assert run.status == "success", run.log_output
    assert (run.stats["agencies"], run.stats["errors"]) == (0, 1)
    [entry] = run.stats["error_messages"]
    assert entry["agency_id"] == str(live.world.agency_id)
    assert entry["code"].startswith("email.provider_")


async def test_an_agency_the_email_sync_job_reads_is_left_to_the_next_run(
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
    sync_engine: Engine,
) -> None:
    """The two jobs share the agency's lock (`email_sync`'s): never both
    reading Lemlist for one agency at once."""
    await scenario(
        live, make_client_profile, make_email_campaign, make_email_member, make_company_link
    )
    await _enable_auto(db_session)
    agency = {"agency": str(live.world.agency_id)}
    lock = text("SELECT pg_try_advisory_lock(hashtextextended('email_sync:' || :agency, 0))")
    unlock = text("SELECT pg_advisory_unlock(hashtextextended('email_sync:' || :agency, 0))")
    with sync_engine.connect() as holder:
        assert holder.scalar(lock, agency) is True
        holder.commit()
        busy = await _auto(live.sessions)
        holder.scalar(unlock, agency)
        holder.commit()
    assert busy.status == "success", busy.log_output
    assert busy.stats["agencies"] == 0
    assert f"agency {live.world.agency_id}: skipped (busy" in (busy.log_output or "")
    assert live.provider.names() == []

    done = await _auto(live.sessions)
    assert (done.stats["agencies"], done.stats["campaigns_imported"]) == (1, 2)
    # Released at the end of the run.
    with sync_engine.connect() as after:
        assert after.scalar(lock, agency) is True
        after.scalar(unlock, agency)
        after.commit()


def test_the_job_is_registered_and_seeded_disabled() -> None:
    from src.jobs.jobs_baseline import DEFAULT_JOB_CONFIGS

    [spec] = [s for s in DEFAULT_JOB_CONFIGS if s["job_id"] == "email_auto_import"]
    assert (spec["cron_expression"], spec["is_enabled"]) == ("5,35 * * * *", False)
    assert "email_auto_import" in JOB_REGISTRY
