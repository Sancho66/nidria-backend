"""Signalement prod (Nicolas, Domiciliation Bulgarie, 05/08) : « les
sociétés rattachées à une fiche ne sont visibles que par le propriétaire
de l'agence ». LA RÈGLE GRAVÉE ICI : la visibilité des sociétés et des
liens personne↔société est une affaire d'AGENCE, jamais de rôle ni de
créateur. Un membre habilité (case.view) voit exactement ce que voit
l'admin — mêmes sociétés, mêmes liens, à l'octet près.

Les trois surfaces du signalement sont comparées sur LA MÊME donnée :
« Ses sociétés » de la fiche personne, la liste de l'annuaire société,
et le détail d'une société.
"""

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def crew(
    make_agency: MakeAgency, make_agent: MakeAgent, system_roles: dict[str, Role]
) -> dict[str, Agent]:
    """Une agence, quatre rôles système sur LA MÊME donnée."""
    agency = await make_agency()
    return {
        name: await make_agent(agency_id=agency.id, role=system_roles[name])
        for name in ("admin", "case_manager", "member", "viewer")
    }


async def test_linked_companies_are_visible_to_every_role_of_the_agency(
    client: AsyncClient, crew: dict[str, Agent], agent_headers: AuthHeaders
) -> None:
    admin_h = agent_headers(crew["admin"])

    # L'admin pose la donnée du signalement : une personne, deux sociétés.
    r = await client.post(
        "/client-profiles",
        headers=admin_h,
        json={"first_name": "Adrien", "last_name": "TROUBAT"},
    )
    assert r.status_code == 201, r.text
    person_id = r.json()["id"]

    company_ids = []
    for name in ("TRADE BRIDGE", "OLAGIU INTERNATIONAL HOLDING"):
        r = await client.post("/company-profiles", headers=admin_h, json={"name": name})
        assert r.status_code == 201, r.text
        company_ids.append(r.json()["id"])
        r = await client.post(
            f"/company-profiles/{company_ids[-1]}/roles",
            headers=admin_h,
            json={
                "client_profile_id": person_id,
                "role": "manager",
                "role_label": "Gerant associe unique",
            },
        )
        assert r.status_code in (200, 201), r.text

    def snapshot(payload: dict[str, Any]) -> list[tuple[str, str, str | None, str | None]]:
        return sorted(
            (c["company_id"], c["name"], c.get("role"), c.get("role_label"))
            for c in payload["companies"]
        )

    r = await client.get(f"/client-profiles/{person_id}", headers=admin_h)
    assert r.status_code == 200, r.text
    reference = snapshot(r.json())
    assert len(reference) == 2, reference

    r = await client.get("/company-profiles", headers=admin_h)
    assert r.status_code == 200, r.text
    reference_list = sorted(c["id"] for c in r.json()["items"])

    # LA COMPARAISON : chaque rôle non-propriétaire, même donnée.
    for role_name in ("case_manager", "member", "viewer"):
        headers = agent_headers(crew[role_name])

        # 1. « Ses sociétés » sur la fiche personne.
        r = await client.get(f"/client-profiles/{person_id}", headers=headers)
        assert r.status_code == 200, f"{role_name} : {r.status_code} {r.text}"
        assert snapshot(r.json()) == reference, f"{role_name} ne voit pas les mêmes sociétés"

        # 2. La vue Sociétés de l'annuaire.
        r = await client.get("/company-profiles", headers=headers)
        assert r.status_code == 200, f"{role_name} : {r.status_code} {r.text}"
        assert sorted(c["id"] for c in r.json()["items"]) == reference_list, (
            f"{role_name} : annuaire société divergent"
        )

        # 3. Le détail d'une société.
        r = await client.get(f"/company-profiles/{company_ids[0]}", headers=headers)
        assert r.status_code == 200, f"{role_name} : {r.status_code} {r.text}"
        assert r.json()["id"] == company_ids[0]
