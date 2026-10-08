"""The variables a template may use, typed and resolved from records already loaded.

Ported from Prism's `variables.py` (reference commit 629a2b0): the mechanics
(`VariableDef`, `RenderContext`, `lookup`, `value_of`, `catalog_for`) are
Prism's, the catalog is Nidria's (spec section 8.3). Three record namespaces:

- `client.*`: the agency's client record (`client_profile`). Its identity
  (first name, last name, email) follows rule N2: the client-space account
  when the record is linked to one, else the record's own columns
  (`client_identity`).
- `company.*`: the company linked to the client by `company_profile_role`,
  only when there is exactly one (`sole_company`). With none or several, every
  `company.*` value is missing: the template's fallback applies, else the
  renderer reports it.
- `agency.*`: the sending agency.

`ai.*` (the step's AI variables) is resolved by the renderer, not here.

Nothing here reads the database: the caller loads the rows and hands them over
in a `RenderContext`. A value that is absent stays absent (None): the renderer
reports it, it never becomes an empty string.

Decision A12: the backend serves a variable's token, source, value type and a
neutral example; the frontend translates each token's label.
`VariableDef.fallback_label` is a technical English name, for logs and for a
token the frontend does not know yet, never a translated label.

Names, not codes (question R8, answered on 08/10/2026): an email NAMES what
Nidria stores as a code, in the AGENCY's default language
(`agency.default_language`, `RenderContext.label_lang`).
- `client.status` reads as its label (`Prospect` / `Client`, the client
  records' own labels, `CLIENT_STATUS_LABELS`);
- `client.nationality`, `company.country` and `agency.country` as the
  country's name (`src.core.countries.country_name`; a value that is not a
  country code is kept as written);
- `client.preferred_lang` as the language's name, spelled out
  (`src.core.languages.language_name`, pycountry's ISO 639 catalogs: « es »
  reads « Espagnol » for a French agency; an unknown code as written);
- `company.employee_count`, a whole number, with the agency language's
  digit grouping (`src.core.numbers.format_integer`, ICU's); any other value
  (a decimal, a text) reads as before.
Provisional decision: in V1 a campaign is written in one language (spec
section 16: per-language variants are V2), the agency's, so its labels
follow that language rather than each client's.

For the frontend's variable picker, the example of a `country`, `language`
or `status` variable is the stored CODE (`FR`, `fr`, `client`): the
frontend names it in the interface's language (its `Intl` names for a
country or a language), as the email names it in the agency's.

Not ported from Prism: the CFO, M&A and LinkedIn variables, `split_name`
(Nidria stores first and last names apart), the money, percent and date
formats, and the per-project-type catalogs.
"""

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from shared.models.agency import Agency
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.company_profile import CompanyProfile
from shared.models.expat_user import ExpatUser
from src.client_profiles.identity import ClientIdentity, effective_identity
from src.core.countries import country_name
from src.core.i18n import (
    DEFAULT_LANG,
    SUPPORTED_LANGUAGES,
    resolve_notification_lang_client,
    supported_lang,
)
from src.core.languages import language_name
from src.core.numbers import format_integer

#: Where a variable's value comes from: its token's namespace.
VariableSource = Literal["client", "company", "agency"]
#: What a variable's value is, for the frontend's picker and its sample.
VariableType = Literal["text", "email", "phone", "number", "url", "country", "language", "status"]

_SOURCES: dict[str, VariableSource] = {"client": "client", "company": "company", "agency": "agency"}

#: The client's derived status as the client records name it (frontend
#: `clients.json`, `status.prospect` / `status.client`), in the seven
#: languages: an email names the status, never its code.
CLIENT_STATUS_LABELS: dict[str, dict[str, str]] = {
    "fr": {"prospect": "Prospect", "client": "Client"},
    "en": {"prospect": "Prospect", "client": "Client"},
    "es": {"prospect": "Prospecto", "client": "Cliente"},
    "ru": {"prospect": "Потенциальный", "client": "Клиент"},
    "pt": {"prospect": "Prospeto", "client": "Cliente"},
    "it": {"prospect": "Potenziale", "client": "Cliente"},
    "hu": {"prospect": "Érdeklődő", "client": "Ügyfél"},
}

assert set(CLIENT_STATUS_LABELS) == set(SUPPORTED_LANGUAGES), "a language lacks status labels"


def client_identity(profile: ClientProfile, account: ExpatUser | None) -> ClientIdentity:
    """Rule N2, called and not copied: `client_profiles.identity.
    effective_identity` (the account's identity when the record is linked,
    else the record's own), with empty values as None.

    `account` is the `expat_user` the record is linked to, loaded by the
    caller. Passing another account is a wiring error, refused: an email must
    never go to someone else's address."""
    if account is not None and account.id is not None and account.id != profile.expat_user_id:
        raise ValueError("The account given is not the one the client record is linked to.")
    identity = effective_identity(profile, account)
    return ClientIdentity(
        _text(identity.first_name), _text(identity.last_name), _text(identity.email)
    )


