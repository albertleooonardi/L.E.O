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


def test_page_rubric_scores_structure_content_and_behavior():
    from benchmarks.run import extract_page, page_rubric
    task = next(t for t in TASKS if t['id'] == 'website')
    full = """```html
<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width"><title>Bean</title>
<style>@media (max-width: 600px) { nav { display: none; } }</style></head>
<body><header><nav><a href="#menu">Menu</a></nav><h1>Bean</h1><button id="o">Order now</button></header>
<main><section id="menu"><p>Latte $4.00</p><p>Mocha $4.50</p><p>Tea $3.00</p></section>
<section><h2>Hours</h2><p>Open daily 7–5. Address: 1 Main St</p></section></main>
<footer>Bean</footer><script>document.getElementById('o').addEventListener('click', () => {})</script></body></html>
```"""
    assert page_rubric(extract_page(full)) == (1.0, [])
    assert acceptance(full, task) == (True, 'rubric 100%')
    thin = "```html\n<html><body><h1>Coffee</h1><img src='x.jpg'></body></html>\n```"
    passed, detail = acceptance(thin, task)
    assert not passed and 'menu with 3+ prices' in detail and 'images have alt text' in detail
    assert acceptance('Here is some advice about coffee.', task) == (False, 'no HTML page')


def test_app_feature_checks_flag_missing_requirements():
    task = next(t for t in TASKS if t['id'] == 'longapp')
    app = """```html
<!doctype html><html><head><style>@media (max-width: 600px) {}</style></head><body>
<form id="f"><input required name="amount"><select name="category"></select><input type="month"></form>
<canvas id="chart"></canvas><button>Export CSV</button>
<script>
function edit(i) {} function remove(i) { items.splice(i, 1); }
localStorage.setItem('tx', '[]'); const blob = new Blob([csv], {type: 'text/csv'});
let balance = 0, income = 0, expense = 0; if (isNaN(x)) {}
</script></body></html>
```"""
    assert acceptance(app, task) == (True, 'features 100%')
    cdn = app.replace('<canvas id="chart"></canvas>', '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>')
    passed, detail = acceptance(cdn, task)
    assert 'chart without libraries' in detail and 'no external libraries' in detail
