"""The money stays out of the activity journal for whoever lacks cost.view.

A dossier's costs and billed price are cost.view-gated everywhere they are
read (detail `billing` block, /costs, export columns) — and the journal
records the same facts: `cost.added/edited/deleted` carry amount + label,
`case.updated` carries the billed price old/new. Every agent-side reader of
that journal (the case journal, the client-profile feed, the export's
dossiers-activite.csv) must apply ONE rule (src/activity/cost_redaction.py):
without cost.view, no `cost.*` event, no billing-only `case.updated`, and the
billing keys stripped from a mixed one — with a `total` that counts what is
served. With cost.view, nothing changes.

Mutation witnesses (run by hand, recorded in the commit report): emptying the
SQL clause turns the case-journal, profile-feed and export tests red; dropping
the Python redaction turns them red too, as does the unit test below."""

import csv
import io
import json
import uuid
import zipfile
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.activity.cost_redaction import hidden_without_cost, redact_cost_details
from src.core.rbac.permissions import Permission
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.test_case_export_pdf import _pdf_text

pytestmark = pytest.mark.usefixtures("rbac_baseline")

# Distinctive money markers: none may surface for a reader without cost.view.
COST_LABEL = "COUTSECRETXYZ"
COST_AMOUNT = "987654.32"
COST_AMOUNT_EDITED = "111222.33"
BILLED_ONLY = "4321.09"
BILLED_MIXED = "5555.55"
MONEY_MARKERS = (COST_LABEL, COST_AMOUNT, COST_AMOUNT_EDITED, BILLED_ONLY, BILLED_MIXED)
COST_EVENTS = {"cost.added", "cost.edited", "cost.deleted"}


@pytest_asyncio.fixture
async def admin(
    make_agent: MakeAgent, system_roles: dict[str, Role], db_session: AsyncSession
) -> Agent:
    """Holds cost.view AND cost.manage (system admin role)."""
    agent = await make_agent(role=system_roles["admin"])
    await db_session.execute(
        update(Agency).where(Agency.id == agent.agency_id).values(currency="EUR")
    )
    await db_session.commit()
    return agent


async def _money_trail(client: AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    """A dossier (created through POST /cases, so it is linked to a client
    profile) whose journal holds every money-bearing event, next to a tag
    edit — a `case.updated` WITHOUT `changes`, the NULL trap of the clause.
    Returns (case_id, client email)."""
    email = f"argent-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/cases", headers=headers, json={"first_name": "Ana", "last_name": "Prix", "email": email}
    )
    assert r.status_code == 201, r.text
    case_id = r.json()["id"]
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Step"})
    r = await client.post(
        f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
    )
    assert r.status_code in (200, 201), r.text
    pid = r.json()[0]["id"]

    # cost.added / cost.edited / cost.deleted
    r = await client.post(
        f"/cases/{case_id}/steps/{pid}/costs",
        headers=headers,
        json={"amount": COST_AMOUNT, "label": COST_LABEL},
    )
    assert r.status_code == 201, r.text
    cost_id = r.json()["id"]
    r = await client.patch(
        f"/cases/{case_id}/costs/{cost_id}", headers=headers, json={"amount": COST_AMOUNT_EDITED}
    )
    assert r.status_code == 200, r.text
    r = await client.delete(f"/cases/{case_id}/costs/{cost_id}", headers=headers)
    assert r.status_code == 200, r.text

    # case.updated, billing ONLY (amount + currency, both first set)
    r = await client.patch(
        f"/cases/{case_id}", headers=headers, json={"billed_amount": BILLED_ONLY}
    )
    assert r.status_code == 200, r.text
    # case.updated, MIXED (price + an ordinary field)
    r = await client.patch(
        f"/cases/{case_id}",
        headers=headers,
        json={"billed_amount": BILLED_MIXED, "source": "salon"},
    )
    assert r.status_code == 200, r.text
    # case.updated WITHOUT `changes` (tags)
    r = await client.post(
        "/cases/bulk-action",
        headers=headers,
        json={"action": "add_tags", "case_ids": [case_id], "tags": ["vip"]},
    )
    assert r.status_code == 200, r.text
    return case_id, email


def _assert_blind(entries: list[tuple[str, dict[str, Any]]], raw: str) -> None:
    """What a reader WITHOUT cost.view gets: no cost event, no billing-only
    update, the mixed update without its billing keys, the tag update kept."""
    actions = [action for action, _ in entries]
    assert not COST_EVENTS & set(actions), actions
    assert not any(action.startswith("cost.") for action in actions)
    updates = [details for action, details in entries if action == "case.updated"]
    assert {"changes": {"source": {"old": None, "new": "salon"}}} in updates
    assert {"tags_added": ["vip"]} in updates
    assert len(updates) == 2, updates  # the billing-only entry is GONE
    for marker in MONEY_MARKERS:
        assert marker not in raw, marker
    assert "billed_" not in raw
    # Non-money events still flow (the clause drops nothing else).
    assert "case.created" in actions


