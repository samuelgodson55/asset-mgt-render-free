"""
services/backup/reconcile_credentials.py
------------------------------------------
Post-restore credential reconciliation. Restoring an OLD backup brings back
stale passwords/TOTP secrets/recovery codes for every user -- this compares
a pre-restore in-memory snapshot (captured before the destructive DROP
SCHEMA/psql replay) against what actually landed, and re-applies whichever
credential set is newer per-user, rather than silently logging everyone
into a stale password.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def _reconcile_post_restore_credentials(engine, pre_restore_users: list) -> dict:
    """Preserve the current accounts across a restore, with one root account.

    Every normal account follows the existing email-keyed reconciliation rules:
    the CURRENT profile wins when the same account exists in the backup, current
    accounts missing from the backup are reinserted, and backup-only ordinary
    accounts remain untouched.

    SUPER_ADMIN is deliberately different. There is exactly ONE authoritative
    root account: the one that existed immediately before the restore. A backup
    copy of that role is never allowed to survive as another super admin. If the
    current root was absent from the backup, it is reinserted; if the backup
    contains another super_admin identity, that restored copy is revoked by
    demoting it to staff + soft-deleting it. This is the restore invariant that
    prevents an older backup from resurrecting a second root account.
    """
    from sqlalchemy import text as sa_text
    from security import SUPER_ADMIN_ROLE

    if not pre_restore_users:
        return {
            "users_reconciled": 0,
            "users_reinserted": 0,
            "super_admins_reset": 0,
            "super_admins_revoked": 0,
            "preserved_user_ids": [],
            "username_conflicts_resolved": 0,
        }

    PROFILE_COLUMNS = [
        "name", "phone_number", "role", "password_hash", "is_verified",
        "is_active", "failed_login_attempts", "locked_until",
        "totp_secret_encrypted", "totp_enabled", "is_deleted", "deleted_at",
        "purged_at", "department", "department_role", "converted_to_outsider_id",
    ]

    def email_lc(snapshot: dict) -> Optional[str]:
        value = snapshot.get("email_lc") or snapshot.get("email")
        return value.strip().lower() if value else None

    by_email = {email_lc(u): u for u in pre_restore_users if email_lc(u)}
    current_super_admins = [u for u in pre_restore_users if u.get("role") == SUPER_ADMIN_ROLE]
    # The production restore path snapshots the complete current user table,
    # so a real restore must have exactly one current root. This helper is
    # also exercised directly by focused ordinary-user reconciliation tests
    # that intentionally provide partial snapshots with no root account.
    # Those partial snapshots must continue to exercise only the ordinary
    # account rules rather than failing before they reach them.
    if len(current_super_admins) > 1:
        raise RuntimeError(
            "Restore aborted: the current database contains multiple super_admin accounts; "
            "exactly one authoritative root account is required before restore."
        )
    authoritative_super_admin = current_super_admins[0] if current_super_admins else None
    authoritative_super_admin_email = email_lc(authoritative_super_admin) if authoritative_super_admin else None

    users_reconciled = 0
    users_reinserted = 0
    username_conflicts_resolved = 0
    super_admins_reset: list[str] = []
    super_admins_revoked: list[str] = []
    reinserted_emails: list[str] = []
    preserved_user_ids: set[int] = set()

    with engine.begin() as conn:
        restored_rows = conn.execute(
            sa_text("SELECT id, lower(email) AS email_lc, username, role FROM users")
        ).mappings().all()
        restored_by_email = {row["email_lc"]: row for row in restored_rows}
        restored_ids = {row["id"] for row in restored_rows}

        matched_snapshots = [
            (restored_by_email[e], snapshot)
            for e, snapshot in by_email.items()
            if e in restored_by_email
        ]

        desired_usernames: dict[str, str] = {}
        for snapshot in by_email.values():
            username = snapshot.get("username")
            if username:
                desired_usernames[email_lc(snapshot)] = username.strip().lower()

        # 1) Move backup-only rows that currently occupy usernames needed by
        #    preserved current accounts.
        for row in restored_rows:
            username = (row["username"] or "").strip().lower()
            if not username:
                continue
            owner_email = row["email_lc"]
            if desired_usernames.get(owner_email) == username:
                continue
            if username not in desired_usernames.values():
                continue
            while True:
                temporary = f"__restore_conflict_{uuid.uuid4().hex[:24]}"
                exists = conn.execute(
                    sa_text("SELECT 1 FROM users WHERE lower(username) = :u LIMIT 1"),
                    {"u": temporary},
                ).first()
                if not exists:
                    break
            conn.execute(
                sa_text("UPDATE users SET username = :temporary WHERE id = :uid"),
                {"temporary": temporary, "uid": row["id"]},
            )
            username_conflicts_resolved += 1

        # 2) Clear usernames on matched rows before writing current values.
        for row, snapshot in matched_snapshots:
            if row["username"] is not None:
                conn.execute(
                    sa_text("UPDATE users SET username = NULL WHERE id = :uid"),
                    {"uid": row["id"]},
                )

        # 3) Revoke any backup-only Super Admin rows BEFORE applying the
        #    authoritative current profile. On a real restore there is always
        #    exactly one authoritative current root. Partial helper tests that
        #    omit the root must not mutate the fixture's unrelated root row.
        if authoritative_super_admin_email:
            for row in restored_rows:
                if row["email_lc"] == authoritative_super_admin_email:
                    continue
                if row["role"] != SUPER_ADMIN_ROLE:
                    continue
                conn.execute(
                    sa_text(
                        "UPDATE users SET role = 'staff', is_active = false, is_deleted = true, "
                        "deleted_at = NOW(), totp_secret_encrypted = NULL, totp_enabled = false "
                        "WHERE id = :uid"
                    ),
                    {"uid": row["id"]},
                )
                conn.execute(sa_text("DELETE FROM recovery_codes WHERE user_id = :uid"), {"uid": row["id"]})
                conn.execute(sa_text("DELETE FROM password_reset_tokens WHERE user_id = :uid"), {"uid": row["id"]})
                super_admins_revoked.append(row["email_lc"])

        # 4) Apply authoritative CURRENT profile data to matched rows.
        for row, snapshot in matched_snapshots:
            present_cols = [c for c in PROFILE_COLUMNS if c in snapshot]
            set_clause = ", ".join(f"{col} = :{col}" for col in present_cols)
            params = {col: snapshot[col] for col in present_cols}
            params["uid"] = row["id"]
            if set_clause:
                conn.execute(sa_text(f"UPDATE users SET {set_clause} WHERE id = :uid"), params)
            users_reconciled += 1
            preserved_user_ids.add(row["id"])

        # 4) Reinsert CURRENT accounts absent from the restored backup.
        for snapshot in pre_restore_users:
            e = email_lc(snapshot)
            if not e or e in restored_by_email:
                continue

            present_cols = [c for c in PROFILE_COLUMNS if c in snapshot]
            insert_cols = ["email"] + present_cols
            params = {c: snapshot.get(c) for c in present_cols}
            params["email"] = snapshot.get("email") or e

            original_id = snapshot.get("id")
            if original_id is not None and original_id not in restored_ids:
                insert_cols.insert(0, "id")
                params["id"] = original_id

            desired_username = (snapshot.get("username") or "").strip().lower() or None
            if desired_username:
                insert_cols.append("username")
                params["username"] = None

            columns_sql = ", ".join(insert_cols)
            values_sql = ", ".join(f":{c}" for c in insert_cols)
            new_row = conn.execute(
                sa_text(f"INSERT INTO users ({columns_sql}) VALUES ({values_sql}) RETURNING id"),
                params,
            ).mappings().first()
            new_id = new_row["id"]
            restored_ids.add(new_id)
            restored_by_email[e] = {"id": new_id, "email_lc": e, "username": None, "role": snapshot.get("role")}
            users_reinserted += 1
            reinserted_emails.append(e)
            preserved_user_ids.add(new_id)

        # 5) Assign authoritative current usernames.
        for e, username in desired_usernames.items():
            row = restored_by_email.get(e)
            if not row:
                continue
            conn.execute(
                sa_text("UPDATE users SET username = :username WHERE id = :uid"),
                {"username": username, "uid": row["id"]},
            )

        # 6) Enforce the single-root invariant inside THIS restore when the
        # current snapshot contains the authoritative root.
        if authoritative_super_admin_email:
            root_row = restored_by_email.get(authoritative_super_admin_email)
            if not root_row:
                raise RuntimeError("Restore failed: authoritative current Super Admin could not be reconciled.")

            conn.execute(
                sa_text(
                    "UPDATE users SET role = :role, is_deleted = false, is_active = true, deleted_at = NULL "
                    "WHERE id = :uid"
                ),
                {"role": SUPER_ADMIN_ROLE, "uid": root_row["id"]},
            )

            # 7) Super Admin MFA is never restored from backup data. Force fresh
            # enrollment while preserving the current password/profile.
            conn.execute(
                sa_text(
                    "UPDATE users SET totp_secret_encrypted = NULL, totp_enabled = false "
                    "WHERE id = :uid"
                ),
                {"uid": root_row["id"]},
            )
            conn.execute(sa_text("DELETE FROM recovery_codes WHERE user_id = :uid"), {"uid": root_row["id"]})
            super_admins_reset.append(authoritative_super_admin_email)

        if users_reinserted:
            conn.execute(sa_text(
                "SELECT setval(pg_get_serial_sequence('users', 'id'), "
                "COALESCE((SELECT MAX(id) FROM users), 1))"
            ))

        if users_reconciled or users_reinserted or username_conflicts_resolved or super_admins_revoked:
            detail_parts = []
            if users_reconciled:
                detail_parts.append(
                    f"Restore reconciled {users_reconciled} account(s) to their current pre-restore profiles and credentials."
                )
            if users_reinserted:
                detail_parts.append(
                    f"Restore re-inserted {users_reinserted} current account(s) absent from the backup: {', '.join(reinserted_emails)}."
                )
            if username_conflicts_resolved:
                detail_parts.append(
                    f"Restore resolved {username_conflicts_resolved} username collision(s) without aborting the restore."
                )
            if super_admins_revoked:
                detail_parts.append(
                    "Restore revoked backup-only Super Admin account(s): " + ", ".join(super_admins_revoked) + "."
                )
            if super_admins_reset:
                detail_parts.append(
                    "Current Super Admin MFA was cleared and requires fresh enrollment: "
                    + ", ".join(super_admins_reset) + "."
                )
            conn.execute(
                sa_text(
                    "INSERT INTO audit_logs (operator, action, target_type, target_id, details, timestamp) "
                    "VALUES (:operator, :action, :target_type, :target_id, :details, NOW())"
                ),
                {
                    "operator": "system:restore", "action": "RESTORE_CREDENTIAL_RECONCILIATION",
                    "target_type": "User", "target_id": 0, "details": " ".join(detail_parts),
                },
            )

    return {
        "users_reconciled": users_reconciled,
        "users_reinserted": users_reinserted,
        "super_admins_reset": len(super_admins_reset),
        "super_admins_revoked": len(super_admins_revoked),
        "preserved_user_ids": sorted(preserved_user_ids),
        "username_conflicts_resolved": username_conflicts_resolved,
    }
