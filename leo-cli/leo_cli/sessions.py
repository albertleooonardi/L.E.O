"""Sessions: conversations with the agent about this folder, several at once, each driven in this process.

A session is a conversation in the local store; each message is an agent run that the session's own task drives with
the engine's worker loop and a LocalWorkspace on the folder (or on the session's git worktree). Approvals pause a run
exactly as on the server; answering one requeues it and the task drives it on.
"""
import asyncio
import os
import uuid

from backend.schemas import RunInput
from backend.worker import process_one

from .mcp import Servers
from .policy import Policy
from .skills import Skills
from .workspace import LocalWorkspace

ACTIVE = {"queued", "running"}
LEASE_WAIT = 40  # Seconds to wait for a closed window's 30-second claim on a run to expire.
DECISIONS = {"once", "session", "project", "deny"}


class Session:
    def __init__(self, sessions, number, root, branch=None, conversation_id=None):
        self.sessions, self.number, self.root, self.branch = sessions, number, root, branch
        self.id, self.run_id, self.task, self.title, self.written = conversation_id, None, None, None, set()
        self.workspace = LocalWorkspace(root, sessions.policy, sessions.skills, on_write=self.wrote, mcp=sessions.mcp)

    def wrote(self, path):
        self.written.add(path)
        self.sessions.collided(self, path)

    @property
    def run(self):
        return self.sessions.store.get_run(self.run_id, self.sessions.uid, True) if self.run_id else None

    @property
    def status(self):
        run = self.run
        return run["status"] if run else "idle"

    @property
    def approval(self):
        run = self.run
        return (run["state"] or {}).get("pending_approval") if run and run["status"] == "waiting" else None

    @property
    def busy(self):
        return self.status in ACTIVE or bool(self.task and not self.task.done())

    def label(self):
        where = f" on {self.branch}" if self.branch else ""
        return f"[{self.number}] {self.title or 'new session'}{where} · {self.status}"


class Sessions:
    def __init__(self, account, project, autorun=None, transport=None, notify=None):
        self.account, self.store, self.uid = account, account.store(), account.user_id
        self.project = os.path.realpath(project)
        self.policy = Policy(self.project, autorun)
        self.skills = Skills(self.project, account.home)
        self.mcp = Servers(self.project, account.home, account)  # Started when the first task needs the agent's tools.
        self.policy.external = self.mcp.describe
        self.transport, self.notify = transport, notify or (lambda session, message: None)
        self.owner = f"leo-cli-{uuid.uuid4().hex[:8]}"
        self.items, self.current = [], None

    # -- opening ----------------------------------------------------------------

    async def open(self, worktree=False):
        number = max((s.number for s in self.items), default=0) + 1
        root, branch = (await self.worktree(number)) if worktree else (self.project, None)
        session = Session(self, number, root, branch)
        self.items.append(session)
        self.current = session
        return session

    async def worktree(self, number):
        """A git worktree and branch of its own, so parallel work never touches the same files."""
        name = f"session-{number}-{uuid.uuid4().hex[:4]}"
        path = os.path.join(self.project, ".leo-cli", "worktrees", name)
        process = await asyncio.create_subprocess_exec("git", "-C", self.project, "worktree", "add", path, "-b", f"leo/{name}",
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await process.communicate()
        if process.returncode:
            self.notify(None, "Could not create a worktree (" + output.decode(errors="replace").strip().splitlines()[-1][:200]
                        + "); this session works in the folder.")
            return self.project, None
        return path, f"leo/{name}"

    def history(self):
        """Earlier sessions on this folder, newest first."""
        found = []
        for conversation in self.store.list_conversations(self.uid):
            workspace = self.store.get_workspace(conversation["id"], self.uid)
            if workspace and workspace.get("project") == self.project:
                found.append(conversation)
        return found

    def resume(self, conversation_id):
        """Reopen an earlier session, and drive its last run on if it was interrupted."""
        workspace = self.store.get_workspace(conversation_id, self.uid) or {}
        conversation = self.store.conversation(conversation_id, self.uid)
        number = max((s.number for s in self.items), default=0) + 1
        root = workspace.get("path") if workspace.get("path") and os.path.isdir(workspace["path"]) else self.project
        session = Session(self, number, root, workspace.get("branch"), conversation_id)
        session.title = conversation["title"] if conversation else None
        session.run_id = conversation["runs"][-1]["id"] if conversation and conversation.get("runs") else None
        self.items.append(session)
        self.current = session
        if session.status in ACTIVE:
            self.start(session)
        return session

    # -- working ----------------------------------------------------------------

    async def send(self, session, text):
        run = self.store.create_run(RunInput(prompt=text, conversation_id=session.id, mode="agent", request_id=str(uuid.uuid4())), self.uid)
        if not session.id:
            session.id, session.title = run["conversation_id"], text.strip().splitlines()[0][:60]
            self.store.update_workspace(session.id, project=self.project, path=session.root, branch=session.branch,
                                        source="local", name=session.title)
        session.run_id = run["id"]
        self.start(session)
        return run

    def start(self, session):
        session.task = asyncio.create_task(self.drive(session))

    async def drive(self, session, lease_wait=LEASE_WAIT):
        waited = 0
        await self.mcp.start()  # Once per window; a server that fails is left out, and /mcp says why.
        while True:
            claimed = await process_one(self.store, self.owner, self.transport, session.workspace, run_id=session.run_id)
            if claimed:
                if session.status != "queued":
                    break
                continue
            run = self.store.get_run(session.run_id, self.uid, True) if session.run_id else None
            if not run or run["status"] != "running":
                break
            # A leo window that just closed still holds the run for up to 30 seconds; take it over when that runs out.
            # A window that is still open keeps renewing it, so after a while this one stops trying.
            if waited >= lease_wait:
                self.notify(session, "This session is running in another leo window; follow it there.")
                break
            if waited == 0:
                self.notify(session, "Waiting for the previous leo window to let go of this session…")
            await asyncio.sleep(1)
            waited += 1
        try:
            await self.account.report_usage()
        except Exception:  # Usage waits in the local store until the server is reachable.
            pass

    def answer(self, session, decision, prefix=None):
        """Answer the session's pending approval: once, session, project (remembered), or deny."""
        pending = session.approval
        if not pending or decision not in DECISIONS:
            return False
        if decision in ("session", "project"):
            self.policy.allow({"kind": pending["kind"], "detail": pending.get("detail") or {}}, decision, prefix)
        self.store.resolve_approval(session.run_id, pending["id"], decision != "deny", self.uid)
        self.start(session)
        return True

    def stop(self, session):
        if session.run_id and session.status in ACTIVE | {"waiting"}:
            self.store.cancel(session.run_id, self.uid)

    def steer(self, session, text):
        self.store.steer(session.run_id, text, self.uid)

    def events(self, session, after=0):
        return self.store.get_events(session.run_id, after) if session.run_id else []

    def cost(self, session):
        return self.store.usage_report(session.id, user_id=self.uid)["totals"] if session.id else None

    def collided(self, session, path):
        for other in self.items:
            if other is not session and path in other.written:
                relative = os.path.relpath(path, self.project)
                self.notify(session, f"Sessions {other.number} and {session.number} both changed {relative}")

    async def close(self):
        """Stop driving; interrupted runs keep their checkpoints and resume with leo resume."""
        for session in self.items:
            if session.task and not session.task.done():
                session.task.cancel()
        await asyncio.gather(*(s.task for s in self.items if s.task), return_exceptions=True)
        await self.mcp.close()
