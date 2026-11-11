import asyncio
import hashlib
import html
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken
from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import SYSTEM_USER, Auth, CurrentUser
from .catalog import PRESETS
from .config import Config
from . import mcp_auth
from .connectors import Connectors, public as public_connector
from .mcp_client import MCPClient, MCPError, NeedsAuth, check_url
from .oauth import OAuth, OAuthError, email_allowed, pkce_pair
from .ninerouter import ACCOUNTS, KEY_PROVIDERS, MODEL_PROVIDERS, RISK_NOTICE, NineRouter, RouterError, RouterUnavailable, SignIns, serve_callback
from .model_connections import ModelConnections
from .local_router import LocalRouters
from .providers import ProviderError, list_models
from .router import choose_mode, classify, rank
from .sandbox import MAX_IMPORT_BYTES, Sandbox, SandboxError, tar_to_zip, workspace_path, zip_to_tar
from .schemas import (AccountSignIn, AdoptModels, ApprovalInput, CliAnswer, CliDeviceStart, CliTokenInput, CliUsageInput, Connection, ConnectorInput, ConnectorUpdate, Me, ModelProviderKey, ModelSelection, PasswordChange, Preferences, RouteInput, RunInput,
                      Skill, SteerInput, UserLogin, UserRegister, User, WorkspaceInput)
from .store import Conflict, Store, TERMINAL
from .tools import ToolError, web_search
from .discovery import check_access, discover, profile


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}

# Routes that do not require a user session (auth, health, docs).
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/providers",
    "/api/session",
    # Arrives from the connector's sign-in page, so the SameSite=Strict session cookie is not sent; the encrypted
    # leo_mcp_oauth cookie says whose sign-in it is.
    "/api/connectors/oauth/callback",
    # L.E.O.C.O.D.E device login: the CLI asks for a code, then polls for its token.
    "/api/cli/device",
    "/api/cli/token",
}
# Only these accept a L.E.O.C.O.D.E device token (Authorization: Bearer), and nothing else does.
CLI_DEVICE_PATHS = {"/api/cli/sync", "/api/cli/usage", "/api/cli/logout", "/api/cli/connections", "/api/cli/connections/rerank"}
# The CLI reaches models on the user's 9Router (on the server's network only) through the server: model calls only.
CLI_RELAY_PREFIX = "/api/cli/relay/"
# The CLI calls the tools of the user's L.E.O Chat connectors through the server, which keeps their sign-ins.
CLI_CONNECTOR_PREFIX = "/api/cli/connectors/"
# The CLI's /models add signs in to ChatGPT, Copilot, and the others with the same flow as Settings → Models.
CLI_ACCOUNTS_PREFIX = "/api/cli/accounts"
CLI_MODEL_PROVIDERS_PREFIX = "/api/cli/model-providers/"
RELAY_PATHS = {"chat/completions", "responses", "models"}
MAX_RELAY_BYTES = 32 * 1024 * 1024
# A stopped 9Router is started again by the relay; this long it may take to answer once started.
ROUTER_BOOT_SECONDS = 60
ROUTER_UNREACHABLE = "Your 9Router is not reachable from the L.E.O. server"
LEGACY_ROUTER = "http://9router:20128"  # The shared 9router service of earlier versions (one per server, not per user).
# OAuth start and callback check the session themselves when linking.
PUBLIC_API_PREFIXES = ("/api/auth/oauth/",)


