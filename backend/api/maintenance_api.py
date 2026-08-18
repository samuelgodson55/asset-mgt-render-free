from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
from deps import require_true_super_admin
from schemas.maintenance_schema import MaintenanceStatusUpdate
import services.maintenance_service as maintenance_service

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

@router.get("/status")
def maintenance_status(db: Session = Depends(get_db)):
    return maintenance_service.get_status(db)

@router.put("/status")
def update_maintenance_status(payload: MaintenanceStatusUpdate, db: Session = Depends(get_db), user: dict = Depends(require_true_super_admin)):
    return maintenance_service.update_status(db, payload.enabled, payload.message, user)
