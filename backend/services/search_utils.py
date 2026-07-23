"""
services/search_utils.py
--------------------------
Tiny shared helper for the free-text `search` parameter now accepted by
GET /assets, GET /users, and GET /outsiders (Asset/User/Outsider
directories) -- the same "true server-side search + pagination" pattern
the audit ledger already used (see services/audit_service.py / limit+offset
there), extended to these three listing endpoints so a large directory
never has to be pulled entirely into the browser just to let someone type
into a search box.

Used by services/asset_service.py, services/user_service.py, and
services/outsider_service.py.
"""

from typing import Iterable, Optional
from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement


def _escape_like(term: str) -> str:
    """
    Escapes SQL LIKE/ILIKE wildcard characters (`%`, `_`) -- and the escape
    character itself -- in a raw, user-typed search term before it's
    wrapped in `%...%` and handed to `.ilike()`.

    Without this, someone typing a literal `%` or `_` (e.g. searching for
    an outsider's contact detail that happens to contain one) would have
    it interpreted as a wildcard instead of a literal character, silently
    matching far more or fewer rows than intended. This is a correctness
    fix, not a security boundary -- SQLAlchemy already parameterizes the
    value, so there's no SQL-injection risk here either way.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def apply_search_filter(query, search: Optional[str], columns: Iterable[ColumnElement]):
    """
    If `search` is a non-blank string, narrows `query` to rows where ANY of
    `columns` case-insensitively contains it (an OR across columns, exactly
    like the existing client-side `filterAndPaginate()` in js/ui.js used to
    do in the browser -- see that function's docstring for the UX contract
    this preserves). Returns `query` unchanged if `search` is None/blank,
    so callers can unconditionally do `query = apply_search_filter(...)`.
    """
    if not search or not search.strip():
        return query
    pattern = f"%{_escape_like(search.strip())}%"
    return query.filter(or_(*[col.ilike(pattern, escape="\\") for col in columns]))
