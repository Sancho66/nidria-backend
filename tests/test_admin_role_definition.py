"""Single definition of 'agency admin' (fix: impersonation ignored cloned
admin roles). `consent_gate.is_agency_admin` and the agency-switcher
`ImpersonationRepository.get_an_admin_of_agency` BOTH consume the shared
`is_admin_role_clause`. This battery pins that they can never diverge: for
every role shape, the gate's verdict on an agent equals whether the switcher
would step in AS that agent.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.rbac.consent_gate import is_agency_admin
from src.impersonation.impersonation_repository import ImpersonationRepository
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import MakeAgent
from tests.plugins.rbac_plugin import MakeRole

# (label, role-kind, system-origin-name, is_admin?)
_VARIANTS = [
    ("system-admin", "system", "admin", True),
    ("clone-of-admin", "clone", "admin", True),  # the Expatriation.io case
    ("clone-of-viewer", "clone", "viewer", False),
    ("plain-custom", "custom", None, False),
    ("system-member", "system", "member", False),
]


async def test_admin_definition_consistent_across_gate_and_impersonation(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict,
) -> None:
    repo = ImpersonationRepository(db_session)

    for label, kind, origin_name, expected in _VARIANTS:
        agency = await make_agency()  # one agent per agency: the switcher pick is unambiguous
        if kind == "system":
            role = system_roles[origin_name]
        elif kind == "clone":
            role = await make_role(
                name=f"Clone-{label}",
                is_system=False,
                agency_id=agency.id,
                cloned_from_role_id=system_roles[origin_name].id,
            )
        else:  # a plain custom role, no CoW link
            role = await make_role(name=f"Custom-{label}", is_system=False, agency_id=agency.id)
        agent = await make_agent(agency_id=agency.id, role=role)

        gate = await is_agency_admin(db_session, agent)
        picked = await repo.get_an_admin_of_agency(agency.id)
        switcher = picked is not None and picked.id == agent.id

        assert gate == expected, f"{label}: gate={gate}"
        assert switcher == expected, f"{label}: switcher={switcher}"
        # The proof: one definition, two callers, identical verdict.
        assert gate == switcher, label


async def test_deactivated_and_external_admin_clone_excluded_from_switcher(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict,
) -> None:
    """The widened admin rule does NOT relax the other switcher criteria: a
    DEACTIVATED holder of an admin clone, or an EXTERNAL one, is still not a
    valid seat to enter as (get_an_admin_of_agency keeps is_external=false and
    deactivated_at IS NULL)."""
    from datetime import UTC, datetime

    repo = ImpersonationRepository(db_session)

    agency = await make_agency()
    clone = await make_role(
        name="Administrateur",
        is_system=False,
        agency_id=agency.id,
        cloned_from_role_id=system_roles["admin"].id,
    )
    await make_agent(agency_id=agency.id, role=clone, deactivated_at=datetime.now(UTC))
    await make_agent(agency_id=agency.id, role=clone, is_external=True)

    assert await repo.get_an_admin_of_agency(agency.id) is None
