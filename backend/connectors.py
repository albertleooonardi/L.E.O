"""Each user's connectors (remote MCP servers): connecting with a token or an OAuth sign-in, listing and calling tools.

Used by the API (Settings → Connectors) and by the worker, whose sub-agents call connector tools during a run.
"""
import re
import time

from . import mcp_auth
from .mcp_client import MCPClient, MCPError, NeedsAuth, check_url

PERMISSIONS = ("ask", "allow", "deny")


def slug(name):
    """A connector's prefix in model-facing tool names: `linear__create_issue`."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:24] or "mcp"


def public(connector):
    """What the browser may see: everything but the secret."""
    secret = connector.get("secret") or {}
    oauth = secret.get("oauth") or {}
    return {**{key: connector.get(key) for key in ("id", "name", "url", "enabled", "auth", "status", "error", "created_at")},
            "tools": connector.get("tools") or [], "permissions": connector.get("permissions") or {},
            "signed_in": bool(secret.get("token") or oauth.get("tokens")), "needs_client": bool(connector.get("needs_client"))}


def tool_specs(active):
    """Model tool definitions for these connectors, without denied tools or tools whose schema a provider would reject.
    Returns (specs, index from model-facing name to (connector id, tool name), skipped tool names)."""
    specs, index, skipped = [], {}, []
    for connector in active:
        prefix, permissions = slug(connector.get("name")), connector.get("permissions") or {}
        for tool in connector.get("tools") or []:
            if permissions.get(tool["name"], "ask") == "deny":
                continue
            schema = tool.get("input_schema")
            name = f"{prefix}__{re.sub(r'[^A-Za-z0-9_-]', '_', tool['name'])}"[:64]
            if not isinstance(schema, dict) or schema.get("type") != "object" or name in index:
                skipped.append(f"{connector.get('name')} · {tool['name']}")
                continue
            index[name] = (connector["id"], tool["name"])
            specs.append({"name": name, "description": f"[{connector.get('name')}] {tool.get('description') or tool['name']}"[:1024],
                          "parameters": schema})
    return specs, index, skipped


class Connectors:
    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport

    @property
    def allow_private(self):
        return self.store.config.mcp_allow_private

    def mark(self, connector, status, error=None):
        self.store.update_connector(connector["id"], connector["user_id"], config={"status": status, "error": error})
        connector.update(status=status, error=error)

    async def token(self, connector, force_refresh=False):
        """The bearer token to send, refreshing an OAuth token that expired (or that the server just refused)."""
        secret = connector.get("secret") or {}
        if connector.get("auth") == "token":
            return secret.get("token")
        if connector.get("auth") != "oauth":
            return None
        oauth = secret.get("oauth") or {}
        tokens = oauth.get("tokens")
        if not tokens:
            raise NeedsAuth()
        if force_refresh or (tokens.get("expires_at") and tokens["expires_at"] < time.time() + 60):
            try:
                tokens = await mcp_auth.refresh(oauth["meta"], oauth["client"], tokens.get("refresh_token"), self.transport)
            except MCPError as exc:
                self.mark(connector, "sign_in", str(exc))
                raise NeedsAuth() from exc
            connector["secret"] = {**secret, "oauth": {**oauth, "tokens": tokens}}
            self.store.update_connector(connector["id"], connector["user_id"], secret=connector["secret"])
        return tokens["access_token"]

    async def session(self, connector, action):
        """Run `action(client)` in a fresh MCP session; an OAuth connector gets one token refresh after a 401."""
        await check_url(connector["url"], self.allow_private)
        for attempt in range(2):
            client = MCPClient(connector["url"], await self.token(connector, force_refresh=attempt == 1), self.transport)
            try:
                async with client.session():
                    return await action(client)
            except NeedsAuth:
                if attempt or connector.get("auth") != "oauth":
                    self.mark(connector, "sign_in" if connector.get("auth") == "oauth" else "error",
                              "Sign in again" if connector.get("auth") == "oauth" else "The connector refused the token")
                    raise

    async def refresh_tools(self, connector):
        """Re-read the tool list. Tools that disappeared lose their permission; new ones start at Ask."""
        try:
            tools = await self.session(connector, lambda client: client.list_tools())
        except NeedsAuth:
            raise
        except MCPError as exc:
            self.mark(connector, "error", str(exc))
            raise
        old = connector.get("permissions") or {}
        permissions = {tool["name"]: old.get(tool["name"], "ask") for tool in tools}
        return self.store.update_connector(connector["id"], connector["user_id"],
                                           config={"tools": tools, "permissions": permissions, "status": "connected", "error": None})

    async def call(self, connector, tool, arguments):
        return await self.session(connector, lambda client: client.call_tool(tool, arguments))
