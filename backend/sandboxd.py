"""sandboxd: creates and drives one locked-down container per agent workspace.

Run with `uvicorn backend.sandboxd:create_app --factory --port 8100` in a container that has the Docker socket.
It is the only L.E.O. service with that socket; the API and worker reach it over HTTP with LEO_SANDBOX_TOKEN.

Each workspace container runs as an unprivileged user with every Linux capability dropped, no-new-privileges,
CPU/memory/process limits, and a named volume at /workspace. It sits on an internal network whose only exit is the
egress proxy (package registries). Git clone and push happen here, never inside the sandbox, so repository tokens
are never visible to model-written code; a push reads the sandbox's history through a `git bundle`, so the
sandbox's own .git/config and hooks are never used.
"""
import asyncio
import base64
import io
import os
import re
import secrets
import shlex
import shutil
import tarfile
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .sandbox import AGENT_UID, MAX_IMPORT_BYTES, WORKSPACE, SandboxError, TarBuilder, rebuild_tar, workspace_path

SESSION = re.compile(r"[a-f0-9-]{8,64}")
LABEL = "leo.session"
# Each user's own 9Router. A separate label, so the sandbox idle reaper never stops them.
ROUTER_LABEL = "leo.router"
ROUTER_PORT = 20128


def _bytes(value):
    """Docker-style sizes such as 2g or 512m."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg]?)b?", value.strip().lower())
    if not match:
        raise ValueError(f"Invalid size: {value}")
    return int(float(match[1]) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match[2]])


class Settings:
    def __init__(self):
        self.token = os.getenv("LEO_SANDBOX_TOKEN", "")
        self.image = os.getenv("LEO_SANDBOX_IMAGE", "leo-sandbox:latest")
        self.network = os.getenv("LEO_SANDBOX_NETWORK", "leo-sandbox")
        self.proxy = os.getenv("LEO_SANDBOX_PROXY", "http://egress:3128")
        self.memory = _bytes(os.getenv("LEO_SANDBOX_MEMORY", "2g"))
        self.cpus = float(os.getenv("LEO_SANDBOX_CPUS", "2"))
        self.pids = int(os.getenv("LEO_SANDBOX_PIDS", "512"))
        self.runtime = os.getenv("LEO_SANDBOX_RUNTIME", "")  # e.g. runsc (gVisor) for stronger isolation
        self.idle_seconds = int(os.getenv("LEO_SANDBOX_IDLE_SECONDS", "1800"))
        self.socket = os.getenv("LEO_DOCKER_SOCKET", "/var/run/docker.sock")
        self.router_image = os.getenv("LEO_ROUTER_IMAGE", "decolua/9router:0.5.112")
        # Reachable by the API and worker, with internet access for the providers the router signs in to.
        self.router_network = os.getenv("LEO_ROUTER_NETWORK", "leo-routers")
        self.router_memory = _bytes(os.getenv("LEO_ROUTER_MEMORY", "512m"))
        # The volume of the shared 9router service from before each user had their own; a user's first router can
        # start from a copy of it, keeping the accounts and keys their models use. Empty: nothing to adopt.
        self.legacy_router_volume = os.getenv("LEO_LEGACY_ROUTER_VOLUME", "")


class DockerError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Docker:
    """The few Docker Engine API calls sandboxd needs, over the Unix socket."""

    def __init__(self, socket_path, transport=None):
        self.client = httpx.AsyncClient(transport=transport or httpx.AsyncHTTPTransport(uds=socket_path), base_url="http://docker",
                                        timeout=httpx.Timeout(120, connect=10))

    async def request(self, method, path, ok=(), **kwargs):
        response = await self.client.request(method, path, **kwargs)
        if response.is_success or response.status_code in ok:
            return response
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = response.text[:300]
        raise DockerError(response.status_code, message or f"Docker returned HTTP {response.status_code}")

    async def inspect(self, name):
        try:
            return (await self.request("GET", f"/containers/{name}/json")).json()
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise

    async def exec(self, name, command, timeout, cwd=WORKSPACE, env=None, max_output=30000, keep=4_000_000):
        """Run a command as the sandbox user; output is stdout and stderr in order, shortened in the middle if long."""
        # Every process this command starts inherits the marker, so a timeout can find them all, even ones that left
        # timeout's process group (setsid, `set -m` job control).
        marker = secrets.token_hex(16)
        spec = {"AttachStdout": True, "AttachStderr": True, "Tty": False, "User": f"{AGENT_UID}:{AGENT_UID}", "WorkingDir": cwd,
                "Env": [f"{k}={v}" for k, v in (env or {}).items()] + [f"LEO_EXEC={marker}"],
                # timeout puts the command in its own process group and signals that whole group when time is up.
                "Cmd": ["timeout", "--kill-after=5", str(int(timeout)), "bash", "-lc", command]}
        exec_id = (await self.request("POST", f"/containers/{name}/exec", json=spec)).json()["Id"]
        head, tail, dropped, buffer = bytearray(), bytearray(), 0, b""
        timed_out = False
        try:
            async with asyncio.timeout(timeout + 20):
                async with self.client.stream("POST", f"/exec/{exec_id}/start", json={"Detach": False, "Tty": False},
                                              timeout=httpx.Timeout(timeout + 30, connect=10)) as response:
                    if not response.is_success:
                        await response.aread()
                        raise DockerError(response.status_code, response.text[:300])
                    async for piece in response.aiter_bytes():
                        buffer += piece
                        # Multiplexed stream: an 8-byte header (stream type, 3 zero bytes, big-endian size), then the payload.
                        while len(buffer) >= 8:
                            size = int.from_bytes(buffer[4:8], "big")
                            if len(buffer) < 8 + size:
                                break
                            payload, buffer = buffer[8:8 + size], buffer[8 + size:]
                            if len(head) < keep:
                                head += payload
                            else:
                                tail += payload
                                if len(tail) > keep:
                                    dropped += len(tail) - keep
                                    del tail[:len(tail) - keep]
        except TimeoutError:
            timed_out = True
        info = (await self.request("GET", f"/exec/{exec_id}/json")).json()
        code = info.get("ExitCode")
        timed_out = timed_out or code in (124, 137)
        if timed_out:
            # Exec inspect's Pid is a host PID, meaningless inside the container, so find the processes by marker instead.
            # Several passes catch children forked while the previous pass ran. Attaching makes start block until it is done.
            sweep = ('for i in 1 2 3; do for p in /proc/[0-9]*; do grep -qzx "LEO_EXEC=$1" "$p/environ" 2>/dev/null '
                     '&& kill -KILL "${p#/proc/}" 2>/dev/null; done; done; true')
            cleanup = {"AttachStdout": True, "AttachStderr": True, "Tty": False, "User": f"{AGENT_UID}:{AGENT_UID}",
                       "Cmd": ["sh", "-c", sweep, "sh", marker]}
            cleanup_id = (await self.request("POST", f"/containers/{name}/exec", json=cleanup)).json()["Id"]
            await self.request("POST", f"/exec/{cleanup_id}/start", json={"Detach": False, "Tty": False})
        text = (bytes(head) + (f"\n…[{dropped} bytes omitted]…\n".encode() if dropped else b"") + bytes(tail)).decode("utf-8", "replace")
        truncated = len(text) > max_output
        if truncated:
            half = max_output // 2
            text = f"{text[:half]}\n…[{len(text) - max_output} characters omitted]…\n{text[-half:]}"
        return {"exit_code": code if code is not None else -1, "output": text, "timed_out": timed_out, "truncated": truncated}


class RouterInput(BaseModel):
    password: str = Field(min_length=16, max_length=200, repr=False)
    adopt_legacy: bool = False  # Start a new router from a copy of the old shared one (LEO_LEGACY_ROUTER_VOLUME).


class ExecInput(BaseModel):
    command: str = Field(min_length=1, max_length=100_000)
    timeout: int = Field(default=120, ge=1, le=3600)
    cwd: str = WORKSPACE
    max_output: int = Field(default=30000, ge=1000, le=1_000_000)


class CloneInput(BaseModel):
    url: str = Field(max_length=500)
    branch: str | None = Field(default=None, max_length=200)
    token: str | None = Field(default=None, max_length=4096, repr=False)


class PushInput(CloneInput):
    branch: str = Field(min_length=1, max_length=200)
    message: str = Field(default="Changes by the L.E.O. agent", max_length=2000)


def _auth(url, token):
    """Git over HTTPS with a token, passed per command so it is never written to a config file."""
    if not token:
        return []
    host = httpx.URL(url).host
    user = "x-access-token" if host.endswith("github.com") else "oauth2" if "gitlab" in host else "git"
    return ["-c", "http.extraHeader=Authorization: Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()]


async def _git(args, cwd, token=None, timeout=600):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": cwd, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    process = await asyncio.create_subprocess_exec("git", "-c", "core.hooksPath=/dev/null", *args, cwd=cwd, env=env,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        async with asyncio.timeout(timeout):
            output, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        raise SandboxError("Git took too long")
    text = output.decode("utf-8", "replace")
    if token:
        text = text.replace(token, "***")
    return process.returncode, text[-4000:]


def create_app(settings=None, docker_transport=None):
    settings = settings or Settings()
    if not settings.token:
        raise RuntimeError("Set LEO_SANDBOX_TOKEN for sandboxd (the API and worker use the same value)")
    docker = Docker(settings.socket, docker_transport)
    locks, last_used = {}, {}

    async def reaper():
        # Containers idle for a while are stopped; their workspace volume stays and they restart on next use.
        while True:
            await asyncio.sleep(60)
            try:
                listed = (await docker.request("GET", "/containers/json", params={"filters": f'{{"label":["{LABEL}"]}}'})).json()
                for item in listed:
                    sid = item.get("Labels", {}).get(LABEL, "")
                    if time.time() - last_used.setdefault(sid, time.time()) > settings.idle_seconds:
                        await docker.request("POST", f"/containers/{item['Id']}/stop", ok=(304, 404), params={"t": 5})
            except Exception as exc:  # The reaper must never stop the service.
                print(f"sandboxd: idle check failed: {exc}")

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(reaper())
        yield
        task.cancel()
        await docker.client.aclose()

    app = FastAPI(title="L.E.O. sandboxd", lifespan=lifespan, docs_url=None, openapi_url=None)

    @app.middleware("http")
    async def authorize(request: Request, call_next):
        if request.url.path != "/health":
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(supplied, settings.token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(DockerError)
    async def docker_failed(request, exc):
        return JSONResponse({"detail": f"Docker: {exc}"}, status_code=502)

    @app.exception_handler(SandboxError)
    async def sandbox_failed(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def check(sid):
        if not SESSION.fullmatch(sid):
            raise HTTPException(400, "Invalid session id")
        last_used[sid] = time.time()
        return f"leo-sbx-{sid}"

    async def ensure(sid):
        name = check(sid)
        async with locks.setdefault(sid, asyncio.Lock()):
            info = await docker.inspect(name)
            if info is None:
                await docker.request("POST", "/volumes/create", json={"Name": f"leo-ws-{sid}", "Labels": {LABEL: sid}})
                proxy = settings.proxy
                host_config = {
                    "Mounts": [{"Type": "volume", "Source": f"leo-ws-{sid}", "Target": WORKSPACE}],
                    "Memory": settings.memory, "MemorySwap": settings.memory, "NanoCpus": int(settings.cpus * 1e9),
                    "PidsLimit": settings.pids, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                    "NetworkMode": settings.network, "Init": True, "RestartPolicy": {"Name": "no"},
                }
                if settings.runtime:
                    host_config["Runtime"] = settings.runtime
                spec = {"Image": settings.image, "Cmd": ["sleep", "infinity"], "User": f"{AGENT_UID}:{AGENT_UID}", "WorkingDir": WORKSPACE,
                        "Hostname": "sandbox", "Labels": {LABEL: sid}, "HostConfig": host_config,
                        "Env": [f"{k}={proxy}" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")] +
                               ["NO_PROXY=localhost,127.0.0.1", "no_proxy=localhost,127.0.0.1",
                                # .pyc freshness is checked by whole-second mtime and size, so an agent's quick
                                # same-length edit would keep running the old code.
                                "PYTHONDONTWRITEBYTECODE=1"]}
                try:
                    await docker.request("POST", "/containers/create", params={"name": name}, json=spec)
                except DockerError as exc:
                    if exc.status == 404:
                        raise HTTPException(503, f"Sandbox image {settings.image} is missing; run `docker compose build sandbox-image`") from exc
                    if exc.status != 409:  # 409: another request created it first.
                        raise
                info = await docker.inspect(name)
            if not info["State"]["Running"]:
                await docker.request("POST", f"/containers/{name}/start", ok=(304,))
        return name

    async def import_tar(name, data):
        """Unpack into a fresh, unguessable folder, then copy into /workspace as the sandbox user.

        Docker unpacks archives as root, so it never writes straight into /workspace, where the agent may have
        planted links; the copy runs with the sandbox user's own permissions."""
        # Reject an import whose destination already traverses a link planted by sandbox code. `cp -a`
        # would safely replace some links, but rejecting the attempt makes the boundary explicit and auditable.
        destinations = set()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as source:
            for member in source:
                target = workspace_path(member.name)
                parts = target.removeprefix(WORKSPACE + "/").split("/")
                destinations.update(WORKSPACE + "/" + "/".join(parts[:n]) for n in range(1, len(parts) + 1))
        if destinations:
            checks = " || ".join(f"test -L {shlex.quote(path)}" for path in sorted(destinations))
            linked = await docker.exec(name, f"if {checks}; then exit 73; fi", 30)
            if linked["exit_code"] == 73:
                raise SandboxError("Import destination contains a symbolic link")
            if linked["exit_code"] != 0:
                raise SandboxError("Could not validate import destinations")
        staging = f"leo-import-{secrets.token_hex(16)}"
        packed = rebuild_tar(data, staging)
        await docker.request("PUT", f"/containers/{name}/archive", params={"path": "/tmp"}, content=packed,
                             headers={"Content-Type": "application/x-tar"})
        result = await docker.exec(name, f'cp -a "/tmp/{staging}/." "{WORKSPACE}/" && rm -rf "/tmp/{staging}"', 300)
        if result["exit_code"] != 0:
            raise SandboxError("Could not copy files into the workspace: " + result["output"][-500:])

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.put("/sessions/{sid}")
    async def create_session(sid: str):
        await ensure(sid)
        return {"id": sid, "status": "running"}

    @app.delete("/sessions/{sid}")
    async def delete_session(sid: str):
        name = check(sid)
        await docker.request("DELETE", f"/containers/{name}", ok=(404,), params={"force": "true"})
        await docker.request("DELETE", f"/volumes/leo-ws-{sid}", ok=(404,), params={"force": "true"})
        last_used.pop(sid, None)
        return {"ok": True}

    def router_name(uid):
        if not SESSION.fullmatch(uid):
            raise HTTPException(400, "Invalid user id")
        return f"leo-router-{uid}"

    async def pull(image):
        """Fetch an image the host does not have yet; the progress stream ends when the pull does."""
        repository, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "latest")
        await docker.request("POST", "/images/create", params={"fromImage": repository, "tag": tag}, timeout=600)

    @app.put("/routers/{uid}")
    async def ensure_router(uid: str, body: RouterInput):
        """Create or start the user's 9Router. Its volume keeps signed-in accounts; the password applies on first start."""
        name = router_name(uid)
        adopted = False
        async with locks.setdefault(name, asyncio.Lock()):
            info = await docker.inspect(name)
            if info and (info.get("Config", {}).get("Labels") or {}).get(ROUTER_LABEL) != uid:
                raise HTTPException(409, f"{name} exists but was not created by sandboxd")
            if info is None:
                await docker.request("POST", "/volumes/create", json={"Name": name, "Labels": {ROUTER_LABEL: uid}})
                if body.adopt_legacy:
                    adopted = await adopt_legacy(name)
                spec = {"Image": settings.router_image, "Hostname": "router", "Labels": {ROUTER_LABEL: uid},
                        "Env": ["DATA_DIR=/app/data", f"INITIAL_PASSWORD={body.password}"],
                        "HostConfig": {"Mounts": [{"Type": "volume", "Source": name, "Target": "/app/data"}],
                                       "Memory": settings.router_memory, "MemorySwap": settings.router_memory, "NanoCpus": int(1e9),
                                       # Its entrypoint chowns the volume and drops to the `node` user with su-exec; nothing else.
                                       "PidsLimit": 256, "CapDrop": ["ALL"], "CapAdd": ["CHOWN", "SETUID", "SETGID"],
                                       "SecurityOpt": ["no-new-privileges"],
                                       "NetworkMode": settings.router_network, "Init": True,
                                       # It refreshes account tokens in the background, so it keeps running.
                                       "RestartPolicy": {"Name": "unless-stopped"}}}
                for attempt in range(2):
                    try:
                        await docker.request("POST", "/containers/create", params={"name": name}, json=spec)
                        break
                    except DockerError as exc:
                        if exc.status == 404 and not attempt:
                            await pull(settings.router_image)
                        elif exc.status != 409:  # 409: another request created it first.
                            raise
                info = await docker.inspect(name)
            if not info["State"]["Running"]:
                await docker.request("POST", f"/containers/{name}/start", ok=(304,))
        return {"url": f"http://{name}:{ROUTER_PORT}", "status": "running", "adopted": adopted}

    async def adopt_legacy(volume):
        """Copy the old shared router's data into a new router's volume (before its first start); False if there is none.

        Only the one configured volume can be copied, read-only, by a throwaway container with no network."""
        legacy = settings.legacy_router_volume
        if not legacy:
            return False
        try:
            await docker.request("GET", f"/volumes/{legacy}")
        except DockerError as exc:
            if exc.status == 404:
                return False
            raise
        spec = {"Image": settings.router_image, "Entrypoint": ["sh", "-c"], "Cmd": ["cp -a /from/. /to/"], "User": "0:0",
                "Labels": {ROUTER_LABEL: "adopt"},
                "HostConfig": {"Mounts": [{"Type": "volume", "Source": legacy, "Target": "/from", "ReadOnly": True},
                                          {"Type": "volume", "Source": volume, "Target": "/to"}],
                               "NetworkMode": "none", "CapDrop": ["ALL"], "CapAdd": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
                               "SecurityOpt": ["no-new-privileges"], "PidsLimit": 64}}
        helper = f"{volume}-adopt"
        await docker.request("DELETE", f"/containers/{helper}", ok=(404,), params={"force": "true"})  # Left by a crash.
        for attempt in range(2):
            try:
                await docker.request("POST", "/containers/create", params={"name": helper}, json=spec)
                break
            except DockerError as exc:
                if exc.status != 404 or attempt:
                    raise
                await pull(settings.router_image)
        try:
            await docker.request("POST", f"/containers/{helper}/start")
            code = (await docker.request("POST", f"/containers/{helper}/wait", timeout=600)).json().get("StatusCode")
        finally:
            await docker.request("DELETE", f"/containers/{helper}", ok=(404,), params={"force": "true"})
        if code != 0:
            raise HTTPException(500, f"Copying the old 9Router data failed (exit {code})")
        return True

    @app.delete("/routers/{uid}")
    async def stop_router(uid: str):
        """Remove the container; the volume (signed-in accounts) stays, so the next sign-in picks up where it left off."""
        await docker.request("DELETE", f"/containers/{router_name(uid)}", ok=(404,), params={"force": "true"})
        return {"ok": True}

    @app.post("/sessions/{sid}/exec")
    async def run(sid: str, body: ExecInput):
        name = await ensure(sid)
        return await docker.exec(name, body.command, body.timeout, workspace_path(body.cwd), max_output=body.max_output)

    @app.put("/sessions/{sid}/import")
    async def import_files(sid: str, request: Request):
        data = await request.body()
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "Archive is too large")
        await import_tar(await ensure(sid), data)
        return {"ok": True}

    @app.get("/sessions/{sid}/export")
    async def export(sid: str, path: str = WORKSPACE):
        name = await ensure(sid)
        try:
            response = await docker.request("GET", f"/containers/{name}/archive", params={"path": workspace_path(path)})
        except DockerError as exc:
            if exc.status == 404:
                raise HTTPException(404, f"No such file or folder: {path}") from exc
            raise
        return Response(response.content, media_type="application/x-tar")

    @app.post("/sessions/{sid}/clone")
    async def clone(sid: str, body: CloneInput):
        name = await ensure(sid)
        with tempfile.TemporaryDirectory() as work:
            target = str(Path(work) / "repo")
            branch = ["--branch", body.branch] if body.branch else []
            code, output = await _git([*_auth(body.url, body.token), "clone", "--no-recurse-submodules", *branch, "--", body.url, target], work, body.token)
            created = False
            if code != 0 and body.branch and re.search(r"not found|did not match", output, re.I):
                # A new branch: clone the default branch and start the new one from it.
                shutil.rmtree(target, ignore_errors=True)
                code, output = await _git([*_auth(body.url, body.token), "clone", "--no-recurse-submodules", "--", body.url, target], work, body.token)
                if code == 0:
                    code, output = await _git(["checkout", "-q", "-b", body.branch], target)
                    created = True
            if code != 0:
                raise SandboxError("git clone failed: " + output.strip()[-600:])
            builder = TarBuilder()
            for path in sorted(Path(target).rglob("*")):
                relative = path.relative_to(target).as_posix()
                if path.is_symlink():
                    builder.add_symlink(relative, os.readlink(path))
                elif path.is_dir():
                    builder.add_dir(relative)
                elif path.is_file():
                    builder.add_file(relative, path.read_bytes(), path.stat().st_mode)
            await import_tar(name, builder.getvalue())
            _, head = await _git(["rev-parse", "--abbrev-ref", "HEAD"], target)
        return {"ok": True, "branch": head.strip(), "created_branch": created}

    @app.post("/sessions/{sid}/push")
    async def push(sid: str, body: PushInput):
        name = await ensure(sid)
        bundle = f"/tmp/leo-{secrets.token_hex(12)}.bundle"
        script = ('set -e; cd /workspace; [ -d .git ] || git init -q -b "$LEO_BRANCH"; git add -A; '
                  'git diff --cached --quiet || git -c user.name="L.E.O. agent" -c user.email=agent@leo.local commit -q -m "$LEO_MESSAGE"; '
                  'git bundle create -q "$LEO_BUNDLE" HEAD')
        result = await docker.exec(name, script, 300, env={"LEO_BRANCH": body.branch, "LEO_MESSAGE": body.message, "LEO_BUNDLE": bundle})
        if result["exit_code"] != 0:
            raise SandboxError("Could not prepare the commit: " + result["output"].strip()[-600:])
        archive = (await docker.request("GET", f"/containers/{name}/archive", params={"path": bundle})).content
        await docker.exec(name, f'rm -f "{bundle}"', 30)
        with tempfile.TemporaryDirectory() as work:
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                member = next(m for m in source if m.isfile())
                Path(work, "changes.bundle").write_bytes(source.extractfile(member).read())
            repo = str(Path(work) / "repo.git")
            for args in (["init", "-q", "--bare", repo], ["-C", repo, "fetch", "-q", str(Path(work) / "changes.bundle"), f"HEAD:refs/heads/{body.branch}"]):
                code, output = await _git(args, work)
                if code != 0:
                    raise SandboxError("Could not read the workspace history: " + output.strip()[-600:])
            # Never forced: a branch that moved on the remote is reported, not overwritten.
            code, output = await _git(["-C", repo, *_auth(body.url, body.token), "push", "--", body.url,
                                       f"refs/heads/{body.branch}:refs/heads/{body.branch}"], work, body.token)
            _, commit = await _git(["-C", repo, "rev-parse", "--short", f"refs/heads/{body.branch}"], work)
        return {"ok": code == 0, "branch": body.branch, "commit": commit.strip(), "output": output.strip()}

    return app
