"""The Email module's pure parts: Nidria's variable catalog, the renderer,
`body_to_html`, `content_hash` and the text of a reply received as HTML.

Ported from Prism's `tests/test_email_rendering.py` (reference 629a2b0), with
the CFO / M&A variables replaced by Nidria's catalog (spec 8.3) and the
renderer's Nidria rules (decision A7): a client without a company is not
blocked, `no_company` only when a `company.*` token needs one, several
companies leave `company.*` missing, the identity follows rule N2 (through
`client_profiles.identity`), and a status or a country reads as its name in
the agency's language (question R8).

The rendering itself reads no database: most tests build the records in
memory. One test renders rows read back from PostgreSQL, so the context is
proven on what the database really returns (ids, dates, JSONB numbers)."""

import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.company_profile import CompanyProfile, CompanyProfileRole
from shared.models.expat_user import ExpatUser
from src.email_campaigns.html_text import html_to_text
from src.email_campaigns.renderer import (
    EmailTemplateRenderer,
    body_to_html,
    content_hash,
    is_valid_ai_key,
    parse_token,
    tokens_in,
)
from src.email_campaigns.variables import (
    CATALOG,
    CLIENT_STATUS_LABELS,
    RenderContext,
    catalog_for,
    client_identity,
    lookup,
    sole_company,
)
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.expat_plugin import MakeExpatUser

EM_DASH = chr(0x2014)  # spelled out: no em dash in the source
TODAY = date(2026, 10, 7)

# ── records in memory ────────────────────────────────────────────────────


def _client(**overrides: Any) -> ClientProfile:
    data: dict[str, Any] = {
        "first_name": "Marie",
        "last_name": "Martin",
        "email": "marie.martin@example.com",
        "phone": "+33 6 12 34 56 78",
        "nationality": "FR",
        "profession": "Architecte",
        "employer": "Atelier Nord",
        "date_of_birth": date(1984, 3, 15),
        "custom_fields": {},
    }
    data.update(overrides)
    return ClientProfile(**data)


def _company(name: str = "Acme", **values: Any) -> CompanyProfile:
    return CompanyProfile(id=uuid.uuid4(), name=name, custom_fields=values)


def _agency(**overrides: Any) -> Agency:
    data: dict[str, Any] = {
        "name": "Horizon Expat",
        "legal_name": "Horizon Expat SAS",
        "city": "Lisbonne",
        "country": "PT",
        "contact_email": "contact@horizon.example",
        "contact_phone": "+351 21 000 00 00",
        "default_language": "fr",
    }
    data.update(overrides)
    return Agency(**data)


def _context(**overrides: Any) -> RenderContext:
    data: dict[str, Any] = {
        "client": _client(),
        "companies": (
            _company(
                "Acme",
                legal_form="SAS",
                industry="Logiciel",
                website="https://acme.example",
                employee_count=42,
                country="FR",
            ),
        ),
        "agency": _agency(),
        "today": TODAY,
    }
    data.update(overrides)
    return RenderContext(**data)


def _kinds(subject: str, body: str, ctx: RenderContext) -> list[tuple[str, str | None]]:
    return [(e.kind, e.token) for e in EmailTemplateRenderer().render(subject, body, ctx).errors]


# ── rendering ────────────────────────────────────────────────────────────


