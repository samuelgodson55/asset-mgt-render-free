from pydantic import BaseModel, Field


class MaintenanceStatusUpdate(BaseModel):
    enabled: bool
    message: str = Field(default="We are currently performing scheduled maintenance. Please check back shortly.", min_length=1, max_length=500)


class MaintenanceStatus(BaseModel):
    enabled: bool
    message: str
    updated_at: str | None = None
    updated_by: str | None = None
