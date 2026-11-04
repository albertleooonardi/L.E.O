"""Real sandboxes: these tests drive a running sandboxd and Docker. Skipped unless LEO_TEST_SANDBOX_URL is set.

    docker compose -f compose.yaml -f tests/compose.sandbox-test.yaml up -d --build sandboxd
    LEO_TEST_SANDBOX_URL=http://127.0.0.1:8100 uv run --frozen python -m pytest tests/test_sandbox_docker.py -v
"""
import io
import os
import subprocess
import uuid
import zipfile

import pytest

from backend.sandbox import Sandbox, SandboxError, zip_to_tar

URL = os.getenv("LEO_TEST_SANDBOX_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs a running sandboxd (set LEO_TEST_SANDBOX_URL)")


@pytest.fixture
async def box():
    sandbox = Sandbox(URL, os.getenv("LEO_SANDBOX_TOKEN", "leo-local-development"))
    sid = str(uuid.uuid4())
    await sandbox.ensure(sid)
    yield sandbox, sid
    await sandbox.delete(sid)


async def run(box, command, timeout=60):
    sandbox, sid = box
    return await sandbox.exec(sid, command, timeout)


async def test_runs_as_unprivileged_user_without_capabilities(box):
    result = await run(box, "id -u; grep CapEff /proc/self/status; touch /etc/x 2>&1 || echo denied; command -v sudo || echo no-sudo")
    lines = result["output"].split()
    assert lines[0] == "1000" and "0000000000000000" in result["output"]
    assert "denied" in result["output"] and "no-sudo" in result["output"]


async def test_files_round_trip_and_listing(box):
    sandbox, sid = box
    await sandbox.write_file(sid, "src/app.py", b"print('hi')\n")
    assert await sandbox.read_file(sid, "src/app.py") == b"print('hi')\n"
    assert (await run(box, "python src/app.py"))["output"].strip() == "hi"
    assert {"path": "src/app.py", "type": "file", "size": 12} in await sandbox.list_files(sid)
    owner = await run(box, "stat -c %u src/app.py")
    assert owner["output"].strip() == "1000"


async def test_rewritten_module_is_not_served_from_a_stale_pyc(box):
    # Python reuses __pycache__ when the source's mtime and size match, so a same-length edit needs a fresh mtime.
    sandbox, sid = box
    await sandbox.write_file(sid, "mod.py", b"VALUE = 1\n")
    assert (await run(box, "python -c 'import mod; print(mod.VALUE)'"))["output"].strip() == "1"
    await sandbox.write_file(sid, "mod.py", b"VALUE = 2\n")
    assert (await run(box, "python -c 'import mod; print(mod.VALUE)'"))["output"].strip() == "2"


async def test_uploaded_zip_is_unpacked(box):
    sandbox, sid = box
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("proj-main/pkg/__init__.py", "")
        archive.writestr("proj-main/pkg/calc.py", "def add(a, b):\n    return a + b\n")
    await sandbox.import_tar(sid, zip_to_tar(buffer.getvalue()))
    result = await run(box, "python -c 'from pkg.calc import add; print(add(2, 3))'")
    assert result["output"].strip() == "5"


async def test_command_timeout_kills_the_process_group(box):
    # `set -m` starts the second job in a process group of its own, out of timeout's reach.
    result = await run(box, "sleep 300 & set -m; sleep 301 >/dev/null 2>&1 & set +m; sleep 302", timeout=3)
    assert result["timed_out"]
    # Count by full command line: the container's own `sleep infinity` keep-alive must not count, and a reaped-late
    # zombie shows as `[sleep] <defunct>`, which is not a running leak.
    left = await run(box, "ps -eo args | grep -cxE 'sleep 30[012]' || true")
    assert left["output"].strip() == "0"


async def test_symlink_planted_by_the_agent_cannot_redirect_writes(box):
    sandbox, sid = box
    await run(box, "ln -s /etc /workspace/evil")
    with pytest.raises(SandboxError):
        await sandbox.write_file(sid, "evil/pwned", b"owned")
    assert "missing" in (await run(box, "test -e /etc/pwned && echo present || echo missing"))["output"]


async def test_network_reaches_registries_only(box):
    blocked = await run(box, "curl -sS -m 10 https://example.com -o /dev/null -w '%{http_code}' 2>&1; echo")
    assert "403" in blocked["output"] or "Forbidden" in blocked["output"]
    direct = await run(box, "curl -sS -m 5 --noproxy '*' https://1.1.1.1 2>&1 || echo no-route")
    assert "no-route" in direct["output"]
    internal = await run(box, "curl -sS -m 5 --noproxy '*' http://sandboxd:8100/health 2>&1 || echo unreachable")
    assert "unreachable" in internal["output"]
    pip = await run(box, "pip install --quiet --no-cache-dir six && python -c 'import six; print(six.__version__)'", timeout=180)
    assert pip["exit_code"] == 0, pip["output"]


async def test_push_goes_through_a_bundle_and_keeps_the_token_out(box):
    sandbox, sid = box
    container = os.getenv("LEO_TEST_SANDBOXD_CONTAINER", "leo-sandboxd-1")
    remote = f"/tmp/remote-{uuid.uuid4().hex[:8]}.git"
    subprocess.run(["docker", "exec", container, "git", "init", "-q", "--bare", remote], check=True)
    await sandbox.write_file(sid, "hello.txt", b"hello\n")
    # A hostile hook in the workspace must never run outside the sandbox.
    await run(box, "git init -q && mkdir -p .git/hooks && printf '#!/bin/sh\\ntouch /tmp/hook-ran\\n' > .git/hooks/pre-push && chmod +x .git/hooks/pre-push")
    result = await sandbox.push(sid, f"file://{remote}", "agent-work", "secret-token", "Add hello")
    assert result["ok"], result["output"]
    log = subprocess.run(["docker", "exec", container, "git", "-C", remote, "log", "--format=%s", "agent-work"], capture_output=True, text=True)
    assert log.stdout.strip() == "Add hello"
    hook = subprocess.run(["docker", "exec", container, "test", "-e", "/tmp/hook-ran"])
    assert hook.returncode != 0
    assert "secret-token" not in result["output"]
