import time
import uuid
import logging
import json
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("gateway.audit")

class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        start_time = time.time()
        
        response: Response = await call_next(request)
        
        duration_ms = round((time.time() - start_time) * 1000, 2)
        response.headers["x-request-id"] = request_id

        log_data = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "latency_ms": duration_ms,
            "client_ip": request.client.host if request.client else "unknown",
        }
        
        logger.info(json.dumps(log_data))
        return response
