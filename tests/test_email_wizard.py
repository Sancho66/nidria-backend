"""The add wizard (routes 34 to 37) and a client record's email state (route
33): the Clients and Companies pages' own lists, the preview's verdict row
by row, an add that sends nothing and never adds twice.

Ported from Prism's `tests/test_email_campaigns.py` (reference 629a2b0), the
wizard's part, rewritten on Nidria's list predicates (decision A15): the
wizard's candidates are compared with what `GET /client-profiles` and
`GET /company-profiles` list for the very same parameters. Nidria's own: a
demo address (rule N11), an unsubscribe never overridden (rule N6), nobody
moved from the wizard (decision A10), an address deduplicated within one
selection (risk R1).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.company_profile import CompanyProfileRole
from shared.models.email_campaign import EmailCampaignMember
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import EmailWorld, MakeCompanyLink, MakeEmailMember
from tests.plugins.expat_plugin import MakeExpatUser
from tests.test_email_campaigns import campaign_via_api, members_of, set_status, step_via_api
from tests.test_email_launch import DEMO, add_via_api

pytestmark = pytest.mark.usefixtures("rbac_baseline")


async def _preview(
    client: AsyncClient, headers: dict[str, str], cid: str, **body: Any
) -> dict[str, Any]:
    payload = {k: [str(i) for i in v] for k, v in body.items()}
    response = await client.post(
        f"/email/campaigns/{cid}/members/preview-add", json=payload, headers=headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _ids(
    client: AsyncClient, headers: dict[str, str], path: str, params: dict[str, Any]
) -> list[str]:
    response = await client.get(path, params=params, headers=headers)
    assert response.status_code == 200, (path, params, response.text)
    return [item["id"] for item in response.json()["items"]]


# ── who will be added ────────────────────────────────────────────────────────


async def test_the_wizard_says_who_will_be_added_and_never_adds_twice(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
    make_email_member: MakeEmailMember,
) -> None:
    agency, headers = email_world.agency_id, email_world.headers
    ana = await make_client_profile(agency_id=agency, first_name="Ana", email="ana@acme.io")
    ben = await make_client_profile(agency_id=agency, first_name="Ben", email="ben@acme.io")
    nomail = await make_client_profile(agency_id=agency, first_name="Nomail", email=None)
    acme = await make_company_link(client_profile_id=ana.id, agency_id=agency, name="Acme")
    for person, role in ((ben, "manager"), (ben, "shareholder"), (nomail, "manager")):
        db_session.add(
            CompanyProfileRole(company_profile_id=acme.id, client_profile_id=person.id, role=role)
        )
    await db_session.commit()
    busy = await make_client_profile(agency_id=agency, first_name="Busy", email="b@x.io")
    gone = await make_client_profile(agency_id=agency, first_name="Gone", email="g@x.io")
    demo = await make_client_profile(agency_id=agency, first_name="Demo", email=DEMO)
    foreign = await make_client_profile(
        agency_id=email_world.other_agency_id, first_name="Jean", email="j@d.fr"
    )
    foreign_company = await make_company_link(
        client_profile_id=foreign.id, agency_id=email_world.other_agency_id, name="Elsewhere"
    )
    other = (await campaign_via_api(client, headers, "Other"))["id"]
    await add_via_api(client, headers, other, client_profile_ids=[busy.id])
    await make_email_member(
        campaign_id=uuid.UUID(other),
        client_profile_id=gone.id,
        agency_id=agency,
        status="EMAIL_UNSUBSCRIBED",
    )

    target = (await campaign_via_api(client, headers, "Target"))["id"]
    companies = (
        await client.get("/email/candidates/companies", params={"search": "Acme"}, headers=headers)
    ).json()["items"]
    # A client counts once per company, whatever its roles there.
    assert [
        (c["name"], c["clients_count"], c["eligible_clients_count"], c["ready_clients_count"])
        for c in companies
    ] == [("Acme", 3, 2, 2)]

    # Another agency's client or company is refused outright, never skipped.
    for foreign_body, code in (
        ({"client_profile_ids": [str(foreign.id)]}, "profile.not_found"),
        ({"company_ids": [str(foreign_company.id)]}, "company_profile.not_found"),
    ):
        for action in ("preview-add", "add"):
            refused = await client.post(
                f"/email/campaigns/{target}/members/{action}", json=foreign_body, headers=headers
            )
            assert refused.status_code == 404, refused.text
            assert refused.json()["code"] == code
    assert await members_of(db_session, target) == []

    selection = {"company_ids": [acme.id], "client_profile_ids": [busy.id, gone.id, demo.id]}
    preview = await _preview(client, headers, target, **selection)
    assert preview["summary"] == {
        "selected": 5,
        "ready": 2,
        "no_email": 0,  # a company brings only its clients with an email
        "invalid_email": 0,
        "demo_recipient": 1,
        "unsubscribed": 1,
        "bounced": 0,
        "in_other_campaign": 1,
        "already_in_campaign": 0,
        "duplicate_email": 0,
        "will_add": 2,
        "will_skip": 3,
    }
    rows = {r["first_name"]: r for r in preview["rows"]}
    assert [r["first_name"] for r in preview["rows"]] == ["Busy", "Gone", "Demo", "Ana", "Ben"]
    assert (rows["Busy"]["eligibility"], rows["Busy"]["action"]) == ("IN_OTHER_CAMPAIGN", "SKIP")
    assert (rows["Busy"]["current_campaign_id"], rows["Busy"]["current_campaign_name"]) == (
        other,
        "Other",
    )
    assert rows["Gone"]["eligibility"] == "UNSUBSCRIBED"
    assert rows["Demo"]["eligibility"] == "DEMO_RECIPIENT"
    assert rows["Ana"]["company_name"] == "Acme" and rows["Ana"]["action"] == "ADD"
    assert "Nomail" not in rows

    # A row taken out by hand: here one of the company's clients.
    trimmed = await _preview(
        client, headers, target, **selection, exclude_client_profile_ids=[ben.id]
    )
    assert trimmed["summary"]["will_add"] == 1

    added = await add_via_api(client, headers, target, **selection)
    assert (added["added"], added["skipped"]) == (2, 3)
    members = await members_of(db_session, target)
    # Ready, never sent: adding hands nothing over.
    assert {(m["client_profile_id"], m["status"]) for m in members} == {
        (ana.id, "EMAIL_READY"),
        (ben.id, "EMAIL_READY"),
    }
    assert all(m["added_by_agent_id"] == email_world.admin_id for m in members)
    assert all(m["provider_lead_id"] is None for m in members)
    # Nobody is moved from the wizard (decision A10): Busy stays where he is.
    in_other = {m["client_profile_id"]: m["status"] for m in await members_of(db_session, other)}
    assert in_other == {busy.id: "EMAIL_READY", gone.id: "EMAIL_UNSUBSCRIBED"}

    twice = await add_via_api(client, headers, target, client_profile_ids=[ana.id])
    assert twice["added"] == 0 and twice["rows"][0]["eligibility"] == "ALREADY_IN_CAMPAIGN"
    nomail_row = await add_via_api(client, headers, target, client_profile_ids=[nomail.id])
    assert nomail_row["rows"][0]["eligibility"] == "NO_EMAIL"
    # An unsubscribe is never added, whatever the request says (rule N6).
    forced = await client.post(
        f"/email/campaigns/{target}/members/add",
        json={"client_profile_ids": [str(gone.id)], "include_unsubscribed": True},
        headers=headers,
    )
    assert forced.status_code == 422  # the option does not exist

    # The database refuses a second live membership whatever the code does.
    db_session.add(
        EmailCampaignMember(
            campaign_id=uuid.UUID(other),
            client_profile_id=ana.id,
            agency_id=agency,
            status="EMAIL_READY",
        )
    )
    with pytest.raises(DBAPIError):
        await db_session.commit()
    await db_session.rollback()


async def test_an_address_is_added_once_per_selection_and_campaign(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
) -> None:
    """Lemlist holds one lead per address in a team (risk R1): two records
    carrying the same effective address are never both added."""
    agency, headers = email_world.agency_id, email_world.headers
    first = await make_client_profile(agency_id=agency, first_name="First", email="Same@acme.io")
    second = await make_client_profile(agency_id=agency, first_name="Second", email="same@acme.io")
    cid = (await campaign_via_api(client, headers))["id"]
    preview = await _preview(client, headers, cid, client_profile_ids=[first.id, second.id])
    rows = {r["first_name"]: r for r in preview["rows"]}
    assert (rows["First"]["eligibility"], rows["First"]["action"]) == ("READY", "ADD")
    assert (rows["Second"]["eligibility"], rows["Second"]["via_client_profile_id"]) == (
        "DUPLICATE_EMAIL",
        str(first.id),
    )
    assert preview["summary"]["duplicate_email"] == 1
    added = await add_via_api(client, headers, cid, client_profile_ids=[first.id, second.id])
    assert added["added"] == 1
    # Once the first is in the campaign, the second is held by its address.
    again = await _preview(client, headers, cid, client_profile_ids=[second.id])
    [row] = again["rows"]
    assert (row["eligibility"], row["via_client_profile_id"], row["current_campaign_id"]) == (
        "IN_OTHER_CAMPAIGN",
        str(first.id),
        cid,
    )


async def test_an_archived_campaign_takes_nobody_and_a_reader_adds_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
) -> None:
    ana = await make_client_profile(agency_id=email_world.agency_id, email="ana@acme.io")
    cid = (await campaign_via_api(client, email_world.headers))["id"]
    await step_via_api(client, email_world.headers, cid)
    await set_status(client, email_world.headers, cid, "ARCHIVED")
    preview = await _preview(client, email_world.headers, cid, client_profile_ids=[ana.id])
    assert preview["summary"]["will_add"] == 1
    refused = await client.post(
        f"/email/campaigns/{cid}/members/add",
        json={"client_profile_ids": [str(ana.id)]},
        headers=email_world.headers,
    )
    assert (refused.status_code, refused.json()["code"]) == (400, "email.campaign_archived")
    assert await members_of(db_session, cid) == []


# ── the Clients and Companies pages' lists ───────────────────────────────────


async def test_the_wizard_lists_clients_exactly_as_the_clients_page_does(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    agency, headers = email_world.agency_id, email_world.headers
    ana = await make_client_profile(
        agency_id=agency, first_name="Ana", last_name="Alpha", email="ana@x.io", tags=["vip"]
    )
    ben = await make_client_profile(
        agency_id=agency,
        first_name="Ben",
        last_name="Beta",
        email="ben@x.io",
        tags=["lead"],
        status_override="client",
    )
    cleo = await make_client_profile(
        agency_id=agency, first_name="Cleo", last_name="Gamma", email=None
    )
    account = await make_expat_user(first_name="Dan", last_name="Delta", email="dan.account@x.io")
    dan = await make_client_profile(
        agency_id=agency,
        first_name="Daniel",
        last_name="Old",
        email="dan.record@x.io",
        expat_user_id=account.id,
    )
    await make_client_case(agency_id=agency, principal_expat_user_id=account.id)
    await make_client_profile(
        agency_id=email_world.other_agency_id, first_name="Ana", email="ana@elsewhere.io"
    )

    # The Clients page's own parameters, applied by its own predicates: the
    # same clients, in the same order, on the same page.
    for params in (
        {},
        {"search": "dan.account"},  # the effective address (rule N2)
        {"search": "delta"},
        {"status": "client"},
        {"status": "prospect"},
        {"tags": ["vip", "lead"]},
        {"client_space_activated": "true"},
        {"client_space_activated": "false"},
        {"has_active_case": "true"},
        {"sort_by": "created_at", "sort_order": "desc"},
        {"sort_by": "last_activity"},
        {"page": 2, "page_size": 1},
    ):
        page = await _ids(client, headers, "/client-profiles", params)
        wizard = await _ids(
            client, headers, "/email/candidates/client-profiles", {**params, "has_email": "any"}
        )
        assert wizard == page, params
    assert len(await _ids(client, headers, "/client-profiles", {})) == 4

    # The wizard's own: only clients with an email by default.
    default = await _ids(client, headers, "/email/candidates/client-profiles", {})
    assert default == [str(ana.id), str(ben.id), str(dan.id)]
    no_email = await _ids(
        client, headers, "/email/candidates/client-profiles", {"has_email": "false"}
    )
    assert no_email == [str(cleo.id)]
    statuses = await _ids(
        client, headers, "/email/candidates/client-profiles", {"client_status": ["client"]}
    )
    assert statuses == [str(ben.id), str(dan.id)]
    both = await _ids(
        client,
        headers,
        "/email/candidates/client-profiles",
        {"client_status": ["client", "prospect"]},
    )
    assert both == default

    cid = (await campaign_via_api(client, headers))["id"]
    await add_via_api(client, headers, cid, client_profile_ids=[ana.id])
    in_campaign = await _ids(
        client, headers, "/email/candidates/client-profiles", {"campaign_id": cid}
    )
    assert in_campaign == [str(ana.id)]
    nowhere = await _ids(
        client, headers, "/email/candidates/client-profiles", {"campaign_id": "none"}
    )
    assert nowhere == [str(ben.id), str(dan.id)]
    bad = await client.get(
        "/email/candidates/client-profiles", params={"campaign_id": "nope"}, headers=headers
    )
    assert bad.status_code == 422

    items = (await client.get("/email/candidates/client-profiles", headers=headers)).json()["items"]
    shown = {item["id"]: item for item in items}
    assert shown[str(ana.id)]["email_status"] == "EMAIL_READY"
    assert shown[str(ana.id)]["current_campaign_name"] == "Founders"
    # A linked record shows its account's identity and address (rule N2).
    assert (shown[str(dan.id)]["full_name"], shown[str(dan.id)]["email"]) == (
        "Dan Delta",
        "dan.account@x.io",
    )
    assert shown[str(dan.id)]["client_status"] == "client"
    assert shown[str(ben.id)]["client_status"] == "client"
    assert shown[str(ana.id)]["client_status"] == "prospect"


async def test_the_wizard_lists_companies_as_the_companies_page_does_with_their_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
    make_email_member: MakeEmailMember,
) -> None:
    agency, headers = email_world.agency_id, email_world.headers
    ana = await make_client_profile(agency_id=agency, first_name="Ana", email="ana@x.io")
    bob = await make_client_profile(agency_id=agency, first_name="Bob", email=None)
    gil = await make_client_profile(agency_id=agency, first_name="Gil", email="gil@x.io")
    hal = await make_client_profile(agency_id=agency, first_name="Hal", email="hal@x.io")
    await make_company_link(client_profile_id=ana.id, agency_id=agency, name="Alpha", tags=["vip"])
    await make_company_link(client_profile_id=bob.id, agency_id=agency, name="Beta")
    gamma = await make_company_link(client_profile_id=gil.id, agency_id=agency, name="Gamma")
    db_session.add(
        CompanyProfileRole(company_profile_id=gamma.id, client_profile_id=hal.id, role="manager")
    )
    await db_session.commit()
    live = (await campaign_via_api(client, headers, "Live"))["id"]
    await add_via_api(client, headers, live, client_profile_ids=[gil.id])
    await make_email_member(
        campaign_id=uuid.UUID(live),
        client_profile_id=hal.id,
        agency_id=agency,
        status="EMAIL_BOUNCED",
    )

    # The Companies page's parameters, applied by its own predicates.
    for params in (
        {},
        {"search": "alp"},
        {"tags": ["vip"]},
        {"has_people": "true"},
        {"sort_by": "name", "sort_order": "desc"},
        {"sort_by": "created_at"},
        {"page": 2, "page_size": 1},
    ):
        page = await _ids(client, headers, "/company-profiles", params)
        wizard = await _ids(
            client, headers, "/email/candidates/companies", {**params, "with_email": "false"}
        )
        assert wizard == page, params

    listed = (
        await client.get(
            "/email/candidates/companies", params={"with_email": "false"}, headers=headers
        )
    ).json()["items"]
    counts = {
        c["name"]: (
            c["clients_count"],
            c["eligible_clients_count"],
            c["ready_clients_count"],
            c["email_status"],
        )
        for c in listed
    }
    assert counts == {
        "Alpha": (1, 1, 1, None),
        "Beta": (1, 0, 0, None),
        # Gil is live elsewhere (eligible, not ready); Hal bounced (neither).
        "Gamma": (2, 1, 0, "EMAIL_READY"),
    }
    # By default only companies with a client that has an email.
    default = (await client.get("/email/candidates/companies", headers=headers)).json()
    assert [c["name"] for c in default["items"]] == ["Alpha", "Gamma"] and default["total"] == 2
    ready = (
        await client.get(
            "/email/candidates/companies", params={"only_with_ready": "true"}, headers=headers
        )
    ).json()["items"]
    assert [c["name"] for c in ready] == ["Alpha"]
    # Adding a company brings its clients with an email: Gil is live in
    # another campaign, Hal held by his bounce.
    following = (await campaign_via_api(client, headers, "Next"))["id"]
    plan = await _preview(client, headers, following, company_ids=[gamma.id])
    assert {(r["first_name"], r["eligibility"]) for r in plan["rows"]} == {
        ("Gil", "IN_OTHER_CAMPAIGN"),
        ("Hal", "BOUNCED"),
    }


async def test_the_wizard_can_list_only_companies_with_an_email(
    client: AsyncClient,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
) -> None:
    agency = email_world.agency_id
    mail = await make_client_profile(agency_id=agency, first_name="Ana", email="ana@mail.io")
    nomail = await make_client_profile(agency_id=agency, first_name="Bob", email=None)
    await make_company_link(client_profile_id=mail.id, agency_id=agency, name="Ana Mail Corp")
    await make_company_link(client_profile_id=nomail.id, agency_id=agency, name="Bob Nomail Corp")

    async def names(**params: Any) -> list[str]:
        response = await client.get(
            "/email/candidates/companies",
            params={"search": "Corp", **params},
            headers=email_world.headers,
        )
        assert response.status_code == 200, response.text
        return sorted(item["name"] for item in response.json()["items"])

    assert await names(with_email="false") == ["Ana Mail Corp", "Bob Nomail Corp"]
    assert await names() == ["Ana Mail Corp"]


# ── a client record's email state (route 33) ─────────────────────────────────


async def test_a_client_records_email_state_is_derived_from_its_memberships(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> None:
    agency, headers = email_world.agency_id, email_world.headers
    campaigns = [
        (await campaign_via_api(client, headers, name))["id"] for name in ("One", "Two", "Three")
    ]

    async def state(profile_id: uuid.UUID) -> dict[str, Any]:
        response = await client.get(f"/email/client-profiles/{profile_id}", headers=headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def person(*statuses: tuple[int, str, bool]) -> uuid.UUID:
        profile = await make_client_profile(agency_id=agency, email=f"{uuid.uuid4().hex}@x.io")
        for index, status, removed in statuses:
            await make_email_member(
                campaign_id=uuid.UUID(campaigns[index]),
                client_profile_id=profile.id,
                agency_id=agency,
                status=status,
                removed_at=datetime.now(UTC) if removed else None,
            )
        return profile.id

    # A reply anywhere wins over a live membership elsewhere.
    replied = await person((0, "EMAIL_REPLIED", False), (1, "EMAIL_READY", False))
    shown = await state(replied)
    assert shown["email_status"] == "EMAIL_REPLIED" and shown["client_status"] == "prospect"
    assert {m["campaign_name"] for m in shown["memberships"]} == {"One", "Two"}
    ready = next(m for m in shown["memberships"] if m["status"] == "EMAIL_READY")
    assert ready["issues"] == []  # still to launch: nothing holds it back
    # Queued or active: in progress, above a pause or a completion.
    assert (await state(await person((0, "EMAIL_COMPLETED", False), (1, "EMAIL_ACTIVE", False))))[
        "email_status"
    ] == "EMAIL_IN_PROGRESS"
    assert (await state(await person((0, "EMAIL_COMPLETED", False), (1, "EMAIL_PAUSED", False))))[
        "email_status"
    ] == "EMAIL_PAUSED"
    # A removed membership is ignored; a removed bounce keeps blocking (A1).
    assert (await state(await person((0, "EMAIL_REMOVED", True))))["email_status"] is None
    assert (await state(await person((0, "EMAIL_REMOVED", True), (1, "EMAIL_BOUNCED", True))))[
        "email_status"
    ] == "EMAIL_BOUNCED"
    assert (
        await state(await person((1, "EMAIL_BOUNCED", False), (2, "EMAIL_UNSUBSCRIBED", False)))
    )["email_status"] == "EMAIL_BOUNCED"
    # Never in a campaign: no email state, its client status still.
    lonely = await make_client_profile(agency_id=agency, status_override="client")
    shown = await state(lonely.id)
    assert (shown["email_status"], shown["client_status"], shown["memberships"]) == (
        None,
        "client",
        [],
    )
    # Another agency's record is not found.
    foreign = await make_client_profile(agency_id=email_world.other_agency_id)
    response = await client.get(f"/email/client-profiles/{foreign.id}", headers=headers)
    assert (response.status_code, response.json()["code"]) == (404, "profile.not_found")
