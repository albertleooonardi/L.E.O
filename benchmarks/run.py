"""Compare single-model calls with L.E.O. orchestration on the same prompts (spec §9).

Baselines call one saved connection directly with only the user prompt: one request, no system prompt,
no review. L.E.O. arms submit the same prompt to a running L.E.O. server in a fresh conversation and use
its current Settings (roles, limits, routing). Keys are decrypted inside this process from the server's
own store and never printed.

Generated code is executed only here, for acceptance testing, inside a macOS sandbox that blocks network
access and writes outside a throwaway directory, with a timeout.

    LEO_DATA_DIR=... uv run --frozen python -m benchmarks.run --cheap nemotron --strong gemini
"""
import argparse
import asyncio
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from backend import providers
from backend.config import Config
from backend.store import Store
from .tasks import TASKS

RESULTS = Path(__file__).parent / "results"
ARMS = ["single-cheap", "single-strong", "leo-auto", "leo-quick", "leo-reviewed", "leo-deep"]


def extract_code(text, symbol):
    """The first Python block defining the required function/class, else the whole reply if it parses."""
    blocks = re.findall(r"```(?:python|py)?[^\n]*\n(.*?)```", text, re.S)
    for block in blocks:
        if re.search(rf"^\s*(def|class)\s+{re.escape(symbol)}\b", block, re.M):
            return block
    return text if re.search(rf"^\s*(def|class)\s+{re.escape(symbol)}\b", text, re.M) else None


