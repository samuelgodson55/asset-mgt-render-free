# Super Admin Database & Backup Guide

## 1. Database command center

This page is read-only operational telemetry. It does not change pool sizes or database settings.

### PostgreSQL connections
`current / max` is the number of PostgreSQL backend sessions currently connected to the database versus the server's connection ceiling.

- A low percentage is healthy.
- Sustained high usage is a capacity warning.
- This number includes connections that may come from migrations, background workers, direct maintenance sessions, and PgBouncer's server side.

### PgBouncer waiting
The number of application clients waiting for a real PostgreSQL server connection.

- `0` is the normal target.
- A sustained value above `0` means the pooler is queueing clients.
- Increasing application pool sizes is **not** the first response; first confirm the server pool and PostgreSQL capacity are actually saturated.

### API checked out
How many SQLAlchemy connections this API process currently has checked out from its own local pool.

This is a separate layer from PostgreSQL connections. PgBouncer can accept many client connections while the API process deliberately keeps only a small number of local SQLAlchemy connections.

A value equal to the pool size is not automatically an outage. It means this process is using all of its currently allocated local slots.

### Idle in transaction
PostgreSQL sessions that started a transaction but are currently idle instead of committing or rolling back.

- A brief value of `1` can be caused by a normal API request and should be watched, not panicked over.
- A value that stays high or grows can indicate code holding transactions open too long.

### Application route
Shows whether the running API is actually using PgBouncer and which endpoint it is pointed at.

`PgBouncer in use` is the important routing signal.

The **PgBouncer admin probe** is separate telemetry. If it says `No live probe` while the application route says `PgBouncer in use` and the site is working, the application route is not automatically broken. It means the PgBouncer management/SHOW interface could not be queried.

### SQLAlchemy pool
- **Pool size:** normal reusable connections allocated to this process.
- **Checked out:** connections currently borrowed by requests/tasks.
- **Checked in:** idle connections ready for reuse.
- **Overflow:** temporary connections above the configured base pool. Sustained overflow means the local pool is under pressure.

Do not increase this blindly. The safe value is constrained by PgBouncer's server pool, the number of backend processes/replicas, background workers, and PostgreSQL's own connection ceiling.

### PgBouncer telemetry
When the admin probe is available, these values describe the pooler's own view:

- **Active clients:** clients connected to PgBouncer.
- **Waiting:** clients queued for a server connection.
- **Server active:** PostgreSQL connections currently executing work.
- **Server idle:** PostgreSQL connections held by PgBouncer and ready for reuse.
- **Avg query:** average query execution time reported by PgBouncer.
- **Avg client wait:** average time clients waited for a server connection.
- **Pool mode:** normally transaction mode for this application.
- **Default / reserve pool:** the normal server pool plus its emergency reserve.

### Configured guardrails
These are policy limits used by the application when calculating admission and SQLAlchemy pool sizes:

- **PgBouncer server pool:** maximum intended server-side pool budget.
- **Safety margin:** percentage deliberately left unused.
- **Background reserve:** connections held back for Celery/maintenance work.
- **Background concurrency:** maximum concurrent background DB work admitted by the application.
- **DB safety margin:** PostgreSQL connections reserved outside the application budget.

Treat these as guardrails, not targets to max out.

## 2. System Backups

### Backup Now
Creates a full PostgreSQL dump. It is a good pre-change checkpoint before migrations, infrastructure changes, or other risky maintenance.

### Daily schedule
Shows when automatic backups run. The displayed time is converted to the configured display timezone.

### Google Drive sync
A durable copy is strongly recommended for Render Free/ephemeral deployments. Local backup files can disappear during a redeploy or spin-down.

### Local backups
These are convenient recovery copies on the running service's local disk. They are not the durable backup strategy on ephemeral infrastructure.

### Download
Downloads a local `.sql.gz` backup so it can be stored outside the running service.

### Delete
Deletes only the local copy. It does not delete an already-uploaded Google Drive copy.

## 3. Safe database restore

A restore is a destructive maintenance operation, but the implementation now follows these rules:

1. The restore lock prevents two restores from running at the same time.
2. A fresh **pre-restore safety backup must succeed** before any destructive database command is allowed.
3. Pre-restore users, outsiders, checkout/quotation activity, and audit history are snapshotted through a dedicated direct PostgreSQL connection, not the normal SQLAlchemy request pool.
4. This matters when the API pool is very small (for example, pool size `1`): the authenticated restore request may already be holding the only normal pool connection.
5. The database schema is reset and the selected dump is loaded with `ON_ERROR_STOP=1`, so a real SQL failure cannot silently look like a successful restore.
6. Older backups are reconciled to the current Alembic head.
7. The current Super Admin account remains authoritative; its current password/profile is preserved and MFA is intentionally reset for fresh enrollment.
8. Pre-restore audit entries are merged back after the restore so audit history is not silently erased.
9. Existing sessions are invalidated and everyone is required to sign in again.

### Restore from File
Use this when the desired backup is no longer on local disk, for example after an ephemeral-service redeploy. Upload the `.sql.gz` file you downloaded from durable storage.

### If restore fails before the destructive step
That is intentional. The safest failure is a restore that refuses to start because it cannot create the safety checkpoint or preserve required continuity data.

### If the UI reports an audit snapshot error
Do **not** keep clicking Restore repeatedly. Check the latest restore status/logs. The implementation should now use the direct database connection and avoid the pool-size-1 self-starvation that caused the failure shown in the previous deployment.

## 4. What to watch in normal operation

A healthy low-traffic deployment can look roughly like this:

- PostgreSQL connections well below max.
- PgBouncer waiting = `0`.
- SQLAlchemy checked out occasionally reaching its pool size during requests, then falling back.
- Overflow = `0` most of the time.
- Idle-in-transaction = `0` most of the time, with brief single-session spikes acceptable.
- Application route = PgBouncer in use.
- PgBouncer admin telemetry may be unavailable on managed/locked-down pooler deployments; that alone is not proof of an application outage.

When diagnosing an outage, distinguish **routing**, **pool pressure**, and **PostgreSQL capacity**. They are three different layers.
