"""Mail texts, wave C3 — what the reader sees in THEIR language.

Five families, one guard each:
1. typography: the space before « : » is French only; quotes per language;
2. plurals: a count agrees (CLDR — ru one/few/many, the others one/other);
3. fallbacks: an unknown agency / client / provider is named in the mail's
   language, never with a French literal;
4. no « Nidria : » prefix on client relationship subjects (decision 14/08),
   while the account and agent mails keep it;
5. the signup mails carry their accents.
"""

import re

import pytest

import src.core.email_templates as et
from src.core.email import sender_as_agency
from src.core.email_templates import (
    activation_reminder_email,
    agent_invitation_email,
    auto_reminder_body,
    digest_email,
    document_signed_client_email,
    expat_activation_email,
    journey_kickoff_email,
    new_case_email,
    new_comment_to_agent,
    new_comment_to_client,
    password_reset_email,
    provider_fallback_name,
    requirement_request_email,
    signup_code_email,
    signup_existing_account_email,
    step_reopened_email,
)
from src.core.i18n import SUPPORTED_LANGUAGES


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


def _catalogs() -> dict[str, dict[str, object]]:
    return {
        name: value
        for name, value in vars(et).items()
        if name.startswith("_") and isinstance(value, dict) and "fr" in value
    }


# --- 1. typography --------------------------------------------------------------------


def test_link_lines_have_no_space_before_the_colon_outside_french() -> None:
    link, login = "https://x.test/activate/tok", "https://x.test/space/login"
    for lang in ("en", "ru"):
        mail = expat_activation_email("Acme", link, 14, None, lang, login_link=login)
        assert " :" not in mail.text, lang
        assert "&nbsp;:" not in mail.html, lang
        assert f"{et._CLIENT_ENTRY[lang]['activate']}: {link}" in mail.text
        assert f"{et._COPY_PASTE[lang]}: <a href" in mail.html
    # French keeps its typography, byte for byte.
    fr = expat_activation_email("Acme", link, 14, None, "fr", login_link=login)
    assert f"Activer mon espace client : {link}" in fr.text
    assert "Ou copiez-collez ce lien&nbsp;: <a href" in fr.html


def test_no_catalog_string_puts_a_space_before_punctuation_outside_french() -> None:
    offenders = [
        f"{name}[{lang}]: {text}"
        for name, catalog in _catalogs().items()
        for lang, block in catalog.items()
        if lang != "fr"
        for text in _strings(block)
        # « &nbsp;: » is the French HTML chrome — never in another language.
        if re.search(r"\s[:;?!]|&nbsp;[:;?!]", text)
    ]
    assert offenders == []


def test_quotes_follow_each_language() -> None:
    offenders: list[str] = []
    for name, catalog in _catalogs().items():
        for lang, block in catalog.items():
            for text in _strings(block):
                wrong = {
                    "en": r"[«»„]",
                    "hu": r"[«»“]",
                    "es": r"[“„]|« | »",
                    "pt": r"[“„]|« | »",
                    "it": r"[“„]|« | »",
                    "ru": r"[“„]|« | »",
                }.get(lang)
                if wrong and re.search(wrong, text):
                    offenders.append(f"{name}[{lang}]: {text}")
                if lang != "fr" and re.search(r'"\{', text):  # straight quotes
                    offenders.append(f"{name}[{lang}]: {text}")
    assert offenders == []


# --- 2. plurals -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [(1, "1 день"), (2, "2 дня"), (5, "5 дней"), (21, "21 день"), (11, "11 дней"), (22, "22 дня")],
)
def test_russian_days_agree_with_the_count(days: int, expected: str) -> None:
    assert auto_reminder_body("Виза", days, "ru").endswith(f"не продвигался {expected}.")
    validity = f"Эта ссылка действительна {expected}."
    assert validity in agent_invitation_email("Acme", "u", days, "ru").text
    assert validity in expat_activation_email("Acme", "u", days, None, "ru").text
    reminder = activation_reminder_email("A", "u", days, None, "ru")
    assert f"Ссылка действует ещё {expected}." in reminder.text


@pytest.mark.parametrize(
    ("count", "item"), [(1, "1 элемент"), (2, "2 элемента"), (5, "5 элементов"), (21, "21 элемент")]
)
def test_russian_kickoff_counts_agree(count: int, item: str) -> None:
    mail = journey_kickoff_email("Acme", [("Виза", count)], "u", "ru")
    assert f"от вас ожидается {item} для начала" in mail.text
    assert f"- Виза: {item}" in mail.text


