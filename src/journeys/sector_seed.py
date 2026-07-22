"""GLOBAL sector journey templates, seeded at boot (idempotent), like the
country library samples: agency_id NULL + is_sample=true, but keyed on
`sector` (NOT `country`). One example journey per business sector, offered
to an agency by CLONING at creation (see agencies.demo_case_seed).

Each template carries BOTH its steps AND its "Informations du dossier"
SECTIONS (the sector field pack): without sections a step can require no
field. Sections + their fields come from the back catalog (SECTION_TYPES),
materialized here and copied into the agency at creation.

DOER mapping (résolution A — the polymorphic CHECK is NEVER touched):
- "Agence"       → participant type=agent, agent_id NULL ("the agency in general").
- "Client"       → participant type=expat (the case principal).
- "Agence + Client" → BOTH participants above on the same step.
- "Prestataire"  → NO participant. A GLOBAL template (agency_id NULL) owns no
  external_contact to reference, and `type='external'` REQUIRES external_id →
  we would break the CHECK. The provider is NAMED in `content_note` so the
  agency wires the real external_contact from its directory when it USES the
  cloned journey.

The VALIDATOR is untouched: default_validated_by_type='agent'. Délais: LOW
bound of the researched range (a floor, not a rule); a variable / recurring
step carries None. Docs: existing StepRequirement (kind=document).

Wording is kept SECTOR-neutral and internationally transposable: no FR-only
institution names (préfecture, ANEF, CFE, DDT/DPE…) — the generic concept
("autorité compétente", "déclarations fiscales annuelles"…) so a non-French
agency reads it plainly. The agency edits its clone freely afterwards.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.journey import (
    JourneySection,
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateField,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.step_requirement import StepRequirement
from src.core.enums import AgencySector, StepParticipantRole
from src.journeys.field_catalog import SECTION_TYPES, field_kind

# A step: (name, estimated_days | None, content_note, doers, [doc labels]).
# `doers` ⊆ {"agent", "expat"} in display order; [] = the step is carried by a
# named provider only (content_note says who). Steps form a linear AND chain.
type _Step = tuple[str, int | None, str, list[str], list[str]]

# sector -> (journey name, steps). Content = validated research (2026-07),
# provider named in content_note at the concerned steps (résolution A),
# worded sector-neutral / internationally transposable.
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
                "Introduction de l'instance (acte introductif)",
                15,
                "Étape impliquant aussi un officier de justice compétent "
                "(signification / notification), à câbler depuis l'annuaire de l'agence.",
                ["agent"],
                ["Acte introductif d'instance", "Bordereau de pièces"],
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
                "Étape portée par l'autorité judiciaire compétente, "
                "à câbler depuis l'annuaire de l'agence.",
                [],
                ["Jugement"],
            ),
            (
                "Exécution ou voies de recours",
                30,
                "",
                ["agent"],
                ["Notification du jugement"],
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
            (
                "Établissement des déclarations fiscales",
                10,
                "",
                ["agent"],
                ["Déclarations fiscales annuelles"],
            ),
            (
                "Validation client et arrêté des comptes",
                7,
                "",
                ["expat", "agent"],
                ["Comptes annuels validés"],
            ),
            (
                "Télétransmission et dépôt légal",
                None,
                "",
                ["agent"],
                ["Accusé de dépôt", "Déclarations transmises"],
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
                "Étape impliquant aussi un diagnostiqueur immobilier, "
                "à câbler depuis l'annuaire de l'agence.",
                ["agent"],
                [
                    "Dossier de diagnostics techniques",
                    "Certificat de performance énergétique",
                    "Photos",
                ],
            ),
            ("Visites et négociation", 30, "", ["agent"], []),
            ("Offre d'achat acceptée", 7, "", ["expat", "agent"], ["Offre d'achat"]),
            (
                "Signature de l'avant-contrat",
                15,
                "Étape portée par un notaire, à câbler depuis l'annuaire de l'agence.",
                [],
                ["Avant-contrat de vente", "Pièces acquéreur"],
            ),
            (
                "Levée des conditions suspensives (financement)",
                45,
                "Étape impliquant aussi l'établissement prêteur, "
                "à câbler depuis l'annuaire de l'agence.",
                ["expat"],
                ["Offre de prêt"],
            ),
            (
                "Signature de l'acte définitif",
                90,
                "Étape portée par un notaire, à câbler depuis l'annuaire de l'agence.",
                [],
                ["Acte de vente définitif"],
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
                ["Rapport d'audit patrimonial", "Document d'information précontractuelle"],
            ),
            (
                "Mise en œuvre des solutions",
                30,
                "Étape impliquant aussi un assureur ou un établissement financier, "
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
                "Démarches d'immigration et de protection sociale",
                30,
                "Étape impliquant aussi un prestataire immigration / protection sociale, "
                "à câbler depuis l'annuaire de l'agence.",
                ["agent"],
                [
                    "Autorisation de travail",
                    "Certificat de couverture sociale",
                    "Affiliation à la protection sociale (international)",
                ],
            ),
            (
                "Installation et intégration locale",
                30,
                "Étape impliquant aussi un prestataire d'installation locale, "
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
                ["Passeport", "Visa d'entrée long séjour"],
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
            ("Dépôt de la demande", 7, "", ["agent"], ["Attestation de dépôt"]),
            (
                "Instruction et suivi auprès de l'autorité compétente",
                120,
                "Étape portée par l'autorité compétente en matière de séjour, "
                "à câbler depuis l'annuaire de l'agence.",
                [],
                ["Attestation de dépôt / de prolongation d'instruction"],
            ),
            (
                "Décision et convocation",
                30,
                "Étape portée par l'autorité compétente en matière de séjour, "
                "à câbler depuis l'annuaire de l'agence.",
                [],
                ["Notification de décision favorable"],
            ),
            (
                "Remise du titre et installation",
                21,
                "",
                ["expat"],
                ["Titre de séjour", "Engagement d'intégration (selon le pays d'accueil)"],
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

# sector -> the "Informations du dossier" SECTIONS the template carries. The
# universal `identity` is the socle everywhere; the rest is the sector pack.
# (See rapport: `legal` intentionally drops `immigration` — a civil-litigation
# journey collects parties, not residence permits.)
SECTOR_SECTIONS: dict[str, tuple[str, ...]] = {
    AgencySector.LEGAL.value: ("identity", "company"),
    AgencySector.ACCOUNTING.value: ("identity", "company", "tax"),
    AgencySector.REAL_ESTATE.value: ("identity", "real_estate_deal"),
    AgencySector.WEALTH.value: ("identity", "family_situation", "tax", "wealth_review"),
    AgencySector.HR_MOBILITY.value: ("identity", "professional", "immigration", "housing"),
    AgencySector.IMMIGRATION.value: ("identity", "immigration"),
    AgencySector.CONSULTING.value: ("identity", "consulting_mission"),
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


async def _seed_sections(
    db: AsyncSession, tpl: JourneyTemplate, section_keys: tuple[str, ...]
) -> None:
    """Materialize the template's "Informations" SECTIONS + their fields from
    the back catalog. Idempotent, anchored by seed_key (sections) and
    (kind, reference) (fields) — never by name; a re-seed refreshes labels /
    positions in place and never duplicates. Same shape as sample_seed's
    _sync_information_sections (samples), applied to a sector template."""
    existing_sections = {
        s.seed_key: s
        for s in (
            await db.execute(
                select(JourneySection).where(
                    JourneySection.template_id == tpl.id, JourneySection.seed_key.is_not(None)
                )
            )
        ).scalars()
    }
    existing_fields = {
        (f.kind, f.reference): f
        for f in (
            await db.execute(
                select(JourneyTemplateField).where(JourneyTemplateField.template_id == tpl.id)
            )
        ).scalars()
    }
    field_position = 0
    for position, key in enumerate(section_keys):
        section_type = SECTION_TYPES[key]
        section = existing_sections.get(key)
        if section is None:
            section = JourneySection(
                template_id=tpl.id,
                name=section_type.labels["fr"],
                name_i18n=dict(section_type.labels),
                seed_key=key,
                position=position,
            )
            db.add(section)
            await db.flush()  # fields FK the section below
        else:
            section.name = section_type.labels["fr"]
            section.name_i18n = dict(section_type.labels)
            section.position = position
        for field_key in section_type.field_keys:
            kind = field_kind(field_key)
            row = existing_fields.get((kind, field_key))
            if row is None:
                row = JourneyTemplateField(
                    template_id=tpl.id,
                    kind=kind,
                    reference=field_key,
                    position=field_position,
                    section_id=section.id,
                )
                db.add(row)
                existing_fields[(kind, field_key)] = row
            else:
                row.section_id = section.id
                row.position = field_position
            field_position += 1


async def _seed_one_sector(db: AsyncSession, sector: str, name: str, steps: list[_Step]) -> None:
    """Idempotent get-or-create + RECONCILE (keyed on sector). Runs on every
    boot: an existing template is reconciled in place (name / steps / notes /
    docs / sections refreshed) so a content edit (e.g. de-francisation) or the
    NEW sections reach an already-seeded database. A GLOBAL sample is never
    referenced by a case (clones copy by value), so reconciling its children
    is safe. Never touches an agency's clone (agency_id filter)."""
    tpl = (
        await db.execute(
            select(JourneyTemplate).where(
                JourneyTemplate.agency_id.is_(None),
                JourneyTemplate.is_sample.is_(True),
                JourneyTemplate.sector == sector,
            )
        )
    ).scalar_one_or_none()
    if tpl is None:
        tpl = JourneyTemplate(
            id=uuid.uuid4(),
            agency_id=None,
            is_sample=True,
            sector=sector,
            name=name,
            name_i18n={"fr": name},
        )
        db.add(tpl)
        await db.flush()
    else:
        tpl.name = name
        tpl.name_i18n = {"fr": name}

    # --- steps: reconcile by position (count is fixed per sector) ---------------------
    existing_steps = {
        s.position: s
        for s in (
            await db.execute(
                select(JourneyTemplateStep).where(JourneyTemplateStep.template_id == tpl.id)
            )
        ).scalars()
    }
    step_objs: list[JourneyTemplateStep] = []
    for position, (step_name, days, note, _doers, _docs) in enumerate(steps):
        step = existing_steps.get(position)
        if step is None:
            step = JourneyTemplateStep(
                id=uuid.uuid4(),
                template_id=tpl.id,
                name=step_name,
                position=position,
                estimated_days=days,
                content_note=note,
                default_validated_by_type="agent",
            )
            db.add(step)
        else:
            step.name = step_name
            step.estimated_days = days
            step.content_note = note
        step_objs.append(step)
    await db.flush()  # ids for children

    step_ids = [s.id for s in step_objs]

    # --- prerequisites: linear AND chain (positions stable → no stale) ----------------
    have_prereqs = {
        (p.step_id, p.prerequisite_step_id)
        for p in (
            await db.execute(select(StepPrerequisite).where(StepPrerequisite.step_id.in_(step_ids)))
        ).scalars()
    }
    for i in range(1, len(step_objs)):
        pair = (step_objs[i].id, step_objs[i - 1].id)
        if pair not in have_prereqs:
            db.add(StepPrerequisite(step_id=pair[0], prerequisite_step_id=pair[1]))

    # --- participants: refreshed per step (no natural key → delete + re-add) ----------
    await db.execute(
        delete(JourneyStepParticipant).where(JourneyStepParticipant.step_id.in_(step_ids))
    )
    for i, (_n, _d, _note, doers, _docs) in enumerate(steps):
        _add_participants(db, step_objs[i].id, doers)

    # --- requirements: reconcile per step (delete stale refs, add missing) ------------
    for i, (_n, _d, _note, _doers, docs) in enumerate(steps):
        existing_reqs = list(
            (
                await db.execute(
                    select(StepRequirement).where(StepRequirement.step_id == step_objs[i].id)
                )
            ).scalars()
        )
        desired = set(docs)
        for req in existing_reqs:
            if req.reference not in desired:
                await db.delete(req)
        have_refs = {req.reference for req in existing_reqs if req.reference in desired}
        for position, label in enumerate(docs):
            if label not in have_refs:
                db.add(
                    StepRequirement(
                        step_id=step_objs[i].id,
                        kind="document",
                        reference=label,
                        scope="principal",
                        position=position,
                    )
                )

    # --- sections (the sector field pack) ---------------------------------------------
    await _seed_sections(db, tpl, SECTOR_SECTIONS.get(sector, ()))
    await db.commit()


async def seed_sector_templates(db: AsyncSession) -> None:
    """Seed the GLOBAL sector templates (idempotent, relaunchable, no dup)."""
    for sector, (name, steps) in SECTOR_TEMPLATES.items():
        await _seed_one_sector(db, sector, name, steps)
