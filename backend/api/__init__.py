"""
api/
----
FastAPI `APIRouter` modules, grouped by resource (auth, assets, users,
outsiders, checkouts, audit). Each router is deliberately thin: it parses
the request (path/query params + a Pydantic body), calls into the matching
`services/*.py` function to do the actual work, and shapes the HTTP
response -- no business logic lives here. `main.py` mounts every router
from this package via `app.include_router(...)`.
"""
