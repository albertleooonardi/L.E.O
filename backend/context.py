import ast
import json
import re


def token_bound(messages):
    """Conservative byte-based reservation, not a provider-specific token count."""
    return sum(len(m["content"].encode("utf-8")) + 32 for m in messages) + 128


def prepare_context(history, system, prompt, limit):
    required = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if token_bound(required) > limit:
        raise ValueError("This request exceeds the context allowance. Shorten it or increase Context tokens in Settings.")
    # Preserve the latest request and system instructions exactly. Retain whole recent turns.
    turns = []
    for message in history:
        if message["role"] == "user":
            turns.append([])
        if turns:
            turns[-1].append({"role": message["role"], "content": message["content"]})
    kept = []
    for turn in reversed(turns):
        trial = [required[0], *turn, *kept, required[1]]
        if token_bound(trial) > limit:
            break
        kept = turn + kept
    result = [required[0], *kept, required[1]]
    return result, len(history) - len(kept)


def static_review(text):
    results = []
    for language, code in re.findall(r"```([\w+-]*)[^\n]*\n(.*?)```", text, re.S):
        language = language.lower()
        if language not in {"python", "py", "json"}:
            continue
        try:
            if language == "json":
                json.loads(code)
            else:
                ast.parse(code)
            results.append({"language": language, "passed": True, "message": "Syntax check passed"})
        except (SyntaxError, ValueError, RecursionError) as exc:
            # This is parsing only. Generated code is never executed by the API or worker.
            results.append({"language": language, "passed": False, "message": str(exc)[:300]})
    return results


def parse_review(text):
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("passed")) is not bool:
            raise ValueError()
        issues = value.get("issues", [])
        if not isinstance(issues, list) or not all(isinstance(i, str) for i in issues):
            raise ValueError()
        return {"passed": value["passed"], "issues": issues[:8]}
    except (ValueError, TypeError):
        return {"passed": False, "issues": ["Reviewer did not return a valid verdict; result remains unverified."]}


def parse_plan(text, skill_ids=()):
    """Accept at most three tasks with backward dependencies; cycles are impossible.

    Optional skill/difficulty labels are kept only when valid; the router fills in anything missing."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        tasks = json.loads(raw)["tasks"]
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 3:
            raise ValueError()
        result = []
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise ValueError()
            instruction = task.get("instruction")
            dependencies = task.get("depends_on", [])
            if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 3000:
                raise ValueError()
            if not isinstance(dependencies, list) or any(type(d) is not int or not 1 <= d <= index for d in dependencies):
                raise ValueError()
            skill, difficulty = task.get("skill"), task.get("difficulty")
            result.append({"id": index + 1, "instruction": instruction, "depends_on": list(dict.fromkeys(dependencies)),
                           "skill": skill if skill in skill_ids else None,
                           "difficulty": difficulty if difficulty in {"light", "standard", "strong"} else None})
        return result
    except (ValueError, TypeError, KeyError):
        return []
