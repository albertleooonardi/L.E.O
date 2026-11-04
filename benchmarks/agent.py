"""Run the agent-mode benchmark (benchmarks/agent_tasks.py) against a running L.E.O. and its sandboxd.

Each task gets a fresh workspace: its starting files are uploaded, the prompt runs in Agent mode, and then hidden
tests are written into the same sandbox and run there, so they see exactly what the agent installed. Results
(pass/fail, steps, sub-agents, calls, tokens, cost, time) are saved after every task.

sandboxd has no host port in the default Compose file; publish it for the checks with the test override:

    docker compose -f compose.yaml -f tests/compose.sandbox-test.yaml up -d --build
    uv run --frozen python -m benchmarks.agent --user me --password ... --sandbox-url http://127.0.0.1:8100
    uv run --frozen python -m benchmarks.agent ... --tasks long --restart-worker-after 120 --approve-budget

To compare with another agent (Claude Code, Antigravity, …), give it the same prompts and starting files, run the
same checks, and add its row to the results file under "external".
"""
import argparse
import asyncio
import io
import json
import os
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

import httpx

from backend.sandbox import Sandbox
from .agent_tasks import AGENT_TASKS

RESULTS = Path(__file__).parent / "results"
TERMINAL = {"completed", "failed", "cancelled"}


