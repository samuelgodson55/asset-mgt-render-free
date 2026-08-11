"""
api/reports_api.py
-------------------
GET /reports/dashboard, plus one endpoint per section for a caller that
only wants a single slice:
  GET /reports/utilization
  GET /reports/overdue-trend
  GET /reports/spend
  GET /reports/revenue
  GET /reports/quotation-turnaround

Manager/Admin/Super-Admin-only (require_privileged_role -- same gate as
GET /audit-logs) business-metrics view: utilization by asset type, overdue trends, rental
revenue by asset department, legacy spend breakdowns, and quotation
approval turnaround time. Deliberately separate from OpenTelemetry tracing
(telemetry.py) -- this answers "how is the fleet being used", not "why
was this request slow". See services/reports_service.py's module
docstring for the full reasoning and exactly how each figure is derived.

Every route is synchronous (unlike GET /audit-logs/export's Celery job
trio) -- these are aggregate reads over the same bounded tables the rest
of the app already queries synchronously (AssetType, AssetCheckout,
Quotation), not a bulk file-generation job, so there's nothing here that
risks tying up a worker process the way a wide CSV/PDF export could.
"""

import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from deps import require_privileged_role
import services.reports_service as reports_service

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/dashboard")
def get_dashboard(
    start_date: Optional[datetime.date] = Query(None, description="Inclusive start of the reporting window."),
    end_date: Optional[datetime.date] = Query(None, description="Inclusive end of the reporting window."),
    category: Optional[str] = Query(None, description="Optional AssetType.category filter for the utilization section."),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return reports_service.get_dashboard(db, start_date, end_date, category)


@router.get("/utilization")
def get_utilization(
    start_date: Optional[datetime.date] = Query(None),
    end_date: Optional[datetime.date] = Query(None),
    category: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return reports_service.get_utilization_by_asset_type(db, start_date, end_date, category)


@router.get("/overdue-trend")
def get_overdue_trend(
    start_date: Optional[datetime.date] = Query(None),
    end_date: Optional[datetime.date] = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return reports_service.get_overdue_trend(db, start_date, end_date)


@router.get("/spend")
def get_spend(
    start_date: Optional[datetime.date] = Query(None),
    end_date: Optional[datetime.date] = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return reports_service.get_spend_breakdown(db, start_date, end_date)


@router.get("/revenue")
def get_revenue(
    start_date: Optional[datetime.date] = Query(None),
    end_date: Optional[datetime.date] = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return reports_service.get_revenue_by_asset_department(db, start_date, end_date)


@router.get("/quotation-turnaround")
def get_quotation_turnaround(
    start_date: Optional[datetime.date] = Query(None),
    end_date: Optional[datetime.date] = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return reports_service.get_quotation_turnaround(db, start_date, end_date)
