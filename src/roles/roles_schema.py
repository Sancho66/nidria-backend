import uuid

from pydantic import BaseModel, ConfigDict, Field


class PermissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
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