def test_client_company_and_agency_variables_resolve() -> None:
    result = EmailTemplateRenderer().render(
        "{{client.first_name}}, une question sur {{ company.name }}",
        "Bonjour {{client.full_name}},\n"
        "{{company.legal_form}} · {{company.industry}} · {{company.website}} · "
        "{{company.employee_count}} · {{company.country}}\n"
        "{{client.profession}} chez {{client.employer}}, {{client.age}} ans, "
        "{{client.nationality}}, {{client.phone}}, {{client.email}}\n"
        "{{agency.name}} ({{agency.legal_name}}), {{agency.city}} {{agency.country}}, "
        "{{agency.contact_email}} {{agency.contact_phone}}",
        _context(),
    )
    assert result.ok, result.errors
    assert result.subject == "Marie, une question sur Acme"
    assert result.body == (
        "Bonjour Marie Martin,\n"
        "SAS · Logiciel · https://acme.example · 42 · France\n"
        "Architecte chez Atelier Nord, 42 ans, France, +33 6 12 34 56 78, "
        "marie.martin@example.com\n"
        "Horizon Expat (Horizon Expat SAS), Lisbonne Portugal, "
        "contact@horizon.example +351 21 000 00 00"
    )
    sources = {v.token: v.source for v in result.variables}
    assert sources["client.first_name"] == "client"
    assert sources["company.name"] == "company"
    assert sources["agency.name"] == "agency"
    assert result.resolved_map()["company.name"] == "Acme"


def test_a_missing_value_is_reported_and_stays_visible() -> None:
    result = EmailTemplateRenderer().render(
        "Hi", "Secteur : {{company.industry}}.", _context(companies=(_company("Acme"),))
    )
    assert not result.ok
    assert result.body == "Secteur : {{company.industry}}."
    assert [(e.kind, e.token) for e in result.errors] == [("missing_variable", "company.industry")]


def test_a_fallback_replaces_a_missing_value_and_only_then() -> None:
    bare = _company("Acme")
    result = EmailTemplateRenderer().render(
        '{{company.name | "votre société"}}',
        'Site : {{company.website | "votre site"}}. Forme : {{ company.legal_form|“” }}.',
        _context(companies=(bare,)),
    )
    # A value wins over its fallback; a missing one takes it, even empty.
    assert result.ok, result.errors
    assert result.subject == "Acme"
    assert result.body == "Site : votre site. Forme : ."
    used = {v.token: (v.value, v.fallback) for v in result.variables}
    assert used["company.name"] == ("Acme", None)
    assert used["company.website"] == (None, "votre site")
    # What was sent is recorded as the text the email carried.
    assert result.resolved_map()["company.website"] == "votre site"
    assert tokens_in('{{company.website | "x"}} {{company.website}}') == ["company.website"]
    # Without a fallback the same value still blocks.
    plain = _kinds("Hi", "{{company.website}}", _context(companies=(bare,)))
    assert plain == [("missing_variable", "company.website")]


def test_an_ai_variable_takes_no_fallback_and_a_broken_one_is_malformed() -> None:
    result = EmailTemplateRenderer(require_ai=False).render(
        "",
        '{{ai.hook | "x"}} {{company.website | votre site}} {{company.website | "a}}',
        _context(ai_values={"hook": "Bonjour"}, ai_keys=frozenset({"hook"})),
    )
    assert [e.kind for e in result.errors] == ["malformed_token"] * 3
    assert "An AI variable takes no fallback" in result.errors[0].message


def test_unknown_and_malformed_tokens_are_errors() -> None:
    # Prism's variables (`contact.*`, the M&A `company.revenue`) are unknown
    # in Nidria, like any token outside the catalog.
    result = EmailTemplateRenderer().render(
        "",
        "{{company.revenue}} {{client.nickname}} {{contact.email}} {{ not a token }}",
        _context(),
    )
    assert sorted(e.kind for e in result.errors) == [
        "malformed_token",
        "unknown_variable",
        "unknown_variable",
        "unknown_variable",
    ]
    assert "{{company.revenue}}" in result.body
    assert lookup("company.revenue") is None and lookup("contact.email") is None


def test_no_client_and_no_email_are_reported() -> None:
    assert _kinds("Hi", "Hello", _context(client=None)) == [("no_client", None)]
    assert _kinds("Hi", "Hello", _context(client=_client(email=None))) == [
        ("no_email", "client.email")
    ]
    assert _kinds("Hi", "Hello", _context(client=_client(email="  "))) == [
        ("no_email", "client.email")
    ]


