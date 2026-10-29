import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("LEO_DATA_DIR", ".leo")).resolve())
    database_url: str = field(default_factory=lambda: os.getenv("LEO_DATABASE_URL", ""))
    api_token: str = field(default_factory=lambda: os.getenv("LEO_API_TOKEN", ""))
    origins: list[str] = field(default_factory=lambda: os.getenv(
        "LEO_ALLOWED_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:4321,http://localhost:4321",
    ).split(","))
    hosts: list[str] = field(default_factory=lambda: os.getenv(
        "LEO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver"
    ).split(","))
    frontend_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1] / "frontend/dist")

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'leo.db'}"
