"""LLM translation client — Z.ai (OpenAI-compatible), GLM flash tier.

Raw HTTP only: one batch call, strict JSON in/out, 30s timeout, 1 retry.
The PROMPT rules mirror the 77-samples editorial rules: official scheme
names, legal terms, acronyms and amounts stay VERBATIM; no em/en dash in
the output (plus a mechanical post-processing safety net — the dash
guard applies to what we store, whatever the model produced).

`validate_translations` is the single gate between the model output and
the database: full key/language coverage, plausible lengths, dash strip.
Any failure raises BEFORE anything is written or debited.

`request_translations_with_repair` is the worker's entry point: one
batch call, then a SINGLE stricter repair pass on the items the model
botched, then FIELD-grain results — (valid translations, failed keys,
usages). Diagnosed live (2026-07-06, 5 real runs): the dominant failure
is the model ECHOING an item verbatim when its text is not in the
declared source language (e.g. an English description in a FR journey);
romanized output, broken JSON and timeouts were never observed. The
prompt now says to translate regardless, the RU check requires a
CYRILLIC RATIO (verbatim scheme names/acronyms stay legitimately latin),
and a persistent miss costs one field, not the lot."""

import json
import logging
from typing import Any

import httpx

from src.core.config import get_settings
from src.core.exceptions import UpstreamError

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You translate the content of expatriation journey templates for a CRM. "
    "Strict rules: keep official scheme names, legal terms, acronyms, codes and "
    "amounts VERBATIM (examples: 'Pink Slip', 'D7', 'KITAS', 'RUC', '500 000 EUR'); "
    "translate everything else faithfully, never summarize, never add content; "
    "some items may be written in ANOTHER language than the declared source: translate "
    "them to the target languages all the same, NEVER copy an item unchanged; "
    "write every language in its own NATIVE SCRIPT (Cyrillic for Russian, never "
    "romanized); NEVER use em dashes or en dashes in the output; answer with STRICT JSON only, "
    'shaped {"translations": {"<key>": {"<lang>": "<text>", ...}, ...}} covering '
    "every requested key and every requested language."
)

# Diagnosed 2026-07-06: the model sometimes ECHOES an item verbatim
# (usually one whose text is not in the declared source language). The
# ru lot carries an explicit Cyrillic reinforcement, and the repair
# retry a harder reminder still.
_RU_PROMPT_SUFFIX = (
    " For Russian ('ru'): write EXCLUSIVELY in the Russian Cyrillic alphabet "
    "(verbatim scheme names, acronyms and amounts excepted), for example "
    '{"translations": {"step.1.name": {"ru": "Подача досье в консульство"}}}.'
)
_STRICT_RETRY_SUFFIX = (
    " STRICT RETRY: your previous answer copied or mistranslated these exact items. "
    "TRANSLATE each one into every requested target language, whatever language its "
    "source text is written in."
)


def _system_prompt(target_langs: list[str], strict_retry: bool) -> str:
    prompt = _SYSTEM_PROMPT
    if "ru" in target_langs:
        prompt += _RU_PROMPT_SUFFIX
    if strict_retry:
        prompt += _STRICT_RETRY_SUFFIX
    return prompt


def strip_dashes(text: str) -> str:
    """Safety net (règle Eric): whatever the model produced, no em/en
    dash ever reaches the database."""
    return text.replace(" — ", " : ").replace("—", "-").replace("–", "-")


