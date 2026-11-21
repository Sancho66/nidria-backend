"""Turn the approved six email steps into the real journey import contract."""

import uuid

from src.admin.demo_schema import AgencyDemoInput, DemoCreateRequest
from src.demo_sandbox.catalog import DemoCatalog, DemoRecord
from src.demo_sandbox.draft import build_record
from src.demo_sandbox.fixture_copy import fixture_copy
from src.journeys.journeys_schema import JourneyImportRequest


def agency_record(payload: AgencyDemoInput, code: str, catalog: DemoCatalog) -> DemoRecord:
    language = payload.language
    copy = fixture_copy(language)
    documents = list(copy["documents"][payload.profession])
    if language == "en" and payload.profession == "accountant":
        documents[2] = {
            "GB": "P60",
            "US": "W-2 statement",
            "CA": "T4 slip",
            "IE": "Employment Detail Summary",
            "AU": "Income statement",
            "NZ": "Income summary",
        }.get(payload.country, documents[2])
    if payload.documents:
        documents = payload.documents
    # Preserve the explicit labels, with a mix of received and missing pieces.
    # Each requirement and its sample file derive from this single list.
    step_documents = [documents[index::6] for index in range(6)]
    titles = payload.step_titles or [""] * 6
    source = {
        "nom": {language: payload.journey_name or copy["journeys"][payload.profession]},
        "langue_par_defaut": language,
        "etapes": [
            {
                "ref": f"step-{index + 1}",
                "nom": {language: titles[index] or copy["step_titles"][payload.profession][index]},
                "informations_fournies": {"note": {language: description}},
                "delai_jours": [2, 5, 3, 4, 2, 1][index],
                "validee_par": "agence",
                "participants": [
                    {"acteur": "agence", "role": "executant"},
                    {"acteur": "client", "role": "fournit_documents"},
                ],
                "informations_a_collecter": [
                    {
                        "cle": "demo_project_notes",
                        "type": "text",
                        "requis": False,
                        "libelle": {language: copy["labels"]["priorities"]},
                    }
                ]
                if index == 0
                else [],
                "prerequis": [f"step-{index}"] if index else [],
                "documents_a_fournir": [
                    {"libelle": {language: document}, "pour": "client"}
                    for document in step_documents[index]
                ],
            }
            for index, description in enumerate(payload.steps)
        ],
    }
    return build_record(
        DemoCreateRequest(
            request_id=uuid.uuid4(),
            name=payload.company_name,
            language=language,
            journey_import=JourneyImportRequest(parcours=source),
            completed_steps=2,
            provided_documents=[
                f"step-{index + 1}/{position}"
                for index in range(2)
                for position in range(len(step_documents[index]))
            ],
            logo_data_url=payload.logo_data_url,
        ),
        code,
        catalog,
    )
