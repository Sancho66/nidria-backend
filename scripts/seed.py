"""Seed: RBAC baseline + job configs (via the SHARED functions — zero
duplicated logic) + the 3 demo agencies/agents + the 3 brief cases
(Martin, Volkov, Dupont).

IDEMPOTENT: get-or-create on natural keys (slug, email, template name);
a case found for (agency, principal) skips its whole block. Re-run at
will, no duplicates. start.sh runs it on EVERY boot.

Modes (--mode or SEED_MODE env):
  --mode dev  (default): baseline + the 3 demo agencies/cases with the
              printed Demo1234! password.
  --mode prod: REFUSES outside ENVIRONMENT=production (mirror of the
              db-reset guard). Baseline + ONE agency (Nidria Demo) with
              real-email accounts whose passwords are random and thrown
              away — first login goes through forgot-password. No
              password is ever printed.

Run: uv run python scripts/seed.py [--mode dev|prod]
"""

import argparse
import asyncio
import os
import secrets
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
from src.core.config import get_settings  # noqa: E402
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
    password: str | None = None,
) -> Agent:
    agent = (await db.execute(select(Agent).where(Agent.email == email))).scalar_one_or_none()
    if agent is None:
        agent = Agent(
            agency_id=agency.id,
            role_id=role.id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            password_hash=hash_password(password or DEMO_PASSWORD),
        )
        db.add(agent)
        await db.flush()
    elif (agent.first_name, agent.last_name) != (first_name, last_name):
        # Seed-owned demo identity: the seed stays the source of truth
        # for these rows' NAMES (a rename in the seed must reach
        # already-seeded databases). Password and roles never touched.
        agent.first_name, agent.last_name = first_name, last_name
        await db.flush()
    return agent


async def get_or_create_expat(
    db: AsyncSession,
    first_name: str,
    last_name: str,
    email: str,
    lang: str = "fr",
    password: str | None = None,
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
            password_hash=hash_password(password or DEMO_PASSWORD),
            activated_at=NOW,  # activated: expat login + forgot-password both work
        )
        db.add(expat)
        await db.flush()
    elif (expat.first_name, expat.last_name) != (first_name, last_name):
        # Same name-sync rule as agents (seed-owned demo identities).
        expat.first_name, expat.last_name = first_name, last_name
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


async def seed_martin(
    db: AsyncSession,
    agency: Agency,
    expat: ExpatUser,
    owner: Agent,
    second_agent: Agent,
    label: str,
) -> str:
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
        return f"{label}: already seeded, skipped"
    case = ClientCase(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=owner.id,
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
        completed_by_agent_id=owner.id,
        updated_at=done_1,
    )
    _progress(
        db,
        case,
        steps[1],
        status="done",
        completed_at=done_2,
        completed_by_agent_id=second_agent.id,
        updated_at=done_2,
    )
    step3 = _progress(db, case, steps[2], status="in_progress", responsible_type="expat")
    _progress(db, case, steps[3])
    _progress(db, case, steps[4])
    await db.flush()

    _log(db, case.id, "agent", owner.id, "case.created", {}, NOW - timedelta(days=45))
    _log(
        db,
        case.id,
        "agent",
        owner.id,
        "step.completed",
        {"step_progress_id": str(steps[0].id)},
        done_1,
    )
    _log(
        db,
        case.id,
        "agent",
        second_agent.id,
        "step.completed",
        {"step_progress_id": str(steps[1].id)},
        done_2,
    )
    _log(
        db,
        case.id,
        "agent",
        owner.id,
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
                f"Bonjour {expat.first_name} {expat.last_name}, il reste 10 jours pour "
                "finaliser l'étape Dépôt de la demande de résidence. "
                "Merci de déposer vos documents."
            ),
        )
    )
    return f"{label}: 5 steps (2 done, step 3 in progress), J+10 reminder TO_APPROVE"


async def seed_volkov(
    db: AsyncSession, agency: Agency, expat: ExpatUser, owner: Agent, label: str
) -> str:
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
        return f"{label}: already seeded, skipped"
    case = ClientCase(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=owner.id,
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
        completed_by_agent_id=owner.id,
        responsible_type=None,
        updated_at=done_1,
    )
    step2 = _progress(db, case, steps[1], status="in_progress", responsible_type="expat")
    for step in steps[2:]:
        _progress(db, case, step)
    await db.flush()

    _log(db, case.id, "agent", owner.id, "case.created", {}, NOW - timedelta(days=10))
    _log(
        db,
        case.id,
        "agent",
        owner.id,
        "step.completed",
        {"step_progress_id": str(steps[0].id)},
        done_1,
    )
    _log(
        db,
        case.id,
        "agent",
        owner.id,
        "step.started",
        {"step_progress_id": str(step2.id)},
        NOW,
    )
    return f"{label}: 7 steps (1 done, step 2 in progress), lang={expat.preferred_lang}"


