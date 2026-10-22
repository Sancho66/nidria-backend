"""Build fictional state from the same journey JSON accepted by the real app."""

import base64
import binascii
import copy
import re
from typing import Any

from src.admin.demo_schema import (
    DemoCreateRequest,
    DemoDocumentChoice,
    DemoJourneyInput,
    DemoJourneyPreview,
)
from src.core.exceptions import ValidationError
from src.core.images import process_logo
from src.demo_sandbox.catalog import DemoCatalog, DemoRecord

TEXT = {
    "fr": (
        "Démonstration : données fictives",
        "Signature d'exemple",
        "Exemple sans valeur contractuelle",
        "Une pièce reste à fournir. Retrouvez-la dans votre espace.",
    ),
    "en": (
        "Demonstration: fictional data",
        "Example signature",
        "Example with no contractual value",
        "A document is still missing. You can find it in your client space.",
    ),
    "es": (
        "Demostración: datos ficticios",
        "Firma de ejemplo",
        "Ejemplo sin valor contractual",
        "Falta un documento. Puede consultarlo en su espacio de cliente.",
    ),
    "pt": (
        "Demonstração: dados fictícios",
        "Assinatura de exemplo",
        "Exemplo sem valor contratual",
        "Falta um documento. Pode consultá-lo na sua área de cliente.",
    ),
    "it": (
        "Dimostrazione: dati fittizi",
        "Firma di esempio",
        "Esempio senza valore contrattuale",
        "Manca ancora un documento. Lo trova nella sua area cliente.",
    ),
    "hu": (
        "Bemutató: kitalált adatok",
        "Aláírásminta",
        "Szerződéses érték nélküli példa",
        "Egy dokumentum még hiányzik. Az ügyfélfelületen megtalálja.",
    ),
    "ru": (
        "Демонстрация: вымышленные данные",
        "Пример подписи",
        "Пример без договорной силы",
        "Один документ ещё не предоставлен. Его можно найти в личном кабинете.",
    ),
}


def invalid() -> ValidationError:
    return ValidationError(
        "Check the journey and its selected language.", code="demo.invalid_journey"
    )


def label(value: Any, language: str) -> str:
    result = (
        value
        if isinstance(value, str)
        else value.get(language)
        if isinstance(value, dict)
        else None
    )
    if not isinstance(result, str) or not result.strip():
        raise invalid()
    return result.strip()


def journey_source(payload: DemoJourneyInput, catalog: DemoCatalog) -> dict[str, Any]:
    source: dict[str, Any]
    if payload.template_id:
        template = catalog.by_id(payload.template_id)
        if template is None:
            raise invalid()
        source = copy.deepcopy(template.journey_import["parcours"])
    elif payload.journey_import:
        if payload.journey_import.version != 1:
            raise invalid()
        source = copy.deepcopy(payload.journey_import.parcours)
    else:
        raise invalid()
    if source.get("langue_par_defaut", payload.language) != payload.language:
        raise ValidationError(
            "The journey language differs from the selected language.",
            code="demo.language_mismatch",
        )
    source["langue_par_defaut"] = payload.language
    source["nom"] = {payload.language: label(source.get("nom"), payload.language)}
    steps = source.get("etapes")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 80:
        raise invalid()
    refs = set()
    count = 0
    for step in steps:
        if not isinstance(step, dict):
            raise invalid()
        ref = step.get("ref")
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ref) or ref in refs:
            raise invalid()
        refs.add(ref)
        step["nom"] = {payload.language: label(step.get("nom"), payload.language)}
        documents = step.get("documents_a_fournir", [])
        if not isinstance(documents, list):
            raise invalid()
        normalized = []
        for item in documents:
            document = {"libelle": item} if isinstance(item, str) else item
            if not isinstance(document, dict):
                raise invalid()
            if document.get("requis", True) is False:
                continue
            document["libelle"] = {
                payload.language: label(document.get("libelle"), payload.language)
            }
            normalized.append(document)
        step["documents_a_fournir"] = normalized
        count += len(normalized)
    if count > 200:
        raise invalid()
    return source


def preview_journey(payload: DemoJourneyInput, catalog: DemoCatalog) -> DemoJourneyPreview:
    source = journey_source(payload, catalog)
    return DemoJourneyPreview(
        name=source["nom"][payload.language],
        language=payload.language,
        steps=[step["nom"][payload.language] for step in source["etapes"]],
        documents=[
            DemoDocumentChoice(
                key=f"{step['ref']}/{index}",
                step_ref=step["ref"],
                step_label=step["nom"][payload.language],
                label=item["libelle"][payload.language],
            )
            for step in source["etapes"]
            for index, item in enumerate(step["documents_a_fournir"])
        ],
    )


def build_record(payload: DemoCreateRequest, code: str, catalog: DemoCatalog) -> DemoRecord:
    source = journey_source(payload, catalog)
    preview = preview_journey(payload, catalog)
    total, done = len(source["etapes"]), payload.completed_steps
    if done > total or not payload.name.strip():
        raise invalid()
    choices = {item.key for item in preview.documents}
    if not set(payload.provided_documents).issubset(choices):
        raise invalid()
    notice, signature_label, signature_notice, reminder_body = TEXT[payload.language]
    current = min(done, total - 1)
    signed_index = max(0, done - 1)
    signed_step = source["etapes"][signed_index]
    if payload.include_signature:
        # A separate, clearly fictitious example never pretends a real contract was signed.
        signed_step["documents_a_fournir"].append(
            {"libelle": {payload.language: signature_label}, "pour": "client", "signature": "ses"}
        )
    state: dict[str, Any] = {
        "id": f"custom-{code}",
        "language": payload.language,
        "fictional_data": True,
        "read_only": True,
        "notice": notice,
        "progress": {"done": done, "total": total},
        "steps": [
            {
                "ref": step["ref"],
                "label": step["nom"][payload.language],
                "status": "done" if index < done else "in_progress" if index == done else "todo",
            }
            for index, step in enumerate(source["etapes"])
        ],
        "documents": [
            {
                "step_ref": item.step_ref,
                "label": item.label,
                "fictional": True,
                "status": "validated" if item.key in payload.provided_documents else "requested",
            }
            for item in preview.documents
        ],
        "reminders": [
            {
                "step_ref": source["etapes"][current]["ref"],
                "body": reminder_body,
                "delivery_enabled": False,
            }
        ]
        if payload.include_reminder
        else [],
        "signature": {
            "enabled": payload.include_signature,
            "step_ref": signed_step["ref"],
            "label": signature_label,
            "notice": signature_notice,
            "provider_call_enabled": False,
        },
    }
    if payload.logo_data_url:
        match = re.fullmatch(
            r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", payload.logo_data_url
        )
        if not match:
            raise ValidationError("Choose a PNG, JPEG or WebP logo.", code="demo.invalid_logo")
        try:
            raw = base64.b64decode(match[2], validate=True)
        except binascii.Error as exc:
            raise ValidationError("Invalid logo.", code="demo.invalid_logo") from exc
        processed, media_type = process_logo(match[1], raw)
        if len(processed) > 250_000:
            raise ValidationError("Choose a smaller logo.", code="demo.invalid_logo")
        ext = "png" if media_type == "image/png" else "jpg"
        state["logo"] = {
            "path": f"demo-sandbox/custom-{code}/logo.{ext}",
            "base64": base64.b64encode(processed).decode(),
        }
    return DemoRecord(
        id=state["id"],
        name=payload.name.strip(),
        language=payload.language,
        public_code=code,
        promised_journey=preview.name,
        journey_import={"version": 1, "parcours": source},
        state=state,
    )