def create_app(config=None, store=None, sandbox=None, oauth=None, mcp_transport=None, relay_transport=None):
    config = config or Config()
    remote = [h for h in config.hosts if h.strip() not in LOCAL_HOSTS]
    if remote and not config.api_token:
        # The workspace has one owner; any non-loopback hostname must sit behind the owner token (spec §8).
        raise RuntimeError(f"LEO_ALLOWED_HOSTS includes {', '.join(remote)}. Set LEO_API_TOKEN before serving a remote "
                           "hostname, and put HTTPS in front of L.E.O.")
    store = store or Store(config)
    oauth = oauth or OAuth(config)
    connectors = Connectors(store, mcp_transport)
    sandbox = sandbox or (Sandbox(config.sandbox_url, config.sandbox_token) if config.sandbox_url else None)
    local_routers = LocalRouters(config.data_dir, config.local_router_entry) if config.local_router_entry and not sandbox else None
    router_manager = sandbox or local_routers
    router_locks = {}
    auth = Auth(store, config)
    async def router_for(user_id, create=False):
        """The user's own 9Router. `create` starts it (creating it on first use); otherwise None if they have none."""
        async with router_locks.setdefault(user_id, asyncio.Lock()):
            saved = store.get_router(user_id)
            if not saved and not create:
                return None
            if not saved:
                saved = {"password": secrets.token_urlsafe(24)}
                saved["url"] = await router_manager.ensure_router(user_id, saved["password"])
                store.save_router(user_id, saved["url"], saved["password"])
            elif create:  # Removed after its last account was disconnected; persisted data remains.
                await router_manager.ensure_router(user_id, saved["password"])
            router = NineRouter(saved["url"], saved["password"])
            if create:
                await router.wait_ready()
            return router

    model_connections = ModelConnections(store, router_for)

    async def add_account_models(provider, user_id, account):
        """After a sign-in: add the account's models, all using one key on the user's router that L.E.O. creates once."""
        spec = ACCOUNTS[provider]
        router = await router_for(user_id)
        models_url = f"{router.url}/v1"
        mine = [c for c in store.list_connections(user_id, True) if c["base_url"] == models_url]
        keyed = next((c for c in mine if c.get("secret")), None)
        key = store.decrypt(keyed) if keyed else await router.create_key()
        found = await discover({"base_url": models_url, "protocol": "openai", "free": True}, key)
        existing, known_skills = {c["model"] for c in mine}, {s["id"] for s in store.list_skills()}
        created = []
        for m in found["models"]:
            if not m["model"].startswith(spec["prefix"] + "/") or m["model"] in existing:
                continue
            source = keyed or (created[0] if created else None)
            created.append(store.save_connection(Connection(
                name=f"{spec['name']} · {m['model'].split('/', 1)[1]}"[:80], base_url=models_url, model=m["model"],
                api_key=None if source else key, key_from=source["id"] if source else None, free=True,
                context_window=m["context_window"], skills=[s for s in m["skills"] if s in known_skills], tier=m["tier"],
                reasoning=m["reasoning"]).model_dump(), user_id=user_id))
        return {"account": account.get("email") or account.get("displayName") or "", "added": len(created)}

    signins = SignIns(router_for, add_account_models) if router_manager else None

    async def adopt_legacy_routers():
        """Models signed in through the shared 9router of earlier versions move to the user's own router, which
        sandboxd starts from a copy of the shared one (accounts and keys included). Retried at the next start if it fails."""
        for uid in await run_in_threadpool(store.connection_owners, LEGACY_ROUTER):
            try:
                saved = store.get_router(uid)
                if not saved:
                    url = await sandbox.ensure_router(uid, config.legacy_router_password, adopt_legacy=True)
                    store.save_router(uid, url, config.legacy_router_password)
                    saved = {"url": url}
                moved = store.move_connections(uid, LEGACY_ROUTER, saved["url"])
                print(f"L.E.O.: moved {moved} models from the shared 9Router to the user's own ({saved['url']})")
            except Exception as exc:  # sandboxd not ready yet, Docker down: try again next start.
                print(f"L.E.O.: could not move models off the shared 9Router yet: {exc}")

    @asynccontextmanager
    async def lifespan(app):
        callback = None
        try:
            if local_routers:
                # Restore before accepting requests, including models called directly by the worker.
                for uid in store.router_users():
                    await router_for(uid, create=True)
            if sandbox:
                app.state.adopting = asyncio.create_task(adopt_legacy_routers())
            if signins and config.router9_callback_port:
                try:
                    callback = await serve_callback(signins, config.router9_callback_port, host="127.0.0.1" if local_routers else "0.0.0.0")
                except OSError as exc:  # Sign-in still works for device-code providers.
                    print(f"L.E.O.: ChatGPT (Codex) sign-in is off; port {config.router9_callback_port} is unavailable ({exc})")
            yield
        finally:
            if sandbox and not app.state.adopting.done():
                app.state.adopting.cancel()
            if callback:
                callback.close()
                await callback.wait_closed()
            if local_routers:
                await local_routers.close()
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
            public = request.url.path in PUBLIC_API_PATHS or request.url.path.startswith(PUBLIC_API_PREFIXES)
            device_path = request.url.path in CLI_DEVICE_PATHS or request.url.path.startswith((CLI_RELAY_PREFIX, CLI_CONNECTOR_PREFIX, CLI_ACCOUNTS_PREFIX, CLI_MODEL_PROVIDERS_PREFIX))
            if device_path and request.method != "OPTIONS":
                device = await run_in_threadpool(store.cli_device, request.headers.get("authorization", "").removeprefix("Bearer ").strip())
                owner = device and await run_in_threadpool(store.get_user, device["user_id"])
                if not owner:
                    return JSONResponse({"detail": "This device was signed out; run leocode login"}, status_code=401)
                request.state.device = device
                request.state.user = CurrentUser(id=owner["id"], username=owner["username"], is_admin=owner["id"] == store.admin_user_id())
            elif request.method != "OPTIONS" and not public:
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
        return {"status": "ok", "version": "0.1.0", "authentication_required": bool(config.api_token),
                "agent": bool(config.server_agent and sandbox)}

    # ---------------- auth ----------------

    def public_user(user):
        return {key: user.get(key) for key in ("id", "username", "created_at", "email", "display_name", "avatar_url")}

    def start_session(response, request, user_id):
        response.set_cookie("leo_session", auth.create_session(user_id), httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=30 * 24 * 60 * 60)
        return response

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
        return start_session(JSONResponse({"user": public_user(user)}), request, user["id"])

    @app.post("/api/auth/logout")
    def logout(request: Request, user: CurrentUser = Depends(current_user)):
        token = request.cookies.get("leo_session", "")
        auth.delete_session(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie("leo_session")
        return response

    def account(user):
        if user.id == "system":
            raise HTTPException(403, "Use a user session")
        db_user = store.get_user(user.id)
        if not db_user:
            raise HTTPException(401, "User not found")
        return db_user

    @app.get("/api/auth/me", response_model=Me)
    def me(user: CurrentUser = Depends(current_user)):
        db_user = account(user)
        return {**public_user(db_user), "identities": [i["provider"] for i in store.list_identities(user.id)],
                "has_password": bool(db_user["password_hash"])}

    @app.put("/api/auth/password")
    def change_password(body: PasswordChange, user: CurrentUser = Depends(current_user)):
        db_user = account(user)
        if db_user["password_hash"]:
            if not body.current_password:
                raise HTTPException(400, "Enter your current password")
            if not Auth.verify_password(body.current_password, db_user["password_hash"]):
                raise HTTPException(403, "Your current password is not correct")
        store.update_user(user.id, password_hash=Auth.hash_password(body.new_password))
        return {"ok": True}

    @app.delete("/api/auth/identities/{provider}")
    def unlink_identity(provider: str, user: CurrentUser = Depends(current_user)):
        db_user = account(user)
        linked = [i["provider"] for i in store.list_identities(user.id)]
        if provider not in linked:
            raise HTTPException(404, "That sign-in method is not linked")
        if not db_user["password_hash"] and len(linked) == 1:
            raise HTTPException(409, "Set a password or link another account first, so you can still sign in")
        store.unlink_identity(user.id, provider)
        return {"ok": True}

    # ---------------- OAuth sign-in ----------------

    @app.get("/api/auth/providers")
    def auth_providers():
        """What the login page offers: the configured OAuth providers, and whether the password form can register."""
        return {"providers": oauth.available(),
                "password_registration": config.allow_registration or (not config.api_token and not store.has_users())}

    def callback_url(request, provider):
        return f"{config.public_url or str(request.base_url).rstrip('/')}/api/auth/oauth/{provider}/callback"

    def safe_next(value):
        # Paths on this site only: "//host" and "/\\host" would leave it.
        return value if value.startswith("/") and not value.startswith(("//", "/\\")) else "/"

    def navigate(target, cookie=("leo_oauth", "/api/auth/oauth")):
        """Leave the callback through a page that navigates itself. After a cross-site redirect from the provider the
        browser holds back SameSite=Strict cookies; a navigation our own page starts sends them."""
        script = json.dumps(target).replace("<", "\\u003c")
        response = HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={html.escape(target)}">'
                                f'<title>Signing in to L.E.O</title><script>location.replace({script})</script>',
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.delete_cookie(cookie[0], path=cookie[1])
        return response

    def fill_profile(user_id, identity):
        """Fill in what the account is missing; the email only when no other account has it."""
        user = store.get_user(user_id)
        values = {key: value for key, value in (("display_name", identity.name), ("avatar_url", identity.avatar_url)) if value and not user[key]}
        if identity.email and not user["email"] and not store.get_user_by_email(identity.email):
            values["email"] = identity.email
        if values:
            store.update_user(user_id, **values)

    def oauth_user(identity):
        """The account this identity signs in to, linked by verified email or created. None: not invited."""
        rules = config.allowed_emails
        if rules and not email_allowed(identity.email, rules):
            return None
        linked = store.find_identity(identity.provider, identity.subject)
        user = store.get_user(linked["user_id"]) if linked else None
        if not user and identity.email:
            user = store.get_user_by_email(identity.email)
            if user:
                store.link_identity(user["id"], identity.provider, identity.subject, identity.email)
        if not user:
            # Without a list, OAuth creates only the very first account, on the same terms as password registration.
            if not rules and (store.has_users() or config.api_token):
                return None
            wanted = identity.login or (identity.email or "").split("@")[0] or identity.name
            user = store.create_user(store.free_username(wanted), "", email=identity.email,
                                     display_name=identity.name or None, avatar_url=identity.avatar_url or None)
            store.link_identity(user["id"], identity.provider, identity.subject, identity.email)
            if store.admin_user_id() == user["id"]:
                store.adopt_orphans(user["id"])  # Connections and chats saved before accounts existed.
        fill_profile(user["id"], identity)
        return user

    def require_provider(provider):
        if provider not in oauth.providers:
            raise HTTPException(404, "That sign-in provider is not configured")

    @app.get("/api/auth/oauth/{provider}/start")
    async def oauth_start(provider: str, request: Request, next_path: str = Query("", alias="next", max_length=500), link: bool = False):
        require_provider(provider)
        linking = None
        if link:
            user = await run_in_threadpool(resolve_user, request)
            if not user or user.id == "system":
                raise HTTPException(401, "Sign in first, then link another account")
            linking = user.id
        state, (verifier, challenge) = secrets.token_urlsafe(24), pkce_pair()
        try:
            url = await oauth.authorize_url(provider, callback_url(request, provider), state, challenge)
        except OAuthError as exc:
            return navigate(f"/login?{urlencode({'error': exc.code})}")
        pending = {"provider": provider, "state": state, "verifier": verifier, "link": linking,
                   "next": safe_next(next_path or ("/?settings=account" if linking else "/"))}
        response = RedirectResponse(url, status_code=307)
        # Encrypted with the store key: nothing to keep on the server, and nothing a script or another site can read or forge.
        response.set_cookie("leo_oauth", store.cipher.encrypt(json.dumps(pending).encode()).decode(), max_age=600, httponly=True,
                            samesite="lax", secure=request.url.scheme == "https", path="/api/auth/oauth")
        return response

    @app.get("/api/auth/oauth/{provider}/callback")
    async def oauth_callback(provider: str, request: Request, code: str = Query("", max_length=4096),
                             state: str = Query("", max_length=200), error: str = Query("", max_length=200)):
        require_provider(provider)
        try:
            pending = json.loads(store.cipher.decrypt(request.cookies.get("leo_oauth", "").encode(), ttl=600))
        except (InvalidToken, ValueError):
            pending = None
        if not pending or pending["provider"] != provider or not state or not secrets.compare_digest(pending["state"], state):
            return navigate("/login?error=failed")
        back = pending["next"] if pending["link"] else "/login"

        def fail(reason, **extra):
            return navigate(f"{back}{'&' if '?' in back else '?'}{urlencode({'error': reason, **extra})}")

        if error or not code:
            return fail("cancelled")
        throttle = f"{request.client.host if request.client else ''}|oauth"
        if auth.login_blocked(throttle):
            return fail("failed")
        try:
            identity = await oauth.identity(provider, code, pending["verifier"], callback_url(request, provider))
        except OAuthError as exc:
            auth.record_failure(throttle)
            return fail(exc.code)
        if pending["link"]:
            current = await run_in_threadpool(resolve_user, request)
            if not current or current.id != pending["link"]:
                return navigate("/login?error=failed")
            owner = store.find_identity(provider, identity.subject)
            if owner and owner["user_id"] != current.id:
                return fail("linked_elsewhere")
            if not owner:
                store.link_identity(current.id, provider, identity.subject, identity.email)
            fill_profile(current.id, identity)
            return navigate(pending["next"])
        user = await run_in_threadpool(oauth_user, identity)
        if not user:
            auth.record_failure(throttle)
            return fail("not_invited", **({"email": identity.email} if identity.email else {}))
        auth.clear_failures(throttle)
        return start_session(navigate(pending["next"]), request, user["id"])

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
        hint = "Prefer Sign in with an account (Settings → Account); this form is for a 9Router you run yourself."
        return [{**p, "hint": hint} if p["id"] == "9router" and signins else p for p in PRESETS]

    def require_router():
        if not signins:
            raise HTTPException(503, "Model connections need the local router (./setup.sh then ./start.sh) or the Docker sandbox service")
        return signins

    @app.get("/api/accounts")
    @app.get(CLI_ACCOUNTS_PREFIX)
    async def read_accounts(user: CurrentUser = Depends(current_user)):
        """Providers offered for sign-in and the accounts this user signed in on their own router."""
        offered = [{"id": key, "name": v["name"], "flow": v["flow"], "risk": RISK_NOTICE if v["risk"] else None} for key, v in ACCOUNTS.items()]
        key_providers = [{"id": key, "name": v["name"], "prefix": v["prefix"]} for key, v in KEY_PROVIDERS.items()]
        catalog = {"key_providers": key_providers, "provider_prefixes": {key: v["prefix"] for key, v in MODEL_PROVIDERS.items()}}
        if not signins:
            return {"available": False, "error": "Model connections need the local router (./setup.sh then ./start.sh) or the Docker sandbox service",
                    "providers": offered, "accounts": [], "router_status": "unavailable", **catalog}
        try:
            uid = account(user)["id"]
            router = await router_for(uid)
            try:
                accounts = await router.accounts() if router else []
            except RouterUnavailable:
                # Compose can remove the container while its saved address and account volume survive.
                router = await router_for(uid, create=True)
                accounts = await router.accounts()
            return {"available": True, "error": None, "providers": offered, "accounts": accounts,
                    "router_status": "connected" if router else "ready", **catalog}
        except (RouterError, SandboxError) as exc:
            return {"available": False, "error": str(exc), "providers": offered, "accounts": [], "router_status": "unavailable", **catalog}

    @app.post("/api/model-providers/connect", status_code=201)
    @app.post(CLI_MODEL_PROVIDERS_PREFIX + "connect", status_code=201)
    async def connect_model_provider(body: ModelProviderKey, user: CurrentUser = Depends(current_user)):
        require_router()
        uid = account(user)["id"]
        if body.provider not in KEY_PROVIDERS:
            raise HTTPException(400, "Choose a supported API-key provider")
        try:
            async with model_connections.lock(uid):
                router = await router_for(uid, create=True)
                await router.connect_api_key(body.provider, body.api_key)
            return {"provider": body.provider, "connected": True}
        except (RouterError, SandboxError) as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/model-providers/{provider}/models")
    @app.get(CLI_MODEL_PROVIDERS_PREFIX + "{provider}/models")
    async def provider_models(provider: str, user: CurrentUser = Depends(current_user)):
        require_router()
        if provider not in MODEL_PROVIDERS:
            raise HTTPException(404, "Provider not found")
        try:
            return await model_connections.catalog(account(user)["id"], provider)
        except (RouterError, SandboxError, ProviderError) as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/model-providers/{provider}/models", status_code=201)
    @app.post(CLI_MODEL_PROVIDERS_PREFIX + "{provider}/models", status_code=201)
    async def select_provider_models(provider: str, body: ModelSelection, user: CurrentUser = Depends(current_user)):
        require_router()
        if provider not in MODEL_PROVIDERS:
            raise HTTPException(404, "Provider not found")
        try:
            return await model_connections.add(account(user)["id"], provider, body.models)
        except (RouterError, SandboxError, ProviderError) as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/model-providers/migrate")
    async def migrate_model_providers(user: CurrentUser = Depends(current_user)):
        require_router()
        try:
            return {"moved": await model_connections.migrate(account(user)["id"])}
        except (RouterError, SandboxError) as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/accounts/signin", status_code=201)
    @app.post(CLI_ACCOUNTS_PREFIX + "/signin", status_code=201)
    async def start_sign_in(body: AccountSignIn, user: CurrentUser = Depends(current_user)):
        flows = require_router()
        if body.provider not in ACCOUNTS:
            raise HTTPException(400, "L.E.O. does not offer sign-in for that provider")
        if ACCOUNTS[body.provider]["risk"] and not body.accept_risk:
            raise HTTPException(400, RISK_NOTICE)
        try:
            return await flows.start(body.provider, account(user)["id"])
        except RouterError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/accounts/signin/{session_id}")
    @app.get(CLI_ACCOUNTS_PREFIX + "/signin/{session_id}")
    async def sign_in_status(session_id: str, user: CurrentUser = Depends(current_user)):
        try:
            return await require_router().status(session_id, user.id)
        except RouterError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.delete("/api/accounts/signin/{session_id}")
    @app.delete(CLI_ACCOUNTS_PREFIX + "/signin/{session_id}")
    def cancel_sign_in(session_id: str, user: CurrentUser = Depends(current_user)):
        require_router().cancel(session_id, user.id)
        return {"ok": True}

    @app.delete("/api/accounts/{account_id}")
    async def disconnect_account(account_id: str, user: CurrentUser = Depends(current_user)):
        """Sign the account out of the user's router; its models leave L.E.O. once no account of that provider is left,
        and the router stops once no account at all is left."""
        require_router()
        router = await router_for(account(user)["id"])
        try:
            accounts = await router.accounts() if router else []
            found = next((a for a in accounts if a["id"] == account_id), None)
            if not found:
                raise HTTPException(404, "Account not found")
            await router.remove(account_id)
        except RouterError as exc:
            raise HTTPException(502, str(exc)) from exc
        removed = 0
        if not any(a["provider"] == found["provider"] and a["id"] != account_id for a in accounts):
            prefix = MODEL_PROVIDERS[found["provider"]]["prefix"] + "/"
            for c in store.list_connections(user.id):
                if c["base_url"] == f"{router.url}/v1" and c["model"].startswith(prefix):
                    store.delete_connection(c["id"], user.id)
                    removed += 1
        if len(accounts) == 1:
            await router_manager.delete_router(user.id)
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

    AGENT_MOVED = "Agent work is moving to L.E.O.C.O.D.E; set LEO_SERVER_AGENT=1 to run it on this server"

    def require_sandbox():
        if not config.server_agent:
            raise HTTPException(403, AGENT_MOVED)
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
        report = store.usage_report(conversation_id, user_id=uid)
        if uid and not conversation_id:
            report["cli"] = store.cli_usage_report(uid)  # What L.E.O.C.O.D.E devices reported: totals only, no prompts or code.
        return report

    # ---------------- L.E.O.C.O.D.E: device login, sync, usage ----------------

    USER_CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # No 0/O or 1/I to misread.

    def code_digest(code):
        return hashlib.sha256(code.encode()).hexdigest()

    @app.post("/api/cli/device")
    def cli_device_start(body: CliDeviceStart, request: Request):
        """A CLI starts signing in: it shows the user code and polls with the device code."""
        throttle = f"{request.client.host if request.client else ''}|cli-device"
        if auth.login_blocked(throttle):
            raise HTTPException(429, "Too many sign-in attempts; try again in a few minutes")
        auth.record_failure(throttle)  # Counts every attempt, so codes cannot be requested in bulk.
        device_code = secrets.token_urlsafe(32)
        letters = "".join(secrets.choice(USER_CODE_LETTERS) for _ in range(8))
        user_code = f"{letters[:4]}-{letters[4:]}"
        store.create_cli_login(code_digest(device_code), user_code, body.device_name.strip(), time.time() + 600)
        base = config.public_url or str(request.base_url).rstrip("/")
        return {"device_code": device_code, "user_code": user_code, "verification_url": f"{base}/cli?{urlencode({'code': user_code})}",
                "interval": 3, "expires_in": 600}

    def pending_login(user_code):
        login = store.cli_login(user_code=user_code.strip().upper())
        if not login or login["expires_at"] < time.time():
            raise HTTPException(404, "This code has expired or does not exist; run leocode login again")
        return login

    @app.get("/api/cli/device/{user_code}")
    def cli_device_read(user_code: str, user: CurrentUser = Depends(current_user)):
        account(user)
        login = pending_login(user_code)
        return {"device_name": login["device_name"], "status": login["status"], "expires_at": login["expires_at"]}

    @app.post("/api/cli/device/{user_code}")
    def cli_device_answer(user_code: str, body: CliAnswer, user: CurrentUser = Depends(current_user)):
        """The signed-in user allows or denies the device; allowing registers it and readies its token."""
        uid = account(user)["id"]
        login = pending_login(user_code)
        if not store.answer_cli_login(login["user_code"], uid, body.approve, secrets.token_urlsafe(32) if body.approve else None):
            raise HTTPException(409, "This code was already answered")
        return {"ok": True, "device_name": login["device_name"]}

    @app.post("/api/cli/token")
    def cli_token(body: CliTokenInput):
        login = store.cli_login(device_hash=code_digest(body.device_code))
        if not login:
            return {"status": "expired"}
        if login["status"] == "denied":
            return {"status": "denied"}
        if login["status"] == "pending":
            return {"status": "expired" if login["expires_at"] < time.time() else "pending"}
        token = store.take_cli_token(login["id"])
        return {"status": "approved", "token": token} if token else {"status": "expired"}

    @app.get("/api/cli/sync")
    def cli_sync(request: Request):
        """Everything the CLI needs to route on the laptop: models with their keys, preferences, and the skill catalog."""
        user, device = request.state.user, request.state.device
        router = store.get_router(user.id)
        models, relayed = [], []
        for c in store.list_connections(user.id, private=True):
            if not c["enabled"]:
                continue
            fields = {k: v for k, v in c.items() if k not in ("secret", "has_key", "cooldown_until")}
            if router and c["base_url"].startswith(router["url"]):
                # Per-user 9Router: reachable from the server only, so the CLI calls it through /api/cli/relay/<id>,
                # with its device token instead of the router key, which never leaves the server.
                relayed.append({k: v for k, v in fields.items() if k != "base_url"})
                continue
            models.append({**fields, "api_key": store.decrypt(c) or None})
        # Chat connectors, for the CLI's /mcp and its agent; their sign-ins and tokens stay on the server, which makes
        # each tool call (CLI_CONNECTOR_PREFIX).
        listed = []
        for c in store.list_connectors(user.id):
            shown = public_connector(c)
            listed.append({key: shown[key] for key in ("id", "name", "url", "enabled", "auth", "status", "error", "signed_in", "tools",
                                                       "permissions")})
        return {"device": {"id": device["id"], "name": device["name"]}, "user": {"username": user.username},
                "connections": models, "relayed": relayed, "chat_only": [], "preferences": store.preferences(), "skills": store.list_skills(),
                "connectors": listed}

    @app.api_route(CLI_RELAY_PREFIX + "{connection_id}/{path:path}", methods=["GET", "POST"])
    async def cli_relay(connection_id: str, path: str, request: Request):
        """Forward one model call from the CLI to the user's own 9Router, streaming the answer back. Nothing is stored."""
        uid = request.state.user.id
        if path not in RELAY_PATHS:
            raise HTTPException(404, "Only model calls are relayed")
        router = store.get_router(uid)
        c = store.get_connection(connection_id, uid, private=True)
        if not c or not c["enabled"] or not router or not c["base_url"].startswith(router["url"]):
            raise HTTPException(404, "This model is not on your 9Router; run leocode sync")
        body = await request.body()
        if len(body) > MAX_RELAY_BYTES:
            raise HTTPException(413, "The request is too large to relay")
        client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), trust_env=False, transport=relay_transport)
        headers = {"Authorization": f"Bearer {store.decrypt(c)}", "Content-Type": request.headers.get("content-type", "application/json"),
                   "Accept": request.headers.get("accept", "*/*")}

        async def send():
            return await client.send(client.build_request(request.method, f"{c['base_url'].rstrip('/')}/{path}", content=body or None,
                                                          headers=headers), stream=True)

        try:
            try:
                upstream = await send()
            except httpx.ConnectError:
                # Nothing listens: the router container stopped (Docker restarted, or it crashed). Start it, then retry while it boots.
                if not router_manager:
                    raise
                await router_manager.ensure_router(uid, router["password"])
                deadline = time.monotonic() + ROUTER_BOOT_SECONDS
                while True:
                    try:
                        upstream = await send()
                        break
                    except httpx.ConnectError:
                        if time.monotonic() > deadline:
                            raise
                        await asyncio.sleep(1)
        except (SandboxError, RouterError) as exc:
            await client.aclose()
            raise HTTPException(502, f"{ROUTER_UNREACHABLE}, and starting it again failed: {exc}") from exc
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(502, ROUTER_UNREACHABLE) from exc

        async def answer():
            try:
                async for chunk in upstream.aiter_bytes():  # Decoded: Content-Encoding is not passed on.
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(answer(), status_code=upstream.status_code, media_type=upstream.headers.get("content-type"))

    class ConnectorCall(BaseModel):
        tool: str = Field(min_length=1, max_length=128)
        arguments: dict = Field(default_factory=dict)

    @app.post(CLI_CONNECTOR_PREFIX + "{connector_id}/call")
    async def cli_connector_call(connector_id: str, body: ConnectorCall, request: Request):
        """Run one tool of the user's Chat connector for the CLI, which asked the user first when the tool needs it."""
        found = store.get_connector(connector_id, request.state.user.id)
        if not found or not found.get("enabled", True):
            raise HTTPException(404, "This connector is not in L.E.O Chat any more; run leocode sync")
        if not any(tool["name"] == body.tool for tool in found.get("tools") or []) or (found.get("permissions") or {}).get(body.tool) == "deny":
            raise HTTPException(403, "This tool is not allowed in L.E.O Chat's connector settings")
        try:
            return await connectors.call(found, body.tool, body.arguments)
        except NeedsAuth as exc:
            raise HTTPException(409, "Sign in to this connector again in L.E.O Chat → Settings → Connectors") from exc
        except MCPError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/cli/connections", status_code=201)
    def cli_add_connection(body: Connection, request: Request):
        """A model added from the CLI is saved on the account, like one added in Settings → Models."""
        saved = store.save_connection(body.model_dump(), user_id=request.state.user.id)
        return {k: v for k, v in saved.items() if k != "secret"}

    @app.post("/api/cli/connections/rerank")
    def cli_rerank_connections(request: Request):
        """Rank models signed in through the user's own router again with today's rules; they were ranked when they were added.
        Models the user added another way, which they may have ranked themselves, are left alone."""
        uid, changed = request.state.user.id, 0
        router = store.get_router(uid)
        for c in store.list_connections(uid) if router else []:
            if c["base_url"].startswith(router["url"]):
                tier = profile(c["model"])["tier"]
                if tier != c["tier"]:
                    # Say that a strong model can call tools, or known ones would stay ahead of it; newer versions go first.
                    version = re.search(r"gpt-(\d+(?:\.\d+)?)", c["model"])
                    extra = {"tools": True, "priority": max(0, 9 - round((float(version.group(1)) - 5) * 5))} if tier == "strong" and version else {}
                    store.rerank(c["id"], tier=tier, **extra)
                    changed += 1
        return {"changed": changed}

    @app.post("/api/cli/usage")
    def cli_usage(body: CliUsageInput, request: Request):
        store.add_cli_usage(request.state.user.id, request.state.device["id"], [e.model_dump() for e in body.entries])
        return {"ok": True, "received": len(body.entries)}

    @app.post("/api/cli/logout")
    def cli_logout(request: Request):
        store.revoke_cli_device(request.state.device["id"], request.state.user.id)
        return {"ok": True}

    @app.get("/api/cli/devices")
    def cli_devices(user: CurrentUser = Depends(current_user)):
        return store.list_cli_devices(account(user)["id"])

    @app.delete("/api/cli/devices/{device_id}")
    def cli_revoke(device_id: str, user: CurrentUser = Depends(current_user)):
        store.revoke_cli_device(device_id, account(user)["id"])
        return {"ok": True}

    # ---------------- connectors (remote MCP servers) ----------------

    def own_connector(connector_id, user):
        found = store.get_connector(connector_id, account(user)["id"])
        if not found:
            raise HTTPException(404, "Connector not found")
        return found

    @app.get("/api/connectors")
    def list_connectors(user: CurrentUser = Depends(current_user)):
        return [public_connector(c) for c in store.list_connectors(account(user)["id"])]

    @app.post("/api/connectors", status_code=201)
    async def add_connector(body: ConnectorInput, user: CurrentUser = Depends(current_user)):
        url = body.url.strip()
        try:
            await check_url(url, config.mcp_allow_private)
        except MCPError as exc:
            raise HTTPException(400, str(exc)) from exc
        secret = {"token": body.token.strip()} if body.auth == "token" and body.token else {}
        if body.auth == "oauth" and body.client_id:
            secret["oauth"] = {"client": {"client_id": body.client_id.strip(), "client_secret": (body.client_secret or "").strip() or None}}
        found = store.create_connector(account(user)["id"], {"name": body.name.strip(), "url": url, "auth": body.auth, "enabled": True,
                                                            "status": "sign_in" if body.auth == "oauth" else "new", "error": None,
                                                            "tools": [], "permissions": {}}, secret or None)
        if body.auth != "oauth":
            try:
                found = await connectors.refresh_tools(found)
            except MCPError:
                found = store.get_connector(found["id"], found["user_id"])  # Saved with the error; fix it in Settings.
        return public_connector(found)

    @app.put("/api/connectors/{connector_id}")
    def edit_connector(connector_id: str, body: ConnectorUpdate, user: CurrentUser = Depends(current_user)):
        found = own_connector(connector_id, user)
        values = {key: value for key, value in (("name", body.name and body.name.strip()), ("enabled", body.enabled)) if value is not None}
        if body.permissions is not None:
            known = {tool["name"] for tool in found.get("tools") or []}
            if set(body.permissions) - known:
                raise HTTPException(400, "Unknown tool: " + ", ".join(sorted(set(body.permissions) - known))[:200])
            values["permissions"] = {**(found.get("permissions") or {}), **body.permissions}
        secret = None
        if body.token is not None and found.get("auth") == "token":
            secret = {**found["secret"], "token": body.token.strip()}
        if body.client_id is not None and found.get("auth") == "oauth":
            secret = {**found["secret"], "oauth": {"client": {"client_id": body.client_id.strip(), "client_secret": (body.client_secret or "").strip() or None}}}
            values.update(status="sign_in", needs_client=False)
        return public_connector(store.update_connector(connector_id, found["user_id"], config=values, secret=secret))

    @app.post("/api/connectors/{connector_id}/refresh")
    async def refresh_connector(connector_id: str, user: CurrentUser = Depends(current_user)):
        found = own_connector(connector_id, user)
        try:
            return public_connector(await connectors.refresh_tools(found))
        except NeedsAuth as exc:
            raise HTTPException(409, "Sign in to this connector again" if found.get("auth") == "oauth" else "The connector refused the token") from exc
        except MCPError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.delete("/api/connectors/{connector_id}")
    def remove_connector(connector_id: str, user: CurrentUser = Depends(current_user)):
        found = own_connector(connector_id, user)
        store.delete_connector(connector_id, found["user_id"])
        return {"ok": True}

    def connector_callback(request):
        return f"{config.public_url or str(request.base_url).rstrip('/')}/api/connectors/oauth/callback"

    MCP_COOKIE = ("leo_mcp_oauth", "/api/connectors")

    @app.get("/api/connectors/{connector_id}/oauth/start")
    async def connector_sign_in(connector_id: str, request: Request, user: CurrentUser = Depends(current_user)):
        """Send the browser to the connector's sign-in page (registering L.E.O. there first if needed)."""
        found = own_connector(connector_id, user)
        if found.get("auth") != "oauth":
            raise HTTPException(400, "This connector does not use a sign-in")
        oauth_state = (found.get("secret") or {}).get("oauth") or {}
        try:
            await check_url(found["url"], config.mcp_allow_private)
            challenge = ""
            try:  # The connector's 401 names where its sign-in metadata lives.
                async with MCPClient(found["url"], transport=mcp_transport).session():
                    pass
            except NeedsAuth as exc:
                challenge = exc.challenge
            except MCPError:
                pass
            meta = await mcp_auth.discover(found["url"], challenge, mcp_transport, config.mcp_allow_private)
            client = oauth_state.get("client") or await mcp_auth.register(meta, connector_callback(request), mcp_transport)
        except MCPError as exc:
            needs_client = "paste a client ID" in str(exc)
            store.update_connector(found["id"], found["user_id"], config={"status": "sign_in", "error": str(exc), "needs_client": needs_client})
            return navigate(f"/?{urlencode({'settings': 'connectors', 'error': 'needs_client' if needs_client else 'failed'})}", MCP_COOKIE)
        store.update_connector(found["id"], found["user_id"], secret={**found["secret"], "oauth": {**oauth_state, "meta": meta, "client": client}})
        state, (verifier, code_challenge) = secrets.token_urlsafe(24), pkce_pair()
        response = RedirectResponse(mcp_auth.authorize_url(meta, client, connector_callback(request), state, code_challenge), status_code=307)
        pending = {"state": state, "verifier": verifier, "connector": found["id"], "user": found["user_id"]}
        response.set_cookie(MCP_COOKIE[0], store.cipher.encrypt(json.dumps(pending).encode()).decode(), max_age=600, httponly=True,
                            samesite="lax", secure=request.url.scheme == "https", path=MCP_COOKIE[1])
        return response

    @app.get("/api/connectors/oauth/callback")
    async def connector_signed_in(request: Request, code: str = Query("", max_length=4096), state: str = Query("", max_length=200),
                                  error: str = Query("", max_length=200)):
        try:
            pending = json.loads(store.cipher.decrypt(request.cookies.get(MCP_COOKIE[0], "").encode(), ttl=600))
        except (InvalidToken, ValueError):
            pending = None
        found = store.get_connector(pending["connector"], pending["user"]) if pending else None
        if not found or not state or not secrets.compare_digest(pending["state"], state):
            return navigate("/?settings=connectors&error=failed", MCP_COOKIE)
        if error or not code:
            return navigate("/?settings=connectors&error=cancelled", MCP_COOKIE)
        oauth_state = found["secret"].get("oauth") or {}
        try:
            tokens = await mcp_auth.exchange(oauth_state["meta"], oauth_state["client"], code, pending["verifier"], connector_callback(request), mcp_transport)
            found = store.update_connector(found["id"], found["user_id"], secret={**found["secret"], "oauth": {**oauth_state, "tokens": tokens}},
                                           config={"status": "connected", "error": None})
            await connectors.refresh_tools(found)
        except (MCPError, KeyError) as exc:
            store.update_connector(found["id"], found["user_id"], config={"status": "sign_in", "error": str(exc)[:300]})
            return navigate("/?settings=connectors&error=failed", MCP_COOKIE)
        return navigate(f"/?{urlencode({'settings': 'connectors', 'connected': found['id']})}", MCP_COOKIE)

    @app.post("/api/runs", status_code=202)
    def create_run(body: RunInput, user: CurrentUser = Depends(current_user)):
        if body.mode == "agent":
            require_sandbox()
        elif body.conversation_id and not config.server_agent and store.get_workspace(body.conversation_id, user.owner_id):
            raise HTTPException(409, f"This is an agent chat. {AGENT_MOVED}, or start a new chat.")
        if body.connectors and any(not store.get_connector(c, user.id) for c in body.connectors):
            raise HTTPException(400, "Unknown connector")
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
        """Answer a run's pending approval (a connector tool call, a git push, or more budget); the run then resumes."""
        run = store.get_run(run_id, user.owner_id, private=True)
        pending = ((run or {}).get("state") or {}).get("pending_approval") or {}
        store.resolve_approval(run_id, body.approval_id, body.approved, user.owner_id)
        detail = pending.get("detail") or {}
        if body.remember and body.approved and pending.get("id") == body.approval_id and pending.get("kind") == "mcp_tool":
            found = store.get_connector(detail.get("connector_id", ""), run["user_id"])
            if found:  # Always allow: this tool runs without asking from now on.
                store.update_connector(found["id"], found["user_id"], config={"permissions": {**(found.get("permissions") or {}), detail["tool"]: "allow"}})
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
