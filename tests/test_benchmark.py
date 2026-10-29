from benchmarks.run import acceptance, extract_code
from benchmarks.tasks import TASKS


def test_extract_code_picks_the_block_defining_the_symbol():
    text = "Usage:\n```python\nprint(add(1, 2))\n```\nCode:\n```python\ndef add(a, b):\n    return a + b\n```"
    assert extract_code(text, 'add').startswith('def add')
    assert extract_code('No code here.', 'add') is None


def test_acceptance_runs_hidden_tests_with_timeout():
    task = next(t for t in TASKS if t['id'] == 'intervals')
    good = "```python\ndef merge_intervals(iv):\n    out = []\n    for s, e in sorted(list(i) for i in iv):\n        if out and s <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], e)\n        else:\n            out.append([s, e])\n    return out\n```"
    assert acceptance(good, task) == (True, 'passed')
    assert acceptance("```python\ndef merge_intervals(iv):\n    return iv\n```", task)[0] is False
    hang = "```python\ndef merge_intervals(iv):\n    return iv\nwhile True:\n    pass\n```"
    assert acceptance(hang, task, timeout=2) == (False, 'timed out')
