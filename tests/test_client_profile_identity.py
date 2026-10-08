"""A client record's effective identity (rule N2, `client_profiles.identity`).

One rule, called by the client records' surfaces and by the Email module: the
linked account's identity, else the record's own. Proven here on the rule
itself, raw values kept, and in SQL: `effective_email_sql` reads, row by row,
the address `effective_email` names. The three surfaces that call it (list,
record, new case from a record) are covered by `test_client_profiles.py`.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.client_profile import ClientProfile
from shared.models.expat_user import ExpatUser
from src.client_profiles.identity import (
    ClientIdentity,
    effective_email,
    effective_email_sql,
    effective_identity,
)
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.expat_plugin import MakeExpatUser


def test_the_linked_account_wins_else_the_record_raw() -> None:
    account = ExpatUser(id=uuid.uuid4(), first_name="Ana", last_name="Souza", email="ana@x.io")
    linked = ClientProfile(
        expat_user_id=account.id, first_name="Anna", last_name="Sousa", email="old@x.io"
    )
    assert effective_identity(linked, account) == ClientIdentity("Ana", "Souza", "ana@x.io")
    assert effective_email(linked, account) == "ana@x.io"
    # Not linked: the record's own columns, as stored (None and "" kept: each
    # caller decides how to show them).
    bare = ClientProfile(first_name="Marie", last_name=None, email="")
    assert effective_identity(bare, None) == ClientIdentity("Marie", None, "")
    assert effective_email(ClientProfile(email=None), None) is None


async def test_the_sql_address_is_the_one_the_rule_names(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_client_profile: MakeClientProfile,
    make_expat_user: MakeExpatUser,
) -> None:
    agency = await make_agency()
    account = await make_expat_user(email="account@example.com")
    linked = await make_client_profile(
        agency_id=agency.id, expat_user_id=account.id, email="column@example.com"
    )
    unlinked = await make_client_profile(agency_id=agency.id, email="own@example.com")
    empty = await make_client_profile(agency_id=agency.id, email=None)
    stmt = (
        select(ClientProfile.id, effective_email_sql())
        .select_from(ClientProfile)
        .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
        .where(ClientProfile.agency_id == agency.id)
    )
    in_sql = dict((await db_session.execute(stmt)).tuples().all())
    assert in_sql == {
        linked.id: effective_email(linked, account),
        unlinked.id: effective_email(unlinked, None),
        empty.id: None,
    }
    assert in_sql[linked.id] == "account@example.com"
    # Compared lower-cased, as the client records' own lookups do.
    typed = (
        select(ClientProfile.id)
        .select_from(ClientProfile)
        .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
        .where(
            ClientProfile.agency_id == agency.id,
            func.lower(effective_email_sql()) == "account@example.com",
        )
    )
    assert (await db_session.execute(typed)).scalars().all() == [linked.id]
