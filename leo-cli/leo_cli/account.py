"""Signing in to a L.E.O. server, syncing models and keys into the local store, and reporting usage back.

The device token and the key that encrypts synced API keys live in the OS keychain (a mode-0600 file where there is
none). Everything else is in ~/.config/leo-cli: config.json and leo.db, the same Store the server uses, on SQLite.
"""
import asyncio
import json
import os
import platform
import socket
import time
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select

from backend.config import Config
from backend.schemas import Connection
from backend.store import Store, calls, runs

SERVICE = "leo-cli"
LOCAL_USER = "local"
# What the browser shows when the sign-in page sends it back to the terminal.
RETURN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>L.E.O.C.O.D.E</title><style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#151515;color:#ececec;
font:16px/1.5 system-ui,sans-serif}}main{{padding:24px;max-width:420px}}h1{{font-size:22px;margin:0 0 8px}}p{{margin:0;color:#a3a3a3}}</style>
</head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


class AccountError(Exception):
    pass


class SignedOut(AccountError):
    def __init__(self, message="This device is not signed in; run leocode login"):
        super().__init__(message)


def default_home():
    if os.getenv("LEO_CLI_HOME"):
        return Path(os.environ["LEO_CLI_HOME"])
    if platform.system() == "Windows" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / SERVICE
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / SERVICE


class Secrets:
    """The OS keychain, or a file only the user can read when there is no keychain (LEO_CLI_KEYRING=file forces it)."""

    def __init__(self, home):
        self.file = Path(home) / "secrets.json"
        self.keyring = None
        if os.getenv("LEO_CLI_KEYRING") != "file":
            try:
                import keyring
                from keyring.errors import NoKeyringError
                keyring.get_password(SERVICE, "probe")
                self.keyring = keyring
            except (ImportError, NoKeyringError, RuntimeError):
                self.keyring = None

    @property
    def where(self):
        return "the OS keychain" if self.keyring else f"{self.file} (no keychain found)"

    def _file(self):
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, name):
        return self.keyring.get_password(SERVICE, name) if self.keyring else self._file().get(name)

    def set(self, name, value):
        if self.keyring:
            self.keyring.set_password(SERVICE, name, value)
            return
        data = {**self._file(), name: value}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(data, handle)

    def delete(self, name):
        if self.keyring:
            try:
                self.keyring.delete_password(SERVICE, name)
            except Exception:  # Already gone.
                pass
        elif self.file.exists():
            data = self._file()
            data.pop(name, None)
            self.set_all(data)

    def set_all(self, data):
        descriptor = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(data, handle)


class Account:
    def __init__(self, home=None, transport=None):
        self.home = Path(home or default_home())
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secrets, self.transport, self._store = Secrets(self.home), transport, None

    # -- config ---------------------------------------------------------------

    @property
    def config_path(self):
        return self.home / "config.json"

    def config(self):
        try:
            return json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, **values):
        self.config_path.write_text(json.dumps({**self.config(), **values}, indent=2) + "\n")

    @property
    def server(self):
        return self.config().get("server")

    def client(self, token=None):
        if not self.server:
            raise SignedOut()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.AsyncClient(base_url=self.server, headers=headers, timeout=30, transport=self.transport)

    # -- local store ----------------------------------------------------------

    def store(self):
        """The local Store; synced API keys are encrypted with a key kept in the keychain."""
        if self._store is None:
            key = self.secrets.get("store-key")
            if not key:
                key = Fernet.generate_key().decode()
                self.secrets.set("store-key", key)
            self._store = Store(Config(data_dir=self.home, database_url=f"sqlite:///{self.home / 'leo.db'}"), encryption_key=key)
            if not self._store.get_user_by_username(LOCAL_USER):
                self._store.create_user(LOCAL_USER, "")
        return self._store

    @property
    def user_id(self):
        return self.store().get_user_by_username(LOCAL_USER)["id"]

    # -- sign in ----------------------------------------------------------------

    async def login(self, server, device_name=None, show=print, sleep=asyncio.sleep):
        """Browser sign-in: `show(user_code, url)` sends the user to the server's sign-in page; returns the sync summary.

        The page asks the user to sign in or register (password or OAuth) and to allow this device, then sends the
        browser back to a listener on 127.0.0.1 so the terminal carries on at once. The redirect carries no secret: the
        token is fetched with the device code, which never leaves this process, so polling alone also works (SSH, or
        a browser on another computer)."""
        server = server.rstrip("/")
        name = (device_name or f"{socket.gethostname()} ({platform.system()})")[:80]
        returned = asyncio.Event()
        listener = await self._listen(returned)
        try:
            async with httpx.AsyncClient(base_url=server, timeout=30, transport=self.transport) as http:
                try:
                    started = (await http.post("/api/cli/device", json={"device_name": name})).raise_for_status().json()
                except httpx.HTTPStatusError as exc:
                    raise AccountError(f"The server refused to start a sign-in (HTTP {exc.response.status_code})") from exc
                except httpx.HTTPError as exc:
                    raise AccountError(f"Cannot reach {server}: {type(exc).__name__}") from exc
                url = started["verification_url"]
                if listener:
                    url += f"&port={listener.sockets[0].getsockname()[1]}"
                show(started["user_code"], url)
                deadline = time.monotonic() + started["expires_in"]
                while time.monotonic() < deadline:
                    pause, back = asyncio.ensure_future(sleep(started["interval"])), asyncio.ensure_future(returned.wait())
                    await asyncio.wait({pause, back}, return_when=asyncio.FIRST_COMPLETED)
                    pause.cancel(), back.cancel()
                    returned.clear()
                    answer = (await http.post("/api/cli/token", json={"device_code": started["device_code"]})).json()
                    if answer["status"] == "approved":
                        break
                    if answer["status"] in ("denied", "expired"):
                        raise AccountError("The sign-in was denied" if answer["status"] == "denied" else "The sign-in expired; try again")
                else:
                    raise AccountError("The sign-in expired; try again")
        finally:
            if listener:
                listener.close()
        self.secrets.set("device-token", answer["token"])
        self.save(server=server, device_name=name)
        return await self.sync()

    async def _listen(self, returned):
        """A one-page HTTP listener on 127.0.0.1 for the browser's return; None if it cannot start (then polling only)."""
        async def answer(reader, writer):
            try:
                request = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
                denied = "status=denied" in request
                page = RETURN_PAGE.format(title="Sign-in denied" if denied else "L.E.O.C.O.D.E is signed in",
                                          text="The device did not get access." if denied else "You can close this tab and go back to your terminal.")
                body = page.encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\n"
                             + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
                if request.startswith("GET /callback"):
                    returned.set()
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                writer.close()

        try:
            return await asyncio.start_server(answer, "127.0.0.1", 0)
        except OSError:
            return None

    async def sync(self):
        """Replace the local models, preferences, and skill catalog with the server's."""
        token = self.secrets.get("device-token")
        if not token:
            raise SignedOut()
        async with self.client(token) as http:
            try:
                response = await http.get("/api/cli/sync")
            except httpx.HTTPError as exc:
                raise AccountError(f"Cannot reach {self.server}: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise SignedOut(response.json().get("detail") or "This device was signed out; run leocode login")
        response.raise_for_status()
        data = response.json()
        store, uid = self.store(), self.user_id
        for old in store.list_connections(uid):
            store.delete_connection(old["id"], uid)
        store.replace_skills(data["skills"])
        known = set(Connection.model_fields)
        ids = {}
        for item in data["connections"]:
            fields = {k: v for k, v in item.items() if k in known and k not in ("api_key", "key_from")}
            saved = store.save_connection(Connection(**fields, api_key=item.get("api_key")).model_dump(), user_id=uid)
            ids[item["id"]] = saved["id"]
        relayed = data.get("relayed", [])
        for item in relayed:
            # Models on the user's 9Router, which only the server reaches: called through the server's relay, signed
            # with this device's token (the router key stays on the server).
            fields = {k: v for k, v in item.items() if k in known and k not in ("api_key", "key_from", "base_url")}
            saved = store.save_connection(Connection(**fields, base_url=f"{self.server}/api/cli/relay/{item['id']}", api_key=token).model_dump(),
                                          user_id=uid)
            ids[item["id"]] = saved["id"]
        preferences = dict(data["preferences"])
        for role in ("generator_id", "planner_id", "reviewer_id"):
            preferences[role] = ids.get(preferences.get(role))
        store.set_preferences(preferences)
        listed = self.home / "chat-connectors.json"  # For /mcp; an older server sends none.
        if "connectors" in data:
            listed.write_text(json.dumps(data["connectors"], indent=2) + "\n")
        else:
            listed.unlink(missing_ok=True)
        self.save(username=data["user"]["username"], device_name=data["device"]["name"], synced_at=time.time(), relayed=len(relayed))
        return {"models": len(data["connections"]) + len(relayed), "relayed": len(relayed), "username": data["user"]["username"]}

    async def call_connector(self, connector_id, tool, arguments):
        """Run a tool of a L.E.O Chat connector through the server, which holds its sign-in: {text, is_error}."""
        from backend.mcp_client import MCPError  # Only the agent's MCP tools need it.
        token = self.secrets.get("device-token")
        if not token:
            raise MCPError("this device is not signed in; run leocode login")
        async with self.client(token) as http:
            http.timeout = httpx.Timeout(150, connect=15)
            try:
                response = await http.post(f"/api/cli/connectors/{connector_id}/call", json={"tool": tool, "arguments": arguments or {}})
            except httpx.HTTPError as exc:
                raise MCPError(f"cannot reach {self.server}: {type(exc).__name__}") from exc
        if not response.is_success:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise MCPError(str(detail or f"the L.E.O. server answered HTTP {response.status_code}"))
        return response.json()

    async def device_call(self, method, path, **kwargs):
        """One call to the server as this device; the answer as JSON."""
        token = self.secrets.get("device-token")
        if not token:
            raise SignedOut()
        async with self.client(token) as http:
            try:
                response = await http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise AccountError(f"Cannot reach {self.server}: {type(exc).__name__}") from exc
        try:
            detail = response.json().get("detail") if not response.is_success else None
        except ValueError:
            detail = None
        if response.status_code == 401:
            if detail == "Sign in":  # This device is fine; the server has no such route for devices yet.
                raise AccountError("Your L.E.O. server is older than this leo; update it (with Docker: docker compose up -d --build).")
            raise SignedOut(detail or "This device was signed out; run leocode login")
        if not response.is_success:
            raise AccountError(str(detail or f"The L.E.O. server answered HTTP {response.status_code}"))
        return response.json()

    async def rerank_models(self):
        """Rank the models of signed-in accounts again on the server, and sync: {changed}."""
        result = await self.device_call("POST", "/api/cli/connections/rerank")
        await self.sync()
        return result

    async def model_accounts(self):
        """The subscription sign-ins the server offers: {available, error, providers, accounts}."""
        return await self.device_call("GET", "/api/cli/accounts")

    async def start_sign_in(self, provider, accept_risk=False):
        # The first sign-in starts this user's 9Router on the server, which takes a while.
        return await self.device_call("POST", "/api/cli/accounts/signin", json={"provider": provider, "accept_risk": accept_risk}, timeout=120)

    async def sign_in_status(self, session_id):
        return await self.device_call("GET", f"/api/cli/accounts/signin/{session_id}", timeout=60)

    async def cancel_sign_in(self, session_id):
        return await self.device_call("DELETE", f"/api/cli/accounts/signin/{session_id}")

    async def add_connection(self, connection, sync=True):
        """Save a model on the account (as Settings → Models does), then sync it down (adding several: sync once, after)."""
        token = self.secrets.get("device-token")
        if not token:
            raise SignedOut()
        async with self.client(token) as http:
            try:
                response = await http.post("/api/cli/connections", json=connection)
            except httpx.HTTPError as exc:
                raise AccountError(f"Cannot reach {self.server}: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise SignedOut()
        if not response.is_success:
            detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
            raise AccountError(f"The server did not save the model: {detail or response.status_code}")
        if sync:
            await self.sync()
        return response.json()

    async def logout(self):
        token = self.secrets.get("device-token")
        if token and self.server:
            try:
                async with self.client(token) as http:
                    await http.post("/api/cli/logout")
            except httpx.HTTPError:
                pass  # Offline: the device can still be revoked in Settings → Account.
        store = self.store()
        for old in store.list_connections(self.user_id):
            store.delete_connection(old["id"], self.user_id)  # Keys leave the laptop with the token.
        self.secrets.delete("device-token")
        (self.home / "chat-connectors.json").unlink(missing_ok=True)
        self.save(synced_at=None)

    def stale(self, hours=1):
        return time.time() - (self.config().get("synced_at") or 0) > hours * 3600

    # -- usage ------------------------------------------------------------------

    def pending_usage(self):
        """Totals per session and model for calls made since the last report (no prompts or output)."""
        since = self.config().get("usage_reported_at") or 0
        query = select(calls, runs.c.conversation_id).join(runs, runs.c.id == calls.c.run_id).where(
            calls.c.created_at > since, calls.c.status.in_(("completed", "failed")))
        groups, latest = {}, since
        with self.store().engine.connect() as db:
            for row in db.execute(query).mappings():
                key = (row["conversation_id"], row["model"], row["provider"])
                entry = groups.setdefault(key, {"session": row["conversation_id"][:64], "model": row["model"][:160], "connection": row["provider"][:120],
                                                "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "estimated": False, "at": row["created_at"]})
                entry["calls"] += 1
                entry["input_tokens"] += row["input_tokens"] or 0
                entry["output_tokens"] += row["output_tokens"] or 0
                entry["cost"] += row["cost"] or 0
                entry["estimated"] |= bool(row["estimated"])
                entry["at"] = max(entry["at"], row["created_at"])
                latest = max(latest, row["created_at"])
        return list(groups.values()), latest

    async def report_usage(self):
        """Send what has not been reported yet; it stays queued if the server cannot be reached."""
        entries, latest = self.pending_usage()
        token = self.secrets.get("device-token")
        if not entries or not token:
            return 0
        try:
            async with self.client(token) as http:
                for start in range(0, len(entries), 500):
                    response = await http.post("/api/cli/usage", json={"entries": entries[start:start + 500]})
                    if response.status_code == 401:
                        raise SignedOut()
                    response.raise_for_status()
        except httpx.HTTPError:
            return 0
        self.save(usage_reported_at=latest)
        return len(entries)
