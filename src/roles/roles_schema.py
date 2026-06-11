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


class MemberRolesSetRequest(BaseModel):
    role_ids: list[uuid.UUID]


class RoleDetailResponse(BaseModel):
    """Mutation responses carry the full matrix so the Settings screen
    never needs a follow-up GET."""

    id: uuid.UUID
    name: str
    is_system: bool
    permissions: list[PermissionResponse]
