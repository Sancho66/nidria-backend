"""GLOBAL sector journey templates, seeded at boot (idempotent), like the
country library samples: agency_id NULL + is_sample=true, but keyed on
`sector` (NOT `country`). One example journey per business sector, offered
to an agency by CLONING at creation (see agencies.demo_case_seed).

DOER mapping (résolution A — the polymorphic CHECK is NEVER touched):
- "Agence"       → participant type=agent, agent_id NULL ("the agency in general").
- "Client"       → participant type=expat (the case principal).
- "Agence + Client" → BOTH participants above on the same step.
- "Prestataire"  → NO participant. A GLOBAL template (agency_id NULL) owns no
  external_contact to reference, and `type='external'` REQUIRES external_id →
  we would break the CHECK. The provider is NAMED in `content_note` (notaire,
  commissaire de justice, banque, préfecture…) so the agency wires the real
  external_contact from its directory when it USES the cloned journey.

The VALIDATOR is untouched: default_validated_by_type='agent' (the agency
validates), exactly like the country samples — no exotic validator here.

Délais: LOW bound of the researched range (a floor, not a rule); a variable /
recurring step carries None. Docs: existing StepRequirement (kind=document).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.journey import (
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.step_requirement import StepRequirement
from src.core.enums import AgencySector, StepParticipantRole

# A step: (name, estimated_days | None, content_note, doers, [doc labels]).
# `doers` ⊆ {"agent", "expat"} in display order; [] = the step is carried by a
# named provider only (content_note says who). Steps form a linear AND chain.
type _Step = tuple[str, int | None, str, list[str], list[str]]

# sector -> (journey name, steps). Content = validated research (2026-07),
# provider named in content_note at the concerned steps (résolution A).
SECTOR_TEMPLATES: dict[str, tuple[str, list[_Step]]] = {
    AgencySector.LEGAL.value: (
        "Contentieux civil",
        [
            (
                "Consultation initiale et ouverture du dossier",
                7,
                "",
                ["agent"],
                ["Pièces du litige", "Justificatif d'identité", "Convention d'honoraires"],
            ),
            (
                "Tentative amiable / mise en demeure",
                15,
                "",
                ["agent"],
                ["Courrier de mise en demeure"],
            ),
            (
                "Rédaction et signification de l'assignation",
                15,
                "Étape portée aussi par un commissaire de justice, "
                "à câbler depuis l'annuaire de l'agence.",
                ["agent"],
                ["Assignation", "Bordereau de pièces"],
            ),
            (
                "Mise en état (échange de conclusions)",
                120,
                "",
                ["agent"],
                ["Conclusions", "Pièces communiquées"],
            ),
            ("Audience de plaidoirie", None, "", ["agent"], []),
            (
                "Jugement et notification",
                30,
                "Étape portée par le greffe / un commissaire de justice, "
                "à câbler depuis l'annuaire de l'agence.",
                [],
                ["Jugement"],
            ),
            (
                "Exécution ou voies de recours",
                30,
                "",
                ["agent"],
                ["Signification du jugement"],
            ),
        ],
    ),
    AgencySector.ACCOUNTING.value: (
        "Établissement des comptes annuels",
        [
            (
                "Lettre de mission et collecte des pièces",
                15,
                "",
                ["agent", "expat"],
                ["Lettre de mission", "Relevés bancaires", "Factures", "Journaux comptables"],
            ),
            (
                "Saisie et révision comptable",
                20,
                "",
                ["agent"],
                ["Pièces comptables", "Rapprochements bancaires"],
            ),
            (
                "Établissement du bilan et du compte de résultat",
                15,
                "",
                ["agent"],
                ["Balance", "Grand livre"],
            ),
            ("Établissement de la liasse fiscale", 10, "", ["agent"], ["Liasse fiscale"]),
            (
                "Validation client et arrêté des comptes",
                7,
                "",
                ["expat", "agent"],
                ["Comptes annuels validés"],
            ),
            (
                "Télétransmission (EDI) et dépôt",
                None,
                "",
                ["agent"],
                ["Accusé de dépôt", "Télédéclarations"],
            ),
        ],
    ),
    AgencySector.REAL_ESTATE.value: (
        "Vente d'un bien",
        [
            (
                "Estimation et signature du mandat",
                7,
                "",
                ["agent"],
                ["Mandat de vente", "Titre de propriété"],
            ),
            (
                "Constitution du dossier et diffusion de l'annonce",
                15,
                "Étape portée aussi par un diagnostiqueur, à câbler depuis l'annuaire de l'agence.",
                ["agent"],
                ["Dossier de diagnostics techniques (DDT)", "DPE", "Photos"],
            ),
            ("Visites et négociation", 30, "", ["agent"], []),
            ("Offre d'achat acceptée", 7, "", ["expat", "agent"], ["Offre d'achat"]),
            (
                "Signature du compromis (avant-contrat)",
                15,
                "Étape portée par un notaire, à câbler depuis l'annuaire de l'agence.",
                [],
                ["Compromis de vente", "Pièces acquéreur"],
            ),
            (
                "Levée des conditions suspensives (financement)",
                45,
                "Étape portée aussi par la banque, à câbler depuis l'annuaire de l'agence.",
                ["expat"],
                ["Offre de prêt"],
            ),
            (
                "Signature de l'acte authentique",
                90,
                "Étape portée par un notaire, à câbler depuis l'annuaire de l'agence.",
                [],
                ["Acte authentique de vente"],
            ),
        ],
    ),
    AgencySector.WEALTH.value: (
        "Bilan patrimonial et préconisations",
        [
            (
                "Entretien découverte et recueil de connaissance client",
                15,
                "",
                ["agent", "expat"],
                [
                    "Pièce d'identité",
                    "Avis d'imposition",
                    "Relevés de comptes",
                    "Questionnaire de connaissance client",
                ],
            ),
            ("Analyse et audit patrimonial", 20, "", ["agent"], []),
            (
                "Remise des préconisations (rapport écrit)",
                15,
                "",
                ["agent"],
                ["Rapport d'audit patrimonial", "Document d'entrée en relation (DER)"],
            ),
            (
                "Mise en œuvre des solutions",
                30,
                "Étape portée aussi par un assureur / une banque, "
                "à câbler depuis l'annuaire de l'agence.",
                ["agent"],
                ["Bulletins de souscription"],
            ),
            (
                "Suivi annuel et actualisation",
                365,
                "",
                ["agent"],
                ["Actualisation du dossier de connaissance client"],
            ),
        ],
    ),
    AgencySector.HR_MOBILITY.value: (
        "Mobilité internationale d'un salarié",
        [
            (
                "Cadrage du projet et choix du régime",
                15,
                "",
                ["agent", "expat"],
                ["Fiche de cadrage", "Définition du poste"],
            ),
            ("Simulation du package de rémunération", 15, "", ["agent"], ["Simulation de package"]),
            (
                "Formalités contractuelles",
                15,
                "",
                ["agent"],
                ["Avenant d'expatriation / contrat local"],
            ),
            (
                "Démarches immigration et protection sociale",
                30,
                "Étape portée aussi par un prestataire immigration / protection sociale, "
                "à câbler depuis l'annuaire de l'agence.",
                ["agent"],
                ["Visa de travail", "Certificat de détachement", "Affiliation CFE"],
            ),
            (
                "Installation et intégration locale",
                30,
                "Étape portée aussi par un prestataire d'installation locale, "
                "à câbler depuis l'annuaire de l'agence.",
                ["expat"],
                ["Bail", "Ouverture de compte bancaire"],
            ),
            (
                "Suivi de mission et préparation du retour",
                None,
                "",
                ["agent"],
                ["Bilan de fin de mission"],
            ),
        ],
    ),
    AgencySector.IMMIGRATION.value: (
        "Première demande de titre de séjour (salarié)",
        [
            (
                "Évaluation d'éligibilité et choix du titre",
                7,
                "",
                ["agent"],
                ["Passeport", "Visa long séjour"],
            ),
            (
                "Constitution du dossier et collecte des pièces",
                30,
                "",
                ["agent", "expat"],
                [
                    "Justificatif de domicile",
                    "Contrat de travail",
                    "Autorisation de travail",
                    "Actes d'état civil traduits",
                ],
            ),
            ("Dépôt de la demande (ANEF)", 7, "", ["agent"], ["Attestation de dépôt"]),
            (
                "Instruction et suivi préfecture",
                120,
                "Étape portée par la préfecture, à câbler depuis l'annuaire de l'agence.",
                [],
                ["Récépissé / attestation de prolongation d'instruction"],
            ),
            (
                "Décision et convocation",
                30,
                "Étape portée par la préfecture, à câbler depuis l'annuaire de l'agence.",
                [],
                ["Attestation de décision favorable"],
            ),
            (
                "Remise du titre et installation",
                21,
                "",
                ["expat"],
                [
                    "Titre de séjour",
                    "Contrat d'engagement au respect des principes de la République",
                ],
            ),
        ],
    ),
    AgencySector.CONSULTING.value: (
        "Mission de conseil",
        [
            (
                "Note de cadrage et lancement",
                10,
                "",
                ["agent", "expat"],
                ["Note de cadrage", "Lettre de mission"],
            ),
            ("Diagnostic / état des lieux", 15, "", ["agent"], ["Rapport de diagnostic"]),
            (
                "Recommandations / préconisations",
                15,
                "",
                ["agent"],
                ["Rapport de recommandations"],
            ),
            ("Déploiement / mise en œuvre", 30, "", ["agent", "expat"], ["Plan d'action"]),
            (
                "Comité de pilotage et suivi",
                None,
                "",
                ["agent", "expat"],
                ["Compte-rendu de comité de pilotage"],
            ),
            ("Clôture et bilan de mission", 10, "", ["agent"], ["Bilan de fin de mission"]),
        ],
    ),
}


def _add_participants(db: AsyncSession, step_id: uuid.UUID, doers: list[str]) -> None:
    """The step's DOER(s). Only 'agent' (agency in general, agent_id NULL) and
    'expat' (the client) — NEVER 'external' on a global template (résolution A:
    the polymorphic CHECK stays intact; the provider is named in content_note)."""
    for doer in doers:
        db.add(
            JourneyStepParticipant(
                step_id=step_id,
                type=doer,
                agent_id=None,
                external_id=None,
                role=StepParticipantRole.EXECUTANT.value,
            )
        )


async def _seed_one_sector(db: AsyncSession, sector: str, name: str, steps: list[_Step]) -> None:
    """Idempotent: keyed on (agency_id IS NULL, is_sample, sector). If the
    sector template already exists, no-op (content edits are a deliberate
    manual re-seed, never an at-boot rewrite — same caution as system roles)."""
    existing = (
        await db.execute(
            select(JourneyTemplate.id).where(
                JourneyTemplate.agency_id.is_(None),
                JourneyTemplate.is_sample.is_(True),
                JourneyTemplate.sector == sector,
            )
        )
    ).first()
    if existing is not None:
        return

    tpl = JourneyTemplate(
        id=uuid.uuid4(),
        agency_id=None,
        is_sample=True,
        sector=sector,
        name=name,
        name_i18n={"fr": name},
    )
    db.add(tpl)
    await db.flush()  # template before its children FK it

    step_ids: list[uuid.UUID] = []
    for position, (step_name, days, note, _doers, _docs) in enumerate(steps):
        sid = uuid.uuid4()
        step_ids.append(sid)
        db.add(
            JourneyTemplateStep(
                id=sid,
                template_id=tpl.id,
                name=step_name,
                position=position,
                estimated_days=days,
                content_note=note,
                default_validated_by_type="agent",  # the agency validates (untouched)
            )
        )
    await db.flush()  # steps before prerequisites / participants / requirements

    # Linear AND chain: step i requires step i-1 (locked steps).
    for i in range(1, len(step_ids)):
        db.add(StepPrerequisite(step_id=step_ids[i], prerequisite_step_id=step_ids[i - 1]))

    for i, (_step_name, _days, _note, doers, docs) in enumerate(steps):
        _add_participants(db, step_ids[i], doers)
        for position, label in enumerate(docs):
            db.add(
                StepRequirement(
                    step_id=step_ids[i],
                    kind="document",
                    reference=label,
                    scope="principal",
                    position=position,
                )
            )
    await db.commit()


async def seed_sector_templates(db: AsyncSession) -> None:
    """Seed the GLOBAL sector templates (idempotent, relaunchable, no dup)."""
    for sector, (name, steps) in SECTOR_TEMPLATES.items():
        await _seed_one_sector(db, sector, name, steps)
