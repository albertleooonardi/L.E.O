import asyncio
import json
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import SYSTEM_USER, Auth, CurrentUser
from .catalog import PRESETS
from .config import Config
from .ninerouter import ACCOUNTS, RISK_NOTICE, NineRouter, RouterError, SignIns, serve_callback
from .providers import ProviderError, list_models
from .router import choose_mode, classify, rank
from .sandbox import MAX_IMPORT_BYTES, Sandbox, SandboxError, tar_to_zip, workspace_path, zip_to_tar
from .schemas import (AccountSignIn, AdoptModels, ApprovalInput, Connection, Preferences, RouteInput, RunInput, Skill, SteerInput,
                      UserLogin, UserRegister, User, WorkspaceInput)
from .store import Conflict, Store, TERMINAL
from .tools import ToolError, web_search
from .discovery import check_access, discover


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}

# Routes that do not require a user session (auth, health, docs).
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/register",
    "/api/auth/login",
    "/api/session",
}


def create_app(config=None, store=None, sandbox=None):
    config = config or Config()
    remote = [h for h in config.hosts if h.strip() not in LOCAL_HOSTS]
    if remote and not config.api_token:
        # The workspace has one owner; any non-loopback hostname must sit behind the owner token (spec §8).
        raise RuntimeError(f"LEO_ALLOWED_HOSTS includes {', '.join(remote)}. Set LEO_API_TOKEN before serving a remote "
                           "hostname, and put HTTPS in front of L.E.O.")
    store = store or Store(config)
    sandbox = sandbox or (Sandbox(config.sandbox_url, config.sandbox_token) if config.sandbox_url else None)
    auth = Auth(store, config)
    router = NineRouter(config.router9_url, config.router9_password) if config.router9_url else None
    router_models = f"{config.router9_url}/v1"

    async def add_account_models(provider, user_id, account):
        """After a sign-in: add the account's models, all using one 9Router key that L.E.O. creates once."""
        spec = ACCOUNTS[provider]
        mine = [c for c in store.list_connections(user_id, True) if c["base_url"] == router_models]
        keyed = next((c for c in mine if c.get("secret")), None)
        key = store.decrypt(keyed) if keyed else await router.create_key()
        found = await discover({"base_url": router_models, "protocol": "openai", "free": True}, key)
        existing, known_skills = {c["model"] for c in mine}, {s["id"] for s in store.list_skills()}
        created = []
        for m in found["models"]:
            if not m["model"].startswith(spec["prefix"] + "/") or m["model"] in existing:
                continue
            source = keyed or (created[0] if created else None)
            created.append(store.save_connection(Connection(
                name=f"{spec['name']} · {m['model'].split('/', 1)[1]}"[:80], base_url=router_models, model=m["model"],
                api_key=None if source else key, key_from=source["id"] if source else None, free=True,
                context_window=m["context_window"], skills=[s for s in m["skills"] if s in known_skills], tier=m["tier"],
                reasoning=m["reasoning"]).model_dump(), user_id=user_id))
        return {"account": account.get("email") or account.get("displayName") or "", "added": len(created)}

    signins = SignIns(router, add_account_models) if router else None

    @asynccontextmanager
    async def lifespan(app):
        callback = None
        if signins and config.router9_callback_port:
            try:
                callback = await serve_callback(signins, config.router9_callback_port)
            except OSError as exc:  # Sign-in still works for device-code providers.
                print(f"L.E.O.: ChatGPT (Codex) sign-in is off; port {config.router9_callback_port} is unavailable ({exc})")
        yield
        if callback:
            callback.close()
        store.engine.dispose()

    app = FastAPI(title="L.E.O.", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.store = store
    app.state.auth = auth
    app.state.signins = signins
    app.add_middleware(CORSMiddleware, allow_origins=config.origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "Authorization", "Last-Event-ID"])

    @app.middleware("http")
    async def access_control(request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin not in config.origins:
                return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)

            # Auth: session cookie or bearer token (LEO_API_TOKEN for admin/MCP). CORS preflights carry no credentials.
            if request.method != "OPTIONS" and request.url.path not in PUBLIC_API_PATHS:
                user = await run_in_threadpool(resolve_user, request)
                if not user:
                    return JSONResponse({"detail": "Sign in"}, status_code=401)
                request.state.user = user

        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def resolve_user(request: Request) -> CurrentUser | None:
        session_token = request.cookies.get("leo_session", "")
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        # Legacy single-token access: the cookie or bearer value is LEO_API_TOKEN itself, treated as admin.
        for supplied in (session_token, bearer):
            if supplied and config.api_token and secrets.compare_digest(supplied, config.api_token):
                return SYSTEM_USER
        return auth.current_user(session_token)

    def current_user(request: Request) -> CurrentUser:
        return request.state.user

    def require_admin(request: Request) -> CurrentUser:
        """Settings, skills and the search address are shared by every account."""
        if not request.state.user.is_admin:
            raise HTTPException(403, "Only the workspace admin can change this")
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

    @app.exception_handler(SandboxError)
    async def sandbox_failed(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Item not found"}, status_code=404)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.1.0", "authentication_required": bool(config.api_token), "agent": sandbox is not None}

    # ---------------- auth ----------------

    def public_user(user):
        return {"id": user["id"], "username": user["username"], "created_at": user["created_at"]}

    @app.post("/api/auth/register", status_code=201, response_model=User)
    def register(body: UserRegister, request: Request):
        caller = resolve_user(request)
        # Without LEO_API_TOKEN (loopback only) the first visitor sets up the workspace; otherwise an admin adds accounts.
        first_local = not config.api_token and not store.has_users()
        if not (first_local or config.allow_registration or (caller and caller.is_admin)):
            raise HTTPException(403, "Registration is closed; ask the workspace admin for an account")
        try:
            user = store.create_user(body.username, Auth.hash_password(body.password))
        except IntegrityError as exc:
            raise HTTPException(409, "Username already taken") from exc
        if store.admin_user_id() == user["id"]:
            store.adopt_orphans(user["id"])  # Connections and chats saved before accounts existed.
        return public_user(user)

    class LoginResponse(BaseModel):
        user: User

    @app.post("/api/auth/login", response_model=LoginResponse)
    def login(body: UserLogin, request: Request):
        throttle = f"{request.client.host if request.client else ''}|{body.username.lower()}"
        if auth.login_blocked(throttle):
            raise HTTPException(429, "Too many failed sign-in attempts; try again in a few minutes")
        user = auth.authenticate_user(body.username, body.password)
        if not user:
            auth.record_failure(throttle)
            raise HTTPException(401, "Invalid username or password")
        auth.clear_failures(throttle)
        token = auth.create_session(user["id"])
        response = JSONResponse({"user": public_user(user)})
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
        return public_user(db_user)

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
        # With 9Router managed by L.E.O., its preset points at the service address the containers use.
        return [{**p, "base_url": router_models, "hint": "Prefer Sign in with an account; this form is for 9Router models added by hand."}
                if p["id"] == "9router" and router else p for p in PRESETS]

    # ---------------- account sign-in (9Router) ----------------

    def require_router():
        if not signins:
            raise HTTPException(400, "Account sign-in is off; set LEO_9ROUTER_URL (docker compose sets it)")
        return signins

    @app.get("/api/accounts")
    async def read_accounts(user: CurrentUser = Depends(current_user)):
        """Providers offered for sign-in and the accounts signed in through 9Router."""
        offered = [{"id": key, "name": v["name"], "flow": v["flow"], "risk": RISK_NOTICE if v["risk"] else None} for key, v in ACCOUNTS.items()]
        if not router:
            return {"available": False, "error": "Account sign-in is off; set LEO_9ROUTER_URL (docker compose sets it)", "providers": offered, "accounts": []}
        try:
            return {"available": True, "error": None, "providers": offered, "accounts": await router.accounts()}
        except RouterError as exc:
            return {"available": False, "error": str(exc), "providers": offered, "accounts": []}

    @app.post("/api/accounts/signin", status_code=201)
    async def start_sign_in(body: AccountSignIn, user: CurrentUser = Depends(require_admin)):
        flows = require_router()
        if body.provider not in ACCOUNTS:
            raise HTTPException(400, "L.E.O. does not offer sign-in for that provider")
        if ACCOUNTS[body.provider]["risk"] and not body.accept_risk:
            raise HTTPException(400, RISK_NOTICE)
        try:
            return await flows.start(body.provider, user.owner_id)
        except RouterError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/accounts/signin/{session_id}")
    async def sign_in_status(session_id: str, user: CurrentUser = Depends(require_admin)):
        try:
            return await require_router().status(session_id, user.owner_id)
        except RouterError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.delete("/api/accounts/signin/{session_id}")
    def cancel_sign_in(session_id: str, user: CurrentUser = Depends(require_admin)):
        require_router().cancel(session_id, user.owner_id)
        return {"ok": True}

    @app.delete("/api/accounts/{account_id}")
    async def disconnect_account(account_id: str, user: CurrentUser = Depends(require_admin)):
        """Sign the account out of 9Router; its models leave L.E.O. once no account of that provider is left."""
        require_router()
        try:
            accounts = await router.accounts()
            account = next((a for a in accounts if a["id"] == account_id), None)
            if not account:
                raise HTTPException(404, "Account not found")
            await router.remove(account_id)
        except RouterError as exc:
            raise HTTPException(502, str(exc)) from exc
        removed = 0
        if not any(a["provider"] == account["provider"] and a["id"] != account_id for a in accounts):
            prefix = ACCOUNTS[account["provider"]]["prefix"] + "/"
            for c in store.list_connections(user.owner_id):
                if c["base_url"] == router_models and c["model"].startswith(prefix):
                    store.delete_connection(c["id"], user.owner_id)
                    removed += 1
        return {"ok": True, "removed_models": removed}

    @app.get("/api/settings")
    def read_settings():
        return store.preferences()

    @app.put("/api/settings")
    def write_settings(body: Preferences, user: CurrentUser = Depends(require_admin)):
        return store.set_preferences(body.model_dump())

    @app.get("/api/connections")
    def read_connections(user: CurrentUser = Depends(current_user)):
        return store.with_usage(store.list_connections(user.owner_id))

    @app.post("/api/connections", status_code=201)
    def create_connection(body: Connection, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        return store.save_connection(body.model_dump(), user_id=uid)

    @app.put("/api/connections/{connection_id}")
    def edit_connection(connection_id: str, body: Connection, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        return store.save_connection(body.model_dump(), connection_id, user_id=uid)

    @app.delete("/api/connections/{connection_id}")
    def delete_connection(connection_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        store.delete_connection(connection_id, user_id=uid)
        return {"ok": True}

    @app.post("/api/connections/{connection_id}/test")
    async def test_connection(connection_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
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
    def create_skill(body: Skill, user: CurrentUser = Depends(require_admin)):
        return store.save_skill(body.model_dump())

    @app.put("/api/skills/{skill_id}")
    def edit_skill(skill_id: str, body: Skill, user: CurrentUser = Depends(require_admin)):
        return store.save_skill(body.model_dump(), skill_id)

    @app.delete("/api/skills/{skill_id}")
    def delete_skill(skill_id: str, user: CurrentUser = Depends(require_admin)):
        store.delete_skill(skill_id)
        return {"ok": True}

    @app.post("/api/search/test")
    async def test_search(user: CurrentUser = Depends(require_admin)):
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
        uid = user.owner_id
        eligible = store.with_usage([c for c in store.list_connections(uid) if c["enabled"] and (c["free"] or prefs["allow_paid"])])
        ordered, reason = rank(eligible, route["skill"], route["difficulty"], prefs) if prefs.get("smart_routing", True) else (eligible, "smart routing is off")
        return {**route, "mode": mode, "reason": reason,
                "note": "Deep mode asks the planner to label each subtask; those subtasks are routed the same way." if mode == "deep" else "",
                "candidates": [{"id": c["id"], "name": c["name"], "model": c["model"], "tier": c["tier"], "skills": c["skills"],
                                "reasoning": c["reasoning"]} for c in ordered]}

    @app.post("/api/connections/{connection_id}/discover")
    async def discover_models(connection_id: str, user: CurrentUser = Depends(current_user)):
        """Every model this connection's key can use (OpenRouter: free only), with suggested routing profiles."""
        uid = user.owner_id
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
        uid = user.owner_id
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
                skills=[s for s in m.skills if s in known_skills], tier=m.tier, reasoning=m.reasoning,
                tools=m.tools).model_dump(), user_id=uid))
        return {"created": created, "skipped": skipped}

    @app.get("/api/conversations")
    def read_conversations(user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        return store.list_conversations(uid)

    @app.get("/api/conversations/{conversation_id}")
    def read_conversation(conversation_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        value = store.conversation(conversation_id, uid)
        if value is None:
            raise HTTPException(404, "Conversation not found")
        return value

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        workspace = store.get_workspace(conversation_id, uid)
        await run_in_threadpool(store.delete_conversation, conversation_id, uid)
        if workspace and sandbox:
            try:
                await sandbox.delete(conversation_id)  # The container and its volume go with the chat.
            except SandboxError:
                pass  # The sandbox service may be down; idle containers are stopped and can be removed later.
        return {"ok": True}

    # ---------------- agent workspaces ----------------

    def require_sandbox():
        if not sandbox:
            raise HTTPException(400, "Agent mode needs the sandbox service; start L.E.O. with docker compose (LEO_SANDBOX_URL is not set)")
        return sandbox

    def own_workspace(workspace_id, user):
        workspace = store.get_workspace(workspace_id, user.owner_id)
        if not workspace:
            raise HTTPException(404, "Workspace not found")
        return workspace

    @app.post("/api/workspaces", status_code=201)
    async def create_workspace(body: WorkspaceInput, user: CurrentUser = Depends(current_user)):
        """A new chat with a sandbox: empty, or cloned from git (tokens stay in L.E.O. and sandboxd, never in the sandbox)."""
        box = require_sandbox()
        workspace = store.create_workspace({**body.model_dump(), "status": "creating"}, user.owner_id)
        try:
            await box.ensure(workspace["id"])
            if body.source == "git":
                cloned = await box.clone(workspace["id"], body.repo_url, body.branch, body.token)
                store.update_workspace(workspace["id"], branch=cloned["branch"])
            store.update_workspace(workspace["id"], status="ready")
        except SandboxError as exc:
            store.update_workspace(workspace["id"], status="error", error=str(exc)[:500])
            raise
        return store.get_workspace(workspace["id"], user.owner_id)

    @app.get("/api/workspaces/{workspace_id}")
    def read_workspace(workspace_id: str, user: CurrentUser = Depends(current_user)):
        return own_workspace(workspace_id, user)

    @app.get("/api/workspaces/{workspace_id}/files")
    async def list_workspace_files(workspace_id: str, path: str = ".", depth: int = 2, user: CurrentUser = Depends(current_user)):
        own_workspace(workspace_id, user)
        return {"path": path, "entries": await require_sandbox().list_files(workspace_id, path, max(1, min(depth, 6)))}

    @app.get("/api/workspaces/{workspace_id}/file")
    async def read_workspace_file(workspace_id: str, path: str, user: CurrentUser = Depends(current_user)):
        own_workspace(workspace_id, user)
        data = await require_sandbox().read_file(workspace_id, path)
        binary = b"\0" in data[:8000]
        return {"path": workspace_path(path), "size": len(data), "binary": binary,
                "content": "" if binary else data[:1_000_000].decode("utf-8", "replace")}

    @app.put("/api/workspaces/{workspace_id}/upload")
    async def upload_workspace(workspace_id: str, request: Request, user: CurrentUser = Depends(current_user)):
        """Unpack a .zip (sent as the request body) into the workspace."""
        own_workspace(workspace_id, user)
        if int(request.headers.get("content-length") or 0) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "Upload is too large")
        conversation = store.conversation(workspace_id, user.owner_id)
        if conversation and conversation["active_run"]:
            raise HTTPException(409, "Wait for the agent to finish (or stop it) before uploading")
        data = await request.body()
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "Upload is too large")
        archive = await run_in_threadpool(zip_to_tar, data)
        await require_sandbox().import_tar(workspace_id, archive)
        return {"ok": True}

    @app.get("/api/workspaces/{workspace_id}/download")
    async def download_workspace(workspace_id: str, user: CurrentUser = Depends(current_user)):
        workspace = own_workspace(workspace_id, user)
        archive = await run_in_threadpool(tar_to_zip, await require_sandbox().export(workspace_id))
        name = "".join(c if c.isalnum() or c in "-_" else "-" for c in workspace.get("name") or "workspace")[:60].strip("-") or "workspace"
        return Response(archive, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

    @app.get("/api/usage")
    def read_usage(conversation_id: str | None = None, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        return store.usage_report(conversation_id, user_id=uid)

    @app.post("/api/runs", status_code=202)
    def create_run(body: RunInput, user: CurrentUser = Depends(current_user)):
        if body.mode == "agent":
            require_sandbox()
        try:
            return store.create_run(body, user.owner_id)
        except IntegrityError as exc:
            raise HTTPException(409, "Message already submitted; reload the conversation") from exc

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        value = store.get_run(run_id, uid)
        if not value:
            raise HTTPException(404, "Run not found")
        return {**value, "calls": store.call_rows(run_id, uid)}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
        if not store.get_run(run_id, uid):
            raise HTTPException(404, "Run not found")
        store.cancel(run_id, uid)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/approve")
    def approve_run(run_id: str, body: ApprovalInput, user: CurrentUser = Depends(current_user)):
        """Answer the agent's pending approval (a git push, or more budget); the run then resumes."""
        store.resolve_approval(run_id, body.approval_id, body.approved, user.owner_id)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/steer")
    def steer_run(run_id: str, body: SteerInput, user: CurrentUser = Depends(current_user)):
        """Send the working agent a message; it reads it before its next step."""
        store.steer(run_id, body.message, user.owner_id)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0, user: CurrentUser = Depends(current_user)):
        uid = user.owner_id
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
