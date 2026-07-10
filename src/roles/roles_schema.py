import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.core.rbac.permissions import Permission


class PermissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # ENUM in the contract, generated from the in-code catalogue (never a
    # hand-kept mirror): every new permission changes the openapi, the CI
    # openapi check forces the regen, the front's generated types carry the
    # new key — the whole chain is mechanical. A rogue DB key (impossible via
    # sync_permissions, insert-only from the enum) would fail LOUDLY here
    # instead of silently reaching the front as an unknown string.
    key: Permission
    label: str | None
    category: str | None


class RoleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    permission_ids: list[uuid.UUID]


class RoleRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RolePermissionsSetRequest(BaseModel):
    permission_ids: list[uuid.UUID]


class RoleDuplicateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MemberRoleSetRequest(BaseModel):
    role_id: uuid.UUID


class RoleDetailResponse(BaseModel):
    """Mutation responses carry the full matrix so the Settings screen
    never needs a follow-up GET. `cloned_from_role_id` set = this is
    the agency's copy-on-write clone of a system role."""

    id: uuid.UUID
    name: str
    is_system: bool
    cloned_from_role_id: uuid.UUID | None
    permissions: list[PermissionResponse]
