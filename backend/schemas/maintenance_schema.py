# Request/response models for the site-wide maintenance-mode toggle (see
# backend/api/maintenance_api.py and backend/middleware/maintenance_mode.py,
# which reads the persisted status this schema describes and short-circuits
# every non-exempt request with a 503 while maintenance is enabled).
from pydantic import BaseModel, Field


class MaintenanceStatusUpdate(BaseModel):
    """Request body for PUT-ing a new maintenance status (admin-only)."""

    enabled: bool
    # Shown to end users on the maintenance page while `enabled` is true;
    # bounded so an admin can't accidentally paste something enormous into
    # a page every visitor will see.
    message: str = Field(default="We are currently performing scheduled maintenance. Please check back shortly.", min_length=1, max_length=500)


class MaintenanceStatus(BaseModel):
    """Current maintenance status, as returned by GET and after an update."""

    enabled: bool
    message: str
    # Both None until maintenance mode has been toggled at least once;
    # populated with an ISO timestamp + the acting admin's identity after
    # that, for basic auditability of who flipped the switch and when.
    updated_at: str | None = None
    updated_by: str | None = None