def sole_company(companies: Iterable[CompanyProfile]) -> CompanyProfile | None:
    """The client's company when exactly one is linked (a company linked
    under several roles counts once); None for none or several."""
    distinct = _distinct(companies)
    return distinct[0] if len(distinct) == 1 else None


def _distinct(companies: Iterable[CompanyProfile]) -> list[CompanyProfile]:
    seen: dict[uuid.UUID | int, CompanyProfile] = {}
    for company in companies:
        # A row not flushed yet has no id: it is told apart by identity.
        seen.setdefault(company.id if company.id is not None else id(company), company)
    return list(seen.values())


@dataclass(frozen=True)
class RenderContext:
    """Everything a template can read for one client, loaded beforehand."""

    #: The agency's client record; None when no client is chosen (a preview).
    client: ClientProfile | None
    #: The client-space account the record is linked to (`expat_user_id`);
    #: None when the record is not linked.
    account: ExpatUser | None = None
    #: Every company linked to the client by `company_profile_role` (one per
    #: role is fine: they are counted once).
    companies: Sequence[CompanyProfile] = ()
    #: The agency that sends.
    agency: Agency | None = None
    #: The client's live cases in this agency, as
    #: `ClientProfilesRepository.cases_for_profiles` returns them: they make
    #: the derived status (`client.status`).
    cases: Sequence[ClientCase] = ()
    #: ai.<key>: generated value (absent until generated).
    ai_values: dict[str, str] = field(default_factory=dict)
    #: The AI variables the step defines; None: not checked.
    ai_keys: frozenset[str] | None = None
    #: The day ages are counted on; None: today (UTC).
    today: date | None = None

    @property
    def identity(self) -> ClientIdentity | None:
        return client_identity(self.client, self.account) if self.client is not None else None

    @property
    def linked_companies(self) -> list[CompanyProfile]:
        return _distinct(self.companies)

    @property
    def company(self) -> CompanyProfile | None:
        return sole_company(self.companies)

    @property
    def label_lang(self) -> str:
        """The language an email names codes in (a status, a country): the
        agency's default language (provisional decision, see the module's
        docstring); the platform's when no agency is given."""
        agency_lang = self.agency.default_language if self.agency is not None else None
        return supported_lang(agency_lang) or DEFAULT_LANG


@dataclass(frozen=True)
class VariableDef:
    token: str
    value_type: VariableType
    resolve: Callable[[RenderContext], Any]
    #: Technical English name, a fallback only: the frontend labels each
    #: token in the interface's language (decision A12).
    fallback_label: str
    #: A neutral sample value; None when any sample would be a sentence in
    #: one language (the frontend shows its own).
    example: str | None = None

    @property
    def namespace(self) -> str:
        return self.token.split(".", 1)[0]

    @property
    def source(self) -> VariableSource:
        return _SOURCES[self.namespace]


# ── Values ─────────────────────────────────────────────────────────────────


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool | dict | list | tuple | set):
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _text(value)


def _age(born: date | None, today: date) -> int | None:
    if born is None:
        return None
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return years if years >= 0 else None


# ── client.* ───────────────────────────────────────────────────────────────


def _identity(attr: str) -> Callable[[RenderContext], Any]:
    def resolve(ctx: RenderContext) -> str | None:
        identity = ctx.identity
        return getattr(identity, attr) if identity is not None else None

    return resolve


def _full_name(ctx: RenderContext) -> str | None:
    identity = ctx.identity
    if identity is None:
        return None
    return " ".join(p for p in (identity.first_name, identity.last_name) if p) or None


def _client(attr: str) -> Callable[[RenderContext], Any]:
    return lambda ctx: _text(getattr(ctx.client, attr, None)) if ctx.client else None


def _country(resolve: Callable[[RenderContext], str | None]) -> Callable[[RenderContext], Any]:
    """A country value named in the email's label language; a value that is
    not a country code is kept as written (`country_name`)."""

    def named(ctx: RenderContext) -> str | None:
        return country_name(resolve(ctx), ctx.label_lang)

    return named


def _client_age(ctx: RenderContext) -> str | None:
    if ctx.client is None:
        return None
    age = _age(ctx.client.date_of_birth, ctx.today or datetime.now(UTC).date())
    return str(age) if age is not None else None


def _client_language(ctx: RenderContext) -> str | None:
    """The language the client's emails go in: the record's, else the
    account's, else the agency's (the chain every client send follows,
    `resolve_notification_lang_client`), NAMED in the email's label
    language (« Espagnol », never « es »)."""
    if ctx.client is None:
        return None
    code = resolve_notification_lang_client(
        ctx.account.preferred_lang if ctx.account is not None else None,
        profile_lang=ctx.client.preferred_lang,
        agency_default=ctx.agency.default_language if ctx.agency is not None else None,
    )
    return language_name(code, ctx.label_lang)


