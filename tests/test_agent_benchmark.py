import subprocess
import sys

from benchmarks.agent import expectations, summarize_events
from benchmarks.agent_tasks import AGENT_TASKS

FIXED_CORE = '''import math
import re
from collections import Counter

WORD = re.compile(r"[a-z']+")


def words(text):
    return WORD.findall(text.lower())


def word_count(text):
    return len(words(text))


def top_words(text, n=3):
    return [w for w, _ in Counter(words(text)).most_common(n)]


def reading_time(text, wpm=200):
    return math.ceil(word_count(text) / wpm)
'''


def pytest_in(directory, *paths):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths], cwd=directory, capture_output=True, text=True)


def write(directory, files):
    for name, content in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def test_fix_task_starts_broken_and_its_checks_accept_a_real_fix(tmp_path):
    task = next(t for t in AGENT_TASKS if t["id"] == "fix")
    write(tmp_path, task["files"])
    assert pytest_in(tmp_path, "tests").returncode != 0
    write(tmp_path, {"textstats/core.py": FIXED_CORE})
    hidden = {name: content.replace('"/workspace"', repr(str(tmp_path))) for name, content in task["hidden"].items()}
    write(tmp_path, hidden)
    result = pytest_in(tmp_path, "tests", *hidden)
    assert result.returncode == 0, result.stdout


def test_long_task_starting_app_passes_its_own_tests(tmp_path):
    task = next(t for t in AGENT_TASKS if t["id"] == "long")
    write(tmp_path, task["files"])
    assert pytest_in(tmp_path, "tests").returncode == 0


def test_run_summary_counts_steps_subagents_and_recovery():
    events = [{"kind": "agent_step", "data": {"agent": "main", "model": "m1"}},
              {"kind": "subagent", "data": {"status": "started", "skill": "research"}},
              {"kind": "agent_step", "data": {"agent": "sub-1", "model": "m2"}},
              {"kind": "subagent", "data": {"status": "completed", "skill": "research"}},
              {"kind": "warning", "data": {"message": "Resumed after a worker interruption. Completed stages are reused."}},
              {"kind": "approval", "data": {"kind": "budget"}}]
    summary = summarize_events(events)
    assert summary["steps"] == 2 and summary["lead_steps"] == 1 and summary["subagent_skills"] == ["research"]
    assert summary["resumed"] and summary["approvals"] == ["budget"] and summary["models"] == ["m1", "m2"]
    long = next(t for t in AGENT_TASKS if t["id"] == "long")
    assert expectations(long, summary) == ["no coding sub-agent", "2 steps, expected at least 50"]
