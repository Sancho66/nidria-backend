"""Seed: RBAC baseline + job configs (via the SHARED functions — zero
duplicated logic) + the 3 demo agencies/agents + the 3 brief cases
(Martin, Volkov, Dupont).

IDEMPOTENT: get-or-create on natural keys (slug, email, template name);
a case found for (agency, principal) skips its whole block. Re-run at
will, no duplicates. start.sh runs it on EVERY boot.

Modes:
  --mode dev  (default): baseline + demo agencies/cases/passwords.
  --mode prod: baseline ONLY (catalogue, system roles, matrix,
               bindings, job configs) — never the Demo1234! accounts.

Run: uv run python scripts/seed.py [--mode dev|prod]
"""

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from shared.models import (  # noqa: E402
    ActivityLog,
    Agency,
    Agent,
    AgentRole,
    CaseStepProgress,
    ClientCase,
    ExpatUser,
    FamilyMember,
    JourneyTemplate,
    JourneyTemplateStep,
    MessageTemplate,
    Reminder,
    Role,
    StepPrerequisite,
)
from src.core.database import async_session_maker  # noqa: E402
from src.core.rbac.baseline import collect_bindings, seed_rbac_baseline  # noqa: E402
from src.core.security import hash_password  # noqa: E402
from src.jobs.jobs_baseline import seed_job_configs  # noqa: E402

# Plain-text on purpose: demo/test credentials, printed below.
DEMO_PASSWORD = "Demo1234!"

NOW = datetime.now(UTC)


# --- get-or-create helpers ----------------------------------------------------------


async def get_or_create_agency(db: AsyncSession, slug: str, name: str) -> Agency:
    agency = (await db.execute(select(Agency).where(Agency.slug == slug))).scalar_one_or_none()
    if agency is None:
        agency = Agency(slug=slug, name=name)
        db.add(agency)
        await db.flush()
    return agency


async def get_or_create_agent(
    db: AsyncSession,
    agency: Agency,
    role: Role,
    first_name: str,
    last_name: str,
    email: str,
) -> Agent:
    agent = (await db.execute(select(Agent).where(Agent.email == email))).scalar_one_or_none()
    if agent is None:
        agent = Agent(
            agency_id=agency.id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            password_hash=hash_password(DEMO_PASSWORD),
        )
        db.add(agent)
        await db.flush()
        db.add(AgentRole(agent_id=agent.id, role_id=role.id))
    return agent


async def get_or_create_expat(
    db: AsyncSession, first_name: str, last_name: str, email: str, lang: str = "fr"
) -> ExpatUser:
    expat = (
        await db.execute(select(ExpatUser).where(ExpatUser.email == email))
    ).scalar_one_or_none()
    if expat is None:
        expat = ExpatUser(
            first_name=first_name,
            last_name=last_name,
            email=email,
            preferred_lang=lang,
            password_hash=hash_password(DEMO_PASSWORD),
            activated_at=NOW,  # the front needs expat logins
        )
        db.add(expat)
        await db.flush()
    return expat


async def get_or_create_template(
    db: AsyncSession,
    agency: Agency,
    name: str,
    steps_spec: list[tuple[str, int | None, str | None, list[str]]],
    prerequisites: dict[int, list[int]],
) -> list[JourneyTemplateStep]:
    """steps_spec: (name, estimated_days, default_responsible_type,
    required_documents), prerequisites: {step_index: [prereq_indexes]}
    (0-based)."""
    template = (
        await db.execute(
            select(JourneyTemplate).where(
                JourneyTemplate.agency_id == agency.id, JourneyTemplate.name == name
            )
        )
    ).scalar_one_or_none()
    if template is not None:
        steps = list(
            (
                await db.execute(
                    select(JourneyTemplateStep)
                    .where(JourneyTemplateStep.template_id == template.id)
                    .order_by(JourneyTemplateStep.position)
                )
            ).scalars()
        )
        # Step-15 fill-if-empty: bring required_documents to templates
        # seeded before the field existed — never overwrite a non-empty
        # (possibly runtime-edited) list.
        for step, spec in zip(steps, steps_spec, strict=False):
            if not step.required_documents and spec[3]:
                step.required_documents = spec[3]
        await db.flush()
        return steps

    template = JourneyTemplate(agency_id=agency.id, name=name)
    db.add(template)
    await db.flush()
    steps = []
    for position, (step_name, estimated_days, responsible, documents) in enumerate(steps_spec):
        step = JourneyTemplateStep(
            template_id=template.id,
            name=step_name,
            position=position,
            estimated_days=estimated_days,
            default_responsible_type=responsible,
            required_documents=documents,
        )
        db.add(step)
        steps.append(step)
    await db.flush()
    for step_index, prereq_indexes in prerequisites.items():
        for prereq_index in prereq_indexes:
            db.add(
                StepPrerequisite(
                    step_id=steps[step_index].id,
                    prerequisite_step_id=steps[prereq_index].id,
                )
            )
    await db.flush()
    return steps


