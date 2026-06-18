"""Backfill: responsible → participant role=executant ("Action à réaliser par"
1 → N refonte). Idempotent (check-then-insert), one transaction.

- TEMPLATE: journey_template_step.default_responsible_* → journey_step_participant
  (executant) — ALL templates.
- INSTANCE: case_step_progress.responsible_* → case_step_participant (executant)
  — ACTIVE dossiers only (not closed/validated, not soft-deleted).
- An is_external agent participant gains its case assignment (portal scoping).
- Steps without a responsible are IGNORED (we never invent a participant).

Runs against the DB pointed at by settings (DATABASE_URL). Use --dry-run for a
READ-ONLY count (no write) before the real run — e.g. on prod:
    fly ssh console -a nidria-api -C "python -m scripts.backfill_participants --dry-run"
then, after validating the counts:
    fly ssh console -a nidria-api -C "python -m scripts.backfill_participants"
"""

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_external_assignment import CaseExternalAssignment
from shared.models.case_step_participant import CaseStepParticipant
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.journey import JourneyStepParticipant, JourneyTemplateStep
from src.core.database import engine

_TERMINAL = ("closed", "validated")


async def _backfill(session: AsyncSession, *, write: bool) -> dict[str, int]:
    """Returns counts. With write=False, nothing is added (pure read)."""
    counts = {
        "template": 0,
        "instance": 0,
        "scoped": 0,
        "template_ignored": 0,
        "instance_ignored": 0,
    }

    # --- TEMPLATE: default_responsible_* → journey_step_participant executant
    existing_tpl = {
        (p.step_id, p.type, p.agent_id)
        for p in (
            await session.execute(
                select(JourneyStepParticipant).where(JourneyStepParticipant.role == "executant")
            )
        ).scalars()
    }
    for s in (await session.execute(select(JourneyTemplateStep))).scalars():
        has_resp = (
            s.default_responsible_type == "expat" or s.default_responsible_agent_id is not None
        )
        if not has_resp:
            counts["template_ignored"] += 1
            continue
        p_type = "expat" if s.default_responsible_type == "expat" else "agent"
        p_agent = None if p_type == "expat" else s.default_responsible_agent_id
        if (s.id, p_type, p_agent) in existing_tpl:
            continue
        counts["template"] += 1
        if write:
            session.add(
                JourneyStepParticipant(
                    step_id=s.id, type=p_type, agent_id=p_agent, role="executant"
                )
            )
            existing_tpl.add((s.id, p_type, p_agent))

    # --- INSTANCE (active dossiers): responsible_* → case_step_participant
    existing_inst = {
        (p.case_step_progress_id, p.type, p.agent_id, p.external_id)
        for p in (
            await session.execute(
                select(CaseStepParticipant).where(CaseStepParticipant.role == "executant")
            )
        ).scalars()
    }
    rows = (
        await session.execute(
            select(
                CaseStepProgress,
                ClientCase.owner_agent_id,
                ClientCase.id.label("cid"),
                Agent.is_external,
            )
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .outerjoin(Agent, Agent.id == CaseStepProgress.responsible_agent_id)
            .where(
                ClientCase.deleted_at.is_(None),
                ClientCase.status.notin_(_TERMINAL),
            )
        )
    ).all()
    for prog, owner_agent_id, cid, is_external in rows:
        rt = prog.responsible_type
        if rt == "expat":
            p_type, p_agent, p_ext = "expat", None, None
        elif rt == "agent":
            p_type, p_agent, p_ext = "agent", prog.responsible_agent_id, None
        elif rt == "external":
            p_type, p_agent, p_ext = "external", None, prog.responsible_external_id
        else:
            counts["instance_ignored"] += 1
            continue
        key = (prog.id, p_type, p_agent, p_ext)
        if key in existing_inst:
            continue
        counts["instance"] += 1
        if write:
            session.add(
                CaseStepParticipant(
                    case_step_progress_id=prog.id,
                    type=p_type,
                    agent_id=p_agent,
                    external_id=p_ext,
                    role="executant",
                )
            )
            existing_inst.add(key)
        if p_type == "agent" and is_external:
            already = (
                await session.execute(
                    select(CaseExternalAssignment).where(
                        CaseExternalAssignment.case_id == cid,
                        CaseExternalAssignment.agent_id == p_agent,
                    )
                )
            ).first()
            if not already:
                counts["scoped"] += 1
                if write:
                    session.add(
                        CaseExternalAssignment(
                            case_id=cid, agent_id=p_agent, assigned_by_agent_id=owner_agent_id
                        )
                    )
    if write:
        await session.commit()
    return counts


async def main(dry_run: bool) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        counts = await _backfill(session, write=not dry_run)
    await engine.dispose()
    mode = "DRY-RUN (read-only)" if dry_run else "WRITE"
    tpl, tpl_ig = counts["template"], counts["template_ignored"]
    inst, inst_ig = counts["instance"], counts["instance_ignored"]
    print(f"[{mode}] participant backfill")
    print(f"  template executant   : {tpl}  (ignored: {tpl_ig})")
    print(f"  instance executant   : {inst}  (ignored: {inst_ig})")
    print(f"  external scoping new : {counts['scoped']}")
    print(f"  TOTAL participant rows: {tpl + inst}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="count only, no write")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
