"""LocalWorkspace: the six operations agent mode uses, on a folder of the user's own machine.

The server's agents work in a Docker sandbox; L.E.O.C.O.D.E's agents work here, as the user, with no sandbox. Files stay
inside the project folder (paths are resolved through symlinks first), and the permission policy decides what may run
without asking.
"""
import asyncio
import functools
import os
import platform
import shutil
import signal
import stat
import subprocess
from pathlib import Path

from backend.sandbox import SandboxError

SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".leo-cli"}
KEEP = 4_000_000  # Bytes of command output kept before the middle is dropped.
WINDOWS = platform.system() == "Windows"


@functools.cache
def find_bash():
    """The bash that runs agent commands. On Windows that is Git Bash: the `bash` on PATH there is often the WSL
    launcher, which would run commands in a different Linux system with its own files and tools."""
    if not WINDOWS:
        return shutil.which("bash") or "/bin/bash"
    candidates = [os.getenv("LEO_CLI_BASH")]
    git = shutil.which("git")
    if git:  # ...\Git\cmd\git.exe or ...\Git\mingw64\bin\git.exe
        install = Path(git).resolve().parents[1 if Path(git).parent.name == "cmd" else 2]
        candidates.append(str(install / "bin" / "bash.exe"))
    for base in (os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
    found = shutil.which("bash")
    if found and "windowsapps" not in found.lower() and "system32" not in found.lower():
        candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise SandboxError("L.E.O.C.O.D.E runs commands with bash; install Git for Windows (it includes Git Bash), "
                       "or set LEO_CLI_BASH to a bash.exe")


def environment():
    system = {"Darwin": "macOS"}.get(platform.system(), platform.system())
    shell = (" Commands run in Git Bash: use Unix commands and forward slashes; C:\\ is /c/ there. Prefer `python` over `python3`."
             if WINDOWS else "")
    return (f"Environment: the user's own computer ({system}, {platform.machine()}), as the user, with the tools installed there.{shell} "
            "There is no sandbox: changes are real. Edits and commands may need the user's approval first; if one is declined, "
            "continue another way or ask the user.")


def kill_tree(pid):
    """Stop a command and everything it started: its process group on Unix, its process tree on Windows."""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


class LocalWorkspace:
    tree = True  # The agents work as a tree (backend/tree.py).

    def __init__(self, root, policy=None, skills=None, on_write=None, mcp=None):
        self.root = os.path.realpath(root)
        self.place = f"the user's own computer, in the project folder {self.root}"
        self.environment = environment()
        self.policy, self.skills, self.on_write, self.mcp = policy, skills, on_write, mcp

    def extra_tools(self):
        """The tools of the user's MCP servers, for the agent (empty until the session has started them)."""
        return list(self.mcp.specs) if self.mcp else []

    def extra_read_tools(self):
        """The MCP tools that only read, which agents that do not change things may use too."""
        return [spec for spec in self.mcp.specs if self.mcp.index[spec["name"]]["read_only"]] if self.mcp else []

    async def call_tool(self, name, arguments):
        if not self.mcp:
            return f"Error: there is no tool called {name}."
        return await self.mcp.call(name, arguments)

    def resolve(self, path):
        """The real path for an agent-given path; refused unless it stays inside the project folder."""
        candidate = Path(path or ".")
        full = os.path.realpath(candidate if candidate.is_absolute() else Path(self.root) / candidate)
        if full != self.root and not full.startswith(self.root + os.sep):
            raise SandboxError(f"Paths must stay inside {self.root}: {path}")
        return full

    async def ensure(self, sid):
        if not os.path.isdir(self.root):
            raise SandboxError(f"The project folder is gone: {self.root}")
        return {"id": sid, "status": "running"}

    async def exec(self, sid, command, timeout=120, cwd=None, max_output=30000):
        """Run a command with bash in the project folder; a timeout stops it and everything it started.

        Not a login shell: `leo` was started from the user's shell, so PATH and the rest are already right, and profile
        scripts would print their noise into every result."""
        # Its own session (Unix) or process group (Windows), so a timeout can stop the whole tree.
        detach = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
        process = await asyncio.create_subprocess_exec(find_bash(), "-c", command, cwd=self.resolve(cwd or "."), stdin=asyncio.subprocess.DEVNULL,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **detach)
        output = bytearray()

        async def collect():  # Read as it comes, so a command that times out still shows what it printed.
            while chunk := await process.stdout.read(65536):
                if len(output) < KEEP:
                    output.extend(chunk[:KEEP - len(output)])

        reader = asyncio.create_task(collect())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await asyncio.to_thread(kill_tree, process.pid)  # Background children go too.
            await process.wait()
        except asyncio.CancelledError:  # Stopped, or paused for an approval elsewhere: it has its own session, so it would run on.
            await asyncio.to_thread(kill_tree, process.pid)
            reader.cancel()
            raise
        try:
            await asyncio.wait_for(reader, 2)  # A background child may still hold the pipe; never wait on it for long.
        except asyncio.TimeoutError:
            reader.cancel()
        text = bytes(output).decode("utf-8", "replace")
        truncated = len(text) > max_output
        if truncated:
            half = max_output // 2
            text = f"{text[:half]}\n…[{len(text) - max_output} characters omitted]…\n{text[-half:]}"
        return {"exit_code": process.returncode if process.returncode is not None else -1, "output": text, "timed_out": timed_out, "truncated": truncated}

    async def read_file(self, sid, path, with_mode=False):
        target = self.resolve(path)
        try:
            data, mode = Path(target).read_bytes(), stat.S_IMODE(os.stat(target).st_mode)
        except IsADirectoryError as exc:
            raise SandboxError(f"{path} is a folder; use list_files") from exc
        except FileNotFoundError as exc:
            raise SandboxError(f"No such file: {path}") from exc
        return (data, mode) if with_mode else data

    async def write_file(self, sid, path, data, mode=0o644):
        target = self.resolve(path)
        if target == self.root:
            raise SandboxError("Give a file path, not the project folder")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        # The parent is checked again now it exists: a symlinked folder must not lead out of the project.
        self.resolve(os.path.dirname(target))
        Path(target).write_bytes(data)
        os.chmod(target, mode)
        if self.on_write:
            self.on_write(target)

    async def list_files(self, sid, path=".", depth=3, limit=2000):
        start = Path(self.resolve(path))
        entries = []

        def walk(folder, level):
            try:
                children = sorted(folder.iterdir(), key=lambda p: p.name)
            except OSError:
                return
            for child in children:
                if len(entries) >= limit:
                    return
                if child.name in SKIP:
                    continue
                directory = child.is_dir() and not child.is_symlink()
                entries.append({"path": Path(os.path.relpath(child, self.root)).as_posix(), "type": "dir" if directory else "file",
                                "size": 0 if directory else child.lstat().st_size})
                if directory and level < depth:
                    walk(child, level + 1)

        if not start.is_dir():
            raise SandboxError(f"Not a folder: {path}")
        walk(start, 1)
        return entries