def _assert_sighted(entries: list[tuple[str, dict[str, Any]]], raw: str) -> None:
    """What a reader WITH cost.view gets: everything, intact."""
    actions = [action for action, _ in entries]
    assert set(actions) >= COST_EVENTS, actions
    added = next(details for action, details in entries if action == "cost.added")
    assert (Decimal(added["amount"]), added["label"]) == (Decimal(COST_AMOUNT), COST_LABEL)
    updates = [details for action, details in entries if action == "case.updated"]
    assert len(updates) == 3, updates
    billing_only = next(d for d in updates if "changes" in d and "source" not in d["changes"])[
        "changes"
    ]
    assert set(billing_only) == {"billed_amount", "billed_currency"}
    assert Decimal(billing_only["billed_amount"]["new"]) == Decimal(BILLED_ONLY)
    mixed = next(d for d in updates if "source" in d.get("changes", {}))["changes"]
    assert set(mixed) == {"source", "billed_amount"}
    old, new = mixed["billed_amount"]["old"], mixed["billed_amount"]["new"]
    assert (Decimal(old), Decimal(new)) == (Decimal(BILLED_ONLY), Decimal(BILLED_MIXED))
    assert {"tags_added": ["vip"]} in updates
    for marker in MONEY_MARKERS:
        assert marker in raw, marker


async def _all_pages(client: AsyncClient, url: str, headers: dict[str, str]) -> list[dict]:
    """Walk a paginated journal by 2 — the pages must cover `total` exactly
    (a count and a page query that disagree would show here)."""
    items: list[dict] = []
    page = 1
    while True:
        r = await client.get(f"{url}?page={page}&page_size=2", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        items += body["items"]
        if not body["items"]:
            assert len(items) == body["total"]
            return items
        page += 1


async def test_case_journal_hides_money_without_cost_view(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: Any,
    agent_headers: AuthHeaders,
) -> None:
    case_id, _ = await _money_trail(client, agent_headers(admin))
    blind_role = await make_role(
        permissions=[Permission.CASE_VIEW, Permission.CASE_EDIT], agency_id=admin.agency_id
    )
    blind = await make_agent(agency_id=admin.agency_id, role=blind_role)
    url = f"/cases/{case_id}/activity"

    r = await client.get(f"{url}?page_size=100", headers=agent_headers(blind))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])  # the count matches what is served
    _assert_blind([(i["action_type"], i["details"]) for i in body["items"]], r.text)
    # Paginated: the pages tile `total` exactly.
    paged = await _all_pages(client, url, agent_headers(blind))
    assert [i["id"] for i in paged] == [i["id"] for i in body["items"]]
    # Asking for a cost event explicitly does not reopen it.
    r = await client.get(f"{url}?action_type=cost.added", headers=agent_headers(blind))
    assert r.json() == {"items": [], "total": 0, "page": 1, "page_size": 25}

    r = await client.get(f"{url}?page_size=100", headers=agent_headers(admin))
    sighted = r.json()
    assert sighted["total"] == len(sighted["items"])
    _assert_sighted([(i["action_type"], i["details"]) for i in sighted["items"]], r.text)
    # 3 cost events + the billing-only update are what the blind reader misses.
    assert sighted["total"] == body["total"] + 4


async def test_profile_feed_hides_money_without_cost_view(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: Any,
    agent_headers: AuthHeaders,
) -> None:
    """Same rule on the client-profile feed (the cross-read of its cases)."""
    _, email = await _money_trail(client, agent_headers(admin))
    listing = (
        await client.get(f"/client-profiles?search={email}", headers=agent_headers(admin))
    ).json()
    profile_id = listing["items"][0]["id"]
    blind_role = await make_role(permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id)
    blind = await make_agent(agency_id=admin.agency_id, role=blind_role)
    url = f"/client-profiles/{profile_id}/activity"

    r = await client.get(f"{url}?page_size=100", headers=agent_headers(blind))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])
    _assert_blind([(i["action_type"], i["details"]) for i in body["items"]], r.text)
    paged = await _all_pages(client, url, agent_headers(blind))
    assert [i["id"] for i in paged] == [i["id"] for i in body["items"]]

    r = await client.get(f"{url}?page_size=100", headers=agent_headers(admin))
    sighted = r.json()
    assert sighted["total"] == len(sighted["items"])
    _assert_sighted([(i["action_type"], i["details"]) for i in sighted["items"]], r.text)
    assert sighted["total"] == body["total"] + 4


