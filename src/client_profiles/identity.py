"""A client record's EFFECTIVE identity: the one name and address Nidria
shows for a client, and the one it writes to (Email module, rule N2).

The rule: when the record is linked to a client-space account
(`client_profile.expat_user_id`), the ACCOUNT's identity wins (first name,
last name, email: the client manages them across agencies); otherwise the
record's own columns, set when the agency created it. After linking, the
record's columns stay as the trace of what the agency first typed.

One rule, called rather than copied:
- by the client records' surfaces (`client_profiles_manager`: the list, the
  record, a new case started from the record);
- by the Email module (`email_campaigns.variables.client_identity`, which
  adds its own cleaning and checks on top);
- in SQL, by the queries that search or match clients by address
  (`effective_email_sql`).

Values are returned raw (a record's column may be None or empty): each
caller keeps its own presentation (`or ""` on a screen, `None` for an email
variable that has no value).
"""

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

from shared.models.client_profile import ClientProfile
from shared.models.expat_user import ExpatUser


@dataclass(frozen=True)
class ClientIdentity:
    """The name and address Nidria shows, and writes to, for a client."""

    first_name: str | None
    last_name: str | None
    email: str | None


def effective_identity(profile: ClientProfile, account: ExpatUser | None) -> ClientIdentity:
    """The account's identity when the record is linked (`account` is the
    `expat_user` the record points to, loaded by the caller), else the
    record's own. Raw values: nothing is trimmed nor defaulted."""
    if account is not None:
        return ClientIdentity(account.first_name, account.last_name, account.email)
    return ClientIdentity(profile.first_name, profile.last_name, profile.email)


def effective_email(profile: ClientProfile, account: ExpatUser | None) -> str | None:
    """The address Nidria writes to for this client (rule N2)."""
    return effective_identity(profile, account).email


def effective_email_sql() -> ColumnElement[str]:
    """`effective_email` in SQL: `coalesce(expat_user.email,
    client_profile.email)`. The query must OUTER JOIN `ExpatUser` on
    `ExpatUser.id == ClientProfile.expat_user_id`. An account's email is NOT
    NULL, so a linked record reads its account's address and an unlinked one
    its own column: the same rule as `effective_identity`. Compare it through
    `func.lower(...)`, as the client records' own address lookups do."""
    return func.coalesce(ExpatUser.email, ClientProfile.email)
