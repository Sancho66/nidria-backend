"""LE TÉMOIN DU N+1 — un budget d'allers-retours SQL, sur le fichier RÉEL.

Le N+1 de l'import est revenu deux fois. La mesure ponctuelle ne l'avait
pas empêché : elle dit ce qui EST, elle ne garde rien. Ce fichier fait du
compte de requêtes un INVARIANT — il compte les exécutions SQL d'un
import complet et casse au-delà du budget. Un `await` dans une boucle de
1844 lignes le fait rougir tout de suite, pas trois mois plus tard sur
une facture de latence.

Pourquoi le fichier réel et pas un CSV de laboratoire : c'est LUI qui a
produit les ~1685 allers-retours mesurés (0,9 requête par ligne, ~8 s en
prod). Un fichier de 10 lignes ne prouve rien sur un N+1 — la pente ne
se voit qu'à l'échelle. Les deux .xlsx sont versionnés dans le repo.

Le budget couvre TOUTE la requête HTTP, enforcement RBAC et résolution
de l'acteur compris : c'est le coût que l'agence paie, pas une portion
choisie.
"""

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTACTS = REPO_ROOT / "Contacts-2026-08-03-16-14-51.xlsx"
COMPANIES = REPO_ROOT / "Companies-2026-08-03-16-15-09.xlsx"

# 1844 lignes (dont 1593 fiches créées) et 439 sociétés doivent tenir en
# ce nombre d'allers-retours. L'import batché en consomme une quinzaine :
# la marge absorbe une requête de plus au RBAC ou une déf déclarée à la
# volée, elle n'absorbe PAS un lookup par ligne — c'est tout l'intérêt.
QUERY_BUDGET = 50


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _histogram(statements: list[str]) -> str:
    """Le compte par FORME de requête — quand le budget saute, on veut
    savoir quelle requête s'est mise à boucler, pas lire 1000 lignes.
    Le suffixe `[groupé]` distingue un `executemany` d'un tir unitaire :
    c'est LA différence entre un insert par paquet et un N+1."""
    from collections import Counter

    counter = Counter(statements)
    return "\n".join(f"  {count:5d} × {form}" for form, count in counter.most_common(8))


@contextmanager
def count_queries(db_session: AsyncSession) -> Iterator[list[str]]:
    """Compte les exécutions SQL réelles du moteur de test.

    Un `executemany` (l'INSERT groupé) compte pour UN : c'est bien un
    aller-retour, et c'est exactement la grandeur qu'on veut borner."""
    statements: list[str] = []
    engine = db_session.bind.sync_engine  # type: ignore[union-attr]

    def on_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        form = " ".join(statement.split()[:4])
        statements.append(f"{form} [groupé]" if executemany else form)

    event.listen(engine, "before_cursor_execute", on_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", on_execute)


async def _suggested_mapping(
    client: AsyncClient, headers: dict[str, str], entity: str, path: Path
) -> dict[str, str]:
    """Le mapping tel que le wizard le construit : la suggestion du back,
    ambiguïtés tranchées au 1er choix (ce que fait un clic)."""
    from src.imports.csv_reader import parse_upload

    parsed = parse_upload(path.name, path.read_bytes())
    r = await client.post(
        f"/imports/{entity}/suggest-mapping", headers=headers, json={"headers": parsed.headers}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    mapping = dict(body["suggestions"])
    for column, choices in body.get("ambiguous", {}).items():
        if column not in mapping and choices:
            mapping[column] = choices[0]
    return mapping


@pytest.mark.skipif(not CONTACTS.exists(), reason="fichier réel absent")
async def test_person_import_stays_within_its_query_budget(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """1844 lignes, DEUX passes — la création puis le ré-import (le pire
    chemin de lecture : 1543 liaisons). Les deux tiennent dans le budget,
    et les verdicts sont ceux que le rejeu réel a mesurés."""
    headers = agent_headers(admin)
    mapping = await _suggested_mapping(client, headers, "client-profiles", CONTACTS)
    payload = {
        "file_b64": base64.b64encode(CONTACTS.read_bytes()).decode(),
        "filename": CONTACTS.name,
        "mapping": mapping,
    }

    with count_queries(db_session) as first_pass:
        r = await client.post("/imports/client-profiles", headers=headers, json=payload)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["total_rows"] == 1844
    # LES VERDICTS NE BOUGENT PAS (le batch change la vitesse, pas les
    # décisions) : les comptes du rejeu réel, gravés.
    assert len(report["created"]) == 1593
    assert len(report["linked"]) == 37
    assert len(report["ignored"]) == 214
    assert len(first_pass) <= QUERY_BUDGET, (
        f"import initial : {len(first_pass)} requêtes pour 1844 lignes "
        f"(budget {QUERY_BUDGET}) — le N+1 est de retour.\n" + _histogram(first_pass)
    )

    # 2e passe : tout existe désormais → le chemin des LIAISONS, celui qui
    # faisait 1543 SELECT unitaires avant ce lot.
    with count_queries(db_session) as second_pass:
        r = await client.post("/imports/client-profiles", headers=headers, json=payload)
    assert r.status_code == 200, r.text
    again = r.json()
    # Les comptes du ré-import, mesurés sur la base réelle : les 1543
    # lignes à email retrouvent leur fiche, les 91 SANS email se recréent
    # (la dédup par identité est intra-batch et ne va JAMAIS contre la
    # base — on ne fusionne pas des homonymes), 210 restent sans identité.
    assert len(again["created"]) == 91
    assert len(again["linked"]) == 1543
    assert len(again["ignored"]) == 210
    assert len(second_pass) <= QUERY_BUDGET, (
        f"ré-import : {len(second_pass)} requêtes pour 1844 lignes "
        f"(budget {QUERY_BUDGET}) — le lookup par ligne est revenu.\n" + _histogram(second_pass)
    )
    print(f"\nBUDGET fiches — création: {len(first_pass)} req · ré-import: {len(second_pass)} req")


@pytest.mark.skipif(not COMPANIES.exists(), reason="fichier réel absent")
async def test_company_import_stays_within_its_query_budget(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Le symétrique société : 439 lignes, création puis ré-import."""
    headers = agent_headers(admin)
    mapping = await _suggested_mapping(client, headers, "company-profiles", COMPANIES)
    payload = {
        "file_b64": base64.b64encode(COMPANIES.read_bytes()).decode(),
        "filename": COMPANIES.name,
        "mapping": mapping,
    }

    with count_queries(db_session) as first_pass:
        r = await client.post("/imports/company-profiles", headers=headers, json=payload)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["total_rows"] == 439
    assert len(report["created"]) == 439
    assert len(report["linked"]) == 0
    assert len(first_pass) <= QUERY_BUDGET, (
        f"import initial sociétés : {len(first_pass)} requêtes pour 439 lignes "
        f"(budget {QUERY_BUDGET}) — le N+1 est de retour.\n" + _histogram(first_pass)
    )

    with count_queries(db_session) as second_pass:
        r = await client.post("/imports/company-profiles", headers=headers, json=payload)
    assert r.status_code == 200, r.text
    again = r.json()
    assert len(again["created"]) == 0
    assert len(again["linked"]) == 439
    assert len(second_pass) <= QUERY_BUDGET, (
        f"ré-import sociétés : {len(second_pass)} requêtes pour 439 lignes "
        f"(budget {QUERY_BUDGET}) — le lookup par ligne est revenu.\n" + _histogram(second_pass)
    )
    print(
        f"\nBUDGET sociétés — création: {len(first_pass)} req · ré-import: {len(second_pass)} req"
    )
