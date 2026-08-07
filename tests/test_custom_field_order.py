"""D12 — PUT /agencies/me/custom-fields/order : l'ordre entier, réécrit
en une transaction.

Ce fichier grave : la réécriture 1..N et sa réponse (l'ordre servi EST
l'ordre gravé), les TROIS 422 nommés (doublon / étranger-inconnu /
absent) avec l'ordre INTACT derrière chacun (la transaction), le rejeu
inerte (idempotence), le tie-breaker stable (deux lectures ne rendent
jamais deux ordres, même à égalité parfaite de position et de
created_at), les archivées repoussées après N, et le gate field.manage.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

FIELDS = "/agencies/me/custom-fields"
ORDER = "/agencies/me/custom-fields/order"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _define(client: AsyncClient, headers: dict[str, str], key: str) -> str:
    r = await client.post(
        FIELDS,
        headers=headers,
        json={"key": key, "label": key, "field_type": "text", "scope": "person"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _served_ids(client: AsyncClient, headers: dict[str, str]) -> list[str]:
    r = await client.get(FIELDS, headers=headers)
    assert r.status_code == 200, r.text
    return [d["id"] for d in r.json()]


async def test_the_full_rewrite_renumbers_one_to_n_and_serves_the_new_order(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Le geste : la liste envoyée devient l'ordre servi, positions 1..N
    uniques — et la RÉPONSE du PUT est déjà cet ordre (pas un second
    aller-retour pour savoir ce que la base a écrit)."""
    headers = agent_headers(admin)
    ids = [await _define(client, headers, f"champ_{i}") for i in range(4)]
    reversed_ids = list(reversed(ids))

    r = await client.put(ORDER, headers=headers, json={"field_ids": reversed_ids})
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()] == reversed_ids
    assert [d["position"] for d in r.json()] == [1, 2, 3, 4]
    assert await _served_ids(client, headers) == reversed_ids


async def test_the_three_422_name_the_offenders_and_leave_the_order_intact(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Les trois refus, chacun NOMMÉ, et derrière chacun l'ordre n'a pas
    bougé — la validation précède toute écriture (la transaction du
    signalement)."""
    headers = agent_headers(admin)
    ids = [await _define(client, headers, f"refus_{i}") for i in range(3)]
    before = await _served_ids(client, headers)

    # 1. doublon
    r = await client.put(
        ORDER, headers=headers, json={"field_ids": [ids[0], ids[0], ids[1], ids[2]]}
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "custom_field.order_duplicate"
    assert r.json()["params"]["duplicates"] == [ids[0]]

    # 2. étranger/inconnu — même mot pour les deux : l'existence d'une
    #    définition d'une autre agence n'est jamais confirmée, même en creux.
    stranger = str(uuid.uuid4())
    r = await client.put(ORDER, headers=headers, json={"field_ids": [*ids, stranger]})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "custom_field.order_unknown"
    assert r.json()["params"]["unknown"] == [stranger]

    # 3. absent — pas de réécriture partielle qui recréerait des trous.
    r = await client.put(ORDER, headers=headers, json={"field_ids": ids[:2]})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "custom_field.order_missing"
    assert r.json()["params"]["missing"] == [ids[2]]

    assert await _served_ids(client, headers) == before, "l'ordre est INTACT après les trois"


async def test_replaying_the_same_list_changes_nothing(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'idempotence, au sens fort : rejouer la même liste ne touche
    AUCUNE ligne (updated_at compris — le garde IS DISTINCT FROM)."""
    headers = agent_headers(admin)
    ids = [await _define(client, headers, f"idem_{i}") for i in range(3)]
    assert (await client.put(ORDER, headers=headers, json={"field_ids": ids})).status_code == 200

    stamps_before = {
        str(row_id): updated
        for row_id, updated in (
            await db_session.execute(
                text("SELECT id, updated_at FROM custom_field_definition WHERE agency_id = :a"),
                {"a": admin.agency_id},
            )
        ).all()
    }
    r = await client.put(ORDER, headers=headers, json={"field_ids": ids})
    assert r.status_code == 200, r.text
    assert await _served_ids(client, headers) == ids
    stamps_after = {
        str(row_id): updated
        for row_id, updated in (
            await db_session.execute(
                text("SELECT id, updated_at FROM custom_field_definition WHERE agency_id = :a"),
                {"a": admin.agency_id},
            )
        ).all()
    }
    assert stamps_after == stamps_before, "le rejeu n'a touché aucune ligne"


async def test_perfect_ties_are_served_in_one_stable_order(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE DÉFAUT D'ORIGINE, rejoué : deux définitions à égalité PARFAITE
    (position ET created_at à la microseconde — secondary_phone/website en
    dev). Le tie-breaker `id` fait que N lectures rendent UN ordre."""
    headers = agent_headers(admin)
    ids = [await _define(client, headers, f"tie_{i}") for i in range(2)]
    now = datetime.now(UTC)
    await db_session.execute(
        text(
            "UPDATE custom_field_definition SET position = 7, created_at = :now"
            " WHERE id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"now": now, "ids": ids},
    )
    await db_session.commit()

    first = await _served_ids(client, headers)
    for _ in range(3):
        assert await _served_ids(client, headers) == first, "deux lectures, deux ordres"
    assert first == sorted(ids), "le dernier ressort est l'id"


async def test_archived_definitions_are_out_of_the_set_and_pushed_after_n(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Les archivées ne sont pas dans l'ensemble exigé (l'écran ne les
    montre pas) — et le PUT les repousse APRÈS N : l'unicité tient dans
    toute la table, une ressuscitée réapparaît en fin de liste."""
    headers = agent_headers(admin)
    ids = [await _define(client, headers, f"arch_{i}") for i in range(3)]
    r = await client.post(f"{FIELDS}/{ids[1]}/archive", headers=headers)
    assert r.status_code == 200, r.text

    live = [ids[2], ids[0]]
    r = await client.put(ORDER, headers=headers, json={"field_ids": live})
    assert r.status_code == 200, r.text

    positions = {
        str(row_id): pos
        for row_id, pos in (
            await db_session.execute(
                text("SELECT id, position FROM custom_field_definition WHERE agency_id = :a"),
                {"a": admin.agency_id},
            )
        ).all()
    }
    assert positions[ids[2]] == 1 and positions[ids[0]] == 2
    assert positions[ids[1]] == 3, "l'archivée est après N"

    r = await client.post(f"{FIELDS}/{ids[1]}/unarchive", headers=headers)
    assert r.status_code == 200, r.text
    assert await _served_ids(client, headers) == [ids[2], ids[0], ids[1]], (
        "la ressuscitée réapparaît en fin de liste"
    )


async def test_reordering_is_gated_by_field_manage(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    ids = [await _define(client, headers, "gate_0")]
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    r = await client.put(ORDER, headers=agent_headers(member), json={"field_ids": ids})
    assert r.status_code == 403, r.text
