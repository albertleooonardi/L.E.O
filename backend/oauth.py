"""OAuth sign-in with Google, GitHub, and any OpenID Connect provider: the authorization-code flow with PKCE.

Identity comes from each provider's userinfo endpoint, called with the access token the token endpoint just issued
over TLS, so there are no ID-token signatures to check. Only verified emails are trusted. Provider tokens are not kept.
"""
import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx


class OAuthError(Exception):
    """A sign-in that did not complete. `code` is the reason the login page explains (failed, cancelled, ...)."""

    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code


@dataclass
class Identity:
    provider: str
    subject: str
    email: str | None  # Verified emails only; None otherwise.
    name: str = ""
    avatar_url: str = ""
    login: str = ""  # GitHub username, used for a new account's username.


GOOGLE = {"name": "Google", "authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
          "userinfo": "https://openidconnect.googleapis.com/v1/userinfo", "scope": "openid email profile"}
GITHUB = {"name": "GitHub", "authorize": "https://github.com/login/oauth/authorize", "token": "https://github.com/login/oauth/access_token",
          "userinfo": "https://api.github.com/user", "emails": "https://api.github.com/user/emails", "scope": "read:user user:email"}


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def email_allowed(email, rules):
    """True when `email` is listed exactly or its domain is listed as @domain."""
    email = (email or "").lower()
    return bool(email) and any(email == rule or (rule.startswith("@") and email.endswith(rule)) for rule in rules)


class OAuth:
    """The providers configured in .env. `transport` lets tests fake the providers' HTTP APIs."""

    def __init__(self, config, transport=None):
        self.transport = transport
        self.providers = {}
        if config.google_client_id and config.google_client_secret:
            self.providers["google"] = {**GOOGLE, "client_id": config.google_client_id, "client_secret": config.google_client_secret}
        if config.github_client_id and config.github_client_secret:
            self.providers["github"] = {**GITHUB, "client_id": config.github_client_id, "client_secret": config.github_client_secret}
        if config.oidc_issuer and config.oidc_client_id and config.oidc_client_secret:
            # Endpoints come from the issuer's discovery document on first use.
            self.providers["oidc"] = {"name": config.oidc_name, "issuer": config.oidc_issuer, "scope": "openid email profile",
                                      "client_id": config.oidc_client_id, "client_secret": config.oidc_client_secret}

    def available(self):
        return [{"id": key, "name": spec["name"]} for key, spec in self.providers.items()]

    def client(self):
        return httpx.AsyncClient(timeout=20, transport=self.transport, follow_redirects=False)

    async def endpoints(self, provider):
        spec = self.providers.get(provider)
        if not spec:
            raise OAuthError("failed", f"{provider} sign-in is not configured")
        if "issuer" in spec and "authorize" not in spec:
            try:
                async with self.client() as http:
                    found = (await http.get(f"{spec['issuer']}/.well-known/openid-configuration")).raise_for_status().json()
                spec.update(authorize=found["authorization_endpoint"], token=found["token_endpoint"], userinfo=found["userinfo_endpoint"])
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise OAuthError("failed", f"Could not read {spec['issuer']}'s OpenID configuration") from exc
        return spec

    async def authorize_url(self, provider, redirect_uri, state, challenge):
        spec = await self.endpoints(provider)
        query = {"response_type": "code", "client_id": spec["client_id"], "redirect_uri": redirect_uri, "scope": spec["scope"],
                 "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        if provider == "google":
            query["prompt"] = "select_account"
        return f"{spec['authorize']}?{urlencode(query)}"

    async def identity(self, provider, code, verifier, redirect_uri):
        spec = await self.endpoints(provider)
        try:
            async with self.client() as http:
                token = (await http.post(spec["token"], headers={"Accept": "application/json"}, data={
                    "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier,
                    "client_id": spec["client_id"], "client_secret": spec["client_secret"]})).json()
                access = token.get("access_token") if isinstance(token, dict) else None
                if not access:
                    raise OAuthError("failed", f"{spec['name']} did not issue a token: {token.get('error_description') or token.get('error') or 'no reason given'}"
                                     if isinstance(token, dict) else f"{spec['name']} did not issue a token")
                headers = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
                info = (await http.get(spec["userinfo"], headers=headers)).raise_for_status().json()
                if provider == "github":
                    emails = (await http.get(spec["emails"], headers=headers)).raise_for_status().json()
                    verified = [e for e in emails if isinstance(e, dict) and e.get("verified") and e.get("email")]
                    chosen = next((e for e in verified if e.get("primary")), verified[0] if verified else None)
                    return Identity("github", str(info["id"]), chosen["email"].lower() if chosen else None,
                                    info.get("name") or info.get("login") or "", info.get("avatar_url") or "", info.get("login") or "")
                # Google sends a boolean; some OIDC servers send the string "true".
                verified = info.get("email_verified") in (True, "true")
                return Identity(provider, str(info["sub"]), info["email"].lower() if verified and info.get("email") else None,
                                info.get("name") or info.get("preferred_username") or "", info.get("picture") or "",
                                info.get("preferred_username") or "")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise OAuthError("failed", f"{spec['name']} sign-in failed while reading your profile") from exc
