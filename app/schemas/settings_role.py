import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RoleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if len(cleaned) < 2:
            raise ValueError("Role name must be at least 2 characters")
        return cleaned


class RoleUpdateRequest(RoleCreateRequest):
    pass


class RoleData(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    is_system: bool


class RoleListData(BaseModel):
    items: list[RoleData]
    page: int
    size: int
    total: int


class RoleListResponse(BaseModel):
    success: bool
    data: RoleListData


class RoleResponse(BaseModel):
    success: bool
    message: str
    data: RoleData


class RoleDeleteResponse(BaseModel):
    success: bool
    message: str
