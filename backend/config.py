import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("LEO_DATA_DIR", ".leo")).resolve())
    database_url: str = field(default_factory=lambda: os.getenv("LEO_DATABASE_URL", ""))
    api_token: str = field(default_factory=lambda: os.getenv("LEO_API_TOKEN", ""))
    # Off by default: only the first local user, or an admin, can create accounts.
    allow_registration: bool = field(default_factory=lambda: os.getenv("LEO_ALLOW_REGISTRATION", "").lower() in {"1", "true", "yes"})
    origins: list[str] = field(default_factory=lambda: os.getenv(
        "LEO_ALLOWED_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:4321,http://localhost:4321",
    ).split(","))
    hosts: list[str] = field(default_factory=lambda: os.getenv(
        "LEO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver"
    ).split(","))
    # OAuth sign-in. Callback URLs are <public_url>/api/auth/oauth/<provider>/callback; empty uses the request's own URL.
    public_url: str = field(default_factory=lambda: os.getenv("LEO_PUBLIC_URL", "").rstrip("/"))
    # Who may sign in with OAuth: exact emails and @domain entries. Empty: existing accounts and the very first user only.
    allowed_emails: list[str] = field(default_factory=lambda: [
        e.strip().lower() for e in os.getenv("LEO_ALLOWED_EMAILS", "").split(",") if e.strip()])
    google_client_id: str = field(default_factory=lambda: os.getenv("LEO_GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: os.getenv("LEO_GOOGLE_CLIENT_SECRET", ""), repr=False)
    github_client_id: str = field(default_factory=lambda: os.getenv("LEO_GITHUB_CLIENT_ID", ""))
    github_client_secret: str = field(default_factory=lambda: os.getenv("LEO_GITHUB_CLIENT_SECRET", ""), repr=False)
    oidc_issuer: str = field(default_factory=lambda: os.getenv("LEO_OIDC_ISSUER", "").rstrip("/"))
    oidc_client_id: str = field(default_factory=lambda: os.getenv("LEO_OIDC_CLIENT_ID", ""))
    oidc_client_secret: str = field(default_factory=lambda: os.getenv("LEO_OIDC_CLIENT_SECRET", ""), repr=False)
    oidc_name: str = field(default_factory=lambda: os.getenv("LEO_OIDC_NAME", "") or "Single sign-on")
    # Where the API listens for the ChatGPT (Codex) sign-in redirect; OpenAI only accepts localhost:1455.
    router9_callback_port: int = field(default_factory=lambda: int(os.getenv("LEO_9ROUTER_CALLBACK_PORT", "1455")))
    # Password of the shared 9router service of earlier versions, which a user's adopted copy keeps.
    legacy_router_password: str = field(default_factory=lambda: os.getenv("LEO_9ROUTER_PASSWORD", "leo-local-development"), repr=False)
    # Connectors (remote MCP servers) must be HTTPS on public addresses; this allows http:// and private networks too,
    # for self-hosted MCP servers.
    mcp_allow_private: bool = field(default_factory=lambda: os.getenv("LEO_MCP_ALLOW_PRIVATE", "").lower() in {"1", "true", "yes"})
    # Agent work (sandbox workspaces, file and command tools, git push) belongs to L.E.O.C.O.D.E. The web chat refuses it
    # unless this turns server-side agent runs back on.
    server_agent: bool = field(default_factory=lambda: os.getenv("LEO_SERVER_AGENT", "").lower() in {"1", "true", "yes"})
    # sandboxd, which runs each user's model router and (with server_agent) agent workspaces; docker compose sets both.
    sandbox_url: str = field(default_factory=lambda: os.getenv("LEO_SANDBOX_URL", "").rstrip("/"))
    sandbox_token: str = field(default_factory=lambda: os.getenv("LEO_SANDBOX_TOKEN", ""), repr=False)
    # Local launcher: a pinned npm 9Router server, managed as loopback-only child processes instead of Docker.
    local_router_entry: str = field(default_factory=lambda: os.getenv("LEO_LOCAL_ROUTER_ENTRY", ""))
    frontend_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1] / "frontend/dist")

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'leo.db'}"
