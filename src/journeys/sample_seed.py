"""Library SAMPLE journeys, seeded at boot (idempotent), like the system
roles: agency_id NULL + is_sample=true → shared, read-only for agencies, an
agency consumes one by CLONING it.

PY-1 — Paraguay résidence temporaire + cédula (inlined), reconstructed from
the Reside Paraguay use cases. CONSTRAINT: on an agency-less sample, the only
NAMEABLE participant is the client (type=expat) — a type=agent participant
needs an agent_id and a sample has no agency, hence no agents. So agency /
provider doers (incl. the sworn translator on step 2) are carried as a
content_note "à assigner au dossier"; the agency names them on the CLONE.
The validator is "the agency" (validated_by_type='agent', agent_id NULL =
"agency in general", allowed by the validator CHECK). Amounts are indicative,
never a rule.
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

PY1_NAME = "Paraguay — Résidence temporaire + Cédula"
PY1_COUNTRY = "PY"

# (name, estimated_days, content_note, client_role | None, [document labels])
# client_role None ⇒ an agency/provider doer (a content_note, no participant
# on the sample). Steps form a linear AND prerequisite chain (each needs the
# previous). The validator is the agency on every step.
_PY1_STEPS: list[tuple[str, int, str, str | None, list[str]]] = [
    (
        "Constitution du dossier",
        15,
        "Réunissez les pièces : acte de naissance apostillé, casier judiciaire "
        "apostillé, passeport valide. L'apostille se demande auprès de l'autorité "
        "compétente de votre pays d'origine.",
        "provides_documents",
        ["Acte de naissance apostillé", "Casier judiciaire apostillé", "Passeport"],
    ),
    (
        "Traduction assermentée des documents",
        7,
        "Traduction par un traducteur assermenté inscrit. À assigner au dossier : "
        "le prestataire externe se nomme sur le dossier, pas sur ce modèle partagé.",
        "provides_documents",
        [],
    ),
    (
        "Dépôt du dossier à l'immigration (DNM)",
        10,
        "Dépôt effectué par l'agence auprès de la Dirección Nacional de Migraciones. "
        "Taxe DNM ≈ 2 700 000 Gs (montant indicatif, non figé).",
        None,
        [],
    ),
    (
        "Obtention de la résidence temporaire",
        45,
        "Délai administratif de la DNM, variable (≈ 30 à 45 jours, indicatif).",
        None,
        [],
    ),
    (
        "Demande de la cédula (carte d'identité)",
        20,
        "Prise d'empreintes et photo au bureau d'identification. Étape débloquée "
        "une fois la résidence temporaire obtenue.",
        "provides_documents",
        ["Photo d'identité"],
    ),
    (
        "Remise de la cédula",
        90,
        "Délai de fabrication de la cédula, variable (≈ 3 à 9 mois, indicatif).",
        None,
        [],
    ),
]


async def seed_sample_journeys(db: AsyncSession) -> None:
    """Idempotent: keyed on (agency_id IS NULL, is_sample, name=PY1_NAME). If
    the sample already exists, do nothing (relaunchable, no duplicate)."""
    existing = (
        await db.execute(
            select(JourneyTemplate).where(
                JourneyTemplate.agency_id.is_(None),
                JourneyTemplate.is_sample.is_(True),
                JourneyTemplate.name == PY1_NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-seed updates the country in place (no duplicate, no re-create).
        if existing.country != PY1_COUNTRY:
            existing.country = PY1_COUNTRY
            await db.commit()
        return

    tpl = JourneyTemplate(
        id=uuid.uuid4(), agency_id=None, is_sample=True, name=PY1_NAME, country=PY1_COUNTRY
    )
    db.add(tpl)
    await db.flush()  # template before its children FK it

    step_ids: list[uuid.UUID] = []
    for position, (name, days, note, _role, _docs) in enumerate(_PY1_STEPS):
        sid = uuid.uuid4()
        step_ids.append(sid)
        db.add(
            JourneyTemplateStep(
                id=sid,
                template_id=tpl.id,
                name=name,
                position=position,
                estimated_days=days,
                content_note=note,
                default_validated_by_type="agent",  # validé par l'agence
            )
        )
    await db.flush()  # steps before prerequisites / participants / requirements

    # Linear AND chain: step i requires step i-1.
    for i in range(1, len(step_ids)):
        db.add(StepPrerequisite(step_id=step_ids[i], prerequisite_step_id=step_ids[i - 1]))

    for i, (_name, _days, _note, role, docs) in enumerate(_PY1_STEPS):
        if role is not None:
            db.add(
                JourneyStepParticipant(step_id=step_ids[i], type="expat", agent_id=None, role=role)
            )
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
