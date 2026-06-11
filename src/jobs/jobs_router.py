import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db, get_sync_session_local
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.jobs.jobs_manager import JobsManager
from src.jobs.jobs_schema import (
    JobConfigResponse,
    JobConfigUpdateRequest,
    JobPauseRequest,
    JobRunDetailResponse,
    JobRunResponse,
    JobTriggerRequest,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Everything (reads included) under job.manage: this is platform ops,
# not tenant reference data. Admin-only in the default matrix.
_MANAGE = Permission.JOB_MANAGE

BINDINGS = [
    RouteBinding("GET", "/jobs", Audience.AGENT, _MANAGE),
    RouteBinding("PATCH", "/jobs/{job_id}", Audience.AGENT, _MANAGE),
    RouteBinding("POST", "/jobs/{job_id}/pause", Audience.AGENT, _MANAGE),
    RouteBinding("POST", "/jobs/{job_id}/resume", Audience.AGENT, _MANAGE),
    RouteBinding("POST", "/jobs/{job_id}/trigger", Audience.AGENT, _MANAGE),
    RouteBinding("GET", "/jobs/{job_id}/runs", Audience.AGENT, _MANAGE),
    RouteBinding("GET", "/jobs/{job_id}/runs/{run_id}", Audience.AGENT, _MANAGE),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]
SyncSessionLocalDep = Annotated[sessionmaker[Session], Depends(get_sync_session_local)]


@router.get("", response_model=list[JobConfigResponse])
async def list_jobs(agent: AgentDep, db: DbDep) -> list[JobConfigResponse]:
    configs = await JobsManager(db).list_jobs()
    return [JobConfigResponse.model_validate(config) for config in configs]


@router.patch("/{job_id}", response_model=JobConfigResponse)
async def update_job(
    job_id: str,
    body: JobConfigUpdateRequest,
    request: Request,
    agent: AgentDep,
    db: DbDep,
) -> JobConfigResponse:
    scheduler = getattr(request.app.state, "scheduler", None)
    session_local = getattr(request.app.state, "sync_session_local", None)
    config = await JobsManager(db).update_job(job_id, body, scheduler, session_local)
    return JobConfigResponse.model_validate(config)


@router.post("/{job_id}/pause", response_model=JobConfigResponse)
async def pause_job(
    job_id: str, body: JobPauseRequest, agent: AgentDep, db: DbDep
) -> JobConfigResponse:
    config = await JobsManager(db).pause_job(job_id, body.until)
    return JobConfigResponse.model_validate(config)


@router.post("/{job_id}/resume", response_model=JobConfigResponse)
async def resume_job(job_id: str, agent: AgentDep, db: DbDep) -> JobConfigResponse:
    config = await JobsManager(db).resume_job(job_id)
    return JobConfigResponse.model_validate(config)


@router.post("/{job_id}/trigger", response_model=JobRunDetailResponse)
async def trigger_job(
    job_id: str,
    body: JobTriggerRequest,
    agent: AgentDep,
    db: DbDep,
    session_local: SyncSessionLocalDep,
) -> JobRunDetailResponse:
    run = await JobsManager(db).trigger_job(agent, job_id, body.dry_run, session_local)
    return JobRunDetailResponse.model_validate(run)


@router.get("/{job_id}/runs", response_model=list[JobRunResponse])
async def list_job_runs(
    job_id: str,
    agent: AgentDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[JobRunResponse]:
    runs = await JobsManager(db).list_runs(job_id, limit)
    return [JobRunResponse.model_validate(run) for run in runs]


@router.get("/{job_id}/runs/{run_id}", response_model=JobRunDetailResponse)
async def get_job_run(
    job_id: str, run_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> JobRunDetailResponse:
    run = await JobsManager(db).get_run(job_id, run_id)
    return JobRunDetailResponse.model_validate(run)
