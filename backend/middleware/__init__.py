"""
middleware/
------------
ASGI middleware for the FastAPI app, each concern in its own small file:

  request_context.py  -> assigns/propagates a Request Correlation ID
                          (X-Request-ID) for every request/response and
                          structured log line.
  rate_limit.py        -> in-memory rate limiting, applied to POST
                          /auth/login to slow down brute-force attacks.
  security_headers.py  -> adds a small set of standard defensive HTTP
                           response headers to every response.

All three are registered on the `app` in main.py via `app.add_middleware(...)`.
See main.py's comment above those calls for why the ORDER they're added in
matters.
"""
