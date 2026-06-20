"""BLOC NOTIF-2 — the 5 case-notification builders render subject + body +
<html lang> in the recipient language. The step name is already resolved
upstream (NOTIF-1); these tests cover the template translation itself, plus the
language routing (client ko → en, agent default → fr)."""

from src.core.email_templates import (
    new_comment_to_agent,
    new_comment_to_client,
    ready_to_validate_email,
    requirement_request_email,
    step_reopened_email,
)
from src.core.i18n import resolve_notification_lang_agent, resolve_notification_lang_client


def test_client_template_es_subject_body_and_html_lang() -> None:
    # A client whose resolved language is ES → ES subject, ES body, html lang=es,
    # and the (already-resolved) ES step name interpolated.
    c = requirement_request_email("Agencia X", "Presentación", "https://x/space", lang="es")
    assert c.subject == "Nidria — Se requiere nueva información"
    assert "necesita información o documentos para la etapa «Presentación»" in c.text
    assert 'html lang="es"' in c.html
    assert "Completar mi expediente" in c.html  # button localized


def test_client_unsupported_preferred_lang_falls_back_to_english() -> None:
    # A client with preferred_lang="ko" → resolver gives "en" → EN template.
    lang = resolve_notification_lang_client("ko")
    assert lang == "en"
    c = new_comment_to_client("Agency X", "Mary", "Submission", "https://x/space", lang=lang)
    assert c.subject == "Nidria — New message from your advisor"
    assert "wrote to you about the step “Submission”" in c.text
    assert 'html lang="en"' in c.html


def test_agent_template_default_fr() -> None:
    # An agent whose agency default is "fr" (or absent) → FR template.
    lang = resolve_notification_lang_agent("fr")
    assert lang == "fr"
    c = ready_to_validate_email("Dossier 42", "Examen médical", "https://x/app", lang=lang)
    assert c.subject == "Nidria — Un dossier est prêt à valider"
    assert "Toutes les informations attendues pour l'étape « Examen médical »" in c.text
    assert 'html lang="fr"' in c.html


def test_client_template_ru_subject_body_and_html_lang() -> None:
    # A client whose resolved language is RU → RU subject, RU body, html lang=ru.
    lang = resolve_notification_lang_client("ru")
    assert lang == "ru"
    c = requirement_request_email("Acme", "Виза D7", "https://x/space", lang=lang)
    assert c.subject == "Nidria — Ожидается новая информация"
    assert "для этапа «Виза D7»" in c.text
    assert 'html lang="ru"' in c.html
    assert "Заполнить моё дело" in c.html  # button localized


def test_agent_template_default_it() -> None:
    # An agent whose agency default is "it" → IT template.
    lang = resolve_notification_lang_agent("it")
    assert lang == "it"
    c = new_comment_to_agent("Jean Martin", "Numero fiscale", "https://x/app", lang=lang)
    assert c.subject == "Nidria — Nuovo messaggio dal tuo cliente"
    assert "ha scritto in merito alla fase «Numero fiscale»" in c.text
    assert 'html lang="it"' in c.html


def test_all_builders_interpolate_step_name_in_every_language() -> None:
    # Guard: no language drops the step-name (or other) placeholder.
    step = "STEP_MARKER"
    for lang in ("fr", "en", "es", "ru", "pt", "it"):
        assert step in requirement_request_email("A", step, "u", lang=lang).text
        assert step in step_reopened_email("A", step, "u", lang=lang).text
        assert step in ready_to_validate_email("Case", step, "u", lang=lang).text
        assert step in new_comment_to_client("A", "Auth", step, "u", lang=lang).text
        assert step in new_comment_to_agent("Client", step, "u", lang=lang).text
