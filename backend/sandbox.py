"""Client for sandboxd, plus the archive helpers both sides share.

Agent mode never touches Docker directly: the API and worker ask sandboxd (the only service holding the Docker
socket) to create a workspace container, run commands in it, and move files in and out as tar archives. Every
path is confined to /workspace, and every archive is rebuilt from validated members before it reaches a sandbox.
"""
import io
import posixpath
import stat
import tarfile
import time
import zipfile

import httpx

WORKSPACE = "/workspace"
AGENT_UID = 1000
MAX_IMPORT_BYTES = 200_000_000
MAX_IMPORT_FILES = 50_000
MAX_FILE_BYTES = 5_000_000


class SandboxError(Exception):
    pass


def workspace_path(path):
    """An absolute path inside /workspace for a model- or user-given path (relative paths start at /workspace)."""
    path = (path or ".").strip() or "."
    full = posixpath.normpath(posixpath.join(WORKSPACE, path))
    if full != WORKSPACE and not full.startswith(WORKSPACE + "/"):
        raise SandboxError(f"Paths must stay inside {WORKSPACE}: {path}")
    return full


def member_name(name):
    """A safe relative archive path, or None for absolute paths and anything that climbs out with '..'."""
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    clean = posixpath.normpath(name)
    if clean in (".", "") or clean == ".." or clean.startswith("../"):
        return None
    return clean


def _dir_info(name, mode=0o755):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.uid, info.gid, info.uname, info.gname = tarfile.DIRTYPE, mode, AGENT_UID, AGENT_UID, "agent", "agent"
    info.mtime = time.time()
    return info


