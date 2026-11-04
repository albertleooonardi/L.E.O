"""Signing in to a connector with OAuth, as the MCP authorization spec describes.

On a 401 the connector names its protected-resource metadata, which names the authorization server. L.E.O. registers
itself there (dynamic client registration) unless the user pasted a client ID, then runs the authorization-code flow
with PKCE and the `resource` parameter, and refreshes tokens when they expire. Every address found along the way gets
the same public-address check as the connector itself.
"""
import re
import time
from urllib.parse import urlencode, urlsplit

import httpx

from .mcp_client import MCPError, check_url


def _origin_and_path(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")


async def _get_json(http, url, allow_private):
    await check_url(url, allow_private)
    try:
        response = await http.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if not response.is_success:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def discover(url, challenge="", transport=None, allow_private=False):
    """Where and how to sign in to the connector at `url`:
    {resource, authorize, token, register (or None), scopes (or None)}."""
    origin, path = _origin_and_path(url)
    async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False, trust_env=False) as http:
        named = re.search(r'resource_metadata="([^"]+)"', challenge or "")
        candidates = [named[1]] if named else [f"{origin}/.well-known/oauth-protected-resource{path}", f"{origin}/.well-known/oauth-protected-resource"]
        resource_meta = None
        for candidate in dict.fromkeys(candidates):
            resource_meta = await _get_json(http, candidate, allow_private)
            if resource_meta:
                break
        servers = (resource_meta or {}).get("authorization_servers") or []
        # Servers from before protected-resource metadata act as their own authorization server.
        issuer = str(servers[0]).rstrip("/") if servers else origin
        issuer_origin, issuer_path = _origin_and_path(issuer)
        metadata = None
        for candidate in dict.fromkeys([f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_path}",
                                        f"{issuer_origin}/.well-known/openid-configuration{issuer_path}",
                                        f"{issuer}/.well-known/openid-configuration"]):
            metadata = await _get_json(http, candidate, allow_private)
            if metadata:
                break
    metadata = metadata or {"authorization_endpoint": f"{issuer}/authorize", "token_endpoint": f"{issuer}/token",
                            "registration_endpoint": f"{issuer}/register"}
    if not metadata.get("authorization_endpoint") or not metadata.get("token_endpoint"):
        raise MCPError("The connector's sign-in service does not describe how to sign in")
    for endpoint in (metadata["authorization_endpoint"], metadata["token_endpoint"], metadata.get("registration_endpoint")):
        if endpoint:
            await check_url(endpoint, allow_private)
    scopes = (resource_meta or {}).get("scopes_supported") or metadata.get("scopes_supported")
    return {"resource": (resource_meta or {}).get("resource") or url, "authorize": metadata["authorization_endpoint"],
            "token": metadata["token_endpoint"], "register": metadata.get("registration_endpoint"),
            "scopes": " ".join(map(str, scopes)) if isinstance(scopes, list) and scopes else None}


async def register(meta, redirect_uri, transport=None):
    """Register L.E.O. as a client of the connector's sign-in service: {client_id, client_secret (or None)}."""
    if not meta.get("register"):
        raise MCPError("This connector does not register apps automatically; paste a client ID (and secret) from its developer settings")
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False, trust_env=False) as http:
            response = await http.post(meta["register"], json={
                "client_name": "L.E.O.", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "token_endpoint_auth_method": "none"})
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MCPError("Could not register L.E.O. with the connector's sign-in service") from exc
    if not response.is_success or not isinstance(body, dict) or not body.get("client_id"):
        detail = body.get("error_description") or body.get("error") if isinstance(body, dict) else None
        raise MCPError(f"The connector's sign-in service refused to register L.E.O.{': ' + str(detail)[:200] if detail else ''}")
    return {"client_id": body["client_id"], "client_secret": body.get("client_secret")}


def authorize_url(meta, client, redirect_uri, state, challenge):
    query = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri, "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256", "resource": meta["resource"]}
    if meta.get("scopes"):
        query["scope"] = meta["scopes"]
    return f"{meta['authorize']}{'&' if '?' in meta['authorize'] else '?'}{urlencode(query)}"


async def _token(meta, client, form, transport):
    form = {**form, "client_id": client["client_id"], "resource": meta["resource"]}
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False, trust_env=False) as http:
            response = await http.post(meta["token"], data=form, headers={"Accept": "application/json"})
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MCPError("The connector's sign-in service did not answer") from exc
    if not isinstance(body, dict) or not body.get("access_token"):
        detail = body.get("error_description") or body.get("error") if isinstance(body, dict) else None
        raise MCPError(f"The connector did not issue a token{': ' + str(detail)[:200] if detail else ''}")
    expires = body.get("expires_in")
    return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token") or form.get("refresh_token"),
            "expires_at": time.time() + float(expires) if isinstance(expires, (int, float)) else None}


async def exchange(meta, client, code, verifier, redirect_uri, transport=None):
    return await _token(meta, client, {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                       "redirect_uri": redirect_uri}, transport)


async def refresh(meta, client, refresh_token, transport=None):
    if not refresh_token:
        raise MCPError("The connector's sign-in has expired; sign in again")
    return await _token(meta, client, {"grant_type": "refresh_token", "refresh_token": refresh_token}, transport)