async def seed_dupont(
    db: AsyncSession, agency: Agency, expat: ExpatUser, owner: Agent, label: str
) -> str:
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
        return f"{label}: already seeded, skipped"
    case = ClientCase(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=owner.id,
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
        responsible_agent_id=owner.id,
    )
    for step in steps[1:]:
        _progress(db, case, step)
    await db.flush()

    _log(db, case.id, "agent", owner.id, "case.created", {}, NOW - timedelta(days=2))
    _log(
        db,
        case.id,
        "agent",
        owner.id,
        "step.started",
        {"step_progress_id": str(step1.id)},
        NOW,
    )
    # Didier's demo: with 2←1 and 3←2, step 3 projects as BLOCKED.
    return f"{label}: 4 steps (step 1 in progress), step 3 projects BLOCKED"


# --- main ------------------------------------------------------------------------------

# Real inboxes; names encode the ROLE (one single human name each for
# Alexandre and Eric, the demo personas are named by what they are).
PROD_AGENT_ADMIN = "alexandre.montilla@gmail.com"  # Alexandre Montilla
PROD_AGENT_ADMIN_2 = "mr.schalk.eric@gmail.com"  # Eric Schalk
PROD_AGENT_MEMBER = "sasha.montilla.66@gmail.com"  # Membre Démo
PROD_EXPAT_MARTIN = "alexandre.montilla@procuroma.com"  # Client Martin
PROD_EXPAT_VOLKOV = "sasha.montilla.66@gmail.com"  # Client Volkov
PROD_EXPAT_DUPONT = "alexandre.montilla@gmail.com"  # Client Dupont


def _throwaway_password() -> str:
    """Random, never stored, never printed: first login goes through
    forgot-password (the prod emails are real, Resend delivers)."""
    return secrets.token_urlsafe(32)


async def seed_dev(db: AsyncSession, roles: dict[str, Role]) -> list[str]:
    admin, member = roles["admin"], roles["member"]

    # Reside Paraguay — Eloïse is a MEMBER: the approval scenario.
    reside = await get_or_create_agency(db, "reside-paraguay", "Reside Paraguay")
    await get_or_create_agent(db, reside, admin, "Alexis", "Renard", "alexis@reside-paraguay.com")
    eloise = await get_or_create_agent(
        db, reside, member, "Eloïse", "Bertin", "eloise@reside-paraguay.com"
    )
    selim = await get_or_create_agent(
        db, reside, member, "Sélim", "Haddad", "selim@reside-paraguay.com"
    )
    await get_or_create_agent(db, reside, member, "Inès", "Costa", "ines@reside-paraguay.com")
    await get_or_create_agent(db, reside, member, "Mathias", "Leroy", "mathias@reside-paraguay.com")

    bulgarie = await get_or_create_agency(db, "domiciliation-bulgarie", "Domiciliation Bulgarie")
    artur = await get_or_create_agent(
        db, bulgarie, admin, "Artur", "Dimitrov", "artur@domiciliation-bulgarie.com"
    )

    expatriation = await get_or_create_agency(db, "expatriation-io", "Expatriation.io")
    sidney = await get_or_create_agent(
        db, expatriation, admin, "Sidney", "Moreau", "sidney@expatriation.io"
    )

    martin = await get_or_create_expat(db, "Jean", "Martin", "jean.martin@example.com")
    volkov = await get_or_create_expat(
        db, "Aleksei", "Volkov", "aleksei.volkov@example.com", lang="ru"
    )
    dupont = await get_or_create_expat(db, "Sophie", "Dupont", "sophie.dupont@example.com")

    return [
        await seed_martin(db, reside, martin, eloise, selim, "Famille Martin"),
        await seed_volkov(db, bulgarie, volkov, artur, "Aleksei Volkov"),
        await seed_dupont(db, expatriation, dupont, sidney, "Sophie Dupont"),
        # The prod dataset, mirrored in dev (DEMO_PASSWORD credentials).
        *await seed_nidria_demo(db, roles, throwaway_passwords=False),
    ]


