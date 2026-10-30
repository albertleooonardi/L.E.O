"""Merge benchmark result files into one comparison, using each L.E.O. run's call ledger for honest tokens.

    uv run python -m benchmarks.report results/a.json results/b.json [--api http://127.0.0.1:8000] > report.md

For L.E.O. runs, "actual" tokens come from completed calls only; failed or interrupted calls are shown separately
as "reserved" (budgets count them in case they were billed). The latest result per task and approach wins.
Provider errors (rate limits, outages) are listed but not counted as wrong answers.
"""
import argparse
import json
import statistics
from pathlib import Path

import httpx

PROVIDER_ERRORS = ("HTTP 429", "HTTP 503", "HTTP 500", "timed out", "rate limit", "Cannot reach")


def ledger(client, run_id):
    run = client.get(f"/api/runs/{run_id}").json()
    done = [c for c in run.get("calls", []) if c["status"] == "completed"]
    return (sum(c["input_tokens"] + c["output_tokens"] for c in done),
            sum(c["reserved_tokens"] for c in run.get("calls", []) if c["status"] != "completed"),
            [f"{c['stage']}→{c['model'].split('/')[-1].replace(':free', '')}" for c in done])


def load(paths, api):
    rows = {}
    with httpx.Client(base_url=api, timeout=30, trust_env=False) as client:
        for path in paths:
            for r in json.loads(Path(path).read_text())["results"]:
                tokens = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
                reserved, steps = 0, r.get("models", [])
                if r.get("run_id"):
                    try:
                        tokens, reserved, steps = ledger(client, r["run_id"])
                    except httpx.HTTPError:
                        pass
                error = r["status"] != "completed" and any(e in (r.get("error") or r.get("detail") or "") for e in PROVIDER_ERRORS)
                rows[(r["task"], r["arm"])] = {**r, "tokens": tokens, "reserved": reserved, "steps": steps, "provider_error": error}
    return rows


def render(rows):
    arms = list(dict.fromkeys(arm for _, arm in rows))
    tasks = list(dict.fromkeys(task for task, _ in rows))
    out = ["| Approach | Correct | Provider errors | Tokens per correct task | Mean seconds |", "| --- | --- | --- | --- | --- |"]
    for arm in arms:
        items = [rows[(t, arm)] for t in tasks if (t, arm) in rows]
        judged = [r for r in items if not r["provider_error"]]
        passed = [r for r in judged if r["passed"]]
        per = round(sum(r["tokens"] for r in judged) / len(passed)) if passed else None
        secs = statistics.mean(r["seconds"] for r in judged) if judged else 0
        out.append(f"| {arm} | {len(passed)}/{len(judged)} | {len(items) - len(judged)} | {'—' if per is None else f'{per:,}'} | {secs:.0f} |")
    out += ["", "| Task | " + " | ".join(arms) + " |", "| --- |" + " --- |" * len(arms)]
    for task in tasks:
        cells = []
        for arm in arms:
            r = rows.get((task, arm))
            if not r:
                cells.append("—")
            elif r["provider_error"]:
                cells.append("provider error")
            else:
                note = "" if r["detail"] in ("passed",) else f" ({r['detail'].split(';')[0]})"
                reserved = f" +{r['reserved']:,} reserved" if r["reserved"] else ""
                cells.append(f"{'✅' if r['passed'] else '❌'} {r['tokens']:,}{reserved}{note}".replace("|", "/"))
        out.append(f"| {task} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    print(render(load(args.files, args.api)))


if __name__ == "__main__":
    main()