def test_ai_variables_need_a_value_and_a_definition() -> None:
    context = _context(ai_keys=frozenset({"company_context"}))
    pending = EmailTemplateRenderer().render("", "{{ai.company_context}}", context)
    assert [e.kind for e in pending.errors] == ["ai_not_generated"]
    # A preview renders before generation without blocking on it.
    assert EmailTemplateRenderer(require_ai=False).render("", "{{ai.company_context}}", context).ok
    undefined = EmailTemplateRenderer().render("", "{{ai.icebreaker}}", context)
    assert [e.kind for e in undefined.errors] == ["unknown_variable"]
    done = EmailTemplateRenderer().render(
        "",
        "{{ai.company_context}}",
        _context(ai_keys=frozenset({"company_context"}), ai_values={"company_context": "Ok."}),
    )
    assert done.ok and done.body == "Ok." and done.variables[0].source == "ai"


def test_tokens_and_content_hash() -> None:
    assert tokens_in("{{client.first_name}} {{ai.x}}", "{{client.first_name}}") == [
        "client.first_name",
        "ai.x",
    ]
    assert parse_token(' client.first_name | "vous" ') == ("client.first_name", "vous")
    assert parse_token("Client.First") is None
    assert is_valid_ai_key("hook_2") and not is_valid_ai_key("2hook")
    base = content_hash("S", "B", {"x": "prompt|"})
    assert base == content_hash("S", "B", {"x": "prompt|"})
    assert base != content_hash("S", "B", {"x": "other prompt|"})
    assert base != content_hash("S", "B2", {"x": "prompt|"})
    assert base != content_hash("S2", "B", {"x": "prompt|"})


# ── Nidria's rules: company, identity, derived values ────────────────────


def test_a_client_without_a_company_renders_when_no_company_token_is_used() -> None:
    result = EmailTemplateRenderer().render(
        "Bonjour {{client.first_name}}",
        "Votre conseillère chez {{agency.name}}.",
        _context(companies=()),
    )
    assert result.ok, result.errors
    assert result.body == "Votre conseillère chez Horizon Expat."


def test_no_company_is_reported_only_when_a_company_token_needs_one() -> None:
    no_company = _context(companies=())
    assert sorted(_kinds("Hi", "Chez {{company.name}} ({{company.country}})", no_company)) == [
        ("missing_variable", "company.country"),
        ("missing_variable", "company.name"),
        ("no_company", None),
    ]
    # A fallback covers a client without a company too.
    covered = EmailTemplateRenderer().render("Hi", 'Chez {{company.name | "vous"}}', no_company)
    assert covered.ok and covered.body == "Chez vous"


def test_several_companies_leave_the_company_variables_missing() -> None:
    two = _context(companies=(_company("Acme"), _company("Globex")))
    assert _kinds("Hi", "{{company.name}}", two) == [("missing_variable", "company.name")]
    fallback = EmailTemplateRenderer().render("Hi", '{{company.name | "votre société"}}', two)
    assert fallback.ok and fallback.body == "votre société"
    # One company linked under two roles is still one company.
    acme = _company("Acme")
    twice = _context(companies=(acme, acme))
    assert EmailTemplateRenderer().render("Hi", "{{company.name}}", twice).body == "Acme"
    assert sole_company([acme, acme]) is acme
    assert sole_company([]) is None


def test_the_identity_is_the_linked_accounts_else_the_records() -> None:
    account = ExpatUser(
        id=uuid.uuid4(),
        first_name="Ana",
        last_name="Souza",
        email="ana.souza@example.com",
        preferred_lang="pt",
    )
    linked = _client(
        expat_user_id=account.id, first_name="Anna", last_name="Sousa", email="old@example.com"
    )
    result = EmailTemplateRenderer().render(
        "{{client.first_name}}",
        "{{client.full_name}} <{{client.email}}>",
        _context(client=linked, account=account),
    )
    assert result.ok, result.errors
    assert (result.subject, result.body) == ("Ana", "Ana Souza <ana.souza@example.com>")
    # Not linked: the record's own identity.
    assert client_identity(_client(), None).email == "marie.martin@example.com"
    # Linked to someone else: refused, never someone else's address.
    stranger = ExpatUser(id=uuid.uuid4(), first_name="X", last_name="Y", email="x@example.com")
    with pytest.raises(ValueError):
        client_identity(linked, stranger)


