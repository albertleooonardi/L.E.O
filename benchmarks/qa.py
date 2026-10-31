"""Grade saved replies from several assistants on the same QA tasks.

Layout: <round>/<assistant>/<task>.md, one reply per file, exactly as the assistant answered. Coding tasks run the
hidden tests from benchmarks/tasks.py; pages get the static rubric plus page checks; reasoning tasks compare the final
number.

    uv run python -m benchmarks.qa .leo-bench/qa/r1               # grade every assistant's replies
    uv run python -m benchmarks.qa .leo-bench/qa/r1 --leo          # first ask the running L.E.O. (port 8000), then grade
    uv run python -m benchmarks.qa .leo-bench/qa/r1 --leo --tasks=coffee
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

from .run import acceptance, extract_page, leo
from .tasks import TASKS

QA_TASKS = {t["id"]: t for t in TASKS}
QA_TASKS["stairs"] = {"id": "stairs", "kind": "number", "answer": 927,
                      "prompt": "In how many different ways can you climb a staircase of 12 steps if each move climbs 1, 2, or 3 "
                                "steps? Give the final number."}
QA_TASKS["coffee"] = {"id": "coffee", "kind": "page", "prompt": "Create me a minimalist, clean dan informatic landing page about coffee"}


def page_checks(html):
    """Defects a visitor would notice: links to sections that do not exist, and placeholder text left in the page."""
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    broken = sorted({a for a in re.findall(r'href="#([^"]+)"', html) if a not in ids})
    placeholders = re.findall(r"\[[^\]\n]{3,40}(image|photo|placeholder|imagery)[^\]\n]*\]|lorem ipsum", html, re.I)
    return broken, len(placeholders)


def grade(task, text):
    if task.get("kind") == "number":
        numbers = re.findall(r"\b\d[\d,]*\b", text)
        final = numbers[-1].replace(",", "") if numbers else None
        bold = re.findall(r"\*\*\s*(\d[\d,]*)\s*\*\*", text)  # "There are **927** ways" states the answer mid-sentence.
        final = bold[-1].replace(",", "") if bold else final
        return final == str(task["answer"]), f"answer {final}"
    ok, detail = acceptance(text, task)
    if task.get("kind") == "page" and (page := extract_page(text)):
        broken, placeholders = page_checks(page)
        detail += f"; broken links: {len(broken)}{' (' + ', '.join(broken) + ')' if broken else ''}; placeholders: {placeholders}"
    return ok, detail


QA_ORDER = ["ipv4", "duration", "roman", "stairs", "coffee"]


async def ask_leo(root, only=None):
    """Send each QA prompt to L.E.O. in Auto mode and save the reply and its token use."""
    out = root / "leo"
    out.mkdir(parents=True, exist_ok=True)
    usage = json.loads((out / "usage.json").read_text()) if (out / "usage.json").exists() else {}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=60, trust_env=False) as client:
        for task_id in only or QA_ORDER:
            result = await leo(client, QA_TASKS[task_id]["prompt"], "auto")
            (out / f"{task_id}.md").write_text(result["output"] or "")
            usage[task_id] = {k: result.get(k) for k in ("status", "error", "calls", "input_tokens", "output_tokens", "seconds", "models")}
            print(task_id, json.dumps(usage[task_id]))
    (out / "usage.json").write_text(json.dumps(usage, indent=2))


def main(folder):
    root = Path(folder)
    for arm in sorted(p for p in root.iterdir() if p.is_dir()):
        for reply in sorted(arm.glob("*.md")):
            task = QA_TASKS.get(reply.stem)
            if task:
                ok, detail = grade(task, reply.read_text())
                print(f"{arm.name:10} {reply.stem:9} {'PASS' if ok else 'FAIL'}  {detail}")


if __name__ == "__main__":
    if "--leo" in sys.argv:
        tasks = next((a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--tasks=")), None)
        asyncio.run(ask_leo(Path(sys.argv[1]), tasks))
    main(sys.argv[1])
