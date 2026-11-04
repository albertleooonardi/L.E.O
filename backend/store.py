import hashlib
import json
import os
import re
import time
import tempfile
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from sqlalchemy import (JSON, Boolean, Column, Float, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        and_, create_engine, delete, event, func, insert, inspect, or_, select, text, update)
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from .config import Config
from .router import STARTER_SKILLS
from .schemas import Preferences


metadata = MetaData()
connections = Table("connections", metadata,
    Column("id", String(36), primary_key=True), Column("user_id", String(36), index=True), Column("config", JSON, nullable=False),
    Column("secret", Text), Column("cooldown_until", Float, default=0, nullable=False))
settings = Table("settings", metadata, Column("id", Integer, primary_key=True), Column("config", JSON, nullable=False))
conversations = Table("conversations", metadata,
    Column("id", String(36), primary_key=True), Column("user_id", String(36), index=True), Column("title", Text, nullable=False),
    Column("updated_at", Float, nullable=False), Column("active_run", String(36)))
messages = Table("messages", metadata,
    Column("id", String(36), primary_key=True), Column("conversation_id", String(36), index=True, nullable=False),
    Column("role", String(20), nullable=False), Column("content", Text, nullable=False),
    Column("created_at", Float, nullable=False), Column("run_id", String(36)))
runs = Table("runs", metadata,
    Column("id", String(36), primary_key=True), Column("user_id", String(36), index=True), Column("request_id", String(100), unique=True, nullable=False),
    Column("conversation_id", String(36), index=True, nullable=False),
    Column("status", String(20), nullable=False), Column("phase", String(40), nullable=False),
    Column("mode", String(20), nullable=False), Column("prompt", Text, nullable=False),
    Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False),
    Column("lease_owner", String(36)), Column("lease_until", Float, default=0, nullable=False),
    Column("cancel_requested", Boolean, default=False, nullable=False),
    Column("state", JSON, nullable=False), Column("output", Text, default="", nullable=False),
    Column("error", Text), Column("usage", JSON, nullable=False))
events = Table("events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("run_id", String(36), index=True, nullable=False),
    Column("kind", String(40), nullable=False), Column("data", JSON, nullable=False), Column("created_at", Float, nullable=False))
calls = Table("calls", metadata,
    Column("id", String(36), primary_key=True), Column("run_id", String(36), index=True, nullable=False),
    Column("stage", String(80), nullable=False), Column("provider", String(80), nullable=False),
    Column("model", String(160), nullable=False), Column("status", String(20), nullable=False),
    Column("input_tokens", Integer, nullable=False), Column("output_tokens", Integer, nullable=False),
    Column("reserved_tokens", Integer, nullable=False), Column("reserved_cost", Float, nullable=False),
    Column("cost", Float, nullable=False), Column("estimated", Boolean, nullable=False),
    Column("created_at", Float, nullable=False), Column("elapsed_ms", Integer, default=0))
skills = Table("skills", metadata,
    Column("id", String(40), primary_key=True), Column("config", JSON, nullable=False), Column("position", Integer, nullable=False))
users = Table("users", metadata,
    Column("id", String(36), primary_key=True), Column("username", String(80), unique=True, nullable=False),
    # "" for accounts that only sign in with OAuth: bcrypt rejects it, so password sign-in fails for them.
    Column("password_hash", String(255), nullable=False), Column("created_at", Float, nullable=False),
    # Only ever set from a verified OAuth email, so it can link providers without taking over password accounts.
    Column("email", String(255)), Column("display_name", String(120)), Column("avatar_url", Text))
# OAuth sign-ins (Google, GitHub, OIDC) attached to a user: one row per provider account.
identities = Table("identities", metadata,
    Column("id", String(36), primary_key=True), Column("user_id", String(36), index=True, nullable=False),
    Column("provider", String(20), nullable=False), Column("subject", String(255), nullable=False),
    Column("email", String(255)), Column("created_at", Float, nullable=False),
    UniqueConstraint("provider", "subject", name="uq_identities_provider_subject"))
# Each user's remote MCP servers. `config` holds the name, URL, cached tools and per-tool permissions; `secret` the
# encrypted token or OAuth state.
connectors = Table("connectors", metadata,
    Column("id", String(36), primary_key=True), Column("user_id", String(36), index=True, nullable=False),
    Column("config", JSON, nullable=False), Column("secret", Text), Column("created_at", Float, nullable=False))
# L.E.O CLI sign-in: a pending device login (device code digest → the user code shown in the browser), the signed-in
# devices (token digests, revocable), and the usage totals devices report back (no prompts or code).
cli_logins = Table("cli_logins", metadata,
    Column("id", String(64), primary_key=True), Column("user_code", String(16), unique=True, nullable=False),
    Column("device_name", String(80), nullable=False), Column("status", String(10), nullable=False), Column("user_id", String(36)),
    Column("token", Text), Column("created_at", Float, nullable=False), Column("expires_at", Float, nullable=False))
cli_devices = Table("cli_devices", metadata,
    Column("id", String(36), primary_key=True), Column("user_id", String(36), index=True, nullable=False),
    Column("name", String(80), nullable=False), Column("token_hash", String(64), unique=True, nullable=False),
    Column("created_at", Float, nullable=False), Column("last_used_at", Float), Column("revoked_at", Float))
cli_usage = Table("cli_usage", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("user_id", String(36), index=True, nullable=False),
    Column("device_id", String(36), nullable=False), Column("session", String(64), nullable=False), Column("model", String(160), nullable=False),
    Column("connection", String(120), nullable=False), Column("calls", Integer, nullable=False), Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False), Column("cost", Float, nullable=False), Column("estimated", Boolean, nullable=False),
    Column("at", Float, nullable=False))
# Each user's own 9Router (run by sandboxd); the password is encrypted like API keys.
routers = Table("routers", metadata,
    Column("user_id", String(36), primary_key=True), Column("url", String(255), nullable=False),
    Column("secret", Text, nullable=False), Column("created_at", Float, nullable=False))
# Agent-mode sandbox workspaces, one per conversation (same id). The git token is encrypted like API keys.
workspaces = Table("workspaces", metadata,
    Column("id", String(36), primary_key=True), Column("user_id", String(36), index=True),
    Column("config", JSON, nullable=False), Column("secret", Text), Column("created_at", Float, nullable=False))
