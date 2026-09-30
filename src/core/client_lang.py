"""The language of a CLIENT as seen from ONE agency (lot « la langue du
client », 30/09) — the DB side of `resolve_notification_lang_client`.

THE PRODUCT RULE (validated by Alexandre): the agency sets ITS interface
language in its settings (`agency.default_language`); on every client it
sets THE CLIENT'S LANGUAGE, stored on the agency's record of that client
(`client_profile.preferred_lang`, one row per (agency, expat)). That value
drives what the client receives from THIS agency — invitations, step mails,
reminders, follow-ups, digests — and what their space shows by default.
The account (`expat_user`, global across agencies) is never rewritten by an
agency: its own `preferred_lang` is only the next fallback.

Chain, first supported value wins:
    client_profile.preferred_lang (agency, expat)
    → expat_user.preferred_lang
    → "en" when the account states an unsupported language (NOTIF-1)
    → agency.default_language when no language is known at all
    → "en"

Every client send that has an agency context resolves through here — async
for the managers, sync (`*_sync`) for the scheduler jobs. The batch forms
read ONE query per call whatever the number of recipients (a digest or a
dispatch tick never pays N lookups). The only client send WITHOUT an agency
context is the expat password reset (auth_manager.forgot_password): an
account may belong to several agencies, none of which owns that mail, so it
keeps the account's own language."""

import uuid
from collections.abc import Iterable, Mapping

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from shared.models.agency import Agency
from shared.models.client_profile import ClientProfile
from src.core.i18n import resolve_notification_lang_client


def _profile_langs_stmt(
    agency_id: uuid.UUID, expat_user_ids: list[uuid.UUID]
) -> Select[tuple[uuid.UUID | None, str | None]]:
    return select(ClientProfile.expat_user_id, ClientProfile.preferred_lang).where(
        ClientProfile.agency_id == agency_id,
        ClientProfile.expat_user_id.in_(expat_user_ids),
        ClientProfile.preferred_lang.is_not(None),
    )


def _resolve_all(
    accounts: Mapping[uuid.UUID, str | None],
    profiles: Mapping[uuid.UUID, str],
    agency_default: str | None,
) -> dict[uuid.UUID, str]:
    return {
        expat_id: resolve_notification_lang_client(
            account_lang, profile_lang=profiles.get(expat_id), agency_default=agency_default
        )
        for expat_id, account_lang in accounts.items()
    }


# --- async (managers) -------------------------------------------------------------


async def profile_langs(
    db: AsyncSession, agency_id: uuid.UUID, expat_user_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """{expat_user_id: the language THIS agency's record sets} — only the
    records that carry one. ONE query."""
    ids = list(dict.fromkeys(expat_user_ids))
    if not ids:
        return {}
    rows = (await db.execute(_profile_langs_stmt(agency_id, ids))).all()
    return {expat_id: lang for expat_id, lang in rows if expat_id is not None and lang}


async def agency_default_language(db: AsyncSession, agency_id: uuid.UUID) -> str | None:
    return (
        await db.execute(select(Agency.default_language).where(Agency.id == agency_id))
    ).scalar_one_or_none()


async def client_langs(
    db: AsyncSession,
    agency_id: uuid.UUID,
    accounts: Mapping[uuid.UUID, str | None],
    *,
    agency_default: str | None = None,
) -> dict[uuid.UUID, str]:
    """The resolved language of each client for this agency.

    `accounts`: {expat_user_id: that account's preferred_lang}. Pass
    `agency_default` when the agency row is already loaded; omitted, it is
    read (one more query). Empty `accounts` → no query at all."""
    if not accounts:
        return {}
    profiles = await profile_langs(db, agency_id, accounts.keys())
    if agency_default is None:
        agency_default = await agency_default_language(db, agency_id)
    return _resolve_all(accounts, profiles, agency_default)


async def client_lang(
    db: AsyncSession,
    agency_id: uuid.UUID,
    expat_user_id: uuid.UUID,
    account_lang: str | None,
    *,
    agency_default: str | None = None,
) -> str:
    """One client's language for this agency (see `client_langs`)."""
    langs = await client_langs(
        db, agency_id, {expat_user_id: account_lang}, agency_default=agency_default
    )
    return langs[expat_user_id]


async def client_langs_by_agency(
    db: AsyncSession, expat_user_id: uuid.UUID, account_lang: str | None, agencies: Iterable[Agency]
) -> dict[uuid.UUID, str]:
    """The CLIENT face: one account, N agencies (its dossiers) → {agency_id:
    the language that agency set for this client}. ONE query for the list."""
    by_id = {agency.id: agency for agency in agencies}
    if not by_id:
        return {}
    rows = (
        await db.execute(
            select(ClientProfile.agency_id, ClientProfile.preferred_lang).where(
                ClientProfile.expat_user_id == expat_user_id,
                ClientProfile.agency_id.in_(list(by_id)),
                ClientProfile.preferred_lang.is_not(None),
            )
        )
    ).all()
    profiles = {agency_id: lang for agency_id, lang in rows if lang}
    return {
        agency_id: resolve_notification_lang_client(
            account_lang,
            profile_lang=profiles.get(agency_id),
            agency_default=agency.default_language,
        )
        for agency_id, agency in by_id.items()
    }


# --- sync (scheduler jobs) --------------------------------------------------------


def profile_langs_sync(
    db: Session, agency_id: uuid.UUID, expat_user_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    ids = list(dict.fromkeys(expat_user_ids))
    if not ids:
        return {}
    rows = db.execute(_profile_langs_stmt(agency_id, ids)).all()
    return {expat_id: lang for expat_id, lang in rows if expat_id is not None and lang}


def client_langs_sync(
    db: Session,
    agency_id: uuid.UUID,
    accounts: Mapping[uuid.UUID, str | None],
    *,
    agency_default: str | None,
) -> dict[uuid.UUID, str]:
    """Sync twin of `client_langs`. The jobs always hold the agency row, so
    `agency_default` is required here (no hidden extra query)."""
    if not accounts:
        return {}
    profiles = profile_langs_sync(db, agency_id, accounts.keys())
    return _resolve_all(accounts, profiles, agency_default)


def client_lang_sync(
    db: Session,
    agency_id: uuid.UUID,
    expat_user_id: uuid.UUID,
    account_lang: str | None,
    *,
    agency_default: str | None,
) -> str:
    return client_langs_sync(
        db, agency_id, {expat_user_id: account_lang}, agency_default=agency_default
    )[expat_user_id]