def test_russian_hours_and_minutes_agree() -> None:
    def validity(minutes: int) -> str:
        return password_reset_email("u", minutes, "ru").text

    assert "действительна 24 часа." in validity(24 * 60)  # was « 24 часов »
    assert "действительна 21 час." in validity(21 * 60)
    assert "действительна 5 часов." in validity(5 * 60)
    assert "действительна 1 минуту." in validity(1)
    assert "действительна 60 минут." in validity(60)


def test_singular_and_plural_in_the_other_languages() -> None:
    assert "This link expires in 1 day." in activation_reminder_email("A", "u", 1, None, "en").text
    assert "This link expires in 2 days." in activation_reminder_email("A", "u", 2, None, "en").text
    assert "Ce lien expire dans 1 jour." in expat_activation_email("A", "u", 1, None, "fr").text
    assert "depuis 1 jour." in auto_reminder_body("Visa", 1, "fr")
    assert "desde hace 1 día." in auto_reminder_body("Visa", 1, "es")
    assert "há 1 dia." in auto_reminder_body("Visa", 1, "pt")
    assert "da 1 giorno." in auto_reminder_body("Visa", 1, "it")
    kickoff = journey_kickoff_email("A", [("Visa", 1)], "u", "it").text
    assert "ti viene richiesto 1 elemento" in kickoff and "- Visa: 1 elemento" in kickoff
    digest = digest_email("A", "weekly", ["s1"], ["s2", "s3"], 1, "u", "fr").text
    assert "1 étape terminée, 2 étapes démarrées, 1 document validé" in digest
    # Hungarian: the noun stays singular after a numeral — one wording.
    assert auto_reminder_body("V", 1, "hu").replace("1", "N") == auto_reminder_body(
        "V", 5, "hu"
    ).replace("5", "N")


def test_no_catalog_hedges_a_plural_anymore() -> None:
    # « invité(e) », « написал(а) » hedge a GENDER, not a count: left alone.
    hedges = re.compile(r"\((s|i|es|ов)\)|a\(e\)|\w/i\b")
    offenders = [
        f"{name}[{lang}]: {text}"
        for name, catalog in _catalogs().items()
        for lang, block in catalog.items()
        for text in _strings(block)
        if hedges.search(text)
    ]
    assert offenders == []


# --- 3. fallbacks in the mail's language ----------------------------------------------


@pytest.mark.parametrize(
    ("lang", "agency"),
    [("en", "Your agency"), ("ru", "Ваше агентство"), ("es", "Su agencia"), ("fr", "Votre agence")],
)
def test_an_unknown_agency_is_named_in_the_mail_language(lang: str, agency: str) -> None:
    mails = [
        expat_activation_email(None, "u", 14, None, lang),
        new_case_email(None, "u", None, lang),
        requirement_request_email(None, "Step", "u", lang),
        step_reopened_email(None, "Step", "u", lang),
        journey_kickoff_email(None, [("Step", 2)], "u", lang),
        document_signed_client_email(None, "Ref", "u", lang),
        new_comment_to_client(None, "Marie", "Step", "u", lang),
    ]
    for mail in mails:
        assert agency in mail.text, mail.subject
        if lang != "fr":
            assert "Votre agence" not in mail.text + mail.html


def test_a_nameless_client_and_a_missing_provider_are_named_in_the_agent_language() -> None:
    assert "Il tuo cliente ha scritto" in new_comment_to_agent(None, "Step", "u", "it").text
    assert "Ваш клиент написал(а)" in new_comment_to_agent("", "Step", "u", "ru").text
    assert provider_fallback_name("en") == "(name unknown)"
    assert provider_fallback_name("fr") == "(nom inconnu)"
    for lang in SUPPORTED_LANGUAGES:
        if lang != "fr":
            assert provider_fallback_name(lang) != "(nom inconnu)"


def test_the_missing_provider_never_repeats_the_word_provider() -> None:
    # « Le prestataire ce prestataire doit… » (01/10): the stand-in names an
    # unknown NAME, it is not a second noun phrase.
    def text(lang: str) -> str:
        return et.reminder_escalation_email("A", et.provider_fallback_name(lang), "x", lang).text

    assert "Le prestataire (nom inconnu) doit" in text("fr")
    assert "Il fornitore (nome sconosciuto) deve" in text("it")


