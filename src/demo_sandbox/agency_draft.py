"""Turn the approved six email steps into the real journey import contract."""

import uuid

from src.admin.demo_schema import AgencyDemoInput, DemoCreateRequest
from src.demo_sandbox.catalog import DemoCatalog, DemoRecord
from src.demo_sandbox.draft import build_record
from src.journeys.journeys_schema import JourneyImportRequest

JOURNEYS = {
    "en": {
        "accountant": "Accounts and tax return",
        "lawyer": "Immigration application",
        "formation": "Company formation",
        "relocation": "Relocation support",
    },
    "fr": {
        "accountant": "Comptabilité et déclaration fiscale",
        "lawyer": "Dossier d'immigration",
        "formation": "Création d'entreprise",
        "relocation": "Accompagnement à l'installation",
    },
}
DOCUMENTS = {
    "en": {
        "accountant": [
            "Proof of identity",
            "Bank statements",
            "Income statement",
            "Expense receipts",
            "Draft accounts",
            "Engagement letter",
        ],
        "lawyer": [
            "Passport",
            "Proof of address",
            "Employment letter",
            "Proof of relationship",
            "Application checklist",
            "Engagement letter",
        ],
        "formation": [
            "Director's identity document",
            "Registered office address",
            "Articles of association",
            "Shareholder details",
            "Registration form",
            "Engagement letter",
        ],
        "relocation": [
            "Passport",
            "Housing preferences",
            "Employment letter",
            "Tenancy agreement",
            "Move-in inventory",
            "Service agreement",
        ],
    },
    "fr": {
        "accountant": [
            "Pièce d'identité",
            "Relevés bancaires",
            "Justificatif de revenus",
            "Justificatifs de dépenses",
            "Projet de comptes",
            "Lettre de mission",
        ],
        "lawyer": [
            "Passeport",
            "Justificatif de domicile",
            "Attestation d'emploi",
            "Preuves de la relation",
            "Liste des pièces du dossier",
            "Lettre de mission",
        ],
        "formation": [
            "Pièce d'identité du dirigeant",
            "Adresse du siège social",
            "Statuts de la société",
            "Liste des associés",
            "Formulaire d'immatriculation",
            "Lettre de mission",
        ],
        "relocation": [
            "Passeport",
            "Critères de recherche de logement",
            "Attestation d'emploi",
            "Bail de location",
            "État des lieux",
            "Contrat de prestation",
        ],
    },
}


STEP_TITLES = {
    "en": {
        "accountant": [
            "Engagement letter",
            "Document collection",
            "Automatic reminders",
            "Accounts preparation",
            "Review and signature",
            "Filing and follow-up",
        ],
        "lawyer": [
            "Initial consultation",
            "Document collection",
            "Automatic reminders",
            "Application preparation",
            "Signature and filing",
            "Application follow-up",
        ],
        "formation": [
            "Project confirmation",
            "Document collection",
            "Automatic reminders",
            "Document preparation",
            "Review and signature",
            "Registration follow-up",
        ],
        "relocation": [
            "Initial consultation",
            "Document collection",
            "Automatic reminders",
            "Move preparation",
            "Arrival arrangements",
            "Settling in",
        ],
    },
    "fr": {
        "accountant": [
            "Lettre de mission",
            "Collecte des pièces",
            "Relances automatiques",
            "Préparation des comptes",
            "Vérification et signature",
            "Dépôt et suivi",
        ],
        "lawyer": [
            "Premier entretien",
            "Collecte des pièces",
            "Relances automatiques",
            "Préparation du dossier",
            "Signature et dépôt",
            "Suivi du dossier",
        ],
        "formation": [
            "Confirmation du projet",
            "Collecte des pièces",
            "Relances automatiques",
            "Préparation des documents",
            "Vérification et signature",
            "Suivi de l'immatriculation",
        ],
        "relocation": [
            "Premier entretien",
            "Collecte des pièces",
            "Relances automatiques",
            "Préparation du départ",
            "Organisation de l'arrivée",
            "Suivi de l'installation",
        ],
    },
}


def agency_record(payload: AgencyDemoInput, code: str, catalog: DemoCatalog) -> DemoRecord:
    language = payload.language
    documents = list(DOCUMENTS[language][payload.profession])
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
        "nom": {language: payload.journey_name or JOURNEYS[language][payload.profession]},
        "langue_par_defaut": language,
        "etapes": [
            {
                "ref": f"step-{index + 1}",
                "nom": {
                    language: titles[index] or STEP_TITLES[language][payload.profession][index]
                },
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
                        "libelle": {
                            language: "Vos priorités" if language == "fr" else "Your priorities"
                        },
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
