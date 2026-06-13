import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_note import CaseNote
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.external_contact import ExternalContact
from shared.models.invitation import CaseInvitation
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.expat_plugin import MakeExpatUser

CASE_DEFAULTS: dict[str, Any] = {
    "origin_country": "FR",
    "dest_country": "PY",
}

MakeClientCase = Callable[..., Awaitable[ClientCase]]
MakeCaseInvitation = Callable[..., Awaitable[CaseInvitation]]
MakeCasePerson = Callable[..., Awaitable[CasePerson]]
MakeExternalContact = Callable[..., Awaitable[ExternalContact]]
MakeCaseNote = Callable[..., Awaitable[CaseNote]]


@pytest_asyncio.fixture
async def make_client_case(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_expat_user: MakeExpatUser,
) -> MakeClientCase:
    async def _make(**overrides: Any) -> ClientCase:
        data = {**CASE_DEFAULTS, **overrides}
        if "agency_id" not in data:
            data["agency_id"] = (await make_agency()).id
        if "principal_expat_user_id" not in data:
            expat = await make_expat_user(activated=False)
            data["principal_expat_user_id"] = expat.id
        case = ClientCase(**data)
        db_session.add(case)
        await db_session.flush()
        # The PRINCIPAL person — created with the case in real code; the
        # detail endpoint requires exactly one per case.
        db_session.add(
            CasePerson(
                case_id=case.id,
                kind="principal",
                expat_user_id=data["principal_expat_user_id"],
            )
        )
        await db_session.commit()
        await db_session.refresh(case)
        return case

    return _make


@pytest_asyncio.fixture
async def make_case_invitation(db_session: AsyncSession) -> MakeCaseInvitation:
    async def _make(*, case: ClientCase, **overrides: Any) -> CaseInvitation:
        data: dict[str, Any] = {
            "case_id": case.id,
            "token": secrets.token_urlsafe(24),
            "expires_at": datetime.now(UTC) + timedelta(days=7),
            **overrides,
        }
        if "email" not in data:
            data["email"] = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
        invitation = CaseInvitation(**data)
        db_session.add(invitation)
        await db_session.commit()
        await db_session.refresh(invitation)
        return invitation

    return _make


@pytest_asyncio.fixture
async def make_case_person(db_session: AsyncSession) -> MakeCasePerson:
    """A FAMILY person on a case (the principal is created with the case)."""

    async def _make(*, case: ClientCase, **overrides: Any) -> CasePerson:
        data = {
            "case_id": case.id,
            "kind": "family",
            "full_name": "Family Member",
            "relationship": "spouse",
            **overrides,
        }
        person = CasePerson(**data)
        db_session.add(person)
        await db_session.commit()
        await db_session.refresh(person)
        return person

    return _make


@pytest_asyncio.fixture
async def make_external_contact(db_session: AsyncSession) -> MakeExternalContact:
    async def _make(*, case: ClientCase, **overrides: Any) -> ExternalContact:
        data = {"case_id": case.id, "name": "Maitre Dupont", "type": "notary", **overrides}
        contact = ExternalContact(**data)
        db_session.add(contact)
        await db_session.commit()
        await db_session.refresh(contact)
        return contact

    return _make


@pytest_asyncio.fixture
async def make_case_note(db_session: AsyncSession) -> MakeCaseNote:
    async def _make(*, case: ClientCase, **overrides: Any) -> CaseNote:
        data = {"case_id": case.id, "body": "A note.", "is_confidential": False, **overrides}
        note = CaseNote(**data)
        db_session.add(note)
        await db_session.commit()
        await db_session.refresh(note)
        return note

    return _make
