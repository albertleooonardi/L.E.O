import json
import os
import re
import time
import tempfile
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import (JSON, Boolean, Column, Float, Integer, MetaData, String, Table, Text,
                        and_, create_engine, delete, event, insert, inspect, or_, select, update)
from sqlalchemy.exc import IntegrityError

from .config import Config
from .router import STARTER_SKILLS
from .schemas import Preferences


metadata = MetaData()
connections = Table("connections", metadata,
    Column("id", String(36), primary_key=True), Column("config", JSON, nullable=False),
    Column("secret", Text), Column("cooldown_until", Float, default=0, nullable=False))
settings = Table("settings", metadata, Column("id", Integer, primary_key=True), Column("config", JSON, nullable=False))
conversations = Table("conversations", metadata,
    Column("id", String(36), primary_key=True), Column("title", Text, nullable=False),
    Column("updated_at", Float, nullable=False), Column("active_run", String(36)))
messages = Table("messages", metadata,
    Column("id", String(36), primary_key=True), Column("conversation_id", String(36), index=True, nullable=False),
    Column("role", String(20), nullable=False), Column("content", Text, nullable=False),
    Column("created_at", Float, nullable=False), Column("run_id", String(36)))
runs = Table("runs", metadata,
    Column("id", String(36), primary_key=True), Column("request_id", String(100), unique=True, nullable=False),
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

TERMINAL = {"completed", "failed", "cancelled"}


class Conflict(ValueError):
    pass


class LostLease(Exception):
    pass


class Store:
    def __init__(self, config: Config):
        self.config = config
        args = {"check_same_thread": False, "timeout": 30} if config.database_url.startswith("sqlite") else {}
        self.engine = create_engine(config.database_url, connect_args=args, pool_pre_ping=True)
        if args:
            @event.listens_for(self.engine, "connect")
            def configure_sqlite(conn, _):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
        seed_skills = not inspect(self.engine).has_table("skills")
        metadata.create_all(self.engine)
        key = os.getenv("LEO_ENCRYPTION_KEY")
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

    def preferences(self):
        with self.engine.connect() as db:
            return db.execute(select(settings.c.config).where(settings.c.id == 1)).scalar_one()

    def set_preferences(self, value):
        with self.engine.begin() as db:
            known = set(db.execute(select(connections.c.id)).scalars())
            for name in ("generator_id", "planner_id", "reviewer_id"):
                if value.get(name) and value[name] not in known:
                    raise ValueError(f"Unknown connection for {name}")
            db.execute(update(settings).where(settings.c.id == 1).values(config=value))
        return value

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

    def list_connections(self, private=False):
        with self.engine.connect() as db:
            rows = db.execute(select(connections)).mappings().all()
        return [{"skills": [], "tier": "standard", "reasoning": False, **row["config"], "id": row["id"], "has_key": bool(row["secret"]),
                 "cooldown_until": row["cooldown_until"], **({"secret": row["secret"]} if private else {})} for row in rows]

    def get_connection(self, connection_id, private=False):
        return next((c for c in self.list_connections(private) if c["id"] == connection_id), None)

    def save_connection(self, value, connection_id=None):
        data = dict(value)
        key = data.pop("api_key", None)
        known = {s["id"] for s in self.list_skills()}
        unknown = [s for s in data.get("skills", []) if s not in known]
        if unknown:
            raise ValueError(f"Unknown skill: {', '.join(unknown)}")
        existing = self.get_connection(connection_id, True) if connection_id else None
        if connection_id and not existing:
            raise KeyError(connection_id)
        secret = existing.get("secret") if existing else None
        if key is not None:
            secret = self.cipher.encrypt(key.encode()).decode() if key else None
        connection_id = connection_id or str(uuid.uuid4())
        with self.engine.begin() as db:
            if existing:
                db.execute(update(connections).where(connections.c.id == connection_id).values(config=data, secret=secret, cooldown_until=0))
            else:
                db.execute(insert(connections).values(id=connection_id, config=data, secret=secret, cooldown_until=0))
        return self.get_connection(connection_id)

    def delete_connection(self, connection_id):
        with self.engine.begin() as db:
            db.execute(delete(connections).where(connections.c.id == connection_id))
            prefs = db.execute(select(settings.c.config).where(settings.c.id == 1)).scalar_one()
            prefs = {k: None if k.endswith("_id") and v == connection_id else v for k, v in prefs.items()}
            db.execute(update(settings).where(settings.c.id == 1).values(config=prefs))

    def decrypt(self, connection):
        return self.cipher.decrypt(connection["secret"].encode()).decode() if connection.get("secret") else ""

    def cooldown(self, connection_id, seconds):
        with self.engine.begin() as db:
            db.execute(update(connections).where(connections.c.id == connection_id).values(cooldown_until=time.time() + seconds))

    def list_conversations(self):
        with self.engine.connect() as db:
            return [dict(row) for row in db.execute(select(conversations).order_by(conversations.c.updated_at.desc())).mappings()]

    def conversation(self, conversation_id):
        with self.engine.connect() as db:
            row = db.execute(select(conversations).where(conversations.c.id == conversation_id)).mappings().first()
            if not row:
                return None
            history = [dict(m) for m in db.execute(select(messages).where(messages.c.conversation_id == conversation_id).order_by(messages.c.created_at, messages.c.id)).mappings()]
            history_runs = [self.public_run(dict(r)) for r in db.execute(select(runs).where(runs.c.conversation_id == conversation_id).order_by(runs.c.created_at)).mappings()]
        return {**dict(row), "messages": history, "runs": history_runs}

    def delete_conversation(self, conversation_id):
        with self.engine.begin() as db:
            active = db.execute(select(conversations.c.active_run).where(conversations.c.id == conversation_id)).scalar()
            if active:
                raise Conflict("Stop the active run before deleting this conversation")
            ids = select(runs.c.id).where(runs.c.conversation_id == conversation_id)
            db.execute(delete(events).where(events.c.run_id.in_(ids)))
            db.execute(delete(calls).where(calls.c.run_id.in_(ids)))
            db.execute(delete(runs).where(runs.c.conversation_id == conversation_id))
            db.execute(delete(messages).where(messages.c.conversation_id == conversation_id))
            db.execute(delete(conversations).where(conversations.c.id == conversation_id))

    def create_run(self, request):
        prefs = self.preferences()
        providers = self.list_connections(True)
        eligible = [p for p in providers if p["enabled"] and (p["free"] or prefs["allow_paid"])]
        if not eligible:
            raise ValueError("Add and enable a connection in Settings. Paid connections also require paid usage to be enabled.")
        now = time.time()
        run_id, conversation_id = str(uuid.uuid4()), request.conversation_id or str(uuid.uuid4())
        mode = request.mode or prefs["mode"]
        with self.engine.begin() as db:
            existing = db.execute(select(runs).where(runs.c.request_id == request.request_id)).mappings().first()
            if existing:
                if existing["prompt"] != request.prompt or (request.conversation_id and existing["conversation_id"] != request.conversation_id):
                    raise Conflict("Request ID was already used for another message")
                return self.public_run(dict(existing))
            if request.conversation_id:
                claimed = db.execute(update(conversations).where(conversations.c.id == conversation_id, conversations.c.active_run.is_(None)).values(active_run=run_id, updated_at=now))
                if not claimed.rowcount:
                    raise Conflict("Conversation is missing or already has an active run")
            else:
                db.execute(insert(conversations).values(id=conversation_id, title=request.prompt[:70], updated_at=now, active_run=run_id))
            history = [{"role": r.role, "content": r.content} for r in db.execute(select(messages).where(messages.c.conversation_id == conversation_id).order_by(messages.c.created_at, messages.c.id))]
            db.execute(insert(messages).values(id=str(uuid.uuid4()), conversation_id=conversation_id, role="user", content=request.prompt, created_at=now, run_id=run_id))
            row = dict(id=run_id, request_id=request.request_id, conversation_id=conversation_id,
                       status="queued", phase="queued", mode=mode, prompt=request.prompt, created_at=now,
                       updated_at=now, lease_until=0, cancel_requested=False,
                       state={"preferences": prefs, "providers": providers, "skills": self.list_skills(), "history": history, "completed": {}, "repair": 0},
                       output="", error=None, usage={})
            db.execute(insert(runs).values(**row))
            db.execute(insert(events).values(run_id=run_id, kind="status", data={"phase": "queued", "message": "Waiting for worker"}, created_at=now))
        return self.public_run(row)

    def get_run(self, run_id, private=False):
        with self.engine.connect() as db:
            row = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
        return (dict(row) if private else self.public_run(dict(row))) if row else None

    @staticmethod
    def public_run(row):
        return {k: v for k, v in row.items() if k not in {"state", "lease_owner", "lease_until", "request_id"}}

    def claim(self, owner):
        now = time.time()
        ready = and_(runs.c.status.in_(["queued", "running"]), runs.c.lease_until < now)
        with self.engine.begin() as db:
            candidate = db.execute(select(runs.c.id, runs.c.status).where(ready).order_by(runs.c.created_at).limit(1)).first()
            if not candidate:
                return None
            row = db.execute(update(runs).where(runs.c.id == candidate.id, ready).values(status="running", lease_owner=owner, lease_until=now + 30, updated_at=now).returning(runs.c.id)).scalar()
            if row and candidate.status == "running":
                # A previous worker lost its lease. Its in-flight calls may have been billed: keep their reservations.
                interrupted = db.execute(update(calls).where(calls.c.run_id == row, calls.c.status == "started").values(status="interrupted")).rowcount
                db.execute(insert(events).values(run_id=row, kind="warning", created_at=now, data={
                    "message": "Resumed after a worker interruption. Completed stages are reused" +
                               (f"; {interrupted} interrupted call(s) keep their reserved usage." if interrupted else ".")}))
        return self.get_run(row, True) if row else None

    def heartbeat(self, run_id, owner):
        with self.engine.begin() as db:
            result = db.execute(update(runs).where(runs.c.id == run_id, runs.c.lease_owner == owner, runs.c.status == "running").values(lease_until=time.time() + 30))
            return bool(result.rowcount)

    def checkpoint(self, run_id, owner, **values):
        with self.engine.begin() as db:
            result = db.execute(update(runs).where(runs.c.id == run_id, runs.c.lease_owner == owner, runs.c.status == "running").values(**values, updated_at=time.time()))
            if not result.rowcount:
                raise LostLease()

    def event(self, run_id, kind, data):
        with self.engine.begin() as db:
            db.execute(insert(events).values(run_id=run_id, kind=kind, data=data, created_at=time.time()))

    def get_events(self, run_id, after=0):
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(select(events).where(events.c.run_id == run_id, events.c.id > after).order_by(events.c.id).limit(200)).mappings()]

    def cancel(self, run_id):
        with self.engine.begin() as db:
            queued = db.execute(update(runs).where(runs.c.id == run_id, runs.c.status == "queued").values(
                status="cancelled", phase="cancelled", cancel_requested=True, error="Stopped by user", updated_at=time.time()
            ).returning(runs.c.conversation_id)).scalar()
            if queued:
                db.execute(update(conversations).where(conversations.c.id == queued, conversations.c.active_run == run_id).values(active_run=None))
                db.execute(insert(events).values(run_id=run_id, kind="done", data={"status": "cancelled", "output": "", "error": "Stopped by user", "usage": {}}, created_at=time.time()))
                return
            db.execute(update(runs).where(runs.c.id == run_id, runs.c.status.not_in(TERMINAL)).values(cancel_requested=True))

    def finish(self, run_id, owner, status, output="", error=None, usage=None):
        with self.engine.begin() as db:
            row = db.execute(update(runs).where(runs.c.id == run_id, runs.c.lease_owner == owner, runs.c.status == "running").values(
                status=status, phase=status, output=output, error=error, usage=usage or {}, updated_at=time.time(), lease_until=0
            ).returning(runs.c.conversation_id)).scalar()
            if not row:
                raise LostLease()
            # Calls cut off by Stop or a time limit stay reserved; label them instead of leaving them "started".
            db.execute(update(calls).where(calls.c.run_id == run_id, calls.c.status == "started").values(status="interrupted"))
            if output:
                db.execute(insert(messages).values(id=str(uuid.uuid4()), conversation_id=row, role="assistant", content=output, created_at=time.time(), run_id=run_id))
            db.execute(update(conversations).where(conversations.c.id == row, conversations.c.active_run == run_id).values(active_run=None, updated_at=time.time()))
            db.execute(insert(events).values(run_id=run_id, kind="done", data={"status": status, "output": output, "error": error, "usage": usage or {}}, created_at=time.time()))

    def call_rows(self, run_id):
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(select(calls).where(calls.c.run_id == run_id).order_by(calls.c.created_at)).mappings()]

    def usage_report(self, conversation_id=None, recent=60):
        """Per-model and per-conversation usage from the call ledger, plus each conversation's latest context size.

        With a conversation ID, every figure (and the call log) covers only that chat."""
        query = select(calls, runs.c.conversation_id, runs.c.mode, runs.c.status.label("run_status")).join(runs, runs.c.id == calls.c.run_id)
        if conversation_id:
            query, recent = query.where(runs.c.conversation_id == conversation_id), None
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
