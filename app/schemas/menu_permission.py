import uuid

from pydantic import BaseModel, ConfigDict, Field


class MenuCatalogItem(BaseModel):
    key: str
    label: str
    actions: list[str]
    permission_keys: list[str]


class MenuCatalogResponse(BaseModel):
    success: bool
    data: list[MenuCatalogItem]


class RolePermissionData(BaseModel):
    role_id: uuid.UUID
    role_name: str
    permission_keys: list[str]


class RolePermissionResponse(BaseModel):
    success: bool
    data: RolePermissionData


class RolePermissionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permission_keys: list[str] = Field(default_factory=list, max_length=100)


class RolePermissionUpdateResponse(BaseModel):
    success: bool
    message: str
    data: RolePermissionData
