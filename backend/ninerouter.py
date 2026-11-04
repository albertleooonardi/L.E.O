"""Account sign-in through 9Router: L.E.O. is the screen, 9Router keeps and refreshes the account tokens.

Every user has their own 9Router, which sandboxd starts on their first sign-in (pinned image, no host port), because a
9Router key reaches every account signed in on that router. It serves those accounts' models on an OpenAI-compatible
/v1 API. L.E.O. drives its dashboard API to start sign-ins and polls device-code logins. It also catches the ChatGPT
(Codex) browser redirect itself: OpenAI only accepts http://localhost:1455/auth/callback, and the browser cannot reach
9Router's container.
"""
import asyncio
import html
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx

# Sign-in providers offered in Settings, keyed by 9Router's provider ID; `prefix` is its model ID prefix.
# Claude subscription sign-in is deliberately absent: Anthropic only permits those tokens in its own apps.
# `risk`: 9Router warns these providers do not license subscription sign-in for router use.
ACCOUNTS = {
    "codex": {"name": "ChatGPT (Codex)", "prefix": "cx", "flow": "redirect", "risk": True},
    "github": {"name": "GitHub Copilot", "prefix": "gh", "flow": "device", "risk": True},
    "kiro": {"name": "Kiro", "prefix": "kr", "flow": "device", "risk": True},
    "kimi": {"name": "Kimi", "prefix": "kimi", "flow": "device", "risk": False},
    "kilocode": {"name": "Kilo Code", "prefix": "kc", "flow": "device", "risk": False},
    "qoder": {"name": "Qoder", "prefix": "qd", "flow": "device", "risk": False},
    "grok-cli": {"name": "Grok CLI", "prefix": "gcli", "flow": "device", "risk": False},
    "glm": {"name": "Z.ai GLM Coding", "prefix": "glm", "flow": "device", "risk": False},
}
RISK_NOTICE = ("This provider does not license its subscription sign-in for router use, so the account may be restricted "
               "or banned (9Router's own warning). Continue only if you accept that risk.")
CODEX_REDIRECT = "http://localhost:1455/auth/callback"
SIGN_IN_SECONDS = 900


class RouterError(Exception):
    pass


class NineRouter:
    """9Router dashboard API client. Signs in with the shared password, and again when the session lapses."""

    def __init__(self, url, password, transport=None):
        self.url, self.password, self.transport = url.rstrip("/"), password, transport
        self.cookies = httpx.Cookies()

    async def request(self, method, path, **kwargs):
        response = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(base_url=self.url, timeout=30, transport=self.transport, trust_env=False,
                                             follow_redirects=False, cookies=self.cookies) as client:
                    if attempt or not self.cookies:
                        login = await client.post("/api/auth/login", json={"password": self.password})
                        if login.status_code != 200 or not login.json().get("success"):
                            raise RouterError("L.E.O. could not sign in to your model router; its saved password no longer matches")
                        self.cookies = httpx.Cookies(client.cookies)
                    response = await client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise RouterError("Your model router is not reachable; check that the sandboxd service is running") from exc
            except ValueError as exc:
                raise RouterError("9Router returned an unreadable response") from exc
            if response.status_code != 401:
                break
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code == 404:
            raise RouterError("This 9Router version does not offer that step; L.E.O. may need an update for it")
        if not response.is_success:
            detail = data.get("error") if isinstance(data, dict) else None
            raise RouterError(f"9Router: {detail if isinstance(detail, str) else f'HTTP {response.status_code}'}"[:240])
        return data if isinstance(data, dict) else {}

    async def wait_ready(self, seconds=60):
        """Wait for a router that was just started to answer."""
        deadline = time.monotonic() + seconds
        async with httpx.AsyncClient(base_url=self.url, timeout=5, transport=self.transport, trust_env=False) as client:
            while True:
                try:
                    if (await client.get("/api/health")).is_success:
                        return
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RouterError("Your model router did not start in time; try again in a minute")
                await asyncio.sleep(1)

    async def accounts(self):
        """Signed-in accounts of the providers L.E.O. offers."""
        found = (await self.request("GET", "/api/providers")).get("connections") or []
        return [{"id": a["id"], "provider": a["provider"], "name": ACCOUNTS[a["provider"]]["name"],
                 "label": a.get("email") or a.get("displayName") or a.get("name") or ""}
                for a in found if isinstance(a, dict) and a.get("id") and a.get("provider") in ACCOUNTS]

    async def remove(self, account_id):
        await self.request("DELETE", f"/api/providers/{account_id}")

    async def create_key(self):
        key = (await self.request("POST", "/api/keys", json={"name": "L.E.O."})).get("key")
        if not isinstance(key, str) or not key:
            raise RouterError("9Router did not return an API key")
        return key


