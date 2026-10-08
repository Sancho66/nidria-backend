"""Readiness: what stands between a client and a campaign's emails, one set
of rules for the add preview, Leads, Launch and the hand-over (spec 8.6).

Ported from Prism's `tests/test_email_readiness.py` (reference 629a2b0):
the syntax check and its cases as they are. Not ported: Prism's Apollo
verification and legacy workflow (spec 8.6), and its API test of the Leads
list (the routes come in phase 3). Nidria's own: issues are codes with
params, DEMO_RECIPIENT, TEAM_MISMATCH, the durable block (decision A1, and
the agency's opposition list of 08/10/2026) and the address two records
share (risk R1), every fact read in one agency.

No provider, no model, no network: the facts are rows written by the
email plugin's factories; the rules are pure.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.client_profile import ClientProfile
from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignAiVariable,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
)
from src.client_profiles.identity import effective_email
from src.email_campaigns.email_repository import EmailRepository
from src.email_campaigns.readiness import (
    AI_AT_LAUNCH,
    ISSUE_CODES,
    ClientFacts,
    Issue,
    TemplateError,
    campaign_gates,
    client_issues,
    daily_limit_gate,
    day_start,
    durable_block,
    email_syntax_ok,
    load_client_facts,
    membership_issues,
    not_launched,
    sender_gate,
    team_mismatch,
    template_issues,
)
from src.email_campaigns.variables import RenderContext
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    TEAM_A,
    TEAM_B,
    MakeEmailCampaign,
    MakeEmailMember,
    MakeEmailSuppression,
)
from tests.plugins.expat_plugin import MakeExpatUser

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

# ── the syntax check (Prism's cases, unchanged) ──────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "ada@acme.io",
        "ada.lovelace+news@mail.acme.co.uk",
        "o'neil@acme.io",
        "  padded@acme.io  ",
        "élodie@société.fr",
        "x@acme.xn--p1ai",
    ],
)
def test_well_formed_addresses_pass(value: str) -> None:
    assert email_syntax_ok(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "ada",
        "ada@",
        "@acme.io",
        "ada@acme",
        "ada@@acme.io",
        "ada lovelace@acme.io",
        "ada@acme..io",
        "ada.@acme.io",
        "ada@-acme.io",
        "ada@acme.i0",
        "n/a",
        "ada@acme.io, bob@acme.io",
    ],
)
def test_malformed_addresses_fail(value: str | None) -> None:
    assert not email_syntax_ok(value)


# ── the person ───────────────────────────────────────────────────────────


def test_the_address_comes_first_and_says_what_is_wrong_with_it() -> None:
    profile_id, facts = uuid.uuid4(), ClientFacts()
    assert client_issues(profile_id, None, facts) == [Issue("NO_EMAIL")]
    assert client_issues(profile_id, "   ", facts) == [Issue("NO_EMAIL")]
    assert client_issues(profile_id, "n/a", facts) == [Issue("INVALID_EMAIL")]
    # A demo address never leaves through Lemlist (N11), however it is typed.
    assert client_issues(profile_id, "demo+horizon-3@nidria.app", facts) == [
        Issue("DEMO_RECIPIENT")
    ]
    assert client_issues(profile_id, " Demo+Horizon-3@Nidria.APP ", facts) == [
        Issue("DEMO_RECIPIENT")
    ]
    assert client_issues(profile_id, "demo@nidria.app", facts) == []
    assert client_issues(profile_id, "ana@example.com", facts) == []
    assert all(i.blocking and i.params == {} for i in client_issues(profile_id, None, facts))


async def _facts(
    db: AsyncSession, agency: Agency, *people: tuple[ClientProfile, str | None]
) -> ClientFacts:
    return await load_client_facts(
        EmailRepository(db), agency.id, [(profile.id, email) for profile, email in people]
    )


async def test_an_unsubscribe_or_a_bounce_blocks_for_good_even_once_removed(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    """Decision A1: the block is read from every membership, removed ones
    included. A removal keeps the blocking status (phase 3), and the block
    outlives it."""
    agency = await make_agency()
    first = await make_email_campaign(agency_id=agency.id)
    second = await make_email_campaign(agency_id=agency.id, name="Second")
    gone = await make_client_profile(agency_id=agency.id, email="gone@example.com")
    bounced = await make_client_profile(agency_id=agency.id, email="bounced@example.com")
    both = await make_client_profile(agency_id=agency.id, email="both@example.com")
    clean = await make_client_profile(agency_id=agency.id, email="clean@example.com")
    for profile, status, campaign in (
        (gone, "EMAIL_UNSUBSCRIBED", first),
        (bounced, "EMAIL_BOUNCED", first),
        (both, "EMAIL_BOUNCED", first),
        (both, "EMAIL_UNSUBSCRIBED", second),
        # A removal that is not a block blocks nothing.
        (clean, "EMAIL_REMOVED", first),
    ):
        await make_email_member(
            campaign_id=campaign.id,
            client_profile_id=profile.id,
            status=status,
            removed_at=NOW,
        )
    people = [(p, p.email) for p in (gone, bounced, both, clean)]
    facts = await _facts(db_session, agency, *people)
    assert facts.blocked == {
        gone.id: "EMAIL_UNSUBSCRIBED",
        bounced.id: "EMAIL_BOUNCED",
        both.id: "EMAIL_UNSUBSCRIBED",  # an unsubscribe outranks a bounce
    }
    assert client_issues(gone.id, gone.email, facts) == [Issue("UNSUBSCRIBED")]
    assert client_issues(bounced.id, bounced.email, facts) == [Issue("BOUNCED")]
    assert client_issues(both.id, both.email, facts) == [Issue("UNSUBSCRIBED")]
    assert client_issues(clean.id, clean.email, facts) == []


async def test_live_elsewhere_blocks_but_not_the_membership_itself(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    agency = await make_agency()
    campaign = await make_email_campaign(agency_id=agency.id)
    profile = await make_client_profile(agency_id=agency.id, email="ana@example.com")
    member = await make_email_member(campaign_id=campaign.id, client_profile_id=profile.id)
    facts = await _facts(db_session, agency, (profile, profile.email))
    # Added to another campaign: live elsewhere, with the campaign named.
    assert client_issues(profile.id, profile.email, facts) == [
        Issue("IN_OTHER_CAMPAIGN", {"campaign_id": str(campaign.id)})
    ]
    # The membership itself is not "elsewhere".
    assert membership_issues(member, profile.email, facts) == []


async def test_a_second_record_with_the_same_address_is_held_like_the_first(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_client_profile: MakeClientProfile,
    make_expat_user: MakeExpatUser,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_email_suppression: MakeEmailSuppression,
) -> None:
    """Risk R1: two records of the agency can carry the same effective
    address (a column, or a linked account's); Lemlist would refuse the
    second as a duplicate lead. The address is read like the record: a live
    membership through it, and the agency's opposition list by its
    fingerprint."""
    agency = await make_agency()
    campaign = await make_email_campaign(agency_id=agency.id)
    # Live through a first, unlinked record.
    live_one = await make_client_profile(agency_id=agency.id, email="Shared@Example.com")
    live_member = await make_email_member(campaign_id=campaign.id, client_profile_id=live_one.id)
    # A second record, linked to an account with that address: its own
    # column says otherwise, the account's address is the effective one (N2).
    account = await make_expat_user(email="shared@example.com")
    linked = await make_client_profile(
        agency_id=agency.id, expat_user_id=account.id, email="old@example.com"
    )
    shared = effective_email(linked, account)
    assert shared == "shared@example.com"
    # Unsubscribed (then removed) through a third record, which listed its
    # address; a fourth has the same address typed differently.
    unsubscribed = await make_client_profile(agency_id=agency.id, email="gone@example.com")
    gone = await make_email_member(
        campaign_id=campaign.id,
        client_profile_id=unsubscribed.id,
        status="EMAIL_UNSUBSCRIBED",
        removed_at=NOW,
    )
    await make_email_suppression(
        agency_id=agency.id, email="gone@example.com", source_member_id=gone.id
    )
    twin = await make_client_profile(agency_id=agency.id, email=" Gone@Example.COM")
    # Another agency's record with the same address blocks nothing here.
    elsewhere = await make_agency()
    stranger = await make_client_profile(agency_id=elsewhere.id, email="clean@example.com")
    theirs = await make_email_member(
        campaign_id=(await make_email_campaign(agency_id=elsewhere.id)).id,
        client_profile_id=stranger.id,
        status="EMAIL_UNSUBSCRIBED",
    )
    await make_email_suppression(
        agency_id=elsewhere.id, email="clean@example.com", source_member_id=theirs.id
    )
    clean = await make_client_profile(agency_id=agency.id, email="clean@example.com")

    facts = await _facts(
        db_session,
        agency,
        (live_one, live_one.email),
        (linked, shared),
        (twin, twin.email),
        (clean, clean.email),
    )
    assert client_issues(linked.id, shared, facts) == [
        Issue(
            "IN_OTHER_CAMPAIGN",
            {"campaign_id": str(campaign.id), "via_client_profile_id": str(live_one.id)},
        )
    ]
    assert client_issues(twin.id, twin.email, facts) == [
        Issue("UNSUBSCRIBED", {"via_client_profile_id": str(unsubscribed.id)})
    ]
    assert client_issues(clean.id, clean.email, facts) == []
    # The live record's own membership is not held by its own address.
    assert membership_issues(live_member, live_one.email, facts) == []


async def test_an_address_on_the_opposition_list_blocks_whoever_carries_it(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_email_suppression: MakeEmailSuppression,
) -> None:
    """The opposition list (decision of 08/10/2026) is the source of truth
    for an address: whoever carries it now is blocked, with the reason as
    its code, even when the record it came from is gone (`source` NULL:
    no `via`). A person's own unsubscribe still blocks it once its address
    changed (rule N6 blocks the person); only the agency's list counts."""
    agency = await make_agency()
    campaign = await make_email_campaign(agency_id=agency.id)
    recreated = await make_client_profile(agency_id=agency.id, email="Gone@Example.com")
    bounced = await make_client_profile(agency_id=agency.id, email="bounced@example.com")
    moved_on = await make_client_profile(agency_id=agency.id, email="new@example.com")
    await make_email_member(
        campaign_id=campaign.id, client_profile_id=moved_on.id, status="EMAIL_UNSUBSCRIBED"
    )
    itself = await make_client_profile(agency_id=agency.id, email="itself@example.com")
    own = await make_email_member(
        campaign_id=campaign.id, client_profile_id=itself.id, status="EMAIL_BOUNCED"
    )
    # Listed through records deleted since, and one through itself.
    await make_email_suppression(agency_id=agency.id, email="gone@example.com")
    await make_email_suppression(agency_id=agency.id, email="bounced@example.com", reason="BOUNCED")
    await make_email_suppression(
        agency_id=agency.id,
        email="itself@example.com",
        reason="BOUNCED",
        source_member_id=own.id,
    )
    elsewhere = await make_agency()
    await make_email_suppression(agency_id=elsewhere.id, email="free@example.com")
    free = await make_client_profile(agency_id=agency.id, email="free@example.com")

    people = [(p, p.email) for p in (recreated, bounced, moved_on, itself, free)]
    facts = await _facts(db_session, agency, *people)
    assert client_issues(recreated.id, recreated.email, facts) == [Issue("UNSUBSCRIBED")]
    assert client_issues(bounced.id, bounced.email, facts) == [Issue("BOUNCED")]
    assert client_issues(moved_on.id, moved_on.email, facts) == [Issue("UNSUBSCRIBED")]
    assert client_issues(itself.id, itself.email, facts) == [Issue("BOUNCED")]
    assert client_issues(free.id, free.email, facts) == []
    assert durable_block(recreated.id, recreated.email, facts) == "EMAIL_UNSUBSCRIBED"
    assert durable_block(free.id, free.email, facts) is None


async def test_a_lead_not_launched_carries_its_own_state(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    agency = await make_agency()
    campaign = await make_email_campaign(agency_id=agency.id)
    paused_profile = await make_client_profile(agency_id=agency.id)
    retried_profile = await make_client_profile(agency_id=agency.id, email=None)
    paused = await make_email_member(
        campaign_id=campaign.id, client_profile_id=paused_profile.id, status="EMAIL_PAUSED"
    )
    retried = await make_email_member(
        campaign_id=campaign.id,
        client_profile_id=retried_profile.id,
        status_reason="provider_already_in_other_campaign",
    )
    facts = await _facts(
        db_session,
        agency,
        (paused_profile, paused_profile.email),
        (retried_profile, retried_profile.email),
    )
    assert membership_issues(paused, paused_profile.email, facts) == [Issue("PAUSED")]
    issues = membership_issues(retried, retried_profile.email, facts)
    assert issues == [
        Issue("NO_EMAIL"),
        Issue("LAST_ATTEMPT", {"reason": "provider_already_in_other_campaign"}, "warning"),
    ]
    assert [i.blocking for i in issues] == [True, False]
    assert not_launched(paused) and not_launched(retried)
    queued = EmailCampaignMember(status="EMAIL_QUEUED", queued_at=NOW, status_reason="x")
    assert not not_launched(queued)
    assert not not_launched(EmailCampaignMember(status="EMAIL_REPLIED"))


# ── the campaign ─────────────────────────────────────────────────────────


def _campaign(**overrides: Any) -> EmailCampaign:
    data: dict[str, Any] = {"name": "C", "status": "ACTIVE", "provider_metadata": {}, **overrides}
    return EmailCampaign(**data)


STEP = EmailCampaignStep(position=1, name="First")


@pytest.mark.parametrize(
    ("campaign", "steps", "external", "key_team", "launching", "codes"),
    [
        # Never synced: no team, nothing at Lemlist to hold.
        (_campaign(status="DRAFT"), [], False, None, True, ["NO_STEPS", "NOT_ACTIVE"]),
        (_campaign(status="DRAFT"), [STEP], False, None, False, []),
        (_campaign(status="PAUSED"), [STEP], False, None, True, ["NOT_ACTIVE"]),
        (_campaign(status="ARCHIVED"), [STEP], False, None, True, ["ARCHIVED"]),
        # At Lemlist, in the key's team, effects on: nothing holds it.
        (
            _campaign(provider_campaign_id="cam_1", provider_metadata={"team_id": TEAM_A}),
            [STEP],
            True,
            TEAM_A,
            True,
            [],
        ),
        # The key opens another team: read-only (N4), and the agency runs dry.
        (
            _campaign(provider_campaign_id="cam_1", provider_metadata={"team_id": TEAM_A}),
            [STEP],
            False,
            TEAM_B,
            True,
            ["TEAM_MISMATCH", "EXTERNAL_EFFECTS_OFF"],
        ),
        # A real campaign without its team: unknown is never the same team.
        (_campaign(provider_campaign_id="cam_1"), [STEP], True, TEAM_A, True, ["TEAM_MISMATCH"]),
        # Effects off: the key's team is not asked, the effects gate holds it.
        (
            _campaign(provider_campaign_id="cam_1", provider_metadata={"team_id": TEAM_A}),
            [STEP],
            False,
            None,
            False,
            ["EXTERNAL_EFFECTS_OFF"],
        ),
        # A dry-run campaign belongs to no team and never reached Lemlist.
        (_campaign(provider_campaign_id="dry_cam_1"), [STEP], False, TEAM_B, True, []),
    ],
)
def test_the_campaign_gates_in_the_sync_order(
    campaign: EmailCampaign,
    steps: list[EmailCampaignStep],
    external: bool,
    key_team: str | None,
    launching: bool,
    codes: list[str],
) -> None:
    gates = campaign_gates(
        campaign, steps, external=external, key_team_id=key_team, launching=launching
    )
    assert [g.code for g in gates] == codes
    assert all(g.blocking and g.params == {} for g in gates)


def test_team_mismatch_reads_the_campaign_stamp() -> None:
    stamped = _campaign(provider_campaign_id="cam_1", provider_metadata={"team_id": TEAM_A})
    assert not team_mismatch(stamped, TEAM_A)
    assert team_mismatch(stamped, TEAM_B)
    assert not team_mismatch(stamped, None)
    assert team_mismatch(_campaign(provider_campaign_id="cam_1", provider_metadata={}), None)
    assert not team_mismatch(_campaign(provider_campaign_id=None), TEAM_B)


def test_the_daily_limit_blocks_and_a_missing_sender_only_warns() -> None:
    assert daily_limit_gate(49, 50) is None
    assert daily_limit_gate(50, 50) == Issue(
        "DAILY_LIMIT", {"launched_today": 50, "daily_limit": 50}
    )
    assert sender_gate({"user_id": "usr_1", "email": "a@example.com"}) is None
    warning = sender_gate(None)
    assert warning == Issue("NO_SENDER", level="warning") and not warning.blocking


def test_the_daily_limit_day_starts_at_midnight_in_the_campaign_timezone() -> None:
    # 01:30 in Paris on the 7th is 23:30 UTC on the 6th: the day began at
    # 22:00 UTC on the 6th.
    now = datetime(2026, 10, 6, 23, 30, tzinfo=UTC)
    assert day_start("Europe/Paris", now) == datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
    assert day_start("UTC", now) == datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    assert day_start("Not/AZone", now) == datetime(2026, 10, 6, 0, 0, tzinfo=UTC)


# ── the templates ────────────────────────────────────────────────────────


def _step(position: int, subject: str, body: str, *ai_keys: str) -> EmailCampaignStep:
    return EmailCampaignStep(
        id=uuid.uuid4(),
        position=position,
        name=f"Step {position}",
        subject_template=subject,
        body_template=body,
        content_hash=f"hash-{position}",
        ai_variables=[EmailCampaignAiVariable(key=k, prompt_template="p") for k in ai_keys],
    )


def _context_for(keys: list[str]) -> RenderContext:
    client = ClientProfile(first_name="Ana", last_name="Lopez", email="ana@example.com")
    agency = Agency(name="Horizon", default_language="fr")
    return RenderContext(client=client, agency=agency, ai_keys=frozenset(keys))


def test_template_problems_are_structured_by_step_and_code() -> None:
    steps = [
        # No subject on the first email, and a company value for a client
        # with no company.
        _step(1, "", "Bonjour {{client.first_name}}, {{company.industry}}"),
        _step(2, "", "  "),
        _step(3, "", "{{ai.hook}} {{client.nickname}}", "hook"),
    ]
    check = template_issues(steps, {}, _context_for, ai_configured=True)
    assert check.errors == [
        TemplateError(1, "no_company"),
        TemplateError(1, "missing_variable", "company.industry"),
        TemplateError(1, "first_subject_required"),
        TemplateError(2, "empty_body"),
        TemplateError(3, "unknown_variable", "client.nickname"),
    ]
    assert [i.params for i in check.issues if i.code == "TEMPLATE"] == [
        {"position": 1, "kind": "no_company"},
        {"position": 1, "kind": "missing_variable", "token": "company.industry"},
        {"position": 1, "kind": "first_subject_required"},
        {"position": 2, "kind": "empty_body"},
        {"position": 3, "kind": "unknown_variable", "token": "client.nickname"},
    ]
    # An AI passage not generated yet is no error: it is written at launch.
    assert check.issues[-1] == Issue(AI_AT_LAUNCH, level="warning")
    assert (check.rendered, check.total) == (0, 3)


def test_sent_steps_are_skipped_and_a_frozen_render_counts() -> None:
    steps = [
        _step(1, "Hello", "Sent already: {{company.industry}}"),
        _step(2, "", "Next, {{ai.hook}}", "hook"),
    ]
    stored = {
        1: EmailMemberRender(
            step_position=1, campaign_step_id=steps[0].id, template_hash="old", sent_at=NOW
        ),
        2: EmailMemberRender(
            step_position=2,
            campaign_step_id=steps[1].id,
            template_hash=steps[1].content_hash,
            ai_values={"hook": {"value": "Quick question", "prompt_hash": "p"}},
        ),
    }
    check = template_issues(steps, stored, _context_for, ai_configured=True)
    # Step 1 is sent: its template is not judged again. Step 2 is frozen
    # with its AI value: counted as rendered, and nothing is pending.
    assert check.errors == [] and check.issues == []
    assert (check.rendered, check.total) == (1, 2)
    # Changed since it was frozen: no longer counted, and its AI is pending.
    steps[1].content_hash = "hash-2-edited"
    stale = template_issues(steps, stored, _context_for, ai_configured=True)
    assert stale.rendered == 0 and stale.issues == [Issue(AI_AT_LAUNCH, level="warning")]


def test_an_ai_passage_blocks_while_ai_is_not_configured() -> None:
    """Adjustment b (decision A14): while AI values cannot be generated, an
    unsent step whose subject or body uses `{{ai.*}}` blocks the client at
    the Launch page, as the Launch itself refuses it. A frozen value, a sent
    step, or an AI variable the templates do not use blocks nothing."""
    steps = [
        _step(1, "Hello", "Sent already, {{ai.opener}}", "opener"),
        _step(2, "", "Defined but unused here.", "unused"),
        _step(3, "{{ai.hook}}", "Next.", "hook"),
    ]
    sent = {
        1: EmailMemberRender(
            step_position=1, campaign_step_id=steps[0].id, template_hash="old", sent_at=NOW
        )
    }
    check = template_issues(steps, sent, _context_for, ai_configured=False)
    assert check.errors == []
    [issue] = check.issues
    assert issue == Issue(AI_AT_LAUNCH, {"reason": "ai_not_configured", "position": 3})
    assert issue.blocking
    # The same templates once AI is configured: a warning, never a block.
    configured = template_issues(steps, sent, _context_for, ai_configured=True)
    assert [(i.code, i.blocking) for i in configured.issues] == [(AI_AT_LAUNCH, False)]
    # Its value frozen (a preview): nothing to write, nothing blocks.
    frozen = {
        **sent,
        3: EmailMemberRender(
            step_position=3,
            campaign_step_id=steps[2].id,
            template_hash=steps[2].content_hash,
            ai_values={"hook": {"value": "Quick question", "prompt_hash": "p"}},
        ),
    }
    assert template_issues(steps, frozen, _context_for, ai_configured=False).issues == []
    # A variable defined on a step whose templates do not use it: nothing.
    unused_only = [steps[0], steps[1]]
    assert template_issues(unused_only, sent, _context_for, ai_configured=False).issues == []


def test_the_codes_are_the_specs_and_none_of_prisms_legacy() -> None:
    assert set(ISSUE_CODES) == {
        "NO_EMAIL",
        "INVALID_EMAIL",
        "DEMO_RECIPIENT",
        "UNSUBSCRIBED",
        "BOUNCED",
        "IN_OTHER_CAMPAIGN",
        "PAUSED",
        "LAST_ATTEMPT",
        "TEAM_MISMATCH",
        "ARCHIVED",
        "NO_STEPS",
        "EXTERNAL_EFFECTS_OFF",
        "NOT_ACTIVE",
        "DAILY_LIMIT",
        "NO_SENDER",
        "TEMPLATE",
        "AI_AT_LAUNCH",
    }
    assert len(ISSUE_CODES) == len(set(ISSUE_CODES))
