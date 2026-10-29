"""Deterministic local fixture for manual end-to-end QA; never a production provider.

Replies name the model that produced them, so routing is visible in the UI. LEO_MOCK_DELAY slows streaming.
"""
import asyncio
import json
import os
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

PAGE = """```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ember & Oat Coffee</title>
<style>
  body { margin: 0; font-family: Georgia, serif; background: #f3ece2; color: #2b1d14; }
  header { padding: 64px 24px; text-align: center; background: #2b1d14; color: #f3ece2; }
  h1 { margin: 0; font-size: 44px; letter-spacing: -1px; }
  .menu { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; padding: 32px 24px; max-width: 760px; margin: 0 auto; }
  .item { background: #fff; border-radius: 10px; padding: 16px; }
  button { margin-top: 16px; padding: 10px 18px; border: 0; border-radius: 99px; background: #c8742c; color: #fff; font-size: 15px; cursor: pointer; }
</style>
</head>
<body>
<header><h1>Ember &amp; Oat</h1><p>Small-batch coffee, roasted on Fridays.</p><button id="order">Order ahead</button><p id="note"></p></header>
<section class="menu">
  <div class="item"><strong>Flat white</strong><p>$4.20</p></div>
  <div class="item"><strong>Oat cortado</strong><p>$4.50</p></div>
  <div class="item"><strong>Cardamom bun</strong><p>$3.80</p></div>
</section>
<script>
  document.getElementById('order').addEventListener('click', () => {
    document.getElementById('note').textContent = 'Order placed: pick it up in 10 minutes.';
  });
</script>
</body>
</html>
```
Built by MODEL from the QA fixture."""


@app.get('/v1/models')
def models():
    return {'data': [{'id': 'leo-test-model'}, {'id': 'mock-design'}, {'id': 'mock-code'}]}


@app.post('/v1/chat/completions')
async def chat(request: Request):
    body = await request.json()
    system, model = body['messages'][0]['content'], body.get('model', 'leo-test-model')
    chunk = 10
    if 'Return only JSON: {"passed"' in system:
        text = '{"passed": true, "issues": []}'
    elif 'execution plan' in system:
        text = json.dumps({'tasks': [
            {'instruction': 'Choose the palette, type, and section layout', 'depends_on': [], 'skill': 'design', 'difficulty': 'standard'},
            {'instruction': 'Build the single-page site with an order button', 'depends_on': [1], 'skill': 'frontend', 'difficulty': 'standard'},
        ]})
    elif 'single ```html block' in system:
        text, chunk = PAGE.replace('MODEL', model), 60
    else:
        text = f'This response comes from the local QA fixture ({model}).\n\n```python\ndef add(a, b):\n    return a + b\n```\n\nThe function returns the sum of its two inputs.'
    delay = float(os.getenv('LEO_MOCK_DELAY', '0.08'))  # Raise to watch streaming UI.

    async def generate():
        for i in range(0, len(text), chunk):
            yield 'data: ' + json.dumps({'choices': [{'delta': {'content': text[i:i + chunk]}, 'finish_reason': None}]}) + '\n\n'
            await asyncio.sleep(delay)
        yield 'data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 120, 'completion_tokens': len(text) // 4}}) + '\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(generate(), media_type='text/event-stream')