async def case_exists(db: AsyncSession, agency: Agency, expat: ExpatUser) -> bool:
    row = (
        await db.execute(
            select(ClientCase.id).where(
                ClientCase.agency_id == agency.id,
                ClientCase.principal_expat_user_id == expat.id,
            )
        )
    ).first()
    return row is not None


def _log(
    db: AsyncSession,
    case_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
    action_type: str,
    details: dict[str, object],
    created_at: datetime,
) -> None:
    db.add(
        ActivityLog(
            case_id=case_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action_type=action_type,
            details=details,
            created_at=created_at,
        )
    )


def _progress(
    db: AsyncSession,
    case: ClientCase,
    step: JourneyTemplateStep,
    **overrides: object,
) -> CaseStepProgress:
    defaults: dict[str, object] = {"status": "todo", "responsible_type": None}
    if step.default_responsible_type == "expat":
        defaults["responsible_type"] = "expat"
    row = CaseStepProgress(case_id=case.id, template_step_id=step.id, **{**defaults, **overrides})
    db.add(row)
    return row


# --- the three brief cases -----------------------------------------------------------


async def seed_martin(db: AsyncSession, agency: Agency, eloise: Agent, selim: Agent) -> str:
    expat = await get_or_create_expat(db, "Jean", "Martin", "jean.martin@example.com")
    steps = await get_or_create_template(
        db,
        agency,
        "Résidence permanente Paraguay",
        [
            (
                "Collecte des documents",
                10,
                "expat",
                ["Passeport (copie certifiée)", "Acte de naissance apostillé"],
            ),
            ("Casier judiciaire & apostilles", 15, "expat", ["Casier judiciaire apostillé"]),
            (
                "Dépôt de la demande de résidence",
                20,
                "expat",
                ["Casier judiciaire apostillé", "Acte de naissance traduit"],
            ),
            ("Retrait de la carte de résident", 30, None, []),
            ("Cédula & RUC", 15, None, ["Photo d'identité"]),
        ],
        {3: [2], 4: [3]},  # 4←3, 5←4 (0-based)
    )

    if await case_exists(db, agency, expat):
        return "Famille Martin: already seeded, skipped"
    case = ClientCase(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=eloise.id,
        journey_template_id=steps[0].template_id,
        origin_country="FR",
        dest_country="PY",
        status="in_progress",
        source="referral",
        tags=["famille", "vip"],
    )
    db.add(case)
    await db.flush()
    db.add(FamilyMember(case_id=case.id, name="Claire Martin", relationship="spouse"))
    db.add(FamilyMember(case_id=case.id, name="Lucas Martin", relationship="child"))

    # Steps 1-2 DONE (Eloïse at -30d, Sélim at -15d), 3 IN_PROGRESS
    # responsible EXPAT, 4-5 TODO (projection blocks them via 4←3, 5←4).
    done_1, done_2 = NOW - timedelta(days=30), NOW - timedelta(days=15)
    _progress(
        db,
        case,
        steps[0],
        status="done",
        completed_at=done_1,
        completed_by_agent_id=eloise.id,
        updated_at=done_1,
    )
    _progress(
        db,
        case,
        steps[1],
        status="done",
        completed_at=done_2,
        completed_by_agent_id=selim.id,
        updated_at=done_2,
    )
    step3 = _progress(db, case, steps[2], status="in_progress", responsible_type="expat")
    _progress(db, case, steps[3])
    _progress(db, case, steps[4])
    await db.flush()

    _log(db, case.id, "agent", eloise.id, "case.created", {}, NOW - timedelta(days=45))
    _log(
        db,
        case.id,
        "agent",
        eloise.id,
        "step.completed",
        {"step_progress_id": str(steps[0].id)},
        done_1,
    )
    _log(
        db,
        case.id,
        "agent",
        selim.id,
        "step.completed",
        {"step_progress_id": str(steps[1].id)},
        done_2,
    )
    _log(
        db,
        case.id,
        "agent",
        eloise.id,
        "step.started",
        {"step_progress_id": str(step3.id)},
        NOW,
    )

    # The J+10 reminder on step 3 — TO_APPROVE, Eloïse's scenario.
    template = (
        await db.execute(
            select(MessageTemplate).where(
                MessageTemplate.agency_id == agency.id,
                MessageTemplate.name == "Relance documents",
            )
        )
    ).scalar_one_or_none()
    if template is None:
        template = MessageTemplate(
            agency_id=agency.id,
            name="Relance documents",
            body=(
                "Bonjour {client_name}, il reste {days_left} jours pour finaliser "
                "l'étape {step_name}. Merci de déposer vos documents."
            ),
        )
        db.add(template)
        await db.flush()
    db.add(
        Reminder(
            case_id=case.id,
            step_progress_id=step3.id,
            message_template_id=template.id,
            channel="mail",
            scheduled_at=NOW + timedelta(days=10),
            status="to_approve",
            recipient_type="expat",
            message_body=(
                "Bonjour Jean Martin, il reste 10 jours pour finaliser l'étape "
                "Dépôt de la demande de résidence. Merci de déposer vos documents."
            ),
        )
    )
    return "Famille Martin: 5 steps (2 done, step 3 in progress), J+10 reminder TO_APPROVE"


