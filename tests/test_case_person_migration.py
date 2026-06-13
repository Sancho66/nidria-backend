"""Protects the prod data migration (de687433439a): the 1-PRINCIPAL
invariant at the DB level, and the backfill SQL run verbatim against a
reconstructed pre-migration state (cases + family_member → case_person)."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_person import CasePerson
from tests.plugins.case_plugin import MakeClientCase


async def test_one_principal_per_case_invariant(
    db_session: AsyncSession, make_client_case: MakeClientCase
) -> None:
    """make_client_case already creates the PRINCIPAL — a second one
    violates the partial unique index."""
    case = await make_client_case()
    db_session.add(
        CasePerson(case_id=case.id, kind="principal", expat_user_id=case.principal_expat_user_id)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_family_persons_are_unconstrained(
    db_session: AsyncSession, make_client_case: MakeClientCase
) -> None:
    """Any number of FAMILY rows — the unique index is principal-only."""
    case = await make_client_case()
    db_session.add(CasePerson(case_id=case.id, kind="family", full_name="A", relationship="child"))
    db_session.add(CasePerson(case_id=case.id, kind="family", full_name="B", relationship="child"))
    await db_session.flush()  # no error


async def test_backfill_sql_creates_principal_and_preserves_family(
    db_session: AsyncSession, make_client_case: MakeClientCase
) -> None:
    """The migration's INSERT statements, verbatim, against a
    reconstructed family_member table: every case gets exactly one
    PRINCIPAL linked to its expat_user, and each family_member becomes
    a FAMILY person with name/relationship preserved — zero loss."""
    # Two cases; case_a will carry two family_member rows. make_client_case
    # already created PRINCIPAL persons, so to simulate the pre-migration
    # world we wipe case_person and rebuild family_member.
    case_a = await make_client_case()
    case_b = await make_client_case()
    await db_session.execute(text("DELETE FROM case_person"))
    await db_session.execute(
        text(
            "CREATE TABLE family_member ("
            "id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES client_case(id), "
            "name VARCHAR(200) NOT NULL, relationship VARCHAR(50) NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    )
    for name, rel in [("Claire", "spouse"), ("Lucas", "child")]:
        await db_session.execute(
            text(
                "INSERT INTO family_member (id, case_id, name, relationship) "
                "VALUES (:i, :c, :n, :r)"
            ),
            {"i": str(uuid.uuid4()), "c": str(case_a.id), "n": name, "r": rel},
        )

    # The two INSERTs from upgrade(), verbatim.
    await db_session.execute(
        text(
            "INSERT INTO case_person (id, case_id, kind, expat_user_id, created_at, updated_at) "
            "SELECT gen_random_uuid(), c.id, 'principal', c.principal_expat_user_id, now(), now() "
            "FROM client_case c"
        )
    )
    await db_session.execute(
        text(
            "INSERT INTO case_person "
            "(id, case_id, kind, full_name, relationship, created_at, updated_at) "
            "SELECT gen_random_uuid(), fm.case_id, 'family', fm.name, fm.relationship, "
            "fm.created_at, fm.updated_at FROM family_member fm"
        )
    )
    await db_session.execute(text("DROP TABLE family_member"))
    await db_session.commit()

    # Every case: exactly one PRINCIPAL, linked to the right expat_user.
    for case in (case_a, case_b):
        principals = (
            (
                await db_session.execute(
                    select(CasePerson).where(
                        CasePerson.case_id == case.id, CasePerson.kind == "principal"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(principals) == 1
        assert principals[0].expat_user_id == case.principal_expat_user_id

    # case_a's two family members preserved (names + relationships).
    family = (
        (
            await db_session.execute(
                select(CasePerson).where(
                    CasePerson.case_id == case_a.id, CasePerson.kind == "family"
                )
            )
        )
        .scalars()
        .all()
    )
    assert {(p.full_name, p.relationship) for p in family} == {
        ("Claire", "spouse"),
        ("Lucas", "child"),
    }
    # case_b had no family → only its principal.
    case_b_persons = (
        (await db_session.execute(select(CasePerson).where(CasePerson.case_id == case_b.id)))
        .scalars()
        .all()
    )
    assert len(case_b_persons) == 1
