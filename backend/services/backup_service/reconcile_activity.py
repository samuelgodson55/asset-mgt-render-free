"""
services/backup/reconcile_activity.py
----------------------------------------
Post-restore reconciliation for "outsider" (non-portal) contacts and asset
check-out/check-in activity -- the other things that legitimately drift
between when a backup was taken and when it's restored. Builds on
reconcile_credentials' per-user comparison for the "who owns this row now"
question these two also need to answer.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _reconcile_post_restore_outsiders(engine, pre_restore_outsiders: list) -> dict:
    """
    Outsider counterpart to _reconcile_post_restore_credentials() above --
    same "current reality wins for accounts we're preserving, nothing
    silently vanishes" philosophy, applied to models.Outsider (the
    ad-hoc, no-login "assign equipment to a name/contact" profiles used
    by checkouts/quotations that aren't tied to a real User).

    MATCHED BY ID, NOT EMAIL: unlike `users`, `outsiders.email` (and
    `.phone_number`, `.name`) carries no DB-level `unique=True` (see
    models.Outsider's own comment -- at least one contact field is
    required at creation time, but which one, and whether it's actually
    unique across profiles, is never enforced). There is no reliable
    business key to match "the same ad-hoc person" across the pre-restore
    and restored data the way lower(email) does for a real account, so
    this matches strictly by `id` -- exactly the same key
    AssetCheckout.outsider_id / Quotation.assigned_outsider_id already
    use to reference this table, and the one thing genuinely guaranteed
    stable for a row that continues to exist.

    Same three cases as the credentials function:
      1. DUPLICATES (id present in both): the pre-restore (current)
         profile wins for every mutable column -- e.g. an Admin editing
         an ad-hoc profile's phone number or soft-deleting it since the
         backup was taken must not silently un-happen.
      2. MISSING FROM RESTORE (existed pre-restore, no row in the
         restored backup): re-inserted wholesale, preserving the
         original id when free -- this is the ad-hoc-profile equivalent
         of "a user invited after the backup was taken": an ad-hoc
         individual added after the backup (e.g. to receive a dispatch)
         would otherwise vanish, silently orphaning any checkout/
         quotation of theirs this same restore is also trying to
         preserve (see _reconcile_post_restore_asset_activity() below,
         which depends on this function's `preserved_outsider_ids`
         running first).
      3. RESTORE-ONLY (present only in the restored backup): left
         completely untouched.

    No credential/MFA analog here -- Outsider rows never have a
    password or 2FA state to protect.
    """
    from sqlalchemy import text as sa_text

    if not pre_restore_outsiders:
        return {"outsiders_reconciled": 0, "outsiders_reinserted": 0, "preserved_outsider_ids": []}

    PROFILE_COLUMNS = ["name", "email", "phone_number", "company", "is_deleted", "deleted_at", "converted_to_user_id"]

    by_id = {o["id"]: o for o in pre_restore_outsiders if o.get("id") is not None}

    outsiders_reconciled = 0
    outsiders_reinserted = 0
    preserved_outsider_ids = set()

    with engine.begin() as conn:
        restored_ids = {
            row["id"] for row in conn.execute(sa_text("SELECT id FROM outsiders")).mappings().all()
        }

        # --- Case 1: duplicates -- matched by id, pre-restore profile wins ---
        for outsider_id, snapshot in by_id.items():
            if outsider_id not in restored_ids:
                continue  # case 2, handled below
            present_cols = [c for c in PROFILE_COLUMNS if c in snapshot]
            set_clause = ", ".join(f"{col} = :{col}" for col in present_cols)
            params = {col: snapshot[col] for col in present_cols}
            params["oid"] = outsider_id
            conn.execute(sa_text(f"UPDATE outsiders SET {set_clause} WHERE id = :oid"), params)
            outsiders_reconciled += 1
            preserved_outsider_ids.add(outsider_id)

        # --- Case 2: missing-from-restore -- re-inserted, id preserved when free ---
        for outsider_id, snapshot in by_id.items():
            if outsider_id in restored_ids:
                continue  # matched above
            present_cols = [c for c in PROFILE_COLUMNS if c in snapshot]
            insert_cols = list(present_cols)
            params = {c: snapshot.get(c) for c in present_cols}
            if outsider_id not in restored_ids:
                insert_cols = ["id"] + insert_cols
                params["id"] = outsider_id
            columns_sql = ", ".join(insert_cols)
            values_sql = ", ".join(f":{c}" for c in insert_cols)
            new_row = conn.execute(
                sa_text(f"INSERT INTO outsiders ({columns_sql}) VALUES ({values_sql}) RETURNING id"),
                params,
            ).mappings().first()
            new_id = new_row["id"]
            restored_ids.add(new_id)
            outsiders_reinserted += 1
            preserved_outsider_ids.add(new_id)

        if outsiders_reinserted:
            conn.execute(sa_text(
                "SELECT setval(pg_get_serial_sequence('outsiders', 'id'), "
                "COALESCE((SELECT MAX(id) FROM outsiders), 1))"
            ))

        if outsiders_reconciled or outsiders_reinserted:
            conn.execute(
                sa_text(
                    "INSERT INTO audit_logs (operator, action, target_type, target_id, details, timestamp) "
                    "VALUES (:operator, :action, :target_type, :target_id, :details, NOW())"
                ),
                {
                    "operator": "system:restore", "action": "RESTORE_OUTSIDER_RECONCILIATION",
                    "target_type": "Outsider", "target_id": 0,
                    "details": (
                        f"Restore reconciled {outsiders_reconciled} ad-hoc profile(s) to their "
                        f"pre-restore (current) values and re-inserted {outsiders_reinserted} "
                        f"profile(s) that existed before the restore but were absent from the "
                        f"restored backup."
                    ),
                },
            )

    return {
        "outsiders_reconciled": outsiders_reconciled,
        "outsiders_reinserted": outsiders_reinserted,
        "preserved_outsider_ids": sorted(preserved_outsider_ids),
    }


def _reconcile_post_restore_asset_activity(
    engine,
    pre_restore_checkouts: list,
    pre_restore_quotations: list,
    pre_restore_quotation_items: list,
    pre_restore_quotation_outsourced_items: list,
    preserved_user_ids: list,
    preserved_outsider_ids: list,
) -> dict:
    """
    Extends the same "current reality wins for accounts we're already
    guaranteeing continuity for" principle from
    _reconcile_post_restore_credentials()/_reconcile_post_restore_outsiders()
    to the actual TRANSACTIONAL records those accounts own: checkouts and
    quotations. Without this, a restore could correctly preserve a
    person's login/profile via the two functions above, while quietly
    reverting every item they'd checked out (or every quote they'd
    submitted/had approved) since the backup was taken -- e.g. a return
    they'd already made would un-happen, showing the item still "out" and
    blocking a genuinely available unit from being dispatched to someone
    else.

    SCOPE, DELIBERATELY: this only reconciles checkouts/quotations that
    belong to a PRESERVED account (`preserved_user_ids` /
    `preserved_outsider_ids`, as returned by the two functions above).
    Activity belonging to a backup-only account (one that was deleted
    before this restore, or genuinely doesn't exist post-restore) is left
    exactly as the restored backup has it -- there's no "current" version
    of it to prefer, so touching it would just be re-implementing a
    diff/merge tool for data this restore was explicitly asked to roll
    back. This mirrors case 3 ("restore-only") of the account-level
    functions exactly.

    REFERENTIAL INTEGRITY IS NON-NEGOTIABLE: unlike a `users` row (which
    only ever points at itself), a checkout/quotation points AT other
    rows -- an AssetType pool, a Quotation, another User. This function
    NEVER creates a dangling foreign key to satisfy "preserve current
    data": a checkout/quotation-item whose referenced AssetType no longer
    exists post-restore (e.g. the asset itself was also deleted, or the
    restored backup simply predates it) is skipped, not force-inserted --
    logged individually and rolled up into this function's audit-log row
    so an Admin can see exactly what could NOT be automatically carried
    forward and re-create it by hand if it still matters. The same
    inventory-integrity bar applies to `quotation_id` on a checkout:
    quotations are reconciled FIRST (see the ordering below) specifically
    so a checkout's `quotation_id` has already been re-inserted and
    resolves by the time checkouts are processed.

    STOCK CONSISTENCY: AssetType.available_quantity is a cached,
    denormalized count (see services/stock.py's own module docstring --
    "Available = Total Capacity - Outbound - Isolated") that a restored
    backup's dump value can no longer be trusted for the moment this
    function re-inserts or reconciles a checkout it didn't originally
    account for. Every asset touched below has its stock recalculated
    from scratch via the exact same recalculate_asset_stock() every
    other checkout/return/isolate code path in this app already uses,
    once, after all checkout changes are applied -- never left to drift.

    WHAT COUNTS AS "MUTABLE" (case 1 duplicates): only fields that
    legitimately change AFTER creation as part of normal use --
    checkout.status/quantity_returned/returned_at/due_date, and
    quotation.status/notes/discount_percent/assigned_to_id/
    assigned_outsider_id/approved_at/approved_by_id/fulfilled_at/
    fulfilled_by_id. Creation-time identity fields (asset_id, user_id,
    outsider_id, the ORIGINAL quantity checked out, quotation.user_id,
    reference_number, created_at) are never touched on a matched row --
    exactly the same "id/identity never moves, only current STATE does"
    rule _reconcile_post_restore_credentials() already applies to a
    matched user's row.

    A Quotation's line items (QuotationItem / QuotationOutsourcedItem)
    are treated as owned wholesale by their parent: on a matched
    (case 1) quotation, its current pre-restore item set REPLACES
    whatever the restored backup's items were (delete + re-insert) rather
    than being diffed line-by-line -- a quote's cart contents are edited
    as a whole document in the UI, not merged field-by-field, so a whole-
    document replace is both simpler and matches how the app itself
    treats them.

    NOT COVERED (documented, not silently ignored): ExtensionRequest
    rows (due-date extension requests tied to a checkout) are NOT
    reconciled by this function -- they're a comparatively low-stakes,
    short-lived workflow (see models.ExtensionRequest), and chaining a
    third level of "reinsert if its parent was also reinserted" here
    would meaningfully increase this function's complexity/blast radius
    for a workflow that's rarely still open by the time anyone restores
    an old backup. A pre-restore extension request missing after a
    restore is a known, accepted gap -- re-request it if it's still
    needed. AssetType/AssetException data is also out of scope entirely
    (this function only ever READS asset_types, to check referential
    integrity -- it never creates, deletes, or edits a catalog row or an
    isolation record).
    """
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import Session
    import services.stock as stock_service
    import models

    result = {
        "checkouts_reconciled": 0, "checkouts_reinserted": 0, "checkouts_skipped": 0,
        "quotations_reconciled": 0, "quotations_reinserted": 0, "quotations_skipped": 0,
        "skipped_details": [],
    }

    if not (pre_restore_checkouts or pre_restore_quotations):
        return result

    preserved_user_ids = set(preserved_user_ids or [])
    preserved_outsider_ids = set(preserved_outsider_ids or [])

    CHECKOUT_ALL_COLUMNS = [
        "asset_id", "user_id", "outsider_id", "quotation_id", "quantity", "quantity_returned",
        "checkout_date", "due_date", "returned_at", "status", "is_outsourced",
        "outsourced_item_name", "outsourced_unit_price", "outsourced_source",
    ]
    CHECKOUT_MUTABLE_COLUMNS = ["quantity_returned", "returned_at", "status", "due_date"]

    QUOTATION_ALL_COLUMNS = [
        "user_id", "created_at", "updated_at", "status", "reference_number", "submitted_at",
        "assigned_to_id", "assigned_outsider_id", "notes", "approved_at", "approved_by_id",
        "fulfilled_at", "fulfilled_by_id", "discount_percent",
    ]
    QUOTATION_MUTABLE_COLUMNS = [
        "status", "notes", "assigned_to_id", "assigned_outsider_id", "approved_at",
        "approved_by_id", "fulfilled_at", "fulfilled_by_id", "discount_percent", "updated_at",
    ]

    QUOTATION_ITEM_COLUMNS = ["quotation_id", "asset_id", "quantity", "start_date", "due_date", "added_at"]
    QUOTATION_OUTSOURCED_ITEM_COLUMNS = [
        "quotation_id", "name", "description", "unit_price", "quantity", "sourced_from",
        "start_date", "due_date", "added_by_id", "added_at",
    ]

    touched_asset_ids = set()

    def _owner_preserved(user_id, outsider_id):
        return (user_id is not None and user_id in preserved_user_ids) or (
            outsider_id is not None and outsider_id in preserved_outsider_ids
        )

    with engine.begin() as conn:
        restored_asset_ids = {
            r["id"] for r in conn.execute(sa_text("SELECT id FROM asset_types")).mappings().all()
        }
        restored_user_ids = {
            r["id"] for r in conn.execute(sa_text("SELECT id FROM users")).mappings().all()
        }

        # ---------------------------------------------------------------
        # QUOTATIONS FIRST -- so any checkout re-inserted below whose
        # quotation_id pointed at a since-missing quote already has that
        # quote back in place by the time it's checked.
        # ---------------------------------------------------------------
        restored_quotation_ids = {
            r["id"] for r in conn.execute(sa_text("SELECT id FROM quotations")).mappings().all()
        }
        items_by_quotation = {}
        for item in pre_restore_quotation_items:
            items_by_quotation.setdefault(item["quotation_id"], []).append(item)
        outsourced_by_quotation = {}
        for item in pre_restore_quotation_outsourced_items:
            outsourced_by_quotation.setdefault(item["quotation_id"], []).append(item)

        def _reinsert_quotation_items(quotation_id, original_quotation_id):
            for item in items_by_quotation.get(original_quotation_id, []):
                if item.get("asset_id") not in restored_asset_ids:
                    result["skipped_details"].append(
                        f"quotation_item for quotation #{quotation_id}: referenced asset "
                        f"#{item.get('asset_id')} no longer exists post-restore -- skipped."
                    )
                    continue
                params = {c: item.get(c) for c in QUOTATION_ITEM_COLUMNS}
                params["quotation_id"] = quotation_id
                cols = ", ".join(QUOTATION_ITEM_COLUMNS)
                vals = ", ".join(f":{c}" for c in QUOTATION_ITEM_COLUMNS)
                conn.execute(sa_text(f"INSERT INTO quotation_items ({cols}) VALUES ({vals})"), params)
            for item in outsourced_by_quotation.get(original_quotation_id, []):
                params = {c: item.get(c) for c in QUOTATION_OUTSOURCED_ITEM_COLUMNS}
                params["quotation_id"] = quotation_id
                if params.get("added_by_id") not in restored_user_ids:
                    params["added_by_id"] = None  # see Quotation.approved_by_id-style FK caveat
                cols = ", ".join(QUOTATION_OUTSOURCED_ITEM_COLUMNS)
                vals = ", ".join(f":{c}" for c in QUOTATION_OUTSOURCED_ITEM_COLUMNS)
                conn.execute(sa_text(f"INSERT INTO quotation_outsourced_items ({cols}) VALUES ({vals})"), params)

        for snapshot in pre_restore_quotations:
            quotation_id = snapshot["id"]
            if not _owner_preserved(snapshot.get("user_id"), None):
                continue  # not our concern -- owner isn't a preserved account (case 3 equivalent)

            if quotation_id in restored_quotation_ids:
                # Case 1: duplicate -- pre-restore (current) state wins for
                # mutable fields; FK targets that no longer resolve are
                # nulled rather than left dangling or force-referenced.
                params = {c: snapshot.get(c) for c in QUOTATION_MUTABLE_COLUMNS}
                if params.get("assigned_to_id") not in restored_user_ids:
                    params["assigned_to_id"] = None
                if params.get("approved_by_id") not in restored_user_ids:
                    params["approved_by_id"] = None
                if params.get("fulfilled_by_id") not in restored_user_ids:
                    params["fulfilled_by_id"] = None
                params["assigned_outsider_id"] = (
                    params.get("assigned_outsider_id")
                    if params.get("assigned_outsider_id") in preserved_outsider_ids
                    or params.get("assigned_outsider_id") is None
                    else None
                )
                set_clause = ", ".join(f"{c} = :{c}" for c in QUOTATION_MUTABLE_COLUMNS)
                params["qid"] = quotation_id
                conn.execute(sa_text(f"UPDATE quotations SET {set_clause} WHERE id = :qid"), params)
                # Whole-document replace of this quote's items (see docstring).
                conn.execute(sa_text("DELETE FROM quotation_items WHERE quotation_id = :qid"), {"qid": quotation_id})
                conn.execute(sa_text("DELETE FROM quotation_outsourced_items WHERE quotation_id = :qid"), {"qid": quotation_id})
                _reinsert_quotation_items(quotation_id, quotation_id)
                result["quotations_reconciled"] += 1
            else:
                # Case 2: missing from restore -- re-insert wholesale.
                params = {c: snapshot.get(c) for c in QUOTATION_ALL_COLUMNS}
                if params.get("user_id") not in restored_user_ids:
                    result["quotations_skipped"] += 1
                    result["skipped_details"].append(
                        f"quotation #{quotation_id}: owning user no longer resolves post-restore -- skipped."
                    )
                    continue
                if params.get("assigned_to_id") not in restored_user_ids:
                    params["assigned_to_id"] = None
                if params.get("approved_by_id") not in restored_user_ids:
                    params["approved_by_id"] = None
                if params.get("fulfilled_by_id") not in restored_user_ids:
                    params["fulfilled_by_id"] = None
                if params.get("assigned_outsider_id") not in preserved_outsider_ids:
                    params["assigned_outsider_id"] = None
                insert_cols = list(QUOTATION_ALL_COLUMNS)
                if quotation_id not in restored_quotation_ids:
                    insert_cols = ["id"] + insert_cols
                    params["id"] = quotation_id
                cols = ", ".join(insert_cols)
                vals = ", ".join(f":{c}" for c in insert_cols)
                new_row = conn.execute(
                    sa_text(f"INSERT INTO quotations ({cols}) VALUES ({vals}) RETURNING id"),
                    params,
                ).mappings().first()
                new_quotation_id = new_row["id"]
                restored_quotation_ids.add(new_quotation_id)
                _reinsert_quotation_items(new_quotation_id, quotation_id)
                result["quotations_reinserted"] += 1

        if result["quotations_reinserted"]:
            conn.execute(sa_text(
                "SELECT setval(pg_get_serial_sequence('quotations', 'id'), "
                "COALESCE((SELECT MAX(id) FROM quotations), 1))"
            ))

        # ---------------------------------------------------------------
        # CHECKOUTS -- quotations (and their new ids, if any) above are
        # now in place, so quotation_id references below can be trusted.
        # ---------------------------------------------------------------
        restored_checkout_ids = {
            r["id"] for r in conn.execute(sa_text("SELECT id FROM asset_checkouts")).mappings().all()
        }
        for snapshot in pre_restore_checkouts:
            checkout_id = snapshot["id"]
            if not _owner_preserved(snapshot.get("user_id"), snapshot.get("outsider_id")):
                continue  # case 3 equivalent -- not a preserved account's activity

            if checkout_id in restored_checkout_ids:
                # Case 1: duplicate -- pre-restore (current) state wins for
                # mutable fields only; asset_id/user_id/outsider_id/
                # quotation_id/original quantity are never touched.
                params = {c: snapshot.get(c) for c in CHECKOUT_MUTABLE_COLUMNS}
                set_clause = ", ".join(f"{c} = :{c}" for c in CHECKOUT_MUTABLE_COLUMNS)
                params["cid"] = checkout_id
                conn.execute(sa_text(f"UPDATE asset_checkouts SET {set_clause} WHERE id = :cid"), params)
                result["checkouts_reconciled"] += 1
                if snapshot.get("asset_id") is not None:
                    touched_asset_ids.add(snapshot["asset_id"])
            else:
                # Case 2: missing from restore -- re-insert only if every
                # FK it needs actually resolves post-restore; otherwise
                # skip and report rather than leave a dangling reference.
                asset_id = snapshot.get("asset_id")
                quotation_id = snapshot.get("quotation_id")
                if asset_id is not None and asset_id not in restored_asset_ids:
                    result["checkouts_skipped"] += 1
                    result["skipped_details"].append(
                        f"checkout #{checkout_id}: referenced asset #{asset_id} no longer "
                        f"exists post-restore -- skipped (could not safely re-insert)."
                    )
                    continue
                if quotation_id is not None and quotation_id not in restored_quotation_ids:
                    result["checkouts_skipped"] += 1
                    result["skipped_details"].append(
                        f"checkout #{checkout_id}: referenced quotation #{quotation_id} no "
                        f"longer exists post-restore -- skipped (could not safely re-insert)."
                    )
                    continue

                params = {c: snapshot.get(c) for c in CHECKOUT_ALL_COLUMNS}
                insert_cols = list(CHECKOUT_ALL_COLUMNS)
                if checkout_id not in restored_checkout_ids:
                    insert_cols = ["id"] + insert_cols
                    params["id"] = checkout_id
                cols = ", ".join(insert_cols)
                vals = ", ".join(f":{c}" for c in insert_cols)
                new_row = conn.execute(
                    sa_text(f"INSERT INTO asset_checkouts ({cols}) VALUES ({vals}) RETURNING id"),
                    params,
                ).mappings().first()
                restored_checkout_ids.add(new_row["id"])
                result["checkouts_reinserted"] += 1
                if asset_id is not None:
                    touched_asset_ids.add(asset_id)

        if result["checkouts_reinserted"]:
            conn.execute(sa_text(
                "SELECT setval(pg_get_serial_sequence('asset_checkouts', 'id'), "
                "COALESCE((SELECT MAX(id) FROM asset_checkouts), 1))"
            ))

        # ---------------------------------------------------------------
        # STOCK CONSISTENCY -- recompute every touched asset's cached
        # available_quantity from scratch (see this function's own
        # docstring on why the restored dump's value can no longer be
        # trusted for these), via the exact same helper every other
        # checkout/return code path in this app already relies on.
        # ---------------------------------------------------------------
        if touched_asset_ids:
            session = Session(bind=conn)
            for asset_type in session.query(models.AssetType).filter(
                models.AssetType.id.in_(touched_asset_ids)
            ).all():
                stock_service.recalculate_asset_stock(session, asset_type)
            session.flush()

        total_reconciled = result["checkouts_reconciled"] + result["quotations_reconciled"]
        total_reinserted = result["checkouts_reinserted"] + result["quotations_reinserted"]
        total_skipped = result["checkouts_skipped"] + result["quotations_skipped"]
        if total_reconciled or total_reinserted or total_skipped:
            detail = (
                f"Restore reconciled {result['checkouts_reconciled']} checkout(s) and "
                f"{result['quotations_reconciled']} quotation(s) to their pre-restore (current) "
                f"state, re-inserted {result['checkouts_reinserted']} checkout(s) and "
                f"{result['quotations_reinserted']} quotation(s) that existed before the restore "
                f"but were absent from the restored backup, and recalculated stock for "
                f"{len(touched_asset_ids)} affected asset(s)."
            )
            if total_skipped:
                detail += (
                    f" {total_skipped} record(s) could NOT be safely re-inserted (a referenced "
                    f"asset/quotation/account no longer exists post-restore) and were skipped -- "
                    f"see this same audit entry's details for the individual reasons."
                )
            conn.execute(
                sa_text(
                    "INSERT INTO audit_logs (operator, action, target_type, target_id, details, timestamp) "
                    "VALUES (:operator, :action, :target_type, :target_id, :details, NOW())"
                ),
                {
                    "operator": "system:restore", "action": "RESTORE_ASSET_ACTIVITY_RECONCILIATION",
                    "target_type": "AssetCheckout", "target_id": 0,
                    "details": detail + (
                        (" Skipped: " + " | ".join(result["skipped_details"])) if result["skipped_details"] else ""
                    ),
                },
            )

    return result