class SignIns:
    """Sign-ins in progress, held in memory by the API process. `router_for(user_id, create=False)` returns that user's
    NineRouter; `on_connected(provider, user_id, account)` adds the account's models to L.E.O. and returns a summary."""

    def __init__(self, router_for, on_connected):
        self.router_for, self.on_connected, self.sessions = router_for, on_connected, {}

    def public(self, session):
        return {key: session.get(key) for key in ("id", "provider", "status", "error", "url", "user_code", "result")}

    async def start(self, provider, user_id):
        spec = ACCOUNTS[provider]
        self.sessions = {k: s for k, s in self.sessions.items() if time.time() - s["created"] < SIGN_IN_SECONDS}
        router = await self.router_for(user_id, create=True)
        if spec["flow"] == "device":
            data = await router.request("GET", f"/api/oauth/{provider}/device-code")
            session = {"device_code": data.get("device_code"), "code_verifier": data.get("codeVerifier"), "user_code": data.get("user_code"),
                       "url": data.get("verification_uri_complete") or data.get("verification_uri"),
                       # Some providers (Kiro) hand back client details the poll must return.
                       "extra": {k: v for k, v in data.items() if k.startswith("_")} or None}
            complete = session["device_code"] and session["url"]
        else:
            data = await router.request("GET", f"/api/oauth/{provider}/authorize", params={"redirect_uri": CODEX_REDIRECT})
            session = {"state": data.get("state"), "code_verifier": data.get("codeVerifier"), "url": data.get("authUrl")}
            complete = session["state"] and session["code_verifier"] and session["url"]
        if not complete:
            raise RouterError("9Router returned an incomplete sign-in; L.E.O. may need an update for this 9Router version")
        sid = secrets.token_urlsafe(16)
        self.sessions[sid] = {**session, "id": sid, "provider": provider, "user_id": user_id, "status": "waiting",
                              "error": None, "result": None, "created": time.time()}
        return self.public(self.sessions[sid])

    async def status(self, sid, user_id):
        session = self.sessions.get(sid)
        if not session or session["user_id"] != user_id:
            raise KeyError(sid)
        if session["status"] == "waiting" and session.get("device_code"):
            router = await self.router_for(user_id)
            data = await router.request("POST", f"/api/oauth/{session['provider']}/poll", json={
                "deviceCode": session["device_code"], "codeVerifier": session["code_verifier"], "extraData": session["extra"]})
            if data.get("success"):
                await self.finish(session, data.get("connection") or {})
            elif not data.get("pending"):
                session.update(status="error", error=data.get("errorDescription") or data.get("error") or "Sign-in was not completed")
        if session["status"] == "waiting" and time.time() - session["created"] > SIGN_IN_SECONDS:
            session.update(status="error", error="Sign-in timed out; start again")
        return self.public(session)

    def cancel(self, sid, user_id):
        session = self.sessions.get(sid)
        if session and session["user_id"] == user_id:
            del self.sessions[sid]

    async def callback(self, query):
        """The browser redirect after a ChatGPT (Codex) sign-in. Returns (ok, message for the browser tab)."""
        state = query.get("state", "")
        session = next((s for s in self.sessions.values() if s.get("state") and state and secrets.compare_digest(s["state"], state)), None)
        if not session or session["status"] != "waiting":
            return False, "This sign-in is no longer active. Start again from L.E.O."
        if query.get("error") or not query.get("code"):
            session.update(status="error", error=query.get("error_description") or query.get("error") or "No sign-in code was returned")
            return False, session["error"]
        session["status"] = "finishing"  # A repeated redirect must not exchange the single-use code twice.
        try:
            router = await self.router_for(session["user_id"])
            data = await router.request("POST", f"/api/oauth/{session['provider']}/exchange", json={
                "code": query["code"], "redirectUri": CODEX_REDIRECT, "codeVerifier": session["code_verifier"], "state": state})
        except RouterError as exc:
            session.update(status="error", error=str(exc))
            return False, str(exc)
        await self.finish(session, data.get("connection") or {})
        if session["status"] != "done":
            return False, session["error"]
        return True, f"Signed in to {ACCOUNTS[session['provider']]['name']}. You can close this tab and return to L.E.O."

    async def finish(self, session, account):
        try:
            session.update(status="done", result=await self.on_connected(session["provider"], session["user_id"], account))
        except Exception as exc:  # The account is signed in either way; say what is left to do.
            session.update(status="error", error=f"Signed in, but adding the models failed: {exc}"[:300])


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>L.E.O. sign-in</title><style>body{{font:15px system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;
min-height:100vh;margin:0;padding:16px}}p{{max-width:420px;line-height:1.6;text-align:center}}</style></head>
<body><p>{message}</p>{script}</body></html>"""


async def serve_callback(signins, port, host="0.0.0.0"):
    """Minimal HTTP listener for the Codex redirect (GET /auth/callback only). Returns the asyncio server."""

    async def handle(reader, writer):
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                pass  # Headers carry nothing this page needs.
            parts = request_line.split(" ")
            target = urlsplit(parts[1]) if len(parts) >= 3 else None
            if parts[0] == "GET" and target and target.path == "/auth/callback":
                ok, message = await signins.callback({k: v[0] for k, v in parse_qs(target.query).items()})
                status = "200 OK"
            else:
                ok, message, status = False, "Not found", "404 Not Found"
            body = PAGE.format(message=html.escape(message), script="<script>setTimeout(() => window.close(), 1500)</script>" if ok else "").encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
                         "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, UnicodeError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
