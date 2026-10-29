import asyncio
import json
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import PRESETS
from .config import Config
from .providers import ProviderError, list_models
from .router import choose_mode, classify, rank
from .schemas import Connection, Preferences, RouteInput, RunInput, Skill
from .store import Conflict, Store, TERMINAL


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}


def create_app(config=None, store=None):
    config = config or Config()
    remote = [h for h in config.hosts if h.strip() not in LOCAL_HOSTS]
    if remote and not config.api_token:
        # The workspace has one owner; any non-loopback hostname must sit behind the owner token (spec §8).
        raise RuntimeError(f"LEO_ALLOWED_HOSTS includes {', '.join(remote)}. Set LEO_API_TOKEN before serving a remote "
                           "hostname, and put HTTPS in front of L.E.O.")
    store = store or Store(config)

    @asynccontextmanager
    async def lifespan(app):
        yield
        store.engine.dispose()

    app = FastAPI(title="L.E.O.", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.store = store
    app.add_middleware(CORSMiddleware, allow_origins=config.origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "Authorization", "Last-Event-ID"])

    @app.middleware("http")
    async def access_control(request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin not in config.origins:
                return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
            # Bind to loopback by default. For remote hosting, require a configured token.
            if request.method != "OPTIONS" and request.url.path not in {"/api/health", "/api/session"} and config.api_token:
                supplied = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get("leo_session", "")
                if not secrets.compare_digest(supplied, config.api_token):
                    return JSONResponse({"detail": "Sign in with the server access token"}, status_code=401)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.hosts)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default error payload includes submitted input (which may contain keys).
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
        return JSONResponse({"detail": detail}, status_code=422)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Item not found"}, status_code=404)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.1.0", "authentication_required": bool(config.api_token)}

    class Login(BaseModel):
        token: str = Field(max_length=4096, repr=False)

    @app.post("/api/session")
    def login(body: Login, request: Request):
        if not config.api_token or not secrets.compare_digest(body.token, config.api_token):
            raise HTTPException(401, "Invalid access token")
        response = JSONResponse({"ok": True})
        response.set_cookie("leo_session", config.api_token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=86400)
        return response

    @app.delete("/api/session")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie("leo_session")
        return response

    @app.get("/api/presets")
    def presets():
        return PRESETS

    @app.get("/api/settings")
    def read_settings():
        return store.preferences()

    @app.put("/api/settings")
    def write_settings(body: Preferences):
        return store.set_preferences(body.model_dump())

    @app.get("/api/connections")
    def read_connections():
        return store.list_connections()

    @app.post("/api/connections", status_code=201)
    def create_connection(body: Connection):
        return store.save_connection(body.model_dump())

    @app.put("/api/connections/{connection_id}")
    def edit_connection(connection_id: str, body: Connection):
        return store.save_connection(body.model_dump(), connection_id)

    @app.delete("/api/connections/{connection_id}")
    def delete_connection(connection_id: str):
        store.delete_connection(connection_id)
        return {"ok": True}

    @app.post("/api/connections/{connection_id}/test")
    async def test_connection(connection_id: str):
        provider = store.get_connection(connection_id, True)
        if not provider:
            raise HTTPException(404, "Connection not found")
        try:
            models = await list_models(provider, store.decrypt(provider))
            return {"ok": True, "models": models, "model_found": provider["model"] in models,
                    "message": "Model catalog reached. Inference access is checked when you send a message."}
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/skills")
    def read_skills():
        return store.list_skills()

    @app.post("/api/skills", status_code=201)
    def create_skill(body: Skill):
        return store.save_skill(body.model_dump())

    @app.put("/api/skills/{skill_id}")
    def edit_skill(skill_id: str, body: Skill):
        return store.save_skill(body.model_dump(), skill_id)

    @app.delete("/api/skills/{skill_id}")
    def delete_skill(skill_id: str):
        store.delete_skill(skill_id)
        return {"ok": True}

    @app.post("/api/route")
    def preview_route(body: RouteInput):
        """What the routing head would decide for this prompt, without calling a model."""
        prefs = store.preferences()
        skills = [s for s in store.list_skills() if s.get("enabled", True)]
        route = classify(body.prompt, skills)
        mode = body.mode or prefs.get("mode", "auto")
        mode = choose_mode(route) if mode == "auto" else mode
        eligible = [c for c in store.list_connections() if c["enabled"] and (c["free"] or prefs["allow_paid"])]
        ordered, reason = rank(eligible, route["skill"], route["difficulty"], prefs) if prefs.get("smart_routing", True) else (eligible, "smart routing is off")
        return {**route, "mode": mode, "reason": reason,
                "note": "Deep mode asks the planner to label each subtask; those subtasks are routed the same way." if mode == "deep" else "",
                "candidates": [{"id": c["id"], "name": c["name"], "model": c["model"], "tier": c["tier"], "skills": c["skills"],
                                "reasoning": c["reasoning"]} for c in ordered]}

    @app.get("/api/conversations")
    def read_conversations():
        return store.list_conversations()

    @app.get("/api/conversations/{conversation_id}")
    def read_conversation(conversation_id: str):
        value = store.conversation(conversation_id)
        if value is None:
            raise HTTPException(404, "Conversation not found")
        return value

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str):
        store.delete_conversation(conversation_id)
        return {"ok": True}

    @app.get("/api/usage")
    def read_usage(conversation_id: str | None = None):
        return store.usage_report(conversation_id)

    @app.post("/api/runs", status_code=202)
    def create_run(body: RunInput):
        try:
            return store.create_run(body)
        except IntegrityError as exc:
            raise HTTPException(409, "Message already submitted; reload the conversation") from exc

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str):
        value = store.get_run(run_id)
        if not value:
            raise HTTPException(404, "Run not found")
        return {**value, "calls": store.call_rows(run_id)}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        if not store.get_run(run_id):
            raise HTTPException(404, "Run not found")
        store.cancel(run_id)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0):
        if not store.get_run(run_id):
            raise HTTPException(404, "Run not found")
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")), 0)
        except ValueError:
            raise HTTPException(400, "Invalid event cursor")

        async def generate():
            cursor = after
            heartbeat = 0
            while not await request.is_disconnected():
                batch = store.get_events(run_id, cursor)
                for item in batch:
                    cursor = item["id"]
                    yield f"id: {cursor}\nevent: {item['kind']}\ndata: {json.dumps(item['data'])}\n\n"
                current = store.get_run(run_id)
                if not current or (current["status"] in TERMINAL and len(batch) < 200):
                    break
                heartbeat += 1
                if heartbeat % 60 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.15)

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    if config.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=config.frontend_dir, html=True), name="frontend")
    else:
        @app.get("/")
        def setup():
            return {"message": "Build the frontend with npm run build in frontend/, or use its dev server on port 4321."}
    return app