async def request_translations(
    items: list[dict[str, str]],
    source_lang: str,
    target_langs: list[str],
    strict_retry: bool = False,
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """One batch call → (raw translations by key, provider usage dict).
    Raises UpstreamError on transport/shape failure — nothing debited.
    `strict_retry` hardens the instruction for the repair pass."""
    settings = get_settings()
    if not settings.ai_translation_api_key:
        raise UpstreamError("AI translation is not configured.", code="ai.not_configured")
    body = {
        "model": settings.ai_translation_model,
        "temperature": 0.2,
        # Translation needs no chain-of-thought: GLM's thinking mode
        # triples the latency/tokens for nothing here (measured: 304
        # reasoning tokens for 2 items). Z.ai ignores it politely on
        # models without the switch.
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _system_prompt(target_langs, strict_retry)},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "source_language": source_lang,
                        "target_languages": target_langs,
                        "items": items,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Authorization": f"Bearer {settings.ai_translation_api_key}"}
    url = f"{settings.ai_translation_base_url.rstrip('/')}/chat/completions"
    data: dict[str, Any] | None = None
    for attempt in (1, 2):  # 1 retry
        try:
            async with httpx.AsyncClient(timeout=settings.ai_translation_timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            break
        except (httpx.HTTPError, ValueError) as exc:
            if attempt == 2:
                raise UpstreamError(
                    "Translation provider unreachable or failing.",
                    code="ai.translation_failed",
                ) from exc
    assert data is not None
    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        translations = parsed["translations"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise UpstreamError(
            "Translation provider returned an unparseable answer.",
            code="ai.translation_failed",
        ) from exc
    if not isinstance(translations, dict):
        raise UpstreamError(
            "Translation provider returned an unexpected shape.",
            code="ai.translation_failed",
        )
    return translations, data.get("usage", {}) or {}


_TOKEN_EDGES = ".,;:()[]\u00ab\u00bb\"'!?"


def _cyrillic_ratio(value: str, source: str) -> float:
    """Cyrillic share of the ALPHABETIC characters of `value`, ignoring
    tokens present verbatim in the source \u2014 protected scheme names,
    acronyms and amounts legitimately stay latin ('Pink Slip', 'D7').
    An all-verbatim value has nothing to judge \u2192 1.0."""
    src_tokens = {t.strip(_TOKEN_EDGES).lower() for t in source.replace("/", " ").split()}
    kept = [
        ch
        for token in value.replace("/", " ").split()
        if token.strip(_TOKEN_EDGES).lower() not in src_tokens
        for ch in token
        if ch.isalpha()
    ]
    if not kept:
        return 1.0
    return sum(1 for ch in kept if "\u0400" <= ch <= "\u04ff") / len(kept)


def _item_error(per_key: Any, source: str, lang: str) -> str | None:
    """One item x one language check. Returns the problem, or None."""
    value = per_key.get(lang) if isinstance(per_key, dict) else None
    if not isinstance(value, str) or not value.strip():
        return "missing"
    if len(value) > max(200, 4 * len(source) + 50):
        return "implausible length"
    if lang == "ru" and _cyrillic_ratio(value, source) < 0.5:
        # Diagnosed live (2026-07-06): the model echoes an item verbatim
        # (often one written in another language than the declared
        # source) or, rarer, romanizes. The RATIO catches full echoes
        # AND half-latin mixes while tolerating protected latin terms.
        return "not Cyrillic"
    return None


def invalid_keys(
    raw: dict[str, Any], items: list[dict[str, str]], target_langs: list[str]
) -> list[str]:
    """Keys with at least one missing/implausible/wrong-script language \u2014
    the re-ask list for the salvage pass."""
    return [
        item["key"]
        for item in items
        if any(_item_error(raw.get(item["key"]), item["text"], lang) for lang in target_langs)
    ]


def validate_translations(
    raw: dict[str, Any],
    items: list[dict[str, str]],
    target_langs: list[str],
) -> dict[str, dict[str, str]]:
    """Full coverage + plausibility gate, dash-stripped output. Raises
    UpstreamError (nothing written, nothing debited) on any miss."""
    out: dict[str, dict[str, str]] = {}
    for item in items:
        key, source = item["key"], item["text"]
        per_key = raw.get(key)
        cleaned: dict[str, str] = {}
        for lang in target_langs:
            error = _item_error(per_key, source, lang)
            if error is not None:
                raise UpstreamError(
                    f"{lang!r} translation for {key!r}: {error}.",
                    code="ai.translation_failed",
                )
            assert isinstance(per_key, dict)  # guaranteed by _item_error
            cleaned[lang] = strip_dashes(per_key[lang].strip())
        out[key] = cleaned
    return out


async def request_translations_with_repair(
    items: list[dict[str, str]], source_lang: str, target_langs: list[str]
) -> tuple[dict[str, dict[str, str]], list[str], list[dict[str, Any]]]:
    """The worker's entry point, FIELD grain: batch call + ONE stricter
    repair pass on the items the model botched (echoed verbatim, wrong
    script, missing, implausible \u2014 a small re-ask is far more reliable
    than the big batch). Returns (validated translations of the GOOD
    items, keys still failed after repair, usage of each call). The
    caller writes the good fields and debits pro rata \u2014 a persistent
    miss costs one field, not the lot."""
    raw, usage = await request_translations(items, source_lang, target_langs)
    usages = [usage]
    bad = invalid_keys(raw, items, target_langs)
    if bad:
        logger.warning(
            "AI translation repair pass for %d/%d items (%s): %s",
            len(bad),
            len(items),
            ",".join(target_langs),
            bad,
        )
        retry_items = [i for i in items if i["key"] in set(bad)]
        raw_retry, usage_retry = await request_translations(
            retry_items, source_lang, target_langs, strict_retry=True
        )
        usages.append(usage_retry)
        raw = {**raw, **{k: v for k, v in raw_retry.items() if k in set(bad)}}
        bad = invalid_keys(raw, items, target_langs)
    good_items = [i for i in items if i["key"] not in set(bad)]
    return validate_translations(raw, good_items, target_langs), sorted(bad), usages
