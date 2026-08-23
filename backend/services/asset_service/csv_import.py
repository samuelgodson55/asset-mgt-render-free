"""
services/asset/csv_import.py
--------------------------------
Batch CSV import: create new pools and/or update existing ones (matched
by Pool ID) in a single upload, mirroring the columns Asset Inventory CSV
export produces (see export.py).
"""

from __future__ import annotations

import csv
import io
import re

from fastapi import HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from services.stock import recalculate_asset_stock

from .constants import MAX_CSV_UPLOAD_BYTES
from .helpers import _coerce_asset_price


def import_assets_from_csv(db: Session, file: UploadFile, user: dict) -> dict:
    """
    Import new asset pools and round-trip updates from Asset Inventory CSV
    exports.

    Two modes are supported in the same file:
      * New pool: no Pool ID; `name`/`Asset Name` and `total_quantity`/`Total`
        are required.
      * Existing pool update: `Pool ID`/`pool_id` identifies the pool. The
        editable columns (name, total quantity, category, department, price)
        are applied to that exact pool. `Available` and `Status` are exported
        for reference only and are intentionally ignored on import because
        available stock is derived from live checkouts/exceptions.

    Blank/"—" values for optional descriptive fields preserve the current
    value on an update. Use `__CLEAR__` when a Super Admin intentionally
    wants to clear category, department, or price.
    """
    raw_bytes = file.file.read(MAX_CSV_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_CSV_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"CSV file is too large. Maximum allowed size is {MAX_CSV_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    try:
        contents = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Could not read this file as UTF-8 text. Please export/save it as a plain-text CSV and try again.",
        )

    try:
        reader = csv.DictReader(io.StringIO(contents))
        if not reader.fieldnames:
            raise HTTPException(status_code=400, detail="Invalid CSV format: the file has no header row.")

        # Accept both the original import-template headers and the exact
        # headers produced by Asset Inventory CSV export. This makes an
        # export -> edit -> import round trip a first-class workflow.
        aliases = {
            "pool_id": ("pool id", "pool_id", "id"),
            "name": ("name", "asset name", "asset_name"),
            "total_quantity": ("total_quantity", "total quantity", "total"),
            "category": ("category",),
            "department": ("department",),
            "price": ("price",),
        }
        normalized_headers = {
            str(h or "").strip().lower().replace("-", " "): h
            for h in reader.fieldnames
        }

        def source_key(field: str):
            for alias in aliases[field]:
                if alias in normalized_headers:
                    return normalized_headers[alias]
            return None

        pool_id_key = source_key("pool_id")
        name_key = source_key("name")
        quantity_key = source_key("total_quantity")
        category_key = source_key("category")
        department_key = source_key("department")
        price_key = source_key("price")

        # A Pool ID column turns the file into an update-capable export. A
        # template/new-pool import still uses name + total_quantity.
        if not pool_id_key and (not name_key or not quantity_key):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid CSV format: use either an Asset Inventory export "
                    "(with 'Pool ID' and 'Asset Name') or a new-pool template "
                    "containing 'name' and 'total_quantity'."
                ),
            )

        imported_count = 0
        updated_count = 0
        errors = []
        seen_pool_ids = set()
        seen_names_this_file = {}

        def cell(row, key):
            return (row.get(key) or "").strip() if key else ""

        def is_preserve(value: str) -> bool:
            return not value or value == "—"

        def parse_pool_id(raw: str):
            if not raw:
                return None, None
            try:
                value = int(raw)
            except ValueError:
                return None, "Pool ID must be a whole number."
            if value <= 0:
                return None, "Pool ID must be greater than zero."
            return value, None

        def parse_quantity(raw: str):
            if not raw:
                return None, "'' is not a whole number for total quantity."
            try:
                value = int(raw)
            except ValueError:
                return None, f"'{raw}' is not a whole number for total quantity."
            if value < 0:
                return None, "Total quantity cannot be negative."
            return value, None

        def parse_price(raw: str):
            if is_preserve(raw):
                return None, None, False
            if raw == "__CLEAR__":
                return None, None, True
            # Exported values may be formatted as "₦1,899.00" (or another
            # configured currency symbol). Strip common presentation chars;
            # the Decimal validator remains the authority on the result.
            # Accept every display currency produced by export_service
            # (₦, $, £, €, GH₵, KSh, R, or a configured ISO-code prefix)
            # without making the importer depend on a particular deployment
            # currency. Keep only the numeric representation.
            cleaned = re.sub(r"[^0-9.\-]", "", raw)
            price, price_error = _coerce_asset_price(cleaned)
            if price_error:
                return None, price_error, False
            return price, None, True

        for line_number, row in enumerate(reader, start=2):
            raw_pool_id = cell(row, pool_id_key)
            pool_id, pool_id_error = parse_pool_id(raw_pool_id)
            if pool_id_error:
                errors.append({"row": line_number, "name": cell(row, name_key), "reason": pool_id_error})
                continue

            if pool_id is not None:
                if pool_id in seen_pool_ids:
                    errors.append({"row": line_number, "name": cell(row, name_key), "reason": f"Pool ID {pool_id} appears more than once in this import file."})
                    continue
                seen_pool_ids.add(pool_id)

                asset = db.query(models.AssetType).filter(
                    models.AssetType.id == pool_id,
                    ~models.AssetType.is_deleted,
                ).first()
                if not asset:
                    errors.append({"row": line_number, "name": cell(row, name_key), "reason": f"Pool ID {pool_id} was not found in active inventory."})
                    continue

                raw_name = cell(row, name_key)
                if raw_name and raw_name != "—":
                    duplicate = db.query(models.AssetType).filter(
                        func.lower(models.AssetType.name) == raw_name.lower(),
                        models.AssetType.id != pool_id,
                        ~models.AssetType.is_deleted,
                    ).first()
                    if duplicate:
                        errors.append({"row": line_number, "name": raw_name, "reason": f'Asset name "{raw_name}" already belongs to Pool ID {duplicate.id}.'})
                        continue

                raw_qty = cell(row, quantity_key)
                qty = None
                if raw_qty and raw_qty != "—":
                    qty, qty_error = parse_quantity(raw_qty)
                    if qty_error:
                        errors.append({"row": line_number, "name": raw_name or asset.name, "reason": qty_error})
                        continue
                    stock = recalculate_asset_stock(db, asset)
                    allocated_items = stock["outbound"] + stock["isolated"]
                    if qty < allocated_items:
                        errors.append({
                            "row": line_number,
                            "name": raw_name or asset.name,
                            "reason": f"Cannot reduce total below {allocated_items} ({stock['outbound']} outbound + {stock['isolated']} isolated).",
                        })
                        continue

                parsed_price = None
                price_provided = False
                if price_key:
                    raw_price = cell(row, price_key)
                    parsed_price, price_error, price_provided = parse_price(raw_price)
                    if price_error:
                        errors.append({"row": line_number, "name": raw_name or asset.name, "reason": price_error})
                        continue

                if qty is not None:
                    asset.total_quantity = qty
                    recalculate_asset_stock(db, asset)

                if raw_name and raw_name != "—" and raw_name != asset.name:
                    asset.name = raw_name

                if category_key:
                    raw_category = cell(row, category_key)
                    if raw_category == "__CLEAR__":
                        asset.category = None
                    elif not is_preserve(raw_category):
                        asset.category = raw_category

                if department_key:
                    raw_department = cell(row, department_key)
                    if raw_department == "__CLEAR__":
                        asset.department = None
                    elif not is_preserve(raw_department):
                        asset.department = raw_department

                if price_provided:
                    asset.price = parsed_price

                updated_count += 1
                continue

            # No Pool ID: this remains the original new-pool import mode.
            name = cell(row, name_key)
            if not name:
                errors.append({"row": line_number, "name": row.get(name_key) if name_key else None, "reason": "Missing asset name."})
                continue
            if name in seen_names_this_file:
                errors.append({"row": line_number, "name": name, "reason": f'Duplicate item "{name}" already exists in this import file (first seen on row {seen_names_this_file[name]}).'})
                continue
            seen_names_this_file[name] = line_number

            qty, qty_error = parse_quantity(cell(row, quantity_key))
            if qty_error:
                errors.append({"row": line_number, "name": name, "reason": qty_error})
                continue

            existing = db.query(models.AssetType).filter(models.AssetType.name == name).first()
            if existing:
                errors.append({
                    "row": line_number,
                    "name": name,
                    "reason": f'Item "{name}" already exists in the system (Pool ID {existing.id}). Update its quantity directly from the Asset Inventory table instead of re-importing it.',
                })
                continue

            category = None
            if category_key and not is_preserve(cell(row, category_key)):
                category = cell(row, category_key) or None
            department = None
            if department_key and not is_preserve(cell(row, department_key)):
                department = cell(row, department_key) or None

            price = None
            if price_key:
                raw_price = cell(row, price_key)
                parsed_price, price_error, provided = parse_price(raw_price)
                if price_error:
                    errors.append({"row": line_number, "name": name, "reason": price_error})
                    continue
                if provided:
                    price = parsed_price

            db.add(models.AssetType(
                name=name,
                total_quantity=qty,
                available_quantity=qty,
                category=category,
                department=department,
                price=price,
            ))
            imported_count += 1

        summary = f"CSV processed. Updated {updated_count} pool(s), registered {imported_count} new pool(s), {len(errors)} row(s) rejected."
        db.add(models.AuditLog(
            operator=user["email"], action="BATCH_IMPORT", target_type="AssetType", target_id=0,
            details=summary,
        ))
        db.commit()
        return {
            "message": summary,
            "imported_count": imported_count + updated_count,
            "updated_count": updated_count,
            "created_count": imported_count,
            "error_count": len(errors),
            "errors": errors,
        }
    except HTTPException:
        raise
    except Exception as e:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(e, None, 500, component="asset_service", operation="csv_import")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