def _file_info(name, size, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.uid, info.gid, info.uname, info.gname = size, mode, AGENT_UID, AGENT_UID, "agent", "agent"
    # TarInfo defaults to mtime 0. Python reuses __pycache__ when mtime and size match, so a same-length edit
    # would keep running the old code; make and other build tools also compare mtimes.
    info.mtime = time.time()
    return info


class TarBuilder:
    """Builds an archive owned by the sandbox user, adding parent directories before their contents."""

    def __init__(self, prefix=""):
        self.buffer, self.prefix, self.dirs, self.total, self.count = io.BytesIO(), prefix, set(), 0, 0
        self.tar = tarfile.open(fileobj=self.buffer, mode="w", format=tarfile.PAX_FORMAT)
        if prefix:
            self.tar.addfile(_dir_info(prefix, 0o700))

    def _parents(self, name):
        parts = name.split("/")[:-1]
        for n in range(1, len(parts) + 1):
            parent = "/".join(parts[:n])
            if parent not in self.dirs:
                self.dirs.add(parent)
                self.tar.addfile(_dir_info(posixpath.join(self.prefix, parent)))

    def _count(self, size):
        self.total += size
        self.count += 1
        if self.total > MAX_IMPORT_BYTES or self.count > MAX_IMPORT_FILES:
            raise SandboxError(f"Archive is too large (limit {MAX_IMPORT_BYTES // 1_000_000} MB, {MAX_IMPORT_FILES} files)")

    def add_dir(self, name, mode=0o755):
        name = member_name(name)
        if name and name not in self.dirs:
            self._parents(name)
            self.dirs.add(name)
            self.tar.addfile(_dir_info(posixpath.join(self.prefix, name), (mode & 0o777) | 0o700))

    def add_file(self, name, data, mode=0o644):
        name = member_name(name)
        if not name:
            return
        self._count(len(data))
        self._parents(name)
        self.tar.addfile(_file_info(posixpath.join(self.prefix, name), len(data), (mode & 0o777) | 0o600), io.BytesIO(data))

    def add_symlink(self, name, target):
        name = member_name(name)
        # Only links that stay inside the archive: a link to /etc or ../.. could redirect a later write.
        resolved = member_name(posixpath.join(posixpath.dirname(name), target)) if name and not target.startswith("/") else None
        if not name or not resolved:
            return
        self._count(0)
        self._parents(name)
        info = tarfile.TarInfo(posixpath.join(self.prefix, name))
        info.type, info.linkname, info.uid, info.gid, info.mode = tarfile.SYMTYPE, target, AGENT_UID, AGENT_UID, 0o777
        self.tar.addfile(info)

    def getvalue(self):
        self.tar.close()
        return self.buffer.getvalue()


def files_tar(files, mode=0o644):
    """An archive of {relative path: bytes}, for writing files into a workspace."""
    builder = TarBuilder()
    for name, data in files.items():
        builder.add_file(name, data, mode)
    return builder.getvalue()


def rebuild_tar(data, prefix=""):
    """Re-pack an untrusted tar: safe names only, sandbox ownership, no devices or hard links, contained symlinks."""
    builder = TarBuilder(prefix)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as source:
            for member in source:
                if member.isdir():
                    builder.add_dir(member.name, member.mode)
                elif member.isfile():
                    if member.size > MAX_IMPORT_BYTES:
                        raise SandboxError("Archive is too large")
                    builder.add_file(member.name, source.extractfile(member).read(), member.mode)
                elif member.issym():
                    builder.add_symlink(member.name, member.linkname)
    except tarfile.TarError as exc:
        raise SandboxError("Not a valid tar archive") from exc
    return builder.getvalue()


def zip_to_tar(data):
    """An uploaded zip as a workspace archive. A single top-level folder (as in GitHub downloads) is removed."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SandboxError("Upload a .zip file") from exc
    entries = [e for e in archive.infolist() if member_name(e.filename) and "__MACOSX" not in e.filename.split("/")]
    if len(entries) > MAX_IMPORT_FILES or sum(e.file_size for e in entries) > MAX_IMPORT_BYTES:
        raise SandboxError(f"Zip is too large (limit {MAX_IMPORT_BYTES // 1_000_000} MB uncompressed, {MAX_IMPORT_FILES} files)")
    named = [(e, member_name(e.filename)) for e in entries]
    tops = {name.split("/")[0] for _, name in named}
    top = next(iter(tops)) if len(tops) == 1 else None
    # Strip only a real wrapper folder: every entry is that folder or inside it.
    strip = top if top and all((name == top and e.is_dir()) or name.startswith(top + "/") for e, name in named) else None
    builder = TarBuilder()
    for entry, name in named:
        if strip:
            if name == strip:
                continue
            name = name[len(strip) + 1:]
        mode = (entry.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            continue  # Links are not needed to start a project and could point anywhere.
        if entry.is_dir():
            builder.add_dir(name)
        else:
            builder.add_file(name, archive.read(entry), 0o755 if mode & 0o111 else 0o644)
    return builder.getvalue()


def tar_to_zip(data):
    """A workspace export (Docker names members workspace/...) as a zip download."""
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for member in source:
            name = member.name.split("/", 1)[1] if "/" in member.name else ""
            if not name or not member.isfile():
                continue
            info = zipfile.ZipInfo(name)
            info.external_attr = (member.mode & 0o777 | stat.S_IFREG) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, source.extractfile(member).read())
    return output.getvalue()


def tar_file(data, with_mode=False):
    """The content (and permission bits) of the single file in an export of one path."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as source:
        for member in source:
            if member.isfile():
                if member.size > MAX_FILE_BYTES:
                    raise SandboxError(f"That file is larger than {MAX_FILE_BYTES // 1_000_000} MB; inspect it with bash instead")
                content = source.extractfile(member).read()
                return (content, member.mode & 0o777) if with_mode else content
            if member.isdir():
                raise SandboxError("That path is a directory")
    raise SandboxError("That path is not a regular file")


class Sandbox:
    """HTTP client for sandboxd. Session IDs are workspace IDs."""

    def __init__(self, url, token, transport=None):
        self.url, self.token, self.transport = url.rstrip("/"), token, transport

    async def _request(self, method, path, timeout=60, **kwargs):
        if not self.url:
            raise SandboxError("Agent mode needs the sandbox service; start L.E.O. with docker compose (LEO_SANDBOX_URL is not set)")
        try:
            headers = {"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})}
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), transport=self.transport, trust_env=False) as client:
                response = await client.request(method, self.url + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise SandboxError("Cannot reach the sandbox service; is the sandboxd container running?") from exc
        if not response.is_success:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise SandboxError(str(detail or f"Sandbox service returned HTTP {response.status_code}"))
        return response

    async def ensure(self, sid):
        return (await self._request("PUT", f"/sessions/{sid}", timeout=120)).json()

    async def delete(self, sid):
        await self._request("DELETE", f"/sessions/{sid}")

    async def exec(self, sid, command, timeout=120, cwd=None, max_output=30000):
        body = {"command": command, "timeout": timeout, "cwd": workspace_path(cwd or "."), "max_output": max_output}
        return (await self._request("POST", f"/sessions/{sid}/exec", json=body, timeout=timeout + 60)).json()

    async def import_tar(self, sid, data):
        await self._request("PUT", f"/sessions/{sid}/import", content=data, headers={"Content-Type": "application/x-tar"}, timeout=300)

    async def export(self, sid, path=WORKSPACE):
        return (await self._request("GET", f"/sessions/{sid}/export", params={"path": workspace_path(path)}, timeout=300)).content

    async def read_file(self, sid, path, with_mode=False):
        return tar_file(await self.export(sid, path), with_mode)

    async def write_file(self, sid, path, data, mode=0o644):
        target = workspace_path(path)
        if target == WORKSPACE:
            raise SandboxError("Give a file path, not the workspace folder")
        await self.import_tar(sid, files_tar({posixpath.relpath(target, WORKSPACE): data}, mode))

    async def list_files(self, sid, path=".", depth=3, limit=2000):
        """Files and folders under a path, without .git and dependency folders."""
        target = workspace_path(path)
        command = (f"find {_quote(target)} -mindepth 1 -maxdepth {int(depth)} \\( -name .git -o -name node_modules -o -name __pycache__ "
                   f"-o -name .venv -o -name venv \\) -prune -o -printf '%y %s %P\\n' | sort -k3 | head -n {int(limit)}")
        result = await self.exec(sid, command, timeout=30, max_output=400000)
        if result["exit_code"] != 0:
            raise SandboxError(result["output"].strip()[:300] or "Cannot list that folder")
        entries = []
        for line in result["output"].splitlines():
            kind, size, name = (line.split(" ", 2) + ["", ""])[:3]
            if name:
                entries.append({"path": posixpath.relpath(posixpath.join(target, name), WORKSPACE), "type": "dir" if kind == "d" else "file",
                                "size": int(size) if size.isdigit() else 0})
        return entries

    async def clone(self, sid, url, branch=None, token=None):
        return (await self._request("POST", f"/sessions/{sid}/clone", json={"url": url, "branch": branch, "token": token}, timeout=900)).json()

    async def push(self, sid, url, branch, token, message):
        body = {"url": url, "branch": branch, "token": token, "message": message}
        return (await self._request("POST", f"/sessions/{sid}/push", json=body, timeout=900)).json()


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"