async def seed_nidria_demo(
    db: AsyncSession, roles: dict[str, Role], *, throwaway_passwords: bool
) -> list[str]:
    """The Nidria Demo agency — IDENTICAL dataset in prod and dev so
    what you test locally is what runs live. Only the credentials
    differ: throwaway (forgot-password first login) in prod, the
    printed DEMO_PASSWORD in dev."""

    def pwd() -> str | None:
        return _throwaway_password() if throwaway_passwords else None

    agency = await get_or_create_agency(db, "nidria-demo", "Nidria Demo")
    alexandre = await get_or_create_agent(
        db, agency, roles["admin"], "Alexandre", "Montilla", PROD_AGENT_ADMIN, password=pwd()
    )
    await get_or_create_agent(
        db, agency, roles["admin"], "Eric", "Schalk", PROD_AGENT_ADMIN_2, password=pwd()
    )
    membre = await get_or_create_agent(
        db, agency, roles["member"], "Membre", "Démo", PROD_AGENT_MEMBER, password=pwd()
    )

    martin = await get_or_create_expat(db, "Client", "Martin", PROD_EXPAT_MARTIN, password=pwd())
    volkov = await get_or_create_expat(db, "Client", "Volkov", PROD_EXPAT_VOLKOV, password=pwd())
    dupont = await get_or_create_expat(db, "Client", "Dupont", PROD_EXPAT_DUPONT, password=pwd())

    return [
        await seed_martin(db, agency, martin, membre, alexandre, "Martin-like"),
        await seed_volkov(db, agency, volkov, alexandre, "Volkov-like"),
        await seed_dupont(db, agency, dupont, alexandre, "Dupont-like"),
    ]


async def seed_prod(db: AsyncSession, roles: dict[str, Role]) -> list[str]:
    """One real agency, real emails, throwaway passwords. Same journeys
    /steps/prerequisites/required documents as the dev cases."""
    return await seed_nidria_demo(db, roles, throwaway_passwords=True)


async def run_seed(db: AsyncSession, mode: str) -> list[str]:
    """The whole seed against an EXISTING session — testable on the
    harness DB. The prod guard lives here so no entry point skips it."""
    if mode not in {"dev", "prod"}:
        raise SystemExit(f"Unknown seed mode {mode!r} — use dev|prod.")
    if mode == "prod" and get_settings().environment != "production":
        raise SystemExit(
            "Refusing --mode prod: ENVIRONMENT != production (mirror of the db-reset guard)."
        )

    # SHARED baselines — the exact functions the test harness uses.
    # Strictly idempotent on every branch: insert-missing catalogue,
    # additive system-role matrix, declarative binding upsert,
    # create-if-absent job configs — safe on every deploy.
    await seed_rbac_baseline(db, bindings=collect_bindings())
    await seed_job_configs(db)

    roles = {
        role.name: role for role in (await db.execute(select(Role).where(Role.is_system))).scalars()
    }
    results = await (seed_prod(db, roles) if mode == "prod" else seed_dev(db, roles))
    await db.commit()
    return results


async def seed(mode: str = "dev") -> None:
    async with async_session_maker() as db:
        results = await run_seed(db, mode)

    print("=" * 72)
    print(f"Nidria seed complete ({mode} mode).")
    print(f"  RBAC baseline: {len(collect_bindings())} bindings, 4 system roles")
    print("  Job configs: dispatch_reminders (* * * * *), auto_reminders (0 7 * * *)")
    for line in results:
        print(f"  {line}")
    print("-" * 72)
    if mode == "prod":
        print("  Agency: Nidria Demo (nidria-demo)")
        print(f"  Agents: {PROD_AGENT_ADMIN} (admin, Alexandre Montilla)")
        print(f"          {PROD_AGENT_ADMIN_2} (admin, Eric Schalk)")
        print(f"          {PROD_AGENT_MEMBER} (member, Membre Démo)")
        print(f"  Expats: {PROD_EXPAT_MARTIN} (Client Martin)")
        print(f"          {PROD_EXPAT_VOLKOV} (Client Volkov)")
        print(f"          {PROD_EXPAT_DUPONT} (Client Dupont)")
        print('  First login: use "Forgot password" — no seeded password is usable.')
    else:
        print(f"  ALL demo passwords (agents AND expats): {DEMO_PASSWORD}")
        print("  Agents:")
        print("    Reside Paraguay        alexis@reside-paraguay.com (admin)")
        print("                           eloise@ / selim@ / ines@ / mathias@reside-paraguay.com")
        print("    Domiciliation Bulgarie artur@domiciliation-bulgarie.com (admin)")
        print("    Expatriation.io        sidney@expatriation.io (admin)")
        print(f"    Nidria Demo            {PROD_AGENT_ADMIN} / {PROD_AGENT_ADMIN_2} (admins)")
        print(f"                           {PROD_AGENT_MEMBER} (member)")
        print("  Expats:")
        print(
            "    jean.martin@example.com / aleksei.volkov@example.com / sophie.dupont@example.com"
        )
        print(f"    {PROD_EXPAT_MARTIN} / {PROD_EXPAT_VOLKOV} / {PROD_EXPAT_DUPONT}")
    print("=" * 72)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the Nidria database (idempotent).")
    parser.add_argument(
        "--mode",
        choices=["dev", "prod"],
        default=os.environ.get("SEED_MODE", "dev"),
    )
    asyncio.run(seed(parser.parse_args().mode))
