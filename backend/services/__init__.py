"""
services/
---------
The actual business/CRUD logic, isolated from FastAPI's routing layer.
Every function here takes a `Session` (and whatever plain arguments it
needs) and either returns plain data or raises `HTTPException` for
expected/user-facing failures -- there's no `@app.get(...)`/`@router.post(...)`
decorator anywhere in this package. `api/*.py` routers are the only thing
that translates HTTP requests into calls into these functions and their
results back into HTTP responses.
"""
