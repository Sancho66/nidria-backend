"""i18n resolution (BLOC 2) — the central resolver's fallback chain and the
request-language channel. Pure-function unit tests: no DB, fast. The
projection wiring is exercised by the domain tests (timeline/clone)."""

from datetime import date

from src.core.i18n import (
    DEFAULT_LANG,
    SUPPORTED_LANGUAGES,
    format_date_for_lang,
    resolve_i18n,
    resolve_notification_lang_agent,
    resolve_notification_lang_client,
    resolve_step_name_for_notif,
)


def test_resolve_prefers_requested_language() -> None:
    blob = {"fr": "Casier", "en": "Record", "es": "Antecedentes"}
    assert resolve_i18n(blob, "en", "fr", "Casier") == "Record"
    assert resolve_i18n(blob, "es", "fr", "Casier") == "Antecedentes"


def test_resolve_falls_back_to_agency_default_when_language_missing() -> None:
    # "en" absent → fall back to the AGENCY default ("es" here), not "fr".
    blob = {"fr": "Casier", "es": "Antecedentes"}
    assert resolve_i18n(blob, "en", "es", "Casier") == "Antecedentes"


def test_resolve_falls_back_to_fr_for_a_sample() -> None:
    # A sample passes agency_default="fr" (no agency row). "en" absent, only fr.
    blob = {"fr": "Casier"}
    assert resolve_i18n(blob, "en", DEFAULT_LANG, "Casier") == "Casier"


def test_resolve_falls_back_to_scalar_when_blob_empty() -> None:
    # Transitional safety: an empty/None blob always yields the legacy scalar.
    assert resolve_i18n({}, "en", "fr", "Legacy") == "Legacy"
    assert resolve_i18n(None, "en", "fr", "Legacy") == "Legacy"


def test_resolve_never_returns_empty_string() -> None:
    # An absent language is an absent key — an empty value falls through to the
    # next candidate (here the scalar), never surfaces as "".
    assert resolve_i18n({"en": ""}, "en", "fr", "Scalar") == "Scalar"


def test_resolve_optional_field_with_no_value_is_none() -> None:
    # An optional field (content_note/description) with nothing anywhere → None
    # (genuinely "no content"), the only case where None is allowed.
    assert resolve_i18n({}, "en", "fr", None) is None


# --- notification language (BLOC NOTIF-1) -------------------------------------


def test_notif_lang_client_supported_kept() -> None:
    for lang in SUPPORTED_LANGUAGES:  # x7 automatique, la liste centrale derive
        assert resolve_notification_lang_client(lang) == lang


def test_notif_lang_client_unsupported_falls_back_to_english() -> None:
    # A client whose preferred_lang is not supported gets ENGLISH (NOT the
    # agency default fr). NB: ru/pt/it are now supported (6-language capacity),
    # so the "unsupported" examples are genuinely-absent codes.
    for lang in ("de", "ko", "zh", None, ""):
        assert resolve_notification_lang_client(lang) == "en"


def test_notif_lang_agent_default_kept_else_french() -> None:
    assert resolve_notification_lang_agent("es") == "es"
    assert resolve_notification_lang_agent("en") == "en"
    # Absent/unsupported agency default → fr.
    assert resolve_notification_lang_agent(None) == "fr"
    assert resolve_notification_lang_agent("de") == "fr"


def test_resolve_step_name_for_notif_never_empty() -> None:
    blob = {"fr": "Dépôt", "en": "Submission"}
    assert resolve_step_name_for_notif(blob, "Dépôt", "en") == "Submission"
    # No EN variant → falls back through fr → the scalar (never empty).
    assert resolve_step_name_for_notif({"fr": "Dépôt"}, "Dépôt", "en") == "Dépôt"
    assert resolve_step_name_for_notif({}, "Dépôt", "es") == "Dépôt"


# --- dates in the recipient's language (BLOC NOTIF-2) ------------------------


def test_a_date_is_written_the_way_each_language_writes_it() -> None:
    """THE point of the formatter: 05/09/2026 is ambiguous the moment a human
    reads it (September 5th or 5 September?). The month is spelled out and the
    ORDER follows the language — English puts the month first, Hungarian the
    year, Spanish and Portuguese insert their « de »."""
    day = date(2026, 9, 5)
    assert format_date_for_lang(day, "fr") == "5 septembre 2026"
    assert format_date_for_lang(day, "en") == "September 5, 2026"
    assert format_date_for_lang(day, "es") == "5 de septiembre de 2026"
    assert format_date_for_lang(day, "pt") == "5 de setembro de 2026"
    assert format_date_for_lang(day, "it") == "5 settembre 2026"
    assert format_date_for_lang(day, "ru") == "5 сентября 2026 г."
    assert format_date_for_lang(day, "hu") == "2026. szeptember 5."


def test_every_supported_language_can_write_every_month() -> None:
    """Adding a language to SUPPORTED_LANGUAGES without its months would be a
    KeyError in the middle of a send. The module asserts it at import; this
    walks the whole table so a truncated one cannot pass either."""
    for lang in SUPPORTED_LANGUAGES:
        rendered = {format_date_for_lang(date(2026, m, 1), lang) for m in range(1, 13)}
        assert len(rendered) == 12  # twelve distinct months, none empty
        assert all(text.strip() and "2026" in text for text in rendered)


def test_an_unsupported_language_falls_back_rather_than_raising() -> None:
    """A formatting detail must never break a send: German is not supported,
    the date still comes out — in the platform default."""
    assert format_date_for_lang(date(2026, 9, 5), "de") == "5 septembre 2026"
    assert format_date_for_lang(date(2026, 9, 5), "") == "5 septembre 2026"