def _activity_csv(content: bytes) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    archive = zipfile.ZipFile(io.BytesIO(content))
    raw = archive.read("dossiers-activite.csv").decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw)))[1:]
    # Columns: Dossier, Date, Acteur, Action, Détails (JSON).
    return [(row[3], json.loads(row[4]) if row[4] else {}) for row in rows], raw


async def test_export_journal_hides_money_without_cost_view(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: Any,
    agent_headers: AuthHeaders,
) -> None:
    """The export's dossiers-activite.csv: same gate as its billed columns."""
    await _money_trail(client, agent_headers(admin))
    manager_role = await make_role(
        permissions=[Permission.AGENCY_MANAGE], agency_id=admin.agency_id
    )
    manager = await make_agent(agency_id=admin.agency_id, role=manager_role)

    r = await client.get("/agencies/me/export", headers=agent_headers(manager))
    assert r.status_code == 200, r.text
    entries, raw = _activity_csv(r.content)
    _assert_blind(entries, raw)
    readme = zipfile.ZipFile(io.BytesIO(r.content)).read("LISEZ-MOI.txt").decode("utf-8")
    assert "journal d'activité" in readme

    r = await client.get("/agencies/me/export", headers=agent_headers(admin))
    assert r.status_code == 200, r.text
    entries, raw = _activity_csv(r.content)
    _assert_sighted(entries, raw)


def test_redaction_works_on_a_copy() -> None:
    """The redaction never touches its input (an ORM row's JSONB): a
    mutated row would be a dirty session, one flush away from rewriting the
    journal itself."""
    changes = {
        "source": {"old": None, "new": "salon"},
        "billed_amount": {"old": None, "new": "1"},
        "billed_currency": {"old": None, "new": "EUR"},
    }
    details = {"changes": changes}
    redacted = redact_cost_details("case.updated", details)
    assert redacted == {"changes": {"source": {"old": None, "new": "salon"}}}
    assert set(details["changes"]) == {"source", "billed_amount", "billed_currency"}
    assert details["changes"] is changes
    # Other events and change-less updates pass through unchanged.
    assert redact_cost_details("case.status_changed", {"old": "a", "new": "b"}) == {
        "old": "a",
        "new": "b",
    }
    assert redact_cost_details("case.updated", {"tags_added": ["vip"]}) == {"tags_added": ["vip"]}
    assert redact_cost_details("case.updated", None) == {}


async def test_case_pdf_lists_no_cost_event_without_cost_view(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: Any,
    agent_headers: AuthHeaders,
) -> None:
    """The case PDF never printed an amount, but it named the cost events
    (« Coût ajouté »…): without cost.view they stay out, as in the app."""
    case_id, _ = await _money_trail(client, agent_headers(admin))
    blind_role = await make_role(permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id)
    blind = await make_agent(agency_id=admin.agency_id, role=blind_role)

    fr = {"Accept-Language": "fr"}
    r = await client.get(f"/cases/{case_id}/export", headers={**agent_headers(blind), **fr})
    assert r.status_code == 200, r.text
    blind_text = _pdf_text(r.content)
    r = await client.get(f"/cases/{case_id}/export", headers={**agent_headers(admin), **fr})
    assert r.status_code == 200, r.text
    sighted_text = _pdf_text(r.content)

    for label in ("Coût ajouté", "Coût modifié", "Coût supprimé"):
        assert label in sighted_text, label
        assert label not in blind_text, label
    # Billing-only update gone, the mixed one and the tag one kept.
    assert sighted_text.count("Dossier modifié") == 3
    assert blind_text.count("Dossier modifié") == 2


def test_the_python_twin_matches_the_clause() -> None:
    assert hidden_without_cost("cost.added", {"amount": "1"})
    assert hidden_without_cost("case.updated", {"changes": {"billed_amount": {}}})
    assert hidden_without_cost(
        "case.updated", {"changes": {"billed_amount": {}, "billed_currency": {}}}
    )
    assert not hidden_without_cost("case.updated", {"changes": {"billed_amount": {}, "source": {}}})
    assert not hidden_without_cost("case.updated", {"changes": {}})
    assert not hidden_without_cost("case.updated", {"tags_added": ["vip"]})
    assert not hidden_without_cost("case.updated", None)
    assert not hidden_without_cost("case.status_changed", {"old": "a", "new": "b"})
