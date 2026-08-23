"""
services/asset/export.py
----------------------------
CSV (streamed) and PDF (materialized) Asset Inventory export, sharing one
filtered/ordered query builder and one per-row shaping function so the
two formats can never drift from each other.
"""

from __future__ import annotations

import csv
import io
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from models import utc_now
import services.export_service as export_service

from .helpers import _can_see_stock, _inventory_status_label

# Unlike the "properties assigned" exports in user_service.py/
# outsider_service.py (which export CHECKOUTS -- who currently holds what),
# this exports the INVENTORY ITSELF -- one row per pool, exactly what the
# Asset Inventory table shows.
_INVENTORY_EXPORT_HEADERS = ["Pool ID", "Asset Name", "Category", "Department", "Price", "Available", "Total", "Status"]


def _inventory_export_query(db: Session, category: Optional[str]):
    """
    Shared filtered/ordered query builder for both the CSV (streamed) and
    PDF (materialized) inventory export paths below -- same category
    filtering rules either way, kept in one place so they can't drift.
    Returns the still-unexecuted query; each caller decides how to consume
    it.
    """
    query = db.query(models.AssetType).filter(~models.AssetType.is_deleted)

    cat_filter = (category or "").strip()
    if cat_filter and cat_filter.lower() != "all":
        # Case-insensitive exact match against the category string --
        # the dropdown that drives this is itself populated from the exact
        # distinct values on file (see list_asset_categories above), so
        # this only ever needs to match one of those, not do substring
        # search.
        query = query.filter(func.lower(models.AssetType.category) == cat_filter.lower())

    return query.order_by(models.AssetType.id)

def _inventory_export_row(p, fmt: str, show_stock: bool) -> list:
    """One CSV/PDF row for a single pool -- shared by both export paths so
    a column added to one format's row can't silently drift from the
    other's."""
    price_for_export = export_service.format_money(p.price) if p.price is not None else "—"
    price_for_csv = f"{float(p.price):.2f}" if p.price is not None else "—"
    row = [p.id, p.name, p.category or "—", p.department or "—"]
    row.append(price_for_export if fmt == "pdf" else price_for_csv)
    if show_stock:
        row += [p.available_quantity, p.total_quantity, _inventory_status_label(p.available_quantity)]
    return row

def _inventory_export_names(category: Optional[str]) -> tuple:
    """Returns (cat_filter, scope_label, filename_stub, today) shared by both export paths."""
    cat_filter = (category or "").strip()
    today = utc_now().strftime("%Y-%m-%d")
    scope_label = cat_filter if (cat_filter and cat_filter.lower() != "all") else "All Categories"
    filename_stub = f"asset_inventory_{cat_filter.replace(' ', '_')}" if (cat_filter and cat_filter.lower() != "all") else "asset_inventory_all"
    return cat_filter, scope_label, filename_stub, today

def export_assets_inventory_csv(db: Session, user: dict, category: Optional[str]):
    """
    Streams the Asset Inventory CSV export one row at a time instead of
    materializing the whole thing in memory first.

    BUG FIX: this used to do `db.query(models.AssetType)...all()` (no
    `.limit()`) and build the entire `rows` list AND the entire CSV
    string in memory before returning it -- fine at this app's current
    scale (a few hundred pools), but the exact same class of problem
    services/audit_service.export_audit_logs_csv() already solved for the
    audit ledger: an unbounded table's "download everything" export
    shouldn't require holding the whole result set (ORM rows, formatted
    strings, AND the final CSV text) in memory simultaneously. Asset pools
    aren't append-only the way audit log entries are, but there's no
    upper bound enforced on how many a growing catalog can have, so this
    export deserves the same treatment before it becomes the problem
    instead of before it's needed.

    Uses `.yield_per()` + `stream_results=True` so the DRIVER (psycopg2)
    also fetches in batches via a server-side cursor, rather than pulling
    every matching row across the wire before Python sees the first one --
    `.all()` (used by export_audit_logs_csv, and still fine there per that
    function's own docstring) buffers client-side; this goes one step
    further and avoids buffering server-side too, which matters more here
    since a full "Download All" export has no date-range filter to bound
    it the way the audit ledger export does.

    Returns (generator, filename) -- the API layer wraps the generator in
    a StreamingResponse.
    """
    show_stock = _can_see_stock(user)
    headers = _INVENTORY_EXPORT_HEADERS if show_stock else _INVENTORY_EXPORT_HEADERS[:5]
    cat_filter, _, filename_stub, today = _inventory_export_names(category)

    query = (
        _inventory_export_query(db, category)
        .execution_options(stream_results=True)
        .yield_per(500)
    )

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for p in query:
            writer.writerow([export_service.csv_safe_cell(cell) for cell in _inventory_export_row(p, "csv", show_stock)])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    return generate_csv(), f"{filename_stub}_{today}.csv"

def export_assets_inventory_pdf(db: Session, user: dict, category: Optional[str]):
    """
    PDF equivalent of export_assets_inventory_csv() above. Unlike the CSV
    path, this still materializes every row up front -- reportlab's
    Table/SimpleDocTemplate need the complete row set to lay the document
    out (column widths, page breaks, etc. -- see export_service.py's
    _column_widths()), so there's no meaningful way to stream a PDF build
    the way the CSV path streams its output. PDF exports of this table
    are also the less time/memory-sensitive of the two in practice --
    reportlab's own in-memory table construction is the dominant cost at
    any real row count, not this query.
    """
    pools = _inventory_export_query(db, category).all()
    show_stock = _can_see_stock(user)
    headers = _INVENTORY_EXPORT_HEADERS if show_stock else _INVENTORY_EXPORT_HEADERS[:5]
    rows = [_inventory_export_row(p, "pdf", show_stock) for p in pools]

    cat_filter, scope_label, filename_stub, today = _inventory_export_names(category)
    title = f"Asset Inventory — {scope_label}"
    subtitle = f"Exported by {user['email']} · {len(rows)} pool(s) · {today}"

    pdf_bytes = export_service.build_pdf_bytes(title, subtitle, headers, rows)
    return pdf_bytes, "application/pdf", f"{filename_stub}_{today}.pdf"
