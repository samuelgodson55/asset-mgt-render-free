# Site-wide maintenance-mode toggle. Two endpoints only: read the current
# status (open to any authenticated caller, including the maintenance
# middleware's own exempt-path check) and flip it (root-admin only). See
# backend/middleware/maintenance_mode.py for how `enabled` actually gates
# traffic, and backend/services/maintenance_service.py for persistence.
from telemetry import trace_operation

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
from deps import require_true_super_admin
from schemas.maintenance_schema import MaintenanceStatusUpdate
import services.maintenance_service as maintenance_service

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

@router.get("/status")
def maintenance_status(db: Session = Depends(get_db)):
    # No auth dependency on purpose: the frontend needs to know whether
    # maintenance mode is on BEFORE a user is necessarily logged in (e.g.
    # to render the maintenance banner instead of the login form), and
    # this endpoint is one of the maintenance middleware's own exempt
    # paths so it keeps answering even while maintenance mode is enabled.
    return maintenance_service.get_status(db)

@router.put("/status")
@trace_operation("maintenance.update")
def update_maintenance_status(payload: MaintenanceStatusUpdate, db: Session = Depends(get_db), user: dict = Depends(require_true_super_admin)):
    # Gated to the true (single, non-impersonated) super admin -- see
    # deps.require_true_super_admin -- since toggling this can lock every
    # other user out of the whole app.
    return maintenance_service.update_status(db, payload.enabled, payload.message, user)
