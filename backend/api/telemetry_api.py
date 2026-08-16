import time
from collections import defaultdict,deque
from fastapi import APIRouter,Request
from pydantic import BaseModel,Field
from integrations.fastapi_errorbeacon import report_client_event
router=APIRouter(prefix='/telemetry',tags=['telemetry']); _hits:dict[str,deque[float]]=defaultdict(deque)
class ClientErrorPayload(BaseModel):
    message:str=Field(max_length=5000); stack:str|None=Field(default=None,max_length=12000); path:str|None=Field(default=None,max_length=2000); request_id:str|None=Field(default=None,max_length=200); context:dict=Field(default_factory=dict)
@router.post('/client-error',status_code=202)
def client_error(payload:ClientErrorPayload,request:Request):
    ip=request.client.host if request.client else 'unknown'; now=time.time(); q=_hits[ip]
    while q and q[0]<=now-60:q.popleft()
    if len(q)>=20:return {'accepted':False,'reason':'rate_limited'}
    q.append(now); rid=payload.request_id or request.headers.get('x-request-id'); report_client_event(payload.message,stack=payload.stack,path=payload.path,request_id=rid,context=payload.context); return {'accepted':True,'request_id':rid}