def acceptance(text, task, timeout=10):
    symbol = re.search(r"from solution import (\w+)", task["tests"]).group(1)
    code = extract_code(text, symbol)
    if code is None:
        return False, f"no code defining {symbol}"
    workdir = Path(tempfile.mkdtemp(prefix="leo-bench-"))
    try:
        (workdir / "solution.py").write_text(code)
        (workdir / "check.py").write_text(task["tests"])
        command = [sys.executable, "-B", "-s", "-E", "check.py"]
        if shutil.which("sandbox-exec"):
            profile = (f'(version 1)(allow default)(deny network*)(deny file-write*)'
                       f'(allow file-write* (subpath "{workdir.resolve()}") (literal "/dev/null"))')
            command = ["sandbox-exec", "-p", profile, *command]
        try:
            done = subprocess.run(command, cwd=workdir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "timed out"
        if done.returncode == 0:
            return True, "passed"
        lines = (done.stderr or done.stdout).strip().splitlines()
        # Results may be shared; keep the local home directory out of error details.
        return False, lines[-1].replace(str(Path.home()), "~")[:200] if lines else f"exit {done.returncode}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def single(store, connection, prompt, prefs, retries=2):
    """One direct request, as if the user called the model themselves."""
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(retries + 1):
        started, text, usage, finish = time.monotonic(), "", {}, None
        try:
            async for chunk in providers.stream(connection, store.decrypt(connection), messages, prefs["max_output_tokens"], prefs["timeout_seconds"]):
                text += chunk.text
                if chunk.input_tokens is not None:
                    usage["input"] = chunk.input_tokens
                if chunk.output_tokens is not None:
                    usage["output"] = chunk.output_tokens
                finish = chunk.finish or finish
            return {"status": "completed", "output": text, "calls": 1, "attempts": attempt + 1,
                    "input_tokens": usage.get("input"), "output_tokens": usage.get("output"),
                    "seconds": round(time.monotonic() - started, 1), "models": [connection["model"]], "finish": finish}
        except providers.ProviderError as exc:
            if not exc.retryable or attempt == retries:
                return {"status": "error", "error": str(exc), "calls": attempt + 1, "attempts": attempt + 1, "output": "",
                        "seconds": round(time.monotonic() - started, 1), "models": [connection["model"]]}
            await asyncio.sleep(min(exc.retry_after or 20, 60))


async def leo(client, prompt, mode):
    started = time.monotonic()
    response = await client.post("/api/runs", json={"prompt": prompt, "mode": mode, "request_id": str(uuid.uuid4())})
    response.raise_for_status()
    run_id = response.json()["id"]
    while True:
        await asyncio.sleep(2)
        run = (await client.get(f"/api/runs/{run_id}")).json()
        if run["status"] in {"completed", "failed", "cancelled"}:
            break
    usage = run["usage"]
    return {"status": run["status"], "error": run.get("error"), "output": run["output"], "run_id": run_id,
            "calls": usage.get("calls", 0), "uncertain_calls": usage.get("uncertain_calls", 0),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "estimated": usage.get("estimated"), "review_passed": usage.get("review_passed"),
            "seconds": round(time.monotonic() - started, 1),
            "models": [f"{c['stage']}:{c['model']}:{c['status']}" for c in run["calls"]]}


def find_connection(store, fragment):
    matches = [c for c in store.list_connections(True) if fragment.lower() in (c["name"] + " " + c["model"]).lower()]
    if len(matches) != 1:
        raise SystemExit(f"--cheap/--strong '{fragment}' must match exactly one saved connection; found {len(matches)}")
    return matches[0]


def summarize(results, arms):
    rows = []
    for arm in arms:
        items = [r for r in results if r["arm"] == arm]
        if not items:
            continue
        passed = [r for r in items if r["passed"]]
        tokens = [(r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in items]
        rows.append({
            "arm": arm, "tasks": len(items), "passed": len(passed), "errors": sum(r["status"] != "completed" for r in items),
            "pass_rate": round(len(passed) / len(items), 2),
            "mean_tokens": round(statistics.mean(tokens)), "total_tokens": sum(tokens),
            "tokens_per_pass": round(sum(tokens) / len(passed)) if passed else None,
            "mean_calls": round(statistics.mean(r["calls"] for r in items), 1),
            "mean_seconds": round(statistics.mean(r["seconds"] for r in items), 1),
        })
    return rows


def markdown(rows, results, meta):
    lines = [f"# L.E.O. benchmark · {meta['started']}", "",
             f"Cheap baseline: `{meta['cheap']}` · Strong baseline: `{meta['strong']}` · L.E.O. roles: {meta['roles']}", "",
             "| Approach | Passed | Errors | Mean tokens | Tokens per passed task | Mean calls | Mean seconds |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['arm']} | {r['passed']}/{r['tasks']} | {r['errors']} | {r['mean_tokens']:,} | "
                     f"{'—' if r['tokens_per_pass'] is None else format(r['tokens_per_pass'], ',')} | {r['mean_calls']} | {r['mean_seconds']} |")
    lines += ["", "| Task | " + " | ".join(r["arm"] for r in rows) + " |", "| --- |" + " --- |" * len(rows)]
    for task in dict.fromkeys(r["task"] for r in results):
        cells = []
        for row in rows:
            item = next((r for r in results if r["task"] == task and r["arm"] == row["arm"]), None)
            cells.append("—" if not item else ("pass" if item["passed"] else f"fail: {item['detail']}").replace("|", "/"))
        lines.append(f"| {task} | " + " | ".join(cells) + " |")
    lines += ["", "Tokens are provider-reported totals across every call in the approach (failed calls count their "
                  "reservation in L.E.O.). Reasoning tokens are included in output. Small samples: treat differences as "
                  "indicative, not statistically significant."]
    return "\n".join(lines) + "\n"


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--cheap", required=True, help="Text matching the inexpensive baseline connection")
    parser.add_argument("--strong", required=True, help="Text matching the stronger baseline connection")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--tasks", default=",".join(t["id"] for t in TASKS))
    parser.add_argument("--deep-tasks", default=None, help="Subset of tasks for leo-deep (it makes the most calls)")
    parser.add_argument("--pause", type=float, default=3, help="Seconds between requests, for free-tier rate limits")
    args = parser.parse_args()
    arms = [a for a in args.arms.split(",") if a]
    unknown = set(arms) - set(ARMS)
    if unknown:
        raise SystemExit(f"Unknown arms: {', '.join(sorted(unknown))}")
    tasks = [t for t in TASKS if t["id"] in args.tasks.split(",")]
    deep_tasks = set(args.deep_tasks.split(",")) if args.deep_tasks else {t["id"] for t in tasks}

    store = Store(Config())
    prefs = store.preferences()
    cheap, strong = find_connection(store, args.cheap), find_connection(store, args.strong)
    names = {c["id"]: c["model"] for c in store.list_connections()}
    meta = {"started": time.strftime("%Y-%m-%d %H:%M"), "cheap": cheap["model"], "strong": strong["model"],
            "roles": ", ".join(f"{r}={names.get(prefs.get(f'{r}_id'), 'auto')}" for r in ("generator", "planner", "reviewer")),
            "max_output_tokens": prefs["max_output_tokens"], "max_repairs": prefs["max_repairs"]}
    print(json.dumps(meta), flush=True)
    results = []
    RESULTS.mkdir(exist_ok=True)
    stem = RESULTS / time.strftime("benchmark-%Y%m%d-%H%M%S")
    async with httpx.AsyncClient(base_url=args.api, timeout=30) as client:
        for task in tasks:
            for arm in arms:
                if arm == "leo-deep" and task["id"] not in deep_tasks:
                    continue
                if arm.startswith("single"):
                    result = await single(store, cheap if arm == "single-cheap" else strong, task["prompt"], prefs)
                else:
                    result = await leo(client, task["prompt"], arm.removeprefix("leo-"))
                passed, detail = acceptance(result["output"], task) if result["output"] else (False, result.get("error") or "no output")
                result.update(task=task["id"], arm=arm, passed=passed, detail=detail)
                results.append(result)
                # Save after every run so a late failure never loses completed (and paid-for) work.
                stem.with_suffix(".json").write_text(json.dumps({"meta": meta, "results": results}, indent=2))
                print(f"{task['id']:<10} {arm:<14} {'PASS' if passed else 'FAIL'}  tokens={(result.get('input_tokens') or 0) + (result.get('output_tokens') or 0):>6}  "
                      f"calls={result['calls']}  {result['seconds']:>5}s  {'' if passed else detail}", flush=True)
                await asyncio.sleep(args.pause)
    store.engine.dispose()
    rows = summarize(results, arms)
    stem.with_suffix(".json").write_text(json.dumps({"meta": meta, "summary": rows, "results": results}, indent=2))
    stem.with_suffix(".md").write_text(markdown(rows, results, meta))
    print("\n" + markdown(rows, results, meta), flush=True)
    print(f"Saved {stem}.json and {stem}.md", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