async def seed_volkov(db: AsyncSession, agency: Agency, artur: Agent) -> str:
    expat = await get_or_create_expat(
        db, "Aleksei", "Volkov", "aleksei.volkov@example.com", lang="ru"
    )
    steps = await get_or_create_template(
        db,
        agency,
        "Domiciliation Bulgarie",
        [
            ("Entretien initial", 3, "agent", []),
            ("Collecte des documents", 10, "expat", ["Passeport", "Justificatif de ressources"]),
            ("Traduction certifiée", 7, None, ["Actes d'état civil originaux"]),
            ("Enregistrement de l'adresse", 5, None, ["Contrat de bail"]),
            ("Dépôt du dossier de résidence", 20, None, []),
            ("Carte d'identité bulgare", 14, None, []),
            ("Numéro fiscal", 7, None, []),
        ],
        {2: [1], 3: [2], 4: [3], 5: [2], 6: [5]},  # 3←2, 4←3, 5←4, 6←3, 7←6
    )

    if await case_exists(db, agency, expat):
        return "Aleksei Volkov: already seeded, skipped"
    case = ClientCase(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=artur.id,
        journey_template_id=steps[0].template_id,
        origin_country="RU",
        dest_country="BG",
        status="in_progress",
        source="website",
        tags=["b2b"],
    )
    db.add(case)
    await db.flush()

    done_1 = NOW - timedelta(days=7)
    _progress(
        db,
        case,
        steps[0],
        status="done",
        completed_at=done_1,
        completed_by_agent_id=artur.id,
        responsible_type=None,
        updated_at=done_1,
    )
    step2 = _progress(db, case, steps[1], status="in_progress", responsible_type="expat")
    for step in steps[2:]:
        _progress(db, case, step)
    await db.flush()

    _log(db, case.id, "agent", artur.id, "case.created", {}, NOW - timedelta(days=10))
    _log(
        db,
        case.id,
        "agent",
        artur.id,
        "step.completed",
        {"step_progress_id": str(steps[0].id)},
        done_1,
    )
    _log(
        db,
        case.id,
        "agent",
        artur.id,
        "step.started",
        {"step_progress_id": str(step2.id)},
        NOW,
    )
    return "Aleksei Volkov: 7 steps (1 done, step 2 in progress), preferred_lang=ru"


async def seed_dupont(db: AsyncSession, agency: Agency, sidney: Agent) -> str:
    expat = await get_or_create_expat(db, "Sophie", "Dupont", "sophie.dupont@example.com")
    steps = await get_or_create_template(
        db,
        agency,
        "Pack expatriation",
        [
            ("Bilan d'expatriation", 5, "agent", []),
            ("Choix de la destination & visa", 10, None, ["Passeport en cours de validité"]),
            (
                "Constitution du dossier",
                15,
                None,
                ["Justificatifs de revenus", "Attestation d'assurance"],
            ),
            ("Installation & formalités locales", 30, None, []),
        ],
        {1: [0], 2: [1], 3: [2]},  # 2←1, 3←2, 4←3
    )

    if await case_exists(db, agency, expat):
        return "Sophie Dupont: already seeded, skipped"
    case = ClientCase(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=sidney.id,
        journey_template_id=steps[0].template_id,
        origin_country="FR",
        dest_country="PT",
        status="in_progress",
        source="instagram",
        tags=[],
    )
    db.add(case)
    await db.flush()

    step1 = _progress(
        db,
        case,
        steps[0],
        status="in_progress",
        responsible_type="agent",
        responsible_agent_id=sidney.id,
    )
    for step in steps[1:]:
        _progress(db, case, step)
    await db.flush()

    _log(db, case.id, "agent", sidney.id, "case.created", {}, NOW - timedelta(days=2))
    _log(
        db,
        case.id,
        "agent",
        sidney.id,
        "step.started",
        {"step_progress_id": str(step1.id)},
        NOW,
    )
    # Didier's demo: with 2←1 and 3←2, step 3 projects as BLOCKED.
    return "Sophie Dupont: 4 steps (step 1 in progress), step 3 projects BLOCKED"


