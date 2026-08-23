"""
services/asset/exceptions.py
--------------------------------
Maintenance exceptions: isolating a serial for repair/loss (pulls it out
of the Available pool without touching Total Capacity) and recalling one
back into service.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from models import utc_now
from schemas.assets_schema import ExceptionCreate
from services.stock import recalculate_asset_stock


def flag_asset_exception(db: Session, asset_id: int, exc: ExceptionCreate, user: dict) -> dict:
    """
    Isolating a serial for repair/loss is a Super Admin-only action.

    Isolating a unit must NOT shrink `total_quantity` -- Total Capacity is a
    fixed number representing how many units the org owns. Isolating a unit
    only pulls it out of the Available pool: Available = Total - Outbound -
    Isolated. We simply create the exception record and let
    recalculate_asset_stock() derive the new Available count from it.
    """
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
    ).with_for_update().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    if asset.total_quantity <= 0:
        raise HTTPException(status_code=400, detail="No stock inventory exists to isolate")

    dup = db.query(models.AssetException).filter(
        models.AssetException.serial_number == exc.serial_number,
        models.AssetException.isolation_status == "isolated",
    ).first()
    if dup:
        raise HTTPException(status_code=400, detail="This serial number is already flagged")

    # Guard against isolating more units than are actually free right now
    # (you can't pull a unit out of the pool that's already checked out).
    stock = recalculate_asset_stock(db, asset)
    if stock["available"] <= 0:
        raise HTTPException(status_code=400, detail="No available units left to isolate -- all units are outbound or already isolated.")

    new_exception = models.AssetException(
        asset_type_id=asset_id, serial_number=exc.serial_number, status_label=exc.status_label,
        notes=exc.notes, isolation_status="isolated",
    )
    db.add(new_exception)
    db.flush()

    recalculate_asset_stock(db, asset)  # Available immediately drops by 1

    db.add(models.AuditLog(
        operator=user["email"], action="MAINTENANCE_ISOLATE", target_type="AssetException", target_id=asset_id,
        details=f"Flagged serial {exc.serial_number} as {exc.status_label}.",
    ))
    db.commit()
    return {"message": "Serial number exception logged."}

def recall_asset_exception(db: Session, asset_id: int, exception_id: int, user: dict) -> dict:
    """
    "Recall and Update" workflow: recovers a stolen item or returns a
    repaired item back into active service. Marks the exception as recalled
    (keeping it for history) and lets recalculate_asset_stock() increase
    Available by exactly 1 -- the isolated count drops, Available goes up by
    that same amount, Total Capacity never changes.
    """
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
    ).with_for_update().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    exception = db.query(models.AssetException).filter(
        models.AssetException.id == exception_id,
        models.AssetException.asset_type_id == asset_id,
        models.AssetException.isolation_status == "isolated",
    ).first()
    if not exception:
        raise HTTPException(status_code=404, detail="Active isolation record not found for this asset.")

    exception.isolation_status = "recalled"
    exception.recalled_at = utc_now()

    recalculate_asset_stock(db, asset)  # Isolated count drops, Available rises by 1

    db.add(models.AuditLog(
        operator=user["email"], action="MAINTENANCE_RECALL", target_type="AssetException", target_id=exception_id,
        details=f"Recalled serial {exception.serial_number} ('{exception.status_label}') back into service for '{asset.name}'.",
    ))
    db.commit()
    return {"message": f"Serial {exception.serial_number} recalled and returned to the Available pool."}