user_sessions = Table("user_sessions", metadata,
    Column("id", String(36), primary_key=True), Column("user_id", String(36), index=True, nullable=False),
    # SHA-256 hex digest of the session token (see auth.token_digest).
    Column("token_hash", String(255), unique=True, index=True, nullable=False), Column("expires_at", Float, nullable=False),
    Column("created_at", Float, nullable=False))

TERMINAL = {"completed", "failed", "cancelled"}


class Conflict(ValueError):
    pass


class LostLease(Exception):
    pass


class Store:
    def __init__(self, config: Config, encryption_key: str | None = None):
        self.config = config
        args = {"check_same_thread": False, "timeout": 30} if config.database_url.startswith("sqlite") else {}
        self.engine = create_engine(config.database_url, connect_args=args, pool_pre_ping=True)
        if args:
            @event.listens_for(self.engine, "connect")
            def configure_sqlite(conn, _):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
        # Check whether the skills table already exists before create_all, so we
        # seed starter skills only on brand-new databases (not on every restart).
        seed_skills = not inspect(self.engine).has_table("skills")
        metadata.create_all(self.engine)
        self._add_user_id_columns()
        key = encryption_key or os.getenv("LEO_ENCRYPTION_KEY")  # L.E.O CLI passes the key from the OS keychain.
        if not key:
            path = config.data_dir / "secret.key"
            if not path.exists():
                # Publish a complete key atomically, even when API and worker start together.
                with tempfile.NamedTemporaryFile(dir=config.data_dir, delete=False, mode="wb") as handle:
                    temporary = handle.name
                    handle.write(Fernet.generate_key())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
                finally:
                    os.unlink(temporary)
            key = path.read_text().strip()
        self.cipher = Fernet(key.encode())
        try:
            with self.engine.begin() as db:
                if not db.execute(select(settings)).first():
                    db.execute(insert(settings).values(id=1, config=Preferences().model_dump()))
        except IntegrityError:
            pass  # API and worker can initialize concurrently.
        if seed_skills:
            # Only when the table is new, so skills an owner deleted stay deleted.
            try:
                with self.engine.begin() as db:
                    for position, skill in enumerate(STARTER_SKILLS):
                        db.execute(insert(skills).values(id=skill["id"], position=position,
                                                         config={k: v for k, v in skill.items() if k != "id"} | {"enabled": True}))
            except IntegrityError:
                pass

    def _add_user_id_columns(self):
        """Add user_id (and the session/ownership indexes) to databases created before accounts existed."""
        for table in ("connections", "conversations", "runs"):
            self._migrate(lambda: "user_id" in {c["name"] for c in inspect(self.engine).get_columns(table)},
                          f"ALTER TABLE {table} ADD COLUMN user_id VARCHAR(36)")
            self._migrate(None, f"CREATE INDEX IF NOT EXISTS ix_{table}_user_id ON {table} (user_id)")
        with self.engine.begin() as db:
            # Sessions issued before tokens were stored as SHA-256 digests can never match again.
            db.execute(delete(user_sessions).where(func.length(user_sessions.c.token_hash) != 64))
        self._migrate(None, "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_sessions_token_hash ON user_sessions (token_hash)")
        self._migrate(None, "CREATE INDEX IF NOT EXISTS ix_user_sessions_user_id ON user_sessions (user_id)")
        for column, kind in (("email", "VARCHAR(255)"), ("display_name", "VARCHAR(120)"), ("avatar_url", "TEXT")):
            self._migrate(lambda: column in {c["name"] for c in inspect(self.engine).get_columns("users")},
                          f"ALTER TABLE users ADD COLUMN {column} {kind}")
        self._migrate(None, "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)")

    def _migrate(self, done, statement):
        """Apply one schema change unless `done()` says it is already there (None: the statement is idempotent)."""
        if done and done():
            return
        try:
            with self.engine.begin() as db:
                db.execute(text(statement))
        except (OperationalError, ProgrammingError, IntegrityError):
            # The API and worker can start together; the other process may have applied it first.
            if done is None:
                with self.engine.begin() as db:
                    db.execute(text(statement))  # IF NOT EXISTS is a no-op once the other process has committed.
            elif not done():
                raise

    # -- preferences ------------------------------------------------------

    def preferences(self):
        with self.engine.connect() as db:
            stored = db.execute(select(settings.c.config).where(settings.c.id == 1)).scalar_one()
        # Settings saved by an older version lack newer fields; their defaults apply until the owner saves.
        return {**Preferences().model_dump(), **stored}

    def set_preferences(self, value):
        with self.engine.begin() as db:
            known = set(db.execute(select(connections.c.id)).scalars())
            for name in ("generator_id", "planner_id", "reviewer_id"):
                if value.get(name) and value[name] not in known:
                    raise ValueError(f"Unknown connection for {name}")
            db.execute(update(settings).where(settings.c.id == 1).values(config=value))
        return value

    # -- users ------------------------------------------------------------

    def create_user(self, username: str, password_hash: str, email=None, display_name=None, avatar_url=None) -> dict:
        user_id = str(uuid.uuid4())
        with self.engine.begin() as db:
            db.execute(insert(users).values(id=user_id, username=username, password_hash=password_hash, created_at=time.time(),
                                            email=email, display_name=display_name, avatar_url=avatar_url))
        return self.get_user(user_id)

    def free_username(self, wanted: str) -> str:
        """`wanted` reduced to safe characters, with a number added if another account has it."""
        base = "-".join(re.findall(r"[a-z0-9_.]+", wanted.lower()))[:70].strip("-.") or "user"
        base = base if len(base) >= 3 else f"{base}-user"
        with self.engine.connect() as db:
            taken = set(db.execute(select(users.c.username).where(users.c.username.like(f"{base}%"))).scalars())
        return next(name for n in range(len(taken) + 2) if (name := f"{base}-{n}" if n else base) not in taken)

    def get_user_by_email(self, email: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(select(users).where(users.c.email == email.lower())).mappings().first()
        return dict(row) if row else None

    def update_user(self, user_id: str, **values) -> None:
        with self.engine.begin() as db:
            db.execute(update(users).where(users.c.id == user_id).values(**values))

    # -- OAuth identities -------------------------------------------------

    def find_identity(self, provider: str, subject: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(select(identities).where(identities.c.provider == provider, identities.c.subject == subject)).mappings().first()
        return dict(row) if row else None

    def link_identity(self, user_id: str, provider: str, subject: str, email: str | None) -> None:
        with self.engine.begin() as db:
            db.execute(insert(identities).values(id=str(uuid.uuid4()), user_id=user_id, provider=provider, subject=subject,
                                                 email=email, created_at=time.time()))

    def list_identities(self, user_id: str) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(select(identities).where(identities.c.user_id == user_id).order_by(identities.c.created_at)).mappings().all()
        return [dict(r) for r in rows]

    def unlink_identity(self, user_id: str, provider: str) -> int:
        with self.engine.begin() as db:
            return db.execute(delete(identities).where(identities.c.user_id == user_id, identities.c.provider == provider)).rowcount

    # -- connectors (remote MCP servers) ------------------------------------

    def list_connectors(self, user_id: str) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(select(connectors).where(connectors.c.user_id == user_id).order_by(connectors.c.created_at)).mappings().all()
        return [self._connector(r) for r in rows]

    def get_connector(self, connector_id: str, user_id: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(select(connectors).where(connectors.c.id == connector_id, connectors.c.user_id == user_id)).mappings().first()
        return self._connector(row) if row else None

    def _connector(self, row):
        secret = json.loads(self.cipher.decrypt(row["secret"].encode()).decode()) if row["secret"] else {}
        return {"id": row["id"], "user_id": row["user_id"], "created_at": row["created_at"], **row["config"], "secret": secret}

    def create_connector(self, user_id: str, config: dict, secret: dict | None = None) -> dict:
        connector_id = str(uuid.uuid4())
        with self.engine.begin() as db:
            db.execute(insert(connectors).values(id=connector_id, user_id=user_id, config=config, created_at=time.time(),
                                                 secret=self.cipher.encrypt(json.dumps(secret).encode()).decode() if secret else None))
        return self.get_connector(connector_id, user_id)

    def update_connector(self, connector_id: str, user_id: str, config: dict | None = None, secret: dict | None = None) -> dict:
        """Merge `config` into the stored settings; `secret` replaces the stored secret ({} clears it)."""
        with self.engine.begin() as db:
            row = db.execute(select(connectors).where(connectors.c.id == connector_id, connectors.c.user_id == user_id)).mappings().first()
            if not row:
                raise KeyError(connector_id)
            values = {}
            if config is not None:
                values["config"] = {**row["config"], **config}
            if secret is not None:
                values["secret"] = self.cipher.encrypt(json.dumps(secret).encode()).decode() if secret else None
            if values:
                db.execute(update(connectors).where(connectors.c.id == connector_id).values(**values))
        return self.get_connector(connector_id, user_id)

    def delete_connector(self, connector_id: str, user_id: str) -> None:
        with self.engine.begin() as db:
            if not db.execute(delete(connectors).where(connectors.c.id == connector_id, connectors.c.user_id == user_id)).rowcount:
                raise KeyError(connector_id)

    # -- L.E.O CLI devices ------------------------------------------------

    def create_cli_login(self, device_hash: str, user_code: str, device_name: str, expires_at: float) -> None:
        with self.engine.begin() as db:
            db.execute(delete(cli_logins).where(cli_logins.c.expires_at < time.time() - 3600))
            db.execute(insert(cli_logins).values(id=device_hash, user_code=user_code, device_name=device_name, status="pending",
                                                 created_at=time.time(), expires_at=expires_at))

    def cli_login(self, user_code: str | None = None, device_hash: str | None = None) -> dict | None:
        where = cli_logins.c.user_code == user_code if user_code else cli_logins.c.id == device_hash
        with self.engine.connect() as db:
            row = db.execute(select(cli_logins).where(where)).mappings().first()
        return dict(row) if row else None

    def answer_cli_login(self, user_code: str, user_id: str, approved: bool, token: str | None = None) -> dict | None:
        """Allow (registering the device under `token`) or deny a pending login; None if it is gone, expired, or answered."""
        with self.engine.begin() as db:
            row = db.execute(select(cli_logins).where(cli_logins.c.user_code == user_code)).mappings().first()
            if not row or row["status"] != "pending" or row["expires_at"] < time.time():
                return None
            values = {"status": "approved" if approved else "denied", "user_id": user_id}
            if approved:
                device_id = str(uuid.uuid4())
                db.execute(insert(cli_devices).values(id=device_id, user_id=user_id, name=row["device_name"], created_at=time.time(),
                                                      token_hash=hashlib.sha256(token.encode()).hexdigest()))
                values["token"] = self.cipher.encrypt(token.encode()).decode()  # Handed to the device once, then deleted.
            db.execute(update(cli_logins).where(cli_logins.c.id == row["id"]).values(**values))
        return {**dict(row), **values}

    def take_cli_token(self, device_hash: str) -> str | None:
        """The approved login's token, exactly once."""
        with self.engine.begin() as db:
            row = db.execute(select(cli_logins).where(cli_logins.c.id == device_hash, cli_logins.c.status == "approved")).mappings().first()
            if not row or not row["token"]:
                return None
            db.execute(delete(cli_logins).where(cli_logins.c.id == device_hash))
        return self.cipher.decrypt(row["token"].encode()).decode()

    def cli_device(self, token: str) -> dict | None:
        """The signed-in, unrevoked device for this token; records that it was used."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.engine.begin() as db:
            row = db.execute(select(cli_devices).where(cli_devices.c.token_hash == digest, cli_devices.c.revoked_at.is_(None))).mappings().first()
            if row:
                db.execute(update(cli_devices).where(cli_devices.c.id == row["id"]).values(last_used_at=time.time()))
        return dict(row) if row else None

    def list_cli_devices(self, user_id: str) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(select(cli_devices).where(cli_devices.c.user_id == user_id, cli_devices.c.revoked_at.is_(None))
                              .order_by(cli_devices.c.created_at)).mappings().all()
        return [{key: r[key] for key in ("id", "name", "created_at", "last_used_at")} for r in rows]

    def revoke_cli_device(self, device_id: str, user_id: str) -> None:
        with self.engine.begin() as db:
            if not db.execute(update(cli_devices).where(cli_devices.c.id == device_id, cli_devices.c.user_id == user_id,
                                                        cli_devices.c.revoked_at.is_(None)).values(revoked_at=time.time())).rowcount:
                raise KeyError(device_id)

    def add_cli_usage(self, user_id: str, device_id: str, entries: list[dict]) -> None:
        with self.engine.begin() as db:
            for entry in entries:
                db.execute(insert(cli_usage).values(user_id=user_id, device_id=device_id, **entry))

    def cli_usage_report(self, user_id: str) -> dict:
        """L.E.O CLI's reported usage: totals and per model."""
        query = select(cli_usage.c.model, func.sum(cli_usage.c.calls), func.sum(cli_usage.c.input_tokens), func.sum(cli_usage.c.output_tokens),
                       func.sum(cli_usage.c.cost), func.max(cli_usage.c.estimated.cast(Integer))).where(cli_usage.c.user_id == user_id).group_by(cli_usage.c.model)
        with self.engine.connect() as db:
            models = [{"model": m, "calls": int(c or 0), "input_tokens": int(i or 0), "output_tokens": int(o or 0), "cost": float(cost or 0),
                       "estimated": bool(e)} for m, c, i, o, cost, e in db.execute(query)]
        totals = {key: sum(m[key] for m in models) for key in ("calls", "input_tokens", "output_tokens", "cost")}
        return {"totals": totals, "models": sorted(models, key=lambda m: -m["calls"])}

    # -- per-user 9Router -------------------------------------------------

    def get_router(self, user_id: str) -> dict | None:
        """{url, password} of the user's own 9Router, or None before their first model-account sign-in."""
        with self.engine.connect() as db:
            row = db.execute(select(routers).where(routers.c.user_id == user_id)).mappings().first()
        return {"url": row["url"], "password": self.cipher.decrypt(row["secret"].encode()).decode()} if row else None

    def save_router(self, user_id: str, url: str, password: str) -> None:
        with self.engine.begin() as db:
            db.execute(insert(routers).values(user_id=user_id, url=url, secret=self.cipher.encrypt(password.encode()).decode(),
                                              created_at=time.time()))

    def get_user(self, user_id: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(select(users).where(users.c.id == user_id)).mappings().first()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(select(users).where(users.c.username == username)).mappings().first()
        return dict(row) if row else None

    def has_users(self) -> bool:
        with self.engine.connect() as db:
            return db.execute(select(users.c.id).limit(1)).first() is not None

    def admin_user_id(self) -> str | None:
        """The first account created owns the workspace-wide settings and skills."""
        with self.engine.connect() as db:
            return db.execute(select(users.c.id).order_by(users.c.created_at, users.c.id).limit(1)).scalar()

    def adopt_orphans(self, user_id: str) -> None:
        """Give rows saved before accounts existed (user_id NULL) to the workspace's first user."""
        with self.engine.begin() as db:
            for table in (connections, conversations, runs):
                db.execute(update(table).where(table.c.user_id.is_(None)).values(user_id=user_id))

    # -- sessions ---------------------------------------------------------

    def create_session(self, user_id: str, token_hash: str, expires_at: float) -> str:
        session_id = str(uuid.uuid4())
        with self.engine.begin() as db:
            db.execute(insert(user_sessions).values(id=session_id, user_id=user_id, token_hash=token_hash,
                                                    expires_at=expires_at, created_at=time.time()))
        return session_id

    def get_session_by_digest(self, token_hash: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(select(user_sessions).where(user_sessions.c.token_hash == token_hash,
                                                         user_sessions.c.expires_at > time.time())).mappings().first()
        return dict(row) if row else None

    def delete_session_by_digest(self, token_hash: str) -> None:
        with self.engine.begin() as db:
            db.execute(delete(user_sessions).where(user_sessions.c.token_hash == token_hash))

    def purge_expired_sessions(self) -> None:
        with self.engine.begin() as db:
            db.execute(delete(user_sessions).where(user_sessions.c.expires_at <= time.time()))

    def replace_skills(self, items):
        """Use exactly this skill catalog (L.E.O CLI takes the server's)."""
        with self.engine.begin() as db:
            db.execute(delete(skills))
            for position, skill in enumerate(items):
                db.execute(insert(skills).values(id=skill["id"], position=position, config={k: v for k, v in skill.items() if k != "id"}))

    def list_skills(self):
        with self.engine.connect() as db:
            rows = db.execute(select(skills).order_by(skills.c.position, skills.c.id)).mappings().all()
        return [{"id": r["id"], **r["config"]} for r in rows]

    def save_skill(self, value, skill_id=None):
        with self.engine.begin() as db:
            if skill_id:
                if not db.execute(update(skills).where(skills.c.id == skill_id).values(config=value)).rowcount:
                    raise KeyError(skill_id)
            else:
                base = "-".join(re.findall(r"[a-z0-9]+", value["name"].lower()))[:30] or "skill"
                taken = set(db.execute(select(skills.c.id)).scalars())
                skill_id = next(f"{base}-{n}" if n else base for n in range(1000) if (f"{base}-{n}" if n else base) not in taken)
                position = (db.execute(select(skills.c.position).order_by(skills.c.position.desc())).scalar() or 0) + 1
                db.execute(insert(skills).values(id=skill_id, config=value, position=position))
        return next(s for s in self.list_skills() if s["id"] == skill_id)

    def delete_skill(self, skill_id):
        with self.engine.begin() as db:
            db.execute(delete(skills).where(skills.c.id == skill_id))
            for row in db.execute(select(connections.c.id, connections.c.config)).mappings().all():
                if skill_id in row["config"].get("skills", []):
                    config = {**row["config"], "skills": [s for s in row["config"]["skills"] if s != skill_id]}
                    db.execute(update(connections).where(connections.c.id == row["id"]).values(config=config))

    def list_connections(self, user_id: str | None = None, private=False):
        query = select(connections)
        if user_id:
            query = query.where(connections.c.user_id == user_id)
        with self.engine.connect() as db:
            rows = db.execute(query).mappings().all()
        return [{"skills": [], "tier": "standard", "reasoning": False, "tools": None, **row["config"], "id": row["id"], "has_key": bool(row["secret"]),
                 "cooldown_until": row["cooldown_until"], **({"secret": row["secret"]} if private else {})} for row in rows]

    def model_usage(self):
        """Per model: completed calls and their average input and output tokens, for cost-aware routing."""
        query = select(calls.c.model, func.count(), func.avg(calls.c.input_tokens), func.avg(calls.c.output_tokens)).where(
            calls.c.status == "completed").group_by(calls.c.model)
        with self.engine.connect() as db:
            return {model: {"calls": n, "avg_input_tokens": round(a or 0), "avg_output_tokens": round(b or 0)} for model, n, a, b in db.execute(query)}

    def with_usage(self, connections_list):
        usage = self.model_usage()
        return [{**c, "calls": 0, "avg_input_tokens": None, "avg_output_tokens": None, **usage.get(c["model"], {})} for c in connections_list]

    def get_connection(self, connection_id: str, user_id: str | None = None, private=False):
        query = select(connections).where(connections.c.id == connection_id)
        if user_id:
            query = query.where(connections.c.user_id == user_id)
        with self.engine.connect() as db:
            row = db.execute(query).mappings().first()
        if not row:
            return None
        row = dict(row)
        return {"skills": [], "tier": "standard", "reasoning": False, "tools": None, **row["config"], "id": row["id"], "has_key": bool(row["secret"]),
                "cooldown_until": row["cooldown_until"], **({"secret": row["secret"]} if private else {})}

    def save_connection(self, value, connection_id=None, user_id: str | None = None):
        data = dict(value)
        key = data.pop("api_key", None)
        key_from = data.pop("key_from", None)
        known = {s["id"] for s in self.list_skills()}
        unknown = [s for s in data.get("skills", []) if s not in known]
        if unknown:
            raise ValueError(f"Unknown skill: {', '.join(unknown)}")
        existing = self.get_connection(connection_id, user_id, True) if connection_id else None
        if connection_id and not existing:
            raise KeyError(connection_id)
        secret = existing.get("secret") if existing else None
        if key is not None:
            secret = self.cipher.encrypt(key.encode()).decode() if key else None
        elif key_from:
            source = self.get_connection(key_from, user_id, True)
            if not source or not source.get("secret"):
                raise ValueError("The connection to copy a key from has no saved key")
            # A key is only reused for the host it was saved for, so it can never be sent somewhere new.
            if urlsplit(source["base_url"]).hostname != urlsplit(data["base_url"]).hostname:
                raise ValueError("A saved key can only be reused for the same provider host")
            secret = source["secret"]
        connection_id = connection_id or str(uuid.uuid4())
        with self.engine.begin() as db:
            if existing:
                db.execute(update(connections).where(connections.c.id == connection_id).values(config=data, secret=secret, cooldown_until=0))
            else:
                db.execute(insert(connections).values(id=connection_id, user_id=user_id, config=data, secret=secret, cooldown_until=0))
        return self.get_connection(connection_id, user_id)

    def delete_connection(self, connection_id: str, user_id: str | None = None):
        with self.engine.begin() as db:
            query = delete(connections).where(connections.c.id == connection_id)
            if user_id:
                query = query.where(connections.c.user_id == user_id)
            result = db.execute(query)
            if result.rowcount == 0:
                raise KeyError(connection_id)
            prefs = db.execute(select(settings.c.config).where(settings.c.id == 1)).scalar_one()
            prefs = {k: None if k.endswith("_id") and v == connection_id else v for k, v in prefs.items()}
            db.execute(update(settings).where(settings.c.id == 1).values(config=prefs))

    def decrypt(self, connection):
        return self.cipher.decrypt(connection["secret"].encode()).decode() if connection.get("secret") else ""

    def cooldown(self, connection_id, seconds):
        with self.engine.begin() as db:
            db.execute(update(connections).where(connections.c.id == connection_id).values(cooldown_until=time.time() + seconds))

    def mark_reasoning(self, connection_id):
        """Record that a model thinks before answering, so later calls send it an effort limit."""
        with self.engine.begin() as db:
            row = db.execute(select(connections.c.config).where(connections.c.id == connection_id)).first()
            if row:
                db.execute(update(connections).where(connections.c.id == connection_id).values(config={**row[0], "reasoning": True}))

    def mark_tools(self, connection_id, supported):
        """Record whether a model accepts native tool calls, learned from its first agent call."""
        with self.engine.begin() as db:
            row = db.execute(select(connections.c.config).where(connections.c.id == connection_id)).first()
            if row and row[0].get("tools") != supported:
                db.execute(update(connections).where(connections.c.id == connection_id).values(config={**row[0], "tools": supported}))

    # -- workspaces -------------------------------------------------------

    def create_workspace(self, value, user_id: str | None):
        """A new conversation with its sandbox workspace record; the caller creates the sandbox itself."""
        data = dict(value)
        token = data.pop("token", None)
        workspace_id, now = str(uuid.uuid4()), time.time()
        repo = (data.get("repo_url") or "").rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        title = data.get("name") or repo or "New workspace"
        with self.engine.begin() as db:
            db.execute(insert(conversations).values(id=workspace_id, user_id=user_id, title=title[:70], updated_at=now, active_run=None))
            db.execute(insert(workspaces).values(id=workspace_id, user_id=user_id, created_at=now, config={**data, "name": title},
                                                 secret=self.cipher.encrypt(token.encode()).decode() if token else None))
        return self.get_workspace(workspace_id, user_id)

    def get_workspace(self, workspace_id, user_id: str | None = None, private=False):
        query = select(workspaces).where(workspaces.c.id == workspace_id)
        if user_id:
            query = query.where(workspaces.c.user_id == user_id)
        with self.engine.connect() as db:
            row = db.execute(query).mappings().first()
        if not row:
            return None
        value = {**row["config"], "id": row["id"], "user_id": row["user_id"], "created_at": row["created_at"], "has_token": bool(row["secret"])}
        if private:
            value["token"] = self.cipher.decrypt(row["secret"].encode()).decode() if row["secret"] else ""
        return value

    def update_workspace(self, workspace_id, **values):
        with self.engine.begin() as db:
            row = db.execute(select(workspaces.c.config).where(workspaces.c.id == workspace_id)).first()
            if row:
                db.execute(update(workspaces).where(workspaces.c.id == workspace_id).values(config={**row[0], **values}))

    def list_conversations(self, user_id: str | None = None):
        query = select(conversations)
        if user_id:
            query = query.where(conversations.c.user_id == user_id)
        query = query.order_by(conversations.c.updated_at.desc())
        with self.engine.connect() as db:
            spaces = set(db.execute(select(workspaces.c.id)).scalars())
            return [{**dict(row), "workspace": row["id"] in spaces} for row in db.execute(query).mappings()]

    def conversation(self, conversation_id: str, user_id: str | None = None):
        query = select(conversations).where(conversations.c.id == conversation_id)
        if user_id:
            query = query.where(conversations.c.user_id == user_id)
        with self.engine.connect() as db:
            row = db.execute(query).mappings().first()
            if not row:
                return None
            history = [dict(m) for m in db.execute(select(messages).where(messages.c.conversation_id == conversation_id).order_by(messages.c.created_at, messages.c.id)).mappings()]
            history_runs = [self.public_run(dict(r)) for r in db.execute(select(runs).where(runs.c.conversation_id == conversation_id).order_by(runs.c.created_at)).mappings()]
        return {**dict(row), "messages": history, "runs": history_runs, "workspace": self.get_workspace(conversation_id)}

    def delete_conversation(self, conversation_id: str, user_id: str | None = None):
        with self.engine.begin() as db:
            query = select(conversations.c.active_run).where(conversations.c.id == conversation_id)
            if user_id:
                query = query.where(conversations.c.user_id == user_id)
            found = db.execute(query).first()
            if not found:
                raise KeyError(conversation_id)
            if found.active_run:
                raise Conflict("Stop the active run before deleting this conversation")
            ids = select(runs.c.id).where(runs.c.conversation_id == conversation_id)
            db.execute(delete(events).where(events.c.run_id.in_(ids)))
            db.execute(delete(calls).where(calls.c.run_id.in_(ids)))
            db.execute(delete(runs).where(runs.c.conversation_id == conversation_id))
            db.execute(delete(messages).where(messages.c.conversation_id == conversation_id))
            db.execute(delete(workspaces).where(workspaces.c.id == conversation_id))
            db.execute(delete(conversations).where(conversations.c.id == conversation_id))

    def create_run(self, request, user_id: str | None):
        prefs = self.preferences()
        providers = self.list_connections(user_id, True)
        eligible = [p for p in providers if p["enabled"] and (p["free"] or prefs["allow_paid"])]
        if not eligible:
            raise ValueError("Add and enable a connection in Settings. Paid connections also require paid usage to be enabled.")
        now = time.time()
        run_id, conversation_id = str(uuid.uuid4()), request.conversation_id or str(uuid.uuid4())
        mode = request.mode or prefs["mode"]
        with self.engine.begin() as db:
            query = select(runs).where(runs.c.request_id == request.request_id)
            if user_id:
                query = query.where(runs.c.user_id == user_id)
            existing = db.execute(query).mappings().first()
            if existing:
                if existing["prompt"] != request.prompt or (request.conversation_id and existing["conversation_id"] != request.conversation_id):
                    raise Conflict("Request ID was already used for another message")
                return self.public_run(dict(existing))
            if request.conversation_id:
                query = update(conversations).where(conversations.c.id == conversation_id, conversations.c.active_run.is_(None))
                if user_id:
                    query = query.where(conversations.c.user_id == user_id)
                claimed = db.execute(query.values(active_run=run_id, updated_at=now))
                if not claimed.rowcount:
                    raise Conflict("Conversation is missing or already has an active run")
            else:
                db.execute(insert(conversations).values(id=conversation_id, user_id=user_id, title=request.prompt[:70], updated_at=now, active_run=run_id))
            if mode == "agent" and not db.execute(select(workspaces.c.id).where(workspaces.c.id == conversation_id)).first():
                # Agent mode works in a sandbox; a chat without one gets an empty workspace.
                db.execute(insert(workspaces).values(id=conversation_id, user_id=user_id, created_at=now, secret=None,
                                                     config={"name": request.prompt[:70], "source": "empty", "repo_url": None, "branch": None}))
            history = [{"role": r.role, "content": r.content} for r in db.execute(select(messages).where(messages.c.conversation_id == conversation_id).order_by(messages.c.created_at, messages.c.id))]
            db.execute(insert(messages).values(id=str(uuid.uuid4()), conversation_id=conversation_id, role="user", content=request.prompt, created_at=now, run_id=run_id))
            row = dict(id=run_id, request_id=request.request_id, conversation_id=conversation_id, user_id=user_id,
                       status="queued", phase="queued", mode=mode, prompt=request.prompt, created_at=now,
                       updated_at=now, lease_until=0, cancel_requested=False,
                       state={"preferences": prefs, "providers": providers, "skills": self.list_skills(), "history": history, "completed": {}, "repair": 0,
                              "search": request.search, "connectors": list(dict.fromkeys(request.connectors))},
                       output="", error=None, usage={})
            db.execute(insert(runs).values(**row))
            db.execute(insert(events).values(run_id=run_id, kind="status", data={"phase": "queued", "message": "Waiting for worker"}, created_at=now))
        return self.public_run(row)

    def get_run(self, run_id: str, user_id: str | None = None, private=False):
        query = select(runs).where(runs.c.id == run_id)
        if user_id:
            query = query.where(runs.c.user_id == user_id)
        with self.engine.connect() as db:
            row = db.execute(query).mappings().first()
        return (dict(row) if private else self.public_run(dict(row))) if row else None

    @staticmethod
    def public_run(row):
        return {k: v for k, v in row.items() if k not in {"state", "lease_owner", "lease_until", "request_id"}}

    def claim(self, owner, user_id: str | None = None, run_id: str | None = None):
        """The oldest run ready for a worker (or exactly `run_id`: L.E.O CLI runs each session's own runs)."""
        now = time.time()
        ready = and_(runs.c.status.in_(["queued", "running"]), runs.c.lease_until < now)
        if user_id:
            ready = and_(ready, runs.c.user_id == user_id)
        if run_id:
            ready = and_(ready, runs.c.id == run_id)
        with self.engine.begin() as db:
            candidate = db.execute(select(runs.c.id, runs.c.status).where(ready).order_by(runs.c.created_at).limit(1)).first()
            if not candidate:
                return None
            row = db.execute(update(runs).where(runs.c.id == candidate.id, ready).values(status="running", lease_owner=owner, lease_until=now + 30, updated_at=now).returning(runs.c.id)).scalar()
            if row and candidate.status == "running":
                interrupted = db.execute(update(calls).where(calls.c.run_id == row, calls.c.status == "started").values(status="interrupted")).rowcount
                db.execute(insert(events).values(run_id=row, kind="warning", created_at=now, data={
                    "message": "Resumed after a worker interruption. Completed stages are reused" +
                               (f"; {interrupted} interrupted call(s) keep their reserved usage." if interrupted else ".")}))
        return self.get_run(row, user_id, True) if row else None

    def heartbeat(self, run_id, owner, user_id: str | None = None):
        with self.engine.begin() as db:
            query = update(runs).where(runs.c.id == run_id, runs.c.lease_owner == owner, runs.c.status == "running")
            if user_id:
                query = query.where(runs.c.user_id == user_id)
            result = db.execute(query.values(lease_until=time.time() + 30))
            return bool(result.rowcount)

    def checkpoint(self, run_id, owner, user_id: str | None = None, **values):
        with self.engine.begin() as db:
            query = update(runs).where(runs.c.id == run_id, runs.c.lease_owner == owner, runs.c.status == "running")
            if user_id:
                query = query.where(runs.c.user_id == user_id)
            result = db.execute(query.values(**values, updated_at=time.time()))
            if not result.rowcount:
                raise LostLease()

    def event(self, run_id, kind, data):
        with self.engine.begin() as db:
            db.execute(insert(events).values(run_id=run_id, kind=kind, data=data, created_at=time.time()))

    def get_events(self, run_id, after=0):
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(select(events).where(events.c.run_id == run_id, events.c.id > after).order_by(events.c.id).limit(200)).mappings()]

    def cancel(self, run_id: str, user_id: str | None = None):
        with self.engine.begin() as db:
            # Queued runs and runs waiting for an approval have no worker; they stop at once.
            query = update(runs).where(runs.c.id == run_id, runs.c.status.in_(["queued", "waiting"]))
            if user_id:
                query = query.where(runs.c.user_id == user_id)
            result = db.execute(query.values(
                status="cancelled", phase="cancelled", cancel_requested=True, error="Stopped by user", updated_at=time.time()
            ).returning(runs.c.conversation_id))
            queued = result.scalar()
            if queued:
                db.execute(update(conversations).where(conversations.c.id == queued, conversations.c.active_run == run_id).values(active_run=None))
                db.execute(insert(events).values(run_id=run_id, kind="done", data={"status": "cancelled", "output": "", "error": "Stopped by user", "usage": {}}, created_at=time.time()))
                return
            query = update(runs).where(runs.c.id == run_id, runs.c.status.not_in(TERMINAL))
            if user_id:
                query = query.where(runs.c.user_id == user_id)
            db.execute(query.values(cancel_requested=True))

    def finish(self, run_id, owner, status, output="", error=None, usage=None, user_id: str | None = None):
        with self.engine.begin() as db:
            query = update(runs).where(runs.c.id == run_id, runs.c.lease_owner == owner, runs.c.status == "running")
            if user_id:
                query = query.where(runs.c.user_id == user_id)
            result = db.execute(query.values(
                status=status, phase=status, output=output, error=error, usage=usage or {}, updated_at=time.time(), lease_until=0
            ).returning(runs.c.conversation_id))
            row = result.scalar()
            if not row:
                raise LostLease()
            db.execute(update(calls).where(calls.c.run_id == run_id, calls.c.status == "started").values(status="interrupted"))
            if output:
                db.execute(insert(messages).values(id=str(uuid.uuid4()), conversation_id=row, role="assistant", content=output, created_at=time.time(), run_id=run_id))
            db.execute(update(conversations).where(conversations.c.id == row, conversations.c.active_run == run_id).values(active_run=None, updated_at=time.time()))
            db.execute(insert(events).values(run_id=run_id, kind="done", data={"status": status, "output": output, "error": error, "usage": usage or {}}, created_at=time.time()))

    def wait(self, run_id, owner, state, approval):
        """Pause a run for the owner's approval: release the lease and keep the checkpoint for resumption."""
        with self.engine.begin() as db:
            result = db.execute(update(runs).where(runs.c.id == run_id, runs.c.lease_owner == owner, runs.c.status == "running").values(
                status="waiting", phase="waiting", state=state, lease_owner=None, lease_until=0, updated_at=time.time()))
            if not result.rowcount:
                raise LostLease()
            db.execute(insert(events).values(run_id=run_id, kind="approval", data=approval, created_at=time.time()))

    def resolve_approval(self, run_id, approval_id, approved, user_id: str | None = None):
        """Record the owner's answer and queue the run again; a worker resumes it from its checkpoint."""
        with self.engine.begin() as db:
            query = select(runs).where(runs.c.id == run_id)
            if user_id:
                query = query.where(runs.c.user_id == user_id)
            row = db.execute(query).mappings().first()
            if not row:
                raise KeyError(run_id)
            pending = (row["state"] or {}).get("pending_approval")
            if row["status"] != "waiting" or not pending or pending["id"] != approval_id:
                raise Conflict("This run is not waiting for that approval")
            state = {**row["state"], "pending_approval": None, "approvals": {**row["state"].get("approvals", {}), approval_id: approved},
                     "resumed_at": time.time()}
            # Matching updated_at makes a double click (or two tabs) resolve the approval only once.
            done = db.execute(update(runs).where(runs.c.id == run_id, runs.c.status == "waiting", runs.c.updated_at == row["updated_at"]).values(
                status="queued", phase="queued", state=state, updated_at=time.time())).rowcount
            if not done:
                raise Conflict("This approval was already answered")
            db.execute(insert(events).values(run_id=run_id, kind="approval_resolved", created_at=time.time(),
                                             data={"id": approval_id, "approved": approved, "kind": pending["kind"]}))

    def steer(self, run_id, message, user_id: str | None = None):
        """Queue a message for a running agent; it reads it before its next step."""
        run = self.get_run(run_id, user_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] in TERMINAL:
            raise Conflict("This run has finished; send a new message instead")
        self.event(run_id, "steer", {"message": message})

    def events_of(self, run_id, kind, after=0):
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(select(events).where(events.c.run_id == run_id, events.c.kind == kind, events.c.id > after)
                                                .order_by(events.c.id)).mappings()]

    def call_rows(self, run_id: str, user_id: str | None = None):
        query = select(calls).where(calls.c.run_id == run_id)
        if user_id:
            query = query.where(calls.c.run_id.in_(select(runs.c.id).where(runs.c.user_id == user_id)))
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(query.order_by(calls.c.created_at)).mappings()]

    def usage_report(self, conversation_id: str | None = None, recent: int = 60, user_id: str | None = None):
        """Per-model and per-conversation usage from the call ledger, plus each conversation's latest context size.

        With a conversation ID, every figure (and the call log) covers only that chat."""
        query = select(calls, runs.c.conversation_id, runs.c.mode, runs.c.status.label("run_status")).join(runs, runs.c.id == calls.c.run_id)
        if user_id:
            query = query.where(runs.c.user_id == user_id)
        if conversation_id:
            query = query.where(runs.c.conversation_id == conversation_id)
            recent = None
        with self.engine.connect() as db:
            rows = db.execute(query.order_by(calls.c.created_at)).mappings().all()
            titles = dict(db.execute(select(conversations.c.id, conversations.c.title)).all())
        # A call cannot still be running once its run has ended (e.g. rows left by a crash): report it as interrupted.
        rows = [{**r, "status": "interrupted"} if r["status"] == "started" and r["run_status"] in TERMINAL else dict(r) for r in rows]
        prefs = self.preferences()
        windows = {}
        for c in self.list_connections():
            windows.setdefault((c["name"], c["model"]), c["context_window"])
            windows.setdefault((None, c["model"]), c["context_window"])

        def role(stage):
            if stage.startswith("agent:"):
                # Agent stages are "agent:<name>:<step>": the main agent, a sub-agent, or context compaction.
                name = stage.split(":")[1]
                return "agent" if name == "main" else "compactor" if name == "compact" else "subagent"
            return "planner" if stage == "planning" else "reviewer" if stage.startswith("reviewing") else "generator"

        def blank():
            return {"calls": 0, "completed": 0, "uncertain": 0, "in_progress": 0, "input_tokens": 0, "output_tokens": 0,
                    "reserved_tokens": 0, "cost": 0.0, "estimated": False, "elapsed_ms": 0, "role_tokens": {}}

        def add(total, row):
            total["calls"] += 1
            if row["status"] == "completed":
                total["completed"] += 1
                total["input_tokens"] += row["input_tokens"]
                total["output_tokens"] += row["output_tokens"]
                total["cost"] += row["cost"]
                total["elapsed_ms"] += row["elapsed_ms"] or 0
                total["estimated"] = total["estimated"] or row["estimated"]
                kind = role(row["stage"])
                total["role_tokens"][kind] = total["role_tokens"].get(kind, 0) + row["input_tokens"] + row["output_tokens"]
            elif row["status"] == "started":
                total["in_progress"] += 1  # Running now; its reservation still counts toward run limits.
                total["reserved_tokens"] += row["reserved_tokens"]
                total["cost"] += row["reserved_cost"]
            else:
                # Failed/interrupted calls may have been billed; report their reservation separately.
                total["uncertain"] += 1
                total["reserved_tokens"] += row["reserved_tokens"]
                total["cost"] += row["reserved_cost"]

        totals, models, chats = blank(), {}, {}
        for row in rows:
            add(totals, row)
            key = (row["provider"], row["model"])
            model = models.setdefault(key, {**blank(), "provider": row["provider"], "model": row["model"], "roles": {}, "last_used": 0,
                                            "context_window": windows.get(key) or windows.get((None, row["model"]))})
            add(model, row)
            model["roles"][role(row["stage"])] = model["roles"].get(role(row["stage"]), 0) + 1
            model["last_used"] = row["created_at"]
            chat = chats.setdefault(row["conversation_id"], {**blank(), "id": row["conversation_id"], "title": titles.get(row["conversation_id"], ""),
                                                             "runs": set(), "context": None, "updated_at": 0})
            add(chat, row)
            chat["runs"].add(row["run_id"])
            chat["updated_at"] = row["created_at"]
            if row["status"] == "completed":
                window = model["context_window"]
                allowance = min(window, prefs["context_tokens"]) if window else prefs["context_tokens"]
                used = row["input_tokens"] + row["output_tokens"]
                chat["context"] = {"tokens": used, "allowance": allowance, "model_window": window, "percent": round(100 * used / allowance, 1),
                                   "provider": row["provider"], "model": row["model"], "stage": row["stage"], "estimated": row["estimated"]}
        for chat in chats.values():
            chat["runs"] = len(chat["runs"])
        recent_calls = [{"id": r["id"], "run_id": r["run_id"], "conversation_id": r["conversation_id"], "conversation": titles.get(r["conversation_id"], ""),
                         "mode": r["mode"], "stage": r["stage"], "role": role(r["stage"]), "provider": r["provider"], "model": r["model"],
                         "status": r["status"], "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                         "estimated": r["estimated"], "elapsed_ms": r["elapsed_ms"], "created_at": r["created_at"]} for r in (rows[-recent:] if recent else rows)[::-1]]
        return {"totals": totals, "context_tokens": prefs["context_tokens"],
                "models": sorted(models.values(), key=lambda m: -m["last_used"]),
                "conversations": sorted(chats.values(), key=lambda c: -c["updated_at"]), "recent_calls": recent_calls}

    def add_call(self, row):
        with self.engine.begin() as db:
            db.execute(insert(calls).values(**row))

    def update_call(self, call_id, **values):
        with self.engine.begin() as db:
            db.execute(update(calls).where(calls.c.id == call_id).values(**values))
