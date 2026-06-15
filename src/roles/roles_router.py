import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.agencies.agencies_schema import AgencyMemberResponse
from src.auth.auth_schema import MessageResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.roles.roles_manager import RolesManager
from src.roles.roles_schema import (
    MemberRoleSetRequest,
    PermissionResponse,
    RoleCreateRequest,
    RoleDetailResponse,
    RoleDuplicateRequest,
    RolePermissionsSetRequest,
    RoleRenameRequest,
)

router = APIRouter(tags=["roles"])

BINDINGS = [
    RouteBinding("GET", "/permissions", Audience.AGENT, Permission.ROLE_MANAGE),
    RouteBinding("GET", "/agencies/me/roles/{role_id}", Audience.AGENT, Permission.ROLE_MANAGE),
    RouteBinding("POST", "/agencies/me/roles", Audience.AGENT, Permission.ROLE_MANAGE),
    RouteBinding("PATCH", "/agencies/me/roles/{role_id}", Audience.AGENT, Permission.ROLE_MANAGE),
    RouteBinding(
        "PUT",
        "/agencies/me/roles/{role_id}/permissions",
        Audience.AGENT,
        Permission.ROLE_MANAGE,
    ),
    RouteBinding("DELETE", "/agencies/me/roles/{role_id}", Audience.AGENT, Permission.ROLE_MANAGE),
    RouteBinding(
        "POST",
        "/agencies/me/roles/{role_id}/duplicate",
        Audience.AGENT,
        Permission.ROLE_MANAGE,
    ),
    # Assignment is an agent-management act, not a role-definition one.
    RouteBinding(
        "PUT",
        "/agencies/me/members/{agent_id}/role",
        Audience.AGENT,
        Permission.AGENT_MANAGE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


def _role_detail(role: Role) -> RoleDetailResponse:
    return RoleDetailResponse(
        id=role.id,
        name=role.name,
        is_system=role.is_system,
        cloned_from_role_id=role.cloned_from_role_id,
        permissions=[
            PermissionResponse.model_validate(p)
            for p in sorted(role.permissions, key=lambda p: p.key)
        ],
    )


def _member(agent: Agent) -> AgencyMemberResponse:
    return AgencyMemberResponse(
        id=agent.id,
        first_name=agent.first_name,
        last_name=agent.last_name,
        email=agent.email,
        role=agent.role.name,
        role_id=agent.role_id,
        is_external=agent.is_external,
    )


@router.get("/permissions", response_model=list[PermissionResponse])
async def list_permissions(agent: AgentDep, db: DbDep) -> list[PermissionResponse]:
    permissions = await RolesManager(db).list_permissions()
    return [PermissionResponse.model_validate(p) for p in permissions]


@router.get("/agencies/me/roles/{role_id}", response_model=RoleDetailResponse)
async def get_role(role_id: uuid.UUID, agent: AgentDep, db: DbDep) -> RoleDetailResponse:
    role = await RolesManager(db).get_role(agent, role_id)
    return _role_detail(role)


@router.post("/agencies/me/roles", response_model=RoleDetailResponse, status_code=201)
async def create_role(body: RoleCreateRequest, agent: AgentDep, db: DbDep) -> RoleDetailResponse:
    role = await RolesManager(db).create_role(agent, body.name, body.permission_ids)
    return _role_detail(role)


@router.patch("/agencies/me/roles/{role_id}", response_model=RoleDetailResponse)
async def rename_role(
    role_id: uuid.UUID, body: RoleRenameRequest, agent: AgentDep, db: DbDep
) -> RoleDetailResponse:
    """On a system role this is copy-on-write: the agency gets a custom
    clone (rebinding its wearers) and the rename lands on the clone."""
    role = await RolesManager(db).rename_role(agent, role_id, body.name)
    return _role_detail(role)


@router.put("/agencies/me/roles/{role_id}/permissions", response_model=RoleDetailResponse)
async def set_role_permissions(
    role_id: uuid.UUID, body: RolePermissionsSetRequest, agent: AgentDep, db: DbDep
) -> RoleDetailResponse:
    """Copy-on-write on system roles, same as PATCH."""
    role = await RolesManager(db).set_role_permissions(agent, role_id, body.permission_ids)
    return _role_detail(role)


@router.delete("/agencies/me/roles/{role_id}", response_model=MessageResponse)
async def delete_role(role_id: uuid.UUID, agent: AgentDep, db: DbDep) -> MessageResponse:
    await RolesManager(db).delete_role(agent, role_id)
    return MessageResponse(detail="Role deleted.")


@router.post(
    "/agencies/me/roles/{role_id}/duplicate",
    response_model=RoleDetailResponse,
    status_code=201,
)
async def duplicate_role(
    role_id: uuid.UUID, body: RoleDuplicateRequest, agent: AgentDep, db: DbDep
) -> RoleDetailResponse:
    role = await RolesManager(db).duplicate_role(agent, role_id, body.name)
    return _role_detail(role)


@router.put("/agencies/me/members/{agent_id}/role", response_model=AgencyMemberResponse)
async def set_member_role(
    agent_id: uuid.UUID, body: MemberRoleSetRequest, agent: AgentDep, db: DbDep
) -> AgencyMemberResponse:
    member = await RolesManager(db).set_member_role(agent, agent_id, body.role_id)
    return _member(member)