@pytest.mark.parametrize(
    ("born", "today", "expected"),
    [
        (date(1984, 10, 8), date(2026, 10, 7), "41"),
        (date(1984, 10, 7), date(2026, 10, 7), "42"),
        (date(2000, 2, 29), date(2026, 2, 28), "25"),
        (date(2000, 2, 29), date(2026, 3, 1), "26"),
        (date(2030, 1, 1), date(2026, 10, 7), None),
        (None, date(2026, 10, 7), None),
    ],
)
def test_the_client_age_is_counted_from_the_date_of_birth(
    born: date | None, today: date, expected: str | None
) -> None:
    result = EmailTemplateRenderer().render(
        "", "{{client.age}}", _context(client=_client(date_of_birth=born), today=today)
    )
    assert result.resolved_map()["client.age"] == expected
    assert result.ok is (expected is not None)


def test_the_client_status_is_the_derived_status_named_in_the_agency_language() -> None:
    def status(**overrides: Any) -> str | None:
        return (
            EmailTemplateRenderer()
            .render("", "{{client.status}}", _context(**overrides))
            .resolved_map()["client.status"]
        )

    assert status() == "Prospect"
    assert status(cases=(ClientCase(),)) == "Client"
    # The agency's explicit status wins over the derivation.
    assert status(client=_client(status_override="prospect"), cases=(ClientCase(),)) == "Prospect"
    assert status(client=_client(status_override="client")) == "Client"
    # Named in the agency's default language, whatever the client's own.
    hungarian = _agency(default_language="hu")
    assert status(agency=hungarian) == "Érdeklődő"
    assert status(agency=hungarian, client=_client(preferred_lang="es"), cases=(ClientCase(),)) == (
        "Ügyfél"
    )
    # No agency in the context: the platform's language.
    assert status(agency=None) == "Prospect"
    # Every language names both statuses.
    for lang, labels in CLIENT_STATUS_LABELS.items():
        assert set(labels) == {"prospect", "client"}, lang
        assert all(label and EM_DASH not in label for label in labels.values())


def test_the_client_language_follows_the_record_then_the_account_then_the_agency() -> None:
    def language(**overrides: Any) -> str | None:
        return (
            EmailTemplateRenderer()
            .render("", "{{client.preferred_lang}}", _context(**overrides))
            .resolved_map()["client.preferred_lang"]
        )

    account = ExpatUser(first_name="Ana", last_name="S", email="a@example.com", preferred_lang="pt")
    assert language(client=_client(preferred_lang="es"), account=account) == "es"
    assert language(client=_client(preferred_lang=None), account=account) == "pt"
    assert language(agency=_agency(default_language="it")) == "it"
    assert language(agency=None) == "en"


def test_company_values_come_from_the_preset_keys_as_stored() -> None:
    ctx = _context(
        companies=(_company("Acme", employee_count=25.0, country="FR", website="acme.example"),)
    )
    result = EmailTemplateRenderer().render(
        "", "{{company.employee_count}} {{company.country}} {{company.website}}", ctx
    )
    # A whole number reads as one; the country reads as its name (question
    # R8); the website as the agency typed it.
    assert result.body == "25 France acme.example"
    odd = _context(companies=(_company("Acme", industry={"x": 1}, legal_form=True),))
    assert sorted(_kinds("", "{{company.industry}} {{company.legal_form}}", odd)) == [
        ("missing_variable", "company.industry"),
        ("missing_variable", "company.legal_form"),
    ]