def test_the_sender_falls_back_to_ours_when_the_agency_is_unknown() -> None:
    configured = sender_as_agency(None)
    assert configured == sender_as_agency("")
    assert "Votre agence" not in configured


# --- 4. no « Nidria : » prefix on client relationship subjects ------------------------


_CLIENT_CATALOGS = (
    "_REQUIREMENT_REQUEST",
    "_STEP_REOPENED",
    "_NEW_COMMENT_CLIENT",
    "_DOCUMENT_SIGNED_CLIENT",
    "_JOURNEY_KICKOFF",
    "_DIGEST",
    "_CASE_ACTIVATION",
    "_NEW_CASE",
    "_ACTIVATION_REMINDER",
    "_REMINDER",
)


def test_client_subjects_carry_no_nidria_prefix() -> None:
    offenders = [
        f"{name}[{lang}]: {block['subject']}"
        for name in _CLIENT_CATALOGS
        for lang, block in getattr(et, name).items()
        if block["subject"].lower().startswith("nidria")
    ]
    assert offenders == []
    # Rendered, as the client reads it — and the subject starts with a capital.
    for mail in (
        requirement_request_email("Acme", "Visa", "u", "ru"),
        document_signed_client_email("Acme", "Passport", "u", "en"),
        digest_email("Acme", "daily", ["s"], [], 0, "u", "hu"),
    ):
        assert not mail.subject.startswith("Nidria")
        assert mail.subject[0].isupper()


def test_account_and_agent_mails_keep_our_name() -> None:
    # Decision 14/08: the password reset is an ACCOUNT gesture, in our name.
    for lang in SUPPORTED_LANGUAGES:
        assert password_reset_email("u", 60, lang).subject.startswith("Nidria")
        assert agent_invitation_email("Acme", "u", 7, lang).subject.startswith("Nidria")
        assert signup_code_email("123456", lang).subject.startswith("Nidria")


# --- 5. the signup mails carry their accents ------------------------------------------


def test_signup_mails_have_their_accents() -> None:
    code_fr = signup_code_email("123456", "fr")
    assert code_fr.subject == "Nidria : votre code de vérification"
    assert "pour créer votre espace" in code_fr.text
    assert "Si vous n'avez pas demandé ce code" in code_fr.text
    existing_fr = signup_existing_account_email("https://x.test/login", "fr")
    assert existing_fr.subject == "Nidria : vous avez déjà un compte"
    assert "Une création d'espace a été demandée" in existing_fr.text
    assert "mot de passe oublié ?" in existing_fr.text
    assert "Si vous n'êtes pas à l'origine" in existing_fr.text
    assert "Aquí tiene su código" in signup_code_email("1", "es").text
    assert "Inicie sesión a continuación;" in signup_existing_account_email("u", "es").text
    assert "o seu código de verificação" in signup_code_email("1", "pt").subject
    assert (
        "já existe uma conta. Inicie sessão abaixo;"
        in signup_existing_account_email("u", "pt").text
    )
    assert "È stata richiesta" in signup_existing_account_email("u", "it").text
    unaccented = re.compile(
        r"\b(verification|creer|deja|demande ce|reinitialisation|codigo|verificacion|"
        r"direccion|sesion|continuacion|realizo|verificacao|espaco|endereco|sessao|nao|"
        r"criacao|gia)\b"
    )
    for lang in ("fr", "es", "pt", "it"):
        for text in _strings(et._SIGNUP_CODE[lang]) + _strings(et._SIGNUP_EXISTING[lang]):
            assert not unaccented.search(text), f"{lang}: {text}"


@pytest.mark.parametrize(
    ("lang", "fragment"),
    [
        ("fr", "mot de passe oublié"),
        ("en", "forgot your password"),
        ("es", "ha olvidado su contraseña"),
        ("ru", "забыли пароль"),
        ("pt", "esqueceu-se da palavra-passe"),
        ("it", "hai dimenticato la"),
        ("hu", "elfelejtett jelszó"),
    ],
)
def test_signup_existing_account_points_to_the_reset_in_every_language(
    lang: str, fragment: str
) -> None:
    """es/ru/pt/it lacked the pointer to the password reset that fr/en/hu
    carried (C3 report): a user with an account was told to log in, never
    how to recover a forgotten password."""
    from src.core.email_templates import signup_existing_account_email

    mail = signup_existing_account_email("https://app.nidria.com/login", lang)
    assert fragment in mail.text.lower()
