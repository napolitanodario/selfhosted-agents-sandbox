"""Same-origin dashboard API. Authentication is the only writable HTTP surface."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from experiment_orchestrator.telemetry import Redactor
from .host import HostSampler
from .reader import Reader


COOKIE = "experiment_dashboard_session"
SESSION_SECONDS = 12 * 60 * 60
LOGGER = logging.getLogger(__name__)


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=1024)


def create_app(
    root: Path,
    *,
    token: str | None = None,
    local_no_auth: bool = False,
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]"),
    secure_cookie: bool = False,
    collect_host: bool = True,
    static_dir: Path | None = None,
) -> FastAPI:
    if not token and not local_no_auth:
        raise ValueError(
            "Set DASHBOARD_ACCESS_TOKEN, or explicitly enable local development access"
        )
    if token and len(token) < 24:
        raise ValueError("DASHBOARD_ACCESS_TOKEN must contain at least 24 characters")
    if "*" in allowed_hosts:
        raise ValueError("Configure explicit dashboard hostnames")
    reader = Reader(
        root, Redactor({**dict(os.environ), "DASHBOARD_ACCESS_TOKEN": token or ""})
    )
    static_dir = static_dir or Path(__file__).parent / "static"
    # React Router prerenders a few bootstrap scripts. Allow only their build
    # hashes, rather than allowing arbitrary inline scripts in artifact content.
    script_hashes = []
    entry = static_dir / "index.html"
    if entry.is_file():
        for attributes, content in re.findall(
            r"<script\b([^>]*)>(.*?)</script>", entry.read_text(), flags=re.S
        ):
            if not re.search(r"\bsrc\s*=", attributes):
                digest = base64.b64encode(
                    hashlib.sha256(content.encode()).digest()
                ).decode()
                script_hashes.append(f"'sha256-{digest}'")
    attempts: dict[str, list[float]] = {}

    async def collect():
        sampler = None
        while True:
            try:
                if sampler is None:
                    sampler = await asyncio.to_thread(HostSampler, root)
                await asyncio.to_thread(sampler.sample)
            except (OSError, sqlite3.Error):
                LOGGER.warning("Local host telemetry is temporarily unavailable")
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(collect()) if collect_host else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Experiment Observatory",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    def authenticated(request: Request):
        if local_no_auth and not token:
            return request.client is not None and request.client.host in {
                "127.0.0.1",
                "::1",
                "testclient",
            }
        cookie = request.cookies.get(COOKIE, "")
        try:
            expires, nonce, signature = cookie.split(".")
            payload = f"{expires}.{nonce}"
            expected = hmac.new(
                token.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            return int(expires) > time.time() and hmac.compare_digest(
                signature, expected
            )
        except (ValueError, AttributeError, TypeError):
            return False

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        path = request.url.path
        public = {"/api/health", "/api/auth", "/api/auth/login"}
        if (
            path.startswith("/api/")
            and path not in public
            and not authenticated(request)
        ):
            return JSONResponse(
                {"detail": "Sign in to view experiment activity"}, status_code=401
            )
        if request.method not in {"GET", "HEAD"}:
            if path not in {"/api/auth/login", "/api/auth/logout"}:
                return JSONResponse(
                    {"detail": "This dashboard is read-only"}, status_code=405
                )
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {"detail": "Cross-origin requests are not allowed"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' {' '.join(script_hashes)}; style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        return JSONResponse(
            {"detail": "Recorded data is temporarily unavailable"}, status_code=503
        )

    @app.exception_handler(KeyError)
    @app.exception_handler(FileNotFoundError)
    async def missing(request, error):
        return JSONResponse({"detail": "Recorded item not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse(
            {"detail": "This recorded item cannot be previewed"}, status_code=400
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": "read_only"}

    @app.get("/api/auth")
    def auth(request: Request):
        return {
            "authenticated": authenticated(request),
            "local_access": local_no_auth and not token,
        }

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request, response: Response):
        # No blocking work here. Keep rate-limit updates on the event-loop thread.
        client = request.client.host if request.client else "unknown"
        timestamp = time.monotonic()
        recent = [value for value in attempts.get(client, []) if timestamp - value < 60]
        if len(recent) >= 10:
            raise HTTPException(429, "Too many attempts. Try again in a minute.")
        if len(attempts) > 1024:
            attempts.clear()
        attempts[client] = [*recent, timestamp]
        if not token or not hmac.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, "The access token is not valid")
        attempts.pop(client, None)
        payload = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
        signature = hmac.new(
            token.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        response.set_cookie(
            COOKIE,
            f"{payload}.{signature}",
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
            path="/",
        )
        return {"authenticated": True}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie(
            COOKIE, path="/", secure=secure_cookie, httponly=True, samesite="strict"
        )
        return {"authenticated": False}

    @app.get("/api/snapshot")
    def snapshot():
        return reader.snapshot()

    @app.get("/api/sessions/{identifier}")
    def session(identifier: str):
        return reader.session(identifier)

    @app.get("/api/sessions/{identifier}/events")
    def events(
        identifier: str,
        after: int | None = Query(None, ge=0),
        limit: int = Query(200, ge=1, le=500),
    ):
        return reader.events(identifier, after, limit)

    @app.get("/api/runs/{identifier}")
    def run(identifier: str):
        return reader.run(identifier)

    @app.get("/api/runs/{identifier}/events")
    def run_events(identifier: str):
        return reader.run_events(identifier)

    @app.get("/api/runs/{identifier}/attempts/{attempt}/artifacts")
    def artifacts(identifier: str, attempt: str):
        try:
            return reader.artifacts(identifier, attempt)
        except OSError:
            raise HTTPException(404, "Artifact index unavailable") from None

    @app.get("/api/runs/{identifier}/attempts/{attempt}/preview")
    def preview(identifier: str, attempt: str, path: str = Query(max_length=1000)):
        try:
            return reader.preview(identifier, attempt, path)
        except OSError:
            raise HTTPException(404, "Artifact unavailable") from None

    @app.get("/api/stream")
    async def stream(request: Request):
        async def snapshots():
            previous = request.headers.get("last-event-id", "")
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                if not authenticated(request):
                    yield "event: auth_expired\ndata: {}\n\n"
                    break
                # Send committed snapshots. Log viewers use durable event cursors
                # independently, so reconnecting does not duplicate log lines.
                value = await asyncio.to_thread(reader.snapshot)
                data = json.dumps(value, separators=(",", ":"), allow_nan=False)
                current = hashlib.sha256(data.encode()).hexdigest()
                if current != previous:
                    yield f"id: {current}\nevent: snapshot\ndata: {data}\n\n"
                    previous = current
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(3)

        return StreamingResponse(
            snapshots(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "API route not found")
        entry = static_dir / "index.html"
        if not entry.is_file():
            raise HTTPException(
                503, "Build the dashboard frontend before starting this service"
            )
        return FileResponse(entry, headers={"Cache-Control": "no-cache"})

    return app