def zipped(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def summarize_events(events):
    """Steps, sub-agents (with their skills), compactions, and approvals from a run's event log."""
    steps = [e for e in events if e["kind"] == "agent_step"]
    subagents = [e["data"] for e in events if e["kind"] == "subagent" and e["data"].get("status") == "started"]
    return {"steps": len(steps), "lead_steps": sum(e["data"]["agent"] == "main" for e in steps),
            "subagents": len(subagents), "subagent_skills": sorted({s["skill"] for s in subagents if s.get("skill")}),
            "compactions": sum(e["kind"] == "compacted" for e in events),
            "approvals": [e["data"]["kind"] for e in events if e["kind"] == "approval"],
            "resumed": any(e["kind"] == "warning" and "Resumed after a worker interruption" in e["data"].get("message", "") for e in events),
            "models": sorted({e["data"]["model"] for e in steps if e["data"].get("model")})}


def expectations(task, summary):
    """Problems with how the run went, beyond the hidden tests (e.g. the long task must delegate and keep going)."""
    expect, problems = task.get("expect", {}), []
    missing = [s for s in expect.get("subagent_skills", []) if s not in summary["subagent_skills"]]
    if missing:
        problems.append(f"no {', '.join(missing)} sub-agent")
    if summary["steps"] < expect.get("min_steps", 0):
        problems.append(f"{summary['steps']} steps, expected at least {expect['min_steps']}")
    return problems


async def events_of(client, run_id):
    """Every event of a run, read from the server-sent event stream."""
    events, cursor = [], 0
    while True:
        response = await client.get(f"/api/runs/{run_id}/events", params={"after": cursor}, timeout=60)
        batch, kind = [], None
        for line in response.text.splitlines():
            if line.startswith("id: "):
                cursor = int(line[4:])
            elif line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: ") and kind:
                batch.append({"id": cursor, "kind": kind, "data": json.loads(line[6:])})
        events += batch
        if len(batch) < 200:
            return events


async def run_task(client, sandbox, task, args):
    started = time.monotonic()
    workspace = (await client.post("/api/workspaces", json={"name": f"bench-{task['id']}"})).raise_for_status().json()
    if task["files"]:
        (await client.put(f"/api/workspaces/{workspace['id']}/upload", content=zipped(task["files"]),
                          headers={"Content-Type": "application/zip"})).raise_for_status()
    run = (await client.post("/api/runs", json={"prompt": task["prompt"], "mode": "agent", "conversation_id": workspace["id"],
                                                "request_id": str(uuid.uuid4())})).raise_for_status().json()
    restarted = False
    answered = set()
    while True:
        await asyncio.sleep(3)
        current = (await client.get(f"/api/runs/{run['id']}")).json()
        if args.restart_worker_after and not restarted and time.monotonic() - started > args.restart_worker_after and current["status"] == "running":
            print(f"  restarting the worker mid-run ({args.compose_cmd} restart worker)", flush=True)
            subprocess.run([*args.compose_cmd.split(), "restart", "worker"], check=False)
            restarted = True
        if current["status"] == "waiting":
            events = await events_of(client, run["id"])
            pending = [e["data"] for e in events if e["kind"] == "approval" and e["data"]["id"] not in answered]
            for approval in pending:
                approve = approval["kind"] in ("budget", "steps") and args.approve_budget
                print(f"  approval {approval['kind']}: {'approving' if approve else 'declining'}", flush=True)
                (await client.post(f"/api/runs/{run['id']}/approve", json={"approval_id": approval["id"], "approved": approve})).raise_for_status()
                answered.add(approval["id"])
        if current["status"] in TERMINAL:
            break
    seconds = round(time.monotonic() - started, 1)
    for name, content in task["hidden"].items():
        await sandbox.write_file(workspace["id"], name, content.encode())
    check = await sandbox.exec(workspace["id"], task["check"], timeout=600)
    changed = []
    for name in task.get("unchanged", []):
        try:
            if (await sandbox.read_file(workspace["id"], name)).decode() != task["files"][name]:
                changed.append(name)
        except Exception:
            changed.append(name)
    summary = summarize_events(await events_of(client, run["id"]))
    problems = expectations(task, summary) + [f"changed {n}" for n in changed]
    usage = current["usage"]
    return {"task": task["id"], "workspace": workspace["id"], "run": run["id"], "status": current["status"], "error": current.get("error"),
            "tests_passed": check["exit_code"] == 0, "passed": current["status"] == "completed" and check["exit_code"] == 0 and not problems,
            "problems": problems, "check_output": check["output"][-3000:], "seconds": seconds, "worker_restarted": restarted,
            "calls": usage.get("calls", 0), "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
            "estimated_cost": usage.get("estimated_cost", 0), **summary}


def table(results):
    lines = ["| Task | Passed | Tests | Steps | Sub-agents | Calls | Tokens | Cost | Time |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['task']} | {'yes' if r['passed'] else 'no'} | {'pass' if r['tests_passed'] else 'fail'} | {r['steps']} | "
                     f"{r['subagents']} ({', '.join(r['subagent_skills']) or '-'}) | {r['calls']} | {r['input_tokens'] + r['output_tokens']:,} | "
                     f"${r['estimated_cost']:.4f} | {r['seconds']:.0f}s |")
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--user", default=os.getenv("LEO_BENCH_USER"))
    parser.add_argument("--password", default=os.getenv("LEO_BENCH_PASSWORD"))
    parser.add_argument("--token", default=os.getenv("LEO_API_TOKEN"), help="Server access token instead of a user account")
    parser.add_argument("--sandbox-url", default=os.getenv("LEO_TEST_SANDBOX_URL", "http://127.0.0.1:8100"))
    parser.add_argument("--sandbox-token", default=os.getenv("LEO_SANDBOX_TOKEN", "leo-local-development"))
    parser.add_argument("--tasks", default=",".join(t["id"] for t in AGENT_TASKS))
    parser.add_argument("--approve-budget", action="store_true", help="Approve requests for more budget or steps (pushes are always declined)")
    parser.add_argument("--restart-worker-after", type=float, default=0, help="Restart the worker this many seconds into each task")
    parser.add_argument("--compose-cmd", default="docker compose")
    args = parser.parse_args()
    wanted = args.tasks.split(",")
    tasks = [t for t in AGENT_TASKS if t["id"] in wanted]
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    sandbox = Sandbox(args.sandbox_url, args.sandbox_token)
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / time.strftime("agent-%Y%m%d-%H%M%S.json")
    results = []
    async with httpx.AsyncClient(base_url=args.api, timeout=60, headers=headers) as client:
        if not args.token:
            if not (args.user and args.password):
                parser.error("give --user and --password (or LEO_BENCH_USER / LEO_BENCH_PASSWORD), or --token")
            (await client.post("/api/auth/login", json={"username": args.user, "password": args.password})).raise_for_status()
        for task in tasks:
            print(f"{task['id']}: running", flush=True)
            result = await run_task(client, sandbox, task, args)
            results.append(result)
            print(f"{task['id']}: {'PASSED' if result['passed'] else 'FAILED'} {'; '.join(result['problems'])}", flush=True)
            path.write_text(json.dumps({"started": time.strftime("%Y-%m-%d %H:%M"), "results": results, "external": []}, indent=2))
    print(table(results))
    print(f"Saved {path}")


if __name__ == "__main__":
    asyncio.run(main())