# --- main ------------------------------------------------------------------------------


async def seed(mode: str = "dev") -> None:
    async with async_session_maker() as db:
        # SHARED baselines — the exact functions the test harness uses.
        # Strictly idempotent on every branch: insert-missing catalogue,
        # additive system-role matrix, declarative binding upsert,
        # create-if-absent job configs — safe on every deploy.
        await seed_rbac_baseline(db, bindings=collect_bindings())
        await seed_job_configs(db)

        if mode == "prod":
            print("=" * 72)
            print("Nidria seed complete (prod mode: baseline only).")
            print(f"  RBAC baseline: {len(collect_bindings())} bindings, 4 system roles")
            print("  Job configs: dispatch_reminders (* * * * *), auto_reminders (0 7 * * *)")
            print("=" * 72)
            return

        roles = {
            role.name: role
            for role in (await db.execute(select(Role).where(Role.is_system))).scalars()
        }
        admin, member = roles["admin"], roles["member"]

        # Reside Paraguay — Eloïse is a MEMBER: the approval scenario.
        reside = await get_or_create_agency(db, "reside-paraguay", "Reside Paraguay")
        await get_or_create_agent(
            db, reside, admin, "Alexis", "Renard", "alexis@reside-paraguay.com"
        )
        eloise = await get_or_create_agent(
            db, reside, member, "Eloïse", "Bertin", "eloise@reside-paraguay.com"
        )
        selim = await get_or_create_agent(
            db, reside, member, "Sélim", "Haddad", "selim@reside-paraguay.com"
        )
        await get_or_create_agent(db, reside, member, "Inès", "Costa", "ines@reside-paraguay.com")
        await get_or_create_agent(
            db, reside, member, "Mathias", "Leroy", "mathias@reside-paraguay.com"
        )

        bulgarie = await get_or_create_agency(
            db, "domiciliation-bulgarie", "Domiciliation Bulgarie"
        )
        artur = await get_or_create_agent(
            db, bulgarie, admin, "Artur", "Dimitrov", "artur@domiciliation-bulgarie.com"
        )

        expatriation = await get_or_create_agency(db, "expatriation-io", "Expatriation.io")
        sidney = await get_or_create_agent(
            db, expatriation, admin, "Sidney", "Moreau", "sidney@expatriation.io"
        )

        results = [
            await seed_martin(db, reside, eloise, selim),
            await seed_volkov(db, bulgarie, artur),
            await seed_dupont(db, expatriation, sidney),
        ]
        await db.commit()

    print("=" * 72)
    print("Nidria seed complete.")
    print(f"  RBAC baseline: {len(collect_bindings())} bindings, 4 system roles")
    print("  Job configs: dispatch_reminders (* * * * *), auto_reminders (0 7 * * *)")
    for line in results:
        print(f"  {line}")
    print("-" * 72)
    print(f"  ALL demo passwords (agents AND expats): {DEMO_PASSWORD}")
    print("  Agents:")
    print("    Reside Paraguay        alexis@reside-paraguay.com (admin)")
    print("                           eloise@ / selim@ / ines@ / mathias@reside-paraguay.com")
    print("    Domiciliation Bulgarie artur@domiciliation-bulgarie.com (admin)")
    print("    Expatriation.io        sidney@expatriation.io (admin)")
    print("  Expats:")
    print("    jean.martin@example.com / aleksei.volkov@example.com / sophie.dupont@example.com")
    print("=" * 72)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the Nidria database (idempotent).")
    parser.add_argument("--mode", choices=["dev", "prod"], default="dev")
    asyncio.run(seed(parser.parse_args().mode))