def test_the_catalog_is_nidrias_and_serves_codes_not_sentences() -> None:
    assert [v.token for v in catalog_for()] == [
        "client.first_name",
        "client.last_name",
        "client.full_name",
        "client.email",
        "client.phone",
        "client.nationality",
        "client.profession",
        "client.employer",
        "client.age",
        "client.preferred_lang",
        "client.status",
        "company.name",
        "company.legal_form",
        "company.industry",
        "company.website",
        "company.employee_count",
        "company.country",
        "agency.name",
        "agency.legal_name",
        "agency.city",
        "agency.country",
        "agency.contact_email",
        "agency.contact_phone",
    ]
    for variable in CATALOG:
        assert variable.source == variable.namespace
        assert lookup(variable.token) is variable
        assert variable.fallback_label and EM_DASH not in variable.fallback_label
        assert EM_DASH not in (variable.example or "")
    # Every variable resolves on a full context, through the same code as a send.
    full = _context(
        account=None,
        client=_client(preferred_lang="fr"),
        cases=(ClientCase(),),
    )
    tokens = " ".join("{{" + v.token + "}}" for v in CATALOG)
    assert EmailTemplateRenderer().render("", tokens, full).ok


@pytest.mark.parametrize(
    ("lang", "france", "portugal"),
    [
        ("fr", "France", "Portugal"),
        ("en", "France", "Portugal"),
        ("es", "Francia", "Portugal"),
        ("ru", "Франция", "Португалия"),
        ("pt", "França", "Portugal"),
        ("it", "Francia", "Portogallo"),
        ("hu", "Franciaország", "Portugália"),
    ],
)
def test_countries_are_named_in_the_agency_language(lang: str, france: str, portugal: str) -> None:
    result = EmailTemplateRenderer().render(
        "",
        "{{client.nationality}} | {{company.country}} | {{agency.country}}",
        _context(agency=_agency(default_language=lang)),
    )
    assert result.ok, result.errors
    assert result.body == f"{france} | {france} | {portugal}"


def test_a_country_value_that_is_not_a_code_is_kept_and_the_language_stays_a_code() -> None:
    result = EmailTemplateRenderer().render(
        "",
        "{{client.nationality}} | {{company.country}} | {{client.preferred_lang}}",
        _context(
            client=_client(nationality="Française", preferred_lang="es"),
            companies=(_company("Acme", country="fr"),),
        ),
    )
    # Free text stays as typed; a lower-case code is still named; the
    # language has no name table in the backend: its code.
    assert result.body == "Française | France | es"
    assert result.resolved_map()["client.nationality"] == "Française"


# ── rows read back from the database ─────────────────────────────────────


async def test_rows_read_back_from_the_database_render(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_expat_user: MakeExpatUser,
) -> None:
    agency = await make_agency(
        name="Horizon Expat", city="Lisbonne", country="PT", default_language="fr"
    )
    account = await make_expat_user(
        first_name="Ana", last_name="Souza", email="ana.souza@example.com", preferred_lang="pt"
    )
    profile = ClientProfile(
        agency_id=agency.id,
        expat_user_id=account.id,
        first_name="Anna",
        last_name="Sousa",
        email="old@example.com",
        date_of_birth=date(1990, 5, 20),
    )
    company = CompanyProfile(
        agency_id=agency.id, name="Acme", custom_fields={"employee_count": 12, "country": "FR"}
    )
    db_session.add_all([profile, company])
    await db_session.flush()
    # Linked twice, under two roles: still the client's one company.
    db_session.add_all(
        [
            CompanyProfileRole(
                company_profile_id=company.id, client_profile_id=profile.id, role="manager"
            ),
            CompanyProfileRole(
                company_profile_id=company.id, client_profile_id=profile.id, role="partner"
            ),
        ]
    )
    await db_session.commit()
    db_session.expunge_all()

    loaded = (
        await db_session.execute(select(ClientProfile).where(ClientProfile.id == profile.id))
    ).scalar_one()
    linked_account = await db_session.get(ExpatUser, loaded.expat_user_id)
    linked = (
        select(CompanyProfile)
        .join(CompanyProfileRole, CompanyProfileRole.company_profile_id == CompanyProfile.id)
        .where(CompanyProfileRole.client_profile_id == loaded.id)
    )
    companies = (await db_session.execute(linked)).scalars().all()
    loaded_agency = await db_session.get(Agency, agency.id)
    assert len(companies) == 2  # one row per role

    result = EmailTemplateRenderer().render(
        "{{client.first_name}}",
        "{{client.email}} · {{client.age}} · {{client.preferred_lang}} · {{client.status}} · "
        "{{company.name}} · {{company.employee_count}} · {{company.country}} · "
        "{{agency.name}} {{agency.country}}",
        RenderContext(
            client=loaded,
            account=linked_account,
            companies=companies,
            agency=loaded_agency,
            today=TODAY,
        ),
    )
    assert result.ok, result.errors
    assert result.subject == "Ana"
    assert result.body == (
        "ana.souza@example.com · 36 · pt · Prospect · Acme · 12 · France · Horizon Expat Portugal"
    )


