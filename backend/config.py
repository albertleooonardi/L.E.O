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
    # 9Router dashboard for account sign-in (Settings → Add provider); empty turns the feature off.
    router9_url: str = field(default_factory=lambda: os.getenv("LEO_9ROUTER_URL", "").rstrip("/"))
    router9_password: str = field(default_factory=lambda: os.getenv("LEO_9ROUTER_PASSWORD", ""), repr=False)
    # Where the API listens for the ChatGPT (Codex) sign-in redirect; OpenAI only accepts localhost:1455.
    router9_callback_port: int = field(default_factory=lambda: int(os.getenv("LEO_9ROUTER_CALLBACK_PORT", "1455")))
    # sandboxd, which runs agent-mode workspaces; empty turns agent mode off (docker compose sets both).
    sandbox_url: str = field(default_factory=lambda: os.getenv("LEO_SANDBOX_URL", "").rstrip("/"))
    sandbox_token: str = field(default_factory=lambda: os.getenv("LEO_SANDBOX_TOKEN", ""), repr=False)
    frontend_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1] / "frontend/dist")

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'leo.db'}"
