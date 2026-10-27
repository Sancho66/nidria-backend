import csv
import io
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from src.admin.demo_schema import AgencyDemoBatchRequest, AgencyDemoInput
from src.core.rbac.baseline import seed_rbac_baseline
from src.demo_sandbox.agency_control import AgencyDemoControlManager
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.import_csv import parse_import
from src.demo_sandbox.people import fictional_people
from tests.test_agency_demo import pilot
from tests.test_demo_import import HEADER, row


def test_optional_first_name_csv_validation() -> None:
    stream = io.StringIO()
    writer = csv.DictWriter(stream, [*HEADER, "first_name"], delimiter=";")
    writer.writeheader()
    writer.writerows(
        [
            row(first_name="  Patricia  "),
            row(first_name=""),
            row(first_name="Éléonore-Anne"),
            row(first_name="x" * 101),
        ]
    )
    result = parse_import(stream.getvalue())
    assert [item.data.get("first_name") for item in result] == [
        "Patricia",
        "",
        "Éléonore-Anne",
        None,
    ]
    assert result[3].errors[0].column == "first_name"
    assert (
        AgencyDemoInput.model_validate(pilot().model_dump(exclude={"first_name"})).first_name == ""
    )


@pytest.mark.asyncio
async def test_first_name_only_changes_new_owner_and_never_reused_demos(
    db_session: AsyncSession,
) -> None:
    await seed_rbac_baseline(db_session)
    named = pilot("Fictional Named", "named@example.com").model_copy(
        update={"first_name": "Patricia"}
    )
    unnamed = pilot("Fictional Unnamed", "unnamed@example.com")
    request = AgencyDemoBatchRequest(
        request_id=uuid.uuid4(), batch="QA-NAMES", items=[named, unnamed]
    )
    manager = AgencyDemoControlManager(db_session, load_catalog())
    result = await manager.create_batch(uuid.uuid4(), request)
    owners = []
    for item in result.items:
        agency = await db_session.scalar(
            select(Agency).where(Agency.slug == f"demo-sandbox-{item.code}")
        )
        assert agency is not None
        owner = await db_session.get(
            Agent, uuid.UUID(agency.settings["sandbox_full_demo"]["agent_id"])
        )
        assert owner is not None
        owners.append((owner.first_name, owner.last_name))
        others = list(
            await db_session.scalars(
                select(Agent).where(Agent.agency_id == agency.id, Agent.id != owner.id)
            )
        )
        assert all(member.first_name != "Patricia" for member in others)
        assert "patricia" not in owner.email.casefold()
    assert owners == [("Patricia", ""), fictional_people(unnamed).team[0]]
    assert not await db_session.scalar(
        select(ExpatUser.id).where(ExpatUser.first_name == "Patricia")
    )
    retried = request.model_copy(
        update={
            "request_id": uuid.uuid4(),
            "items": [
                named.model_copy(update={"first_name": "Wrong"}),
                unnamed.model_copy(update={"first_name": "Another"}),
            ],
        }
    )
    again = await manager.create_batch(uuid.uuid4(), retried)
    assert [item.code for item in result.items] == [item.code for item in again.items]
    agency = await db_session.scalar(
        select(Agency).where(Agency.slug == f"demo-sandbox-{result.items[0].code}")
    )
    assert agency is not None
    owner = await db_session.get(Agent, uuid.UUID(agency.settings["sandbox_full_demo"]["agent_id"]))
    assert owner and owner.first_name == "Patricia" and owner.last_name == ""