def _client_status(ctx: RenderContext) -> str | None:
    if ctx.client is None:
        return None
    # Imported here: the client records' manager will call the email module
    # (deletion hooks), so a module-level import would make a cycle.
    from src.client_profiles.client_profiles_manager import derived_client_status

    status = derived_client_status(list(ctx.cases), ctx.client.status_override)
    return CLIENT_STATUS_LABELS[ctx.label_lang].get(status, status)


# ── company.* ──────────────────────────────────────────────────────────────


def _company_name(ctx: RenderContext) -> str | None:
    company = ctx.company
    return _text(company.name) if company is not None else None


def _company_value(ctx: RenderContext, key: str) -> Any:
    """A preset key of the company's values (`company_profile.custom_fields`,
    `profile_sections.COMPANY_PRESET_PROFILE_SECTION`)."""
    company = ctx.company
    if company is None:
        return None
    return (company.custom_fields or {}).get(key)


def _company_field(key: str) -> Callable[[RenderContext], Any]:
    return lambda ctx: _text(_company_value(ctx, key))


def _company_headcount(ctx: RenderContext) -> str | None:
    """A whole number of employees, grouped as the email's label language
    writes it (« 1 234 » in French); a decimal or a text as stored."""
    value = _company_value(ctx, "employee_count")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return format_integer(value, ctx.label_lang)
    return _number(value)


# ── agency.* ───────────────────────────────────────────────────────────────


def _agency(attr: str) -> Callable[[RenderContext], Any]:
    return lambda ctx: _text(getattr(ctx.agency, attr, None)) if ctx.agency else None


CATALOG: tuple[VariableDef, ...] = (
    # client.*
    VariableDef("client.first_name", "text", _identity("first_name"), "First name", "Marie"),
    VariableDef("client.last_name", "text", _identity("last_name"), "Last name", "Martin"),
    VariableDef("client.full_name", "text", _full_name, "Full name", "Marie Martin"),
    VariableDef("client.email", "email", _identity("email"), "Email", "marie.martin@example.com"),
    VariableDef("client.phone", "phone", _client("phone"), "Phone", "+33 6 12 34 56 78"),
    VariableDef(
        "client.nationality", "country", _country(_client("nationality")), "Nationality", "FR"
    ),
    VariableDef("client.profession", "text", _client("profession"), "Profession"),
    VariableDef("client.employer", "text", _client("employer"), "Employer"),
    VariableDef("client.age", "number", _client_age, "Age", "42"),
    VariableDef("client.preferred_lang", "language", _client_language, "Preferred language", "fr"),
    VariableDef("client.status", "status", _client_status, "Client status", "client"),
    # company.*
    VariableDef("company.name", "text", _company_name, "Company name"),
    VariableDef("company.legal_form", "text", _company_field("legal_form"), "Legal form"),
    VariableDef("company.industry", "text", _company_field("industry"), "Industry"),
    VariableDef(
        "company.website", "url", _company_field("website"), "Website", "https://example.com"
    ),
    VariableDef("company.employee_count", "number", _company_headcount, "Employees", "25"),
    VariableDef("company.country", "country", _country(_company_field("country")), "Country", "FR"),
    # agency.*
    VariableDef("agency.name", "text", _agency("name"), "Agency name"),
    VariableDef("agency.legal_name", "text", _agency("legal_name"), "Agency legal name"),
    VariableDef("agency.city", "text", _agency("city"), "Agency city"),
    VariableDef("agency.country", "country", _country(_agency("country")), "Agency country", "FR"),
    VariableDef(
        "agency.contact_email",
        "email",
        _agency("contact_email"),
        "Agency email",
        "contact@example.com",
    ),
    VariableDef(
        "agency.contact_phone",
        "phone",
        _agency("contact_phone"),
        "Agency phone",
        "+33 1 23 45 67 89",
    ),
)

_BY_TOKEN: dict[str, VariableDef] = {v.token: v for v in CATALOG}

assert len(_BY_TOKEN) == len(CATALOG), "a variable token is declared twice"
assert all(v.namespace in _SOURCES for v in CATALOG), "a variable outside client/company/agency"


def catalog_for() -> list[VariableDef]:
    """Every variable a template may use (one catalog: Nidria has no project types)."""
    return list(CATALOG)


def value_of(definition: VariableDef, ctx: RenderContext) -> str | None:
    """The variable's text for this client; None when absent, never ""."""
    value = definition.resolve(ctx)
    return None if value is None else str(value).strip() or None


def lookup(token: str) -> VariableDef | None:
    return _BY_TOKEN.get(token)
