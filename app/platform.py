from __future__ import annotations

import re
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import jwt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import JSONResponse, Response

_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_caller_service: ContextVar[str | None] = ContextVar("caller_service", default=None)

HTTP_REQUESTS = Counter(
    "platform_http_requests_total",
    "Total HTTP requests handled by the service.",
    ["service", "method", "path", "status"],
)
HTTP_DURATION = Histogram(
    "platform_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["service", "method", "path"],
)
AUTH_FAILURES = Counter(
    "platform_internal_auth_failures_total",
    "Rejected internal authentication attempts.",
    ["service", "reason"],
)


def create_service_token(settings: Any, audience: str) -> str:
    if settings.internal_auth_enabled and not settings.internal_auth_signing_key:
        raise RuntimeError("INTERNAL_AUTH_SIGNING_KEY is required when internal auth is enabled")
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": settings.internal_auth_issuer,
            "sub": settings.internal_auth_service_name,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(seconds=settings.internal_auth_token_ttl_seconds),
        },
        settings.internal_auth_signing_key,
        algorithm="HS256",
    )


def current_tenant_id() -> str | None:
    return _tenant_id.get()


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class PlatformMiddleware:
    def __init__(
        self,
        app,
        *,
        settings: Any,
        public_paths: Iterable[str] = (),
        tenant_required_paths: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.settings = settings
        self.public_paths = tuple(public_paths)
        self.tenant_required_paths = tuple(tenant_required_paths)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "UNKNOWN")
        normalized_path = _normalize_path(path)
        started = time.perf_counter()
        status_code = 500
        tenant_token = None
        caller_token = None

        async def capture_status(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            if not _matches(path, self.public_paths):
                auth_result = self._authenticate(headers.get("authorization"))
                if isinstance(auth_result, JSONResponse):
                    status_code = auth_result.status_code
                    await auth_result(scope, receive, send)
                    return
                caller_token = _caller_service.set(auth_result)

            tenant_id = headers.get("x-tenant-id")
            if tenant_id:
                tenant_token = _tenant_id.set(tenant_id)
            if _matches(path, self.tenant_required_paths) and not tenant_id:
                status_code = 400
                await JSONResponse({"detail": "X-Tenant-Id header is required."}, status_code=400)(
                    scope, receive, send
                )
                return

            await self.app(scope, receive, capture_status)
        finally:
            if tenant_token is not None:
                _tenant_id.reset(tenant_token)
            if caller_token is not None:
                _caller_service.reset(caller_token)
            HTTP_REQUESTS.labels(
                self.settings.internal_auth_service_name,
                method,
                normalized_path,
                str(status_code),
            ).inc()
            HTTP_DURATION.labels(
                self.settings.internal_auth_service_name,
                method,
                normalized_path,
            ).observe(time.perf_counter() - started)

    def _authenticate(self, authorization: str | None) -> str | JSONResponse:
        if not self.settings.internal_auth_enabled:
            return "auth-disabled"
        if not self.settings.internal_auth_signing_key:
            AUTH_FAILURES.labels(self.settings.internal_auth_service_name, "server_misconfigured").inc()
            return JSONResponse({"detail": "Internal authentication is not configured."}, status_code=503)
        if not authorization or not authorization.startswith("Bearer "):
            AUTH_FAILURES.labels(self.settings.internal_auth_service_name, "missing_token").inc()
            return JSONResponse({"detail": "Missing bearer token."}, status_code=401)
        try:
            claims = jwt.decode(
                authorization.removeprefix("Bearer ").strip(),
                self.settings.internal_auth_signing_key,
                algorithms=["HS256"],
                audience=self.settings.internal_auth_service_name,
                issuer=self.settings.internal_auth_issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            AUTH_FAILURES.labels(self.settings.internal_auth_service_name, "expired_token").inc()
            return JSONResponse({"detail": "Expired bearer token."}, status_code=401)
        except jwt.PyJWTError:
            AUTH_FAILURES.labels(self.settings.internal_auth_service_name, "invalid_token").inc()
            return JSONResponse({"detail": "Invalid bearer token."}, status_code=401)
        caller = claims.get("sub")
        if not isinstance(caller, str) or not caller:
            AUTH_FAILURES.labels(self.settings.internal_auth_service_name, "missing_subject").inc()
            return JSONResponse({"detail": "Token subject is required."}, status_code=401)
        return caller


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def _normalize_path(path: str) -> str:
    path = re.sub(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", "/{id}", path)
    path = re.sub(r"/\d+", "/{id}", path)
    path = re.sub(r"/[A-Za-z0-9_-]{24,}", "/{id}", path)
    return path
