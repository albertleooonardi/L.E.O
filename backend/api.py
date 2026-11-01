import asyncio
import json
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import Auth, CurrentUser
from .catalog import PRESETS
from .config import Config
from .providers import ProviderError, list_models
from .router import choose_mode, classify, rank
from .schemas import (AdoptModels, Connection, Preferences, RouteInput, RunInput, Skill,
                      Token, UserLogin, UserRegister, User)
from .store import Conflict, Store, TERMINAL
from .tools import ToolError, web_search
from .discovery import check_access, discover


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}

# Routes that do not require a user session (auth, health, docs).
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/token",
    "/api/session",
}


def create_app(config=None, store=None):
    config = config or Config()
    remote = [h for h in config.hosts if h.strip() not in LOCAL_HOSTS]
    if remote and not config.api_token:
        # The workspace has one owner; any non-loopback hostname must sit behind the owner token (spec §8).
        raise RuntimeError(f"LEO_ALLOWED_HOSTS includes {', '.join(remote)}. Set LEO_API_TOKEN before serving a remote "
                           "hostname, and put HTTPS in front of L.E.O.")
    store = store or Store(config)
    auth = Auth(store, config)

    @asynccontextmanager
    async def lifespan(app):
        yield
        store.engine.dispose()

    app = FastAPI(title="L.E.O.", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.store = store
    app.state.auth = auth
    app.add_middleware(CORSMiddleware, allow_origins=config.origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "Authorization", "Last-Event-ID"])

    @app.middleware("http")
    async def access_control(request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin not in config.origins:
                return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)

            # Auth: session cookie or bearer token (LEO_API_TOKEN for admin/MCP).
            is_public = request.url.path in PUBLIC_API_PATHS
            session_token = request.cookies.get("leo_session", "")
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")

            if not is_public:
                user = None
                if session_token:
                    # Legacy single-token: the session cookie value itself may be the LEO_API_TOKEN.
                    if config.api_token and secrets.compare_digest(session_token, config.api_token):
                        user = CurrentUser(id="system", username="system", is_authenticated=True)
                    else:
                        user = auth.current_user(session_token)
                if not user and bearer and config.api_token and secrets.compare_digest(bearer, config.api_token):
                    # Legacy server-level bearer token: treated as admin access.
                    user = CurrentUser(id="system", username="system", is_authenticated=True)
                if not user:
                    return JSONResponse({"detail": "Sign in"}, status_code=401)
                # Attach user to request state for route handlers.
                request.state.user = user

        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def current_user(request: Request) -> CurrentUser:
        return request.state.user

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.hosts)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
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

    # ---------------- auth ----------------

    @app.post("/api/auth/register", status_code=201, response_model=User)
    def register(body: UserRegister):
        if store.get_user_by_username(body.username):
            raise HTTPException(400, "Username already taken")
        user = store.create_user(body.username, Auth.hash_password(body.password))
        return {"id": user["id"], "username": user["username"], "created_at": user["created_at"]}

    class LoginResponse(BaseModel):
        user: User
        token: str

    @app.post("/api/auth/login", response_model=LoginResponse)
    def login(body: UserLogin, request: Request):
        user = auth.authenticate_user(body.username, body.password)
        if not user:
            raise HTTPException(401, "Invalid username or password")
        token = auth.create_session(user["id"])
        response = JSONResponse({"user": {"id": user["id"], "username": user["username"], "created_at": user["created_at"]},
                                 "token": token})
        response.set_cookie("leo_session", token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=30 * 24 * 60 * 60)
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request, user: CurrentUser = Depends(current_user)):
        token = request.cookies.get("leo_session", "")
        auth.delete_session(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie("leo_session")
        return response

    @app.get("/api/auth/me", response_model=User)
    def me(user: CurrentUser = Depends(current_user)):
        if user.id == "system":
            raise HTTPException(403, "Use a user session")
        db_user = store.get_user(user.id)
        if not db_user:
            raise HTTPException(401, "User not found")
        return {"id": db_user["id"], "username": db_user["username"], "created_at": db_user["created_at"]}

    # Legacy single-token session (kept for MCP/CLI compatibility).
    class Login(BaseModel):
        token: str = Field(max_length=4096, repr=False)

    @app.post("/api/session")
    def legacy_login(body: Login, request: Request):
        if not config.api_token or not secrets.compare_digest(body.token, config.api_token):
            raise HTTPException(401, "Invalid access token")
        response = JSONResponse({"ok": True})
        response.set_cookie("leo_session", config.api_token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=86400)
        return response

    @app.delete("/api/session")
    def legacy_logout():
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
    def write_settings(body: Preferences, user: CurrentUser = Depends(current_user)):
        return store.set_preferences(body.model_dump())

    @app.get("/api/connections")
    def read_connections(user: CurrentUser = Depends(current_user)):
        return store.with_usage(store.list_connections(user.id if user.id != "system" else None))

    @app.post("/api/connections", status_code=201)
    def create_connection(body: Connection, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        return store.save_connection(body.model_dump(), user_id=uid)

    @app.put("/api/connections/{connection_id}")
    def edit_connection(connection_id: str, body: Connection, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        return store.save_connection(body.model_dump(), connection_id, user_id=uid)

    @app.delete("/api/connections/{connection_id}")
    def delete_connection(connection_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        store.delete_connection(connection_id, user_id=uid)
        return {"ok": True}

    @app.post("/api/connections/{connection_id}/test")
    async def test_connection(connection_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        provider = store.get_connection(connection_id, uid, True)
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

    @app.post("/api/search/test")
    async def test_search():
        """Check the configured SearXNG instance with one query; no model is called."""
        url = store.preferences().get("search_url", "")
        try:
            found = await web_search("open source language models", url)
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "results": len(found), "titles": [r["title"] for r in found[:3]]}

    @app.post("/api/route")
    def preview_route(body: RouteInput, user: CurrentUser = Depends(current_user)):
        """What the routing head would decide for this prompt, without calling a model."""
        prefs = store.preferences()
        skills = [s for s in store.list_skills() if s.get("enabled", True)]
        route = classify(body.prompt, skills)
        mode = body.mode or prefs.get("mode", "auto")
        mode = choose_mode(route) if mode == "auto" else mode
        uid = user.id if user.id != "system" else None
        eligible = store.with_usage([c for c in store.list_connections(uid) if c["enabled"] and (c["free"] or prefs["allow_paid"])])
        ordered, reason = rank(eligible, route["skill"], route["difficulty"], prefs) if prefs.get("smart_routing", True) else (eligible, "smart routing is off")
        return {**route, "mode": mode, "reason": reason,
                "note": "Deep mode asks the planner to label each subtask; those subtasks are routed the same way." if mode == "deep" else "",
                "candidates": [{"id": c["id"], "name": c["name"], "model": c["model"], "tier": c["tier"], "skills": c["skills"],
                                "reasoning": c["reasoning"]} for c in ordered]}

    @app.post("/api/connections/{connection_id}/discover")
    async def discover_models(connection_id: str, user: CurrentUser = Depends(current_user)):
        """Every model this connection's key can use (OpenRouter: free only), with suggested routing profiles."""
        uid = user.id if user.id != "system" else None
        source = store.get_connection(connection_id, uid, True)
        if not source:
            raise HTTPException(404, "Connection not found")
        try:
            found = await discover(source, store.decrypt(source))
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        added = {c["model"] for c in store.list_connections(uid) if c["base_url"] == source["base_url"]}
        for model in found["models"]:
            model["added"] = model["model"] in added
        return found

    @app.post("/api/connections/{connection_id}/adopt", status_code=201)
    async def adopt_models(connection_id: str, body: AdoptModels, user: CurrentUser = Depends(current_user)):
        """Add discovered models as connections that reuse this connection's key; optionally check access first."""
        uid = user.id if user.id != "system" else None
        source = store.get_connection(connection_id, uid, True)
        if not source:
            raise HTTPException(404, "Connection not found")
        existing = {c["model"] for c in store.list_connections(uid) if c["base_url"] == source["base_url"]}
        wanted = [m for m in body.models if m.model not in existing]
        problems = await check_access(source, store.decrypt(source), [m.model_dump() for m in wanted]) if body.check else {}
        known_skills = {s["id"] for s in store.list_skills()}
        created, skipped = [], [{"model": m.model, "reason": "already added"} for m in body.models if m.model in existing]
        for m in wanted:
            checked = problems.get(m.model) or {}
            if checked.get("reason"):
                skipped.append({"model": m.model, "reason": checked["reason"]})
                continue
            if "reasoning" in checked:
                m.reasoning = checked["reasoning"]  # The check may have found the API rejects an effort setting.
            label = (m.name or m.model).removesuffix(" (free)")
            created.append(store.save_connection(Connection(
                name=f"{source['name'].split(' · ')[0]} · {label}"[:80], protocol=source["protocol"], base_url=source["base_url"], model=m.model,
                key_from=connection_id if source.get("secret") else None, free=source["free"] if m.free is None else m.free,
                local=source["local"], priority=source["priority"], context_window=m.context_window, token_parameter=source["token_parameter"],
                input_price=m.input_price if m.input_price is not None else source.get("input_price"),
                output_price=m.output_price if m.output_price is not None else source.get("output_price"),
                skills=[s for s in m.skills if s in known_skills], tier=m.tier, reasoning=m.reasoning).model_dump(), user_id=uid))
        return {"created": created, "skipped": skipped}

    @app.get("/api/conversations")
    def read_conversations(user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        return store.list_conversations(uid)

    @app.get("/api/conversations/{conversation_id}")
    def read_conversation(conversation_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        value = store.conversation(conversation_id, uid)
        if value is None:
            raise HTTPException(404, "Conversation not found")
        return value

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        store.delete_conversation(conversation_id, user_id=uid)
        return {"ok": True}

    @app.get("/api/usage")
    def read_usage(conversation_id: str | None = None, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        return store.usage_report(conversation_id, user_id=uid)

    @app.post("/api/runs", status_code=202)
    def create_run(body: RunInput, user: CurrentUser = Depends(current_user)):
        try:
            uid = user.id if user.id != "system" else None
            if not uid:
                raise HTTPException(403, "User session required to create runs")
            return store.create_run(body, uid)
        except IntegrityError as exc:
            raise HTTPException(409, "Message already submitted; reload the conversation") from exc

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        value = store.get_run(run_id, uid)
        if not value:
            raise HTTPException(404, "Run not found")
        return {**value, "calls": store.call_rows(run_id, uid)}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        if not store.get_run(run_id, uid):
            raise HTTPException(404, "Run not found")
        store.cancel(run_id, uid)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0, user: CurrentUser = Depends(current_user)):
        uid = user.id if user.id != "system" else None
        if not store.get_run(run_id, uid):
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
                current = store.get_run(run_id, uid)
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
