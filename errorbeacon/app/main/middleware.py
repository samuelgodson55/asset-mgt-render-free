"""
app/main/middleware.py
--------------------------
Two ASGI middlewares: a hard request-body size cap (checked up-front via
Content-Length, and enforced again while streaming for chunked requests
that omit it) and the request-id correlation middleware that stamps every
response with an X-Request-ID and makes it available to logging via
request_id_var (see logging_setup.py).
"""

from __future__ import annotations

import uuid

from starlette.types import ASGIApp, Receive, Scope, Send

from .logging_setup import request_id_var


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get('headers', []))
        content_length = headers.get(b'content-length')
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    body = (f'Request body too large; maximum is {self.max_bytes} bytes').encode()
                    await send({'type':'http.response.start','status':413,'headers':[(b'content-type',b'text/plain'),(b'content-length',str(len(body)).encode())]})
                    await send({'type':'http.response.body','body':body})
                    return
            except ValueError:
                pass

        total = 0
        finished = False
        async def limited_receive():
            nonlocal total, finished
            message = await receive()
            if message.get('type') == 'http.request':
                chunk = message.get('body', b'') or b''
                total += len(chunk)
                if total > self.max_bytes:
                    finished = True
                    raise ValueError('request body too large')
                if not message.get('more_body', False):
                    finished = True
            return message
        try:
            await self.app(scope, limited_receive, send)
        except ValueError as exc:
            if str(exc) == 'request body too large' and not finished:
                body = (f'Request body too large; maximum is {self.max_bytes} bytes').encode()
                await send({'type':'http.response.start','status':413,'headers':[(b'content-type',b'text/plain'),(b'content-length',str(len(body)).encode())]})
                await send({'type':'http.response.body','body':body})
            elif str(exc) == 'request body too large':
                body = (f'Request body too large; maximum is {self.max_bytes} bytes').encode()
                try:
                    await send({'type':'http.response.start','status':413,'headers':[(b'content-type',b'text/plain'),(b'content-length',str(len(body)).encode())]})
                    await send({'type':'http.response.body','body':body})
                except RuntimeError:
                    pass
            else:
                raise


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        incoming = None
        for key, value in scope.get('headers', []):
            if key.lower() == b'x-request-id':
                candidate = value.decode('latin-1').strip()
                if candidate:
                    incoming = candidate[:200]
                break
        rid = incoming or str(uuid.uuid4())
        token = request_id_var.set(rid)
        async def send_with_request_id(message):
            if message.get('type') == 'http.response.start':
                headers = list(message.get('headers', []))
                headers = [(k, v) for k, v in headers if k.lower() != b'x-request-id']
                # HTTP header names are case-insensitive; this is the
                # X-Request-ID response header used to correlate each probe/event.
                headers.append((b'x-request-id', rid.encode('latin-1')))
                message['headers'] = headers
            await send(message)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)
