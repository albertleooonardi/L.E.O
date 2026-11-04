"""Remote MCP servers ("connectors") over Streamable HTTP: initialize, list tools, call a tool.

Each request is a JSON-RPC POST; the server answers with JSON or a short event stream, and may hand out an
Mcp-Session-Id that later requests carry. The older HTTP+SSE transport is not supported.
"""
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from .tools import _public_host

PROTOCOL = "2025-06-18"
MAX_RESULT = 20000
MAX_PAGES = 20


class MCPError(Exception):
    pass


class NeedsAuth(MCPError):
    """HTTP 401: the server wants an OAuth sign-in (or a better token). `challenge` is its WWW-Authenticate header."""

    def __init__(self, challenge=""):
        super().__init__("The connector needs you to sign in again")
        self.challenge = challenge


async def check_url(url, allow_private=False, resolver=None):
    """Connectors must be HTTPS on public addresses, unless the server allows private ones (LEO_MCP_ALLOW_PRIVATE)."""
    parts = urlsplit(url)
    if parts.scheme not in ({"https", "http"} if allow_private else {"https"}) or not parts.hostname or parts.username or parts.password:
        raise MCPError("Connector URLs must start with https://" if not allow_private else "Connector URLs must be http(s) addresses")
    if not allow_private and not await asyncio.to_thread(_public_host, parts.hostname, resolver or socket.getaddrinfo):
        raise MCPError(f"Blocked a private or unresolvable address: {parts.hostname}")


def _messages(response):
    """The JSON-RPC messages in a response: a JSON body, or the `data:` events of an event stream."""
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind == "text/event-stream":
        found, data = [], []
        for line in response.text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                try:
                    found.append(json.loads("\n".join(data)))
                except ValueError:
                    pass
                data = []
        return found
    try:
        body = response.json()
    except ValueError as exc:
        raise MCPError("The connector returned an unreadable response") from exc
    return body if isinstance(body, list) else [body]


def result_text(result):
    """A tool result as text for a model: text parts as-is, other parts as short notes, cut to MAX_RESULT."""
    parts = []
    for item in result.get("content") or []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} ({item.get('mimeType', 'unknown type')}) omitted]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource["text"]) if "text" in resource else f"[resource {resource.get('uri', '')} omitted]")
        elif kind == "resource_link":
            parts.append(f"[link: {item.get('name') or ''} {item.get('uri', '')}]".replace("  ", " "))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts) or "(no content)"
    return text if len(text) <= MAX_RESULT else text[:MAX_RESULT] + f"\n…[{len(text) - MAX_RESULT} characters omitted]"


class MCPClient:
    """One connection to a connector. Use `async with client.session():` around list_tools/call_tool."""

    def __init__(self, url, token=None, transport=None, timeout=30):
        self.url, self.token, self.transport, self.timeout = url, token, transport, timeout
        self.session_id, self.http, self.next_id = None, None, 0

    async def _post(self, body):
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            response = await self.http.post(self.url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise MCPError("The connector did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise MCPError(f"Could not reach the connector: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise NeedsAuth(response.headers.get("www-authenticate", ""))
        if not response.is_success:
            raise MCPError(f"The connector returned HTTP {response.status_code}")
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        return response

    async def request(self, method, params=None):
        self.next_id += 1
        ident = self.next_id
        response = await self._post({"jsonrpc": "2.0", "id": ident, "method": method, **({"params": params} if params is not None else {})})
        message = next((m for m in _messages(response) if isinstance(m, dict) and m.get("id") == ident), None)
        if message is None:
            raise MCPError(f"The connector sent no answer to {method}")
        if message.get("error"):
            error = message["error"]
            raise MCPError(str(error.get("message") if isinstance(error, dict) else error)[:300] or f"{method} failed")
        return message.get("result") or {}

    @asynccontextmanager
    async def session(self):
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10), transport=self.transport,
                                     follow_redirects=False, trust_env=False) as self.http:
            await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                              "clientInfo": {"name": "L.E.O.", "version": "0.1.0"}})
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            yield self

    async def list_tools(self):
        """Every tool, following pagination: [{name, description, input_schema, read_only, destructive}]."""
        tools, cursor = [], None
        for _ in range(MAX_PAGES):
            page = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            for tool in page.get("tools") or []:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                hints = tool.get("annotations") or {}
                tools.append({"name": str(tool["name"])[:128], "description": str(tool.get("description") or "")[:2000],
                              "input_schema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else None,
                              "read_only": hints.get("readOnlyHint") is True, "destructive": hints.get("destructiveHint") is not False})
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name, arguments):
        """{"text": the result as text, "is_error": whether the tool reported a failure}."""
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return {"text": result_text(result), "is_error": bool(result.get("isError"))}