# ── body_to_html ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Lien : https://nidria.example",
            '<p>Lien : <a href="https://nidria.example">https://nidria.example</a></p>',
        ),
        (
            "Voir https://example.com/a?x=1&y=2.",
            '<p>Voir <a href="https://example.com/a?x=1&amp;y=2">'
            "https://example.com/a?x=1&amp;y=2</a>.</p>",
        ),
        (
            "(cf. https://example.com/p) et www.example.org, merci",
            '<p>(cf. <a href="https://example.com/p">https://example.com/p</a>) et '
            '<a href="https://www.example.org">www.example.org</a>, merci</p>',
        ),
        (
            "https://en.wikipedia.org/wiki/Foo_(bar) ok",
            '<p><a href="https://en.wikipedia.org/wiki/Foo_(bar)">'
            "https://en.wikipedia.org/wiki/Foo_(bar)</a> ok</p>",
        ),
        (
            "Fin : https://example.com/x!?",
            '<p>Fin : <a href="https://example.com/x">https://example.com/x</a>!?</p>',
        ),
        (
            'evil https://x.com/"onmouseover=alert(1) <b>',
            '<p>evil <a href="https://x.com/">https://x.com/</a>'
            "&quot;onmouseover=alert(1) &lt;b&gt;</p>",
        ),
        ("rien : http:// ni www.", "<p>rien : http:// ni www.</p>"),
        ("Bonjour,\n\nA & <b>\nB", "<p>Bonjour,</p><p>A &amp; &lt;b&gt;<br>B</p>"),
    ],
)
def test_a_body_becomes_html_with_real_links(body: str, expected: str) -> None:
    """Every web address is an `<a href>` (Lemlist counts the clicks of links
    only); the sentence's final punctuation stays out of it, a balanced
    closing bracket in; everything else stays escaped text."""
    assert body_to_html(body) == expected


# ── a reply's text ───────────────────────────────────────────────────────

REPLY_HTML = (
    '<div dir="ltr">Avec plaisir, appelons-nous jeudi.<br><br>Ana</div>'
    '<div class="gmail_quote"><div class="gmail_attr">On Mon, Oct 5, 2026 at 9:00 AM '
    "Horizon &lt;hello@horizon.example&gt; wrote:<br></div><blockquote>Bonjour Ana</blockquote>"
    "</div>"
)


@pytest.mark.parametrize(
    ("html", "text"),
    [
        (REPLY_HTML, "Avec plaisir, appelons-nous jeudi.\n\nAna"),
        (
            "<p>Merci !</p><p>Le lun. 5 oct. 2026, Horizon a écrit :</p><p>&gt; Bonjour</p>",
            "Merci !",
        ),
        ("<style>p{}</style><p>Oui<br>volontiers</p>", "Oui\nvolontiers"),
        ("<blockquote>only quoted</blockquote>", "only quoted"),
        ("Plain text reply\n\nOn Mon, Oct 5 Horizon wrote:\n> hi", "Plain text reply"),
        (None, ""),
    ],
)
def test_a_reply_is_read_as_text_without_its_quoted_history(html: str | None, text: str) -> None:
    assert html_to_text(html) == text
