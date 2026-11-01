import json

import httpx

from backend.router import STARTER_SKILLS, choose_mode, classify, rank
from backend.schemas import Connection
from .conftest import make_run, sse_response
from .test_orchestrator import execute

SKILLS = [{**s, "enabled": True} for s in STARTER_SKILLS]
GEMINI = {'id': 'g', 'name': 'Gemini', 'model': 'gemini-flash', 'skills': ['frontend', 'design', 'general'], 'tier': 'standard', 'reasoning': False, 'local': False, 'free': True, 'priority': 20}
NEMOTRON = {'id': 'n', 'name': 'Nemotron', 'model': 'nemotron', 'skills': ['coding', 'backend', 'reasoning'], 'tier': 'standard', 'reasoning': True, 'local': False, 'free': True, 'priority': 10}
SMALL = {'id': 's', 'name': 'Ollama', 'model': 'llama-small', 'skills': ['general'], 'tier': 'light', 'reasoning': False, 'local': True, 'free': True, 'priority': 30}
BIG = {'id': 'b', 'name': 'Big', 'model': 'big-coder', 'skills': ['coding', 'backend'], 'tier': 'strong', 'reasoning': False, 'local': False, 'free': True, 'priority': 40}
PREFS = {'routing': 'local_first'}


def paid(id, input_price, output_price, **extra):
    return {**GEMINI, 'id': id, 'model': id, 'free': False, 'input_price': input_price, 'output_price': output_price, 'priority': 1, **extra}


def test_auto_routing_weighs_every_chosen_model_by_cost():
    haiku, opus = paid('haiku', 0.1, 0.5), paid('opus', 5, 25)
    ordered, reason = rank([opus, haiku, GEMINI], 'frontend', 'standard', {'routing': 'auto'})
    assert [c['id'] for c in ordered] == ['g', 'haiku', 'opus'], 'free first, then the cheaper paid model, despite priority'
    assert reason.endswith('free')
    ordered, reason = rank([opus, haiku], 'frontend', 'standard', {'routing': 'auto'})
    assert ordered[0]['id'] == 'haiku' and '$0.0010 per call' in reason


def test_auto_routing_prices_paid_models_from_their_own_token_use():
    # Same prices, but one model has used far more output tokens per call (e.g. long hidden thinking).
    verbose = paid('verbose', 1, 5, avg_input_tokens=3000, avg_output_tokens=9000)
    concise = paid('concise', 1, 5, avg_input_tokens=3000, avg_output_tokens=600)
    assert rank([verbose, concise], 'frontend', 'standard', {'routing': 'auto'})[0][0]['id'] == 'concise'


def test_auto_routing_still_puts_fit_before_cost():
    strong_paid = paid('opus', 5, 25, tier='strong', skills=['coding', 'backend'])
    assert rank([SMALL, strong_paid], 'coding', 'strong', {'routing': 'auto'})[0][0]['id'] == 'opus', 'a free model too light for the task loses'


def test_classify_skill_and_difficulty():
    simple = classify('What is the capital of France?', SKILLS)
    assert simple['skill'] == 'general' and simple['difficulty'] == 'light' and choose_mode(simple) == 'quick'
    site = classify('Create me a coffee shop website using HTML', SKILLS)
    assert site['skill'] == 'frontend' and site['difficulty'] == 'standard' and choose_mode(site) == 'quick', 'one page, one call'
    styled = classify('Create me a minimalist, clean dan informatic landing page about coffee', SKILLS)
    assert styled['skill'] == 'frontend' and 'single-page build' in styled['signals'] and choose_mode(styled) == 'quick'
    app = classify('Build a complete personal finance tracker app as a single HTML file', SKILLS)
    assert app['difficulty'] == 'strong' and 'build request' in app['signals'], 'an app or scope words keep the full treatment'
    full = classify('Build a coffee shop website with an order API and a database for orders', SKILLS)
    assert {'frontend', 'backend'} <= set(full['skills']) and choose_mode(full) == 'deep'
    assert classify('Create a website for my coffee shop', SKILLS)['skill'] == 'frontend'
    assert classify('Write an HTML hero section', SKILLS)['skill'] == 'frontend'
    assert classify('Build an API for a coffee shop website', SKILLS)['skill'] == 'backend'
    function = classify('Write a Python function that reverses a string', SKILLS)
    assert function['skill'] == 'coding' and function['difficulty'] == 'standard' and choose_mode(function) == 'quick'


def test_rank_prefers_capable_specialist_then_cheapest_tier():
    models = [GEMINI, NEMOTRON, SMALL, BIG]
    assert rank(models, 'frontend', 'standard', PREFS)[0][0]['id'] == 'g'
    assert rank(models, 'backend', 'standard', PREFS)[0][0]['id'] == 'n', 'standard tier is enough; strong costs more'
    assert rank(models, 'backend', 'strong', PREFS)[0][0]['id'] == 'b'
    ordered, reason = rank(models, 'general', 'light', PREFS)
    assert ordered[0]['id'] == 's' and 'light tier fits light work' in reason
    assert rank(models, 'coding', 'light', PREFS)[0][0]['id'] != 'n', 'reasoning models are avoided for light work'
    assert rank([SMALL], 'frontend', 'strong', PREFS)[1].startswith('no model lists this skill')
    assert 'models with the frontend skill are too light for strong work' in rank([GEMINI, BIG], 'frontend', 'strong', PREFS)[1]


async def test_deep_routes_each_subtask_to_its_specialist(store):
    store.save_connection(Connection(name='Gemini', base_url='http://gemini.test/v1', model='gemini-flash', free=True,
                                     skills=['frontend', 'design', 'general']).model_dump(), user_id=store.user_id)
    store.save_connection(Connection(name='Coder', base_url='http://coder.test/v1', model='coder', free=True,
                                     skills=['backend', 'coding']).model_dump(), user_id=store.user_id)
    seen = []
    def handler(request):
        body = json.loads(request.content)
        system, prompt = body['messages'][0]['content'], body['messages'][-1]['content']
        if 'execution plan' in system:
            seen.append(('plan', request.url.host))
            assert 'frontend (' in system, 'planner sees the skill catalog'
            return sse_response(json.dumps({'tasks': [
                {'instruction': 'Design the menu page look', 'depends_on': [], 'skill': 'design', 'difficulty': 'standard'},
                {'instruction': 'Write the order API', 'depends_on': [], 'skill': 'backend', 'difficulty': 'standard'},
            ]}))
        if 'Return only JSON' in system:
            seen.append(('review', request.url.host))
            return sse_response('{"passed": true, "issues": []}')
        if 'YOUR SUBTASK:\nDesign' in prompt:
            seen.append(('design', request.url.host))
            assert 'distinctive direction' in system, 'design skill instructions are added'
            return sse_response('Palette and layout')
        if 'YOUR SUBTASK:\nWrite the order API' in prompt:
            seen.append(('backend', request.url.host))
            return sse_response('API code')
        seen.append(('synthesis', request.url.host))
        assert 'single ```html block' in system, 'frontend instructions for the website'
        return sse_response('<html>site</html>')
    run = make_run(store, mode='auto', prompt='Build a coffee shop website with an order API')
    result = await execute(store, run, handler)
    assert result['status'] == 'completed'
    assert dict(seen)['design'] == 'gemini.test' and dict(seen)['backend'] == 'coder.test' and dict(seen)['synthesis'] == 'gemini.test'
    events = store.get_events(run['id'])
    route = next(e['data'] for e in events if e['kind'] == 'route')
    assert route['mode'] == 'deep' and route['auto'] is True
    models = [e['data'] for e in events if e['kind'] == 'model']
    assert any(m['skill'] == 'backend' and 'has the backend skill' in m['reason'] for m in models)
    assert result['usage']['route']['skill'] == 'frontend'


async def test_auto_mode_keeps_simple_requests_to_one_call(store):
    store.save_connection(Connection(name='Small', base_url='http://small.test/v1', model='small', free=True, local=True,
                                     skills=['general'], tier='light').model_dump(), user_id=store.user_id)
    store.save_connection(Connection(name='Thinker', base_url='http://think.test/v1', model='thinker', free=True,
                                     tier='strong', reasoning=True).model_dump(), user_id=store.user_id)
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        return sse_response('Paris')
    result = await execute(store, make_run(store, mode='auto', prompt='What is the capital of France?'), handler)
    assert result['status'] == 'completed' and result['usage']['calls'] == 1 and hosts == ['small.test']


def test_skills_api_and_route_preview(client, store):
    skills = client.get('/api/skills').json()
    assert {'general', 'frontend', 'backend', 'coding', 'design', 'reasoning'} <= {s['id'] for s in skills}
    created = client.post('/api/skills', json={'name': 'Data Science', 'keywords': ['Pandas', 'dataframe', ' '], 'instructions': 'Use pandas.'})
    assert created.status_code == 201 and created.json()['id'] == 'data-science' and created.json()['keywords'] == ['pandas', 'dataframe']
    bad = client.post('/api/connections', json=Connection(name='X', base_url='http://x.test/v1', model='x', free=True).model_dump() | {'skills': ['nope']})
    assert bad.status_code == 400 and 'Unknown skill' in bad.json()['detail']
    analyst = client.post('/api/connections', json=Connection(name='Analyst', base_url='http://x.test/v1', model='x', free=True, skills=['data-science']).model_dump()).json()
    preview = client.post('/api/route', json={'prompt': 'Clean this pandas dataframe and plot it'}).json()
    assert preview['skill'] == 'data-science' and preview['candidates'][0]['id'] == analyst['id'] and preview['mode'] == 'quick'
    assert client.delete('/api/skills/data-science').status_code == 200
    assert store.get_connection(analyst['id'], store.user_id)['skills'] == []


def test_auto_reviews_only_checkable_work():
    from backend.router import wants_review
    assert not wants_review({'auto': True, 'skill': 'frontend'}, 'deep'), 'design reviews are cosmetic: skip in Auto'
    assert wants_review({'auto': True, 'skill': 'backend'}, 'deep')
    assert wants_review({'auto': False, 'skill': 'frontend'}, 'deep'), 'a mode you pick yourself always reviews'
    assert wants_review({'auto': True, 'skill': 'design'}, 'reviewed')
    assert not wants_review({'auto': True, 'skill': 'coding'}, 'quick')


def test_simple_questions_are_light_and_general_work_goes_to_the_lightest_capable_model():
    assert classify('What is an API?', SKILLS)['difficulty'] == 'light' and classify('What is an API?', SKILLS)['skill'] == 'general'
    assert classify('Translate good morning to Indonesian', SKILLS)['difficulty'] == 'light'
    lite = {'id': 'l', 'name': 'Lite', 'model': 'flash-lite', 'skills': [], 'tier': 'light', 'reasoning': False, 'local': False, 'free': True, 'priority': 10}
    flash = {**GEMINI, 'skills': ['general', 'frontend']}
    ordered, reason = rank([flash, lite], 'general', 'light', PREFS)
    assert ordered[0]['id'] == 'l' and 'lightest capable' in reason


async def test_connections_report_calls_and_average_tokens(store):
    from .test_orchestrator import add_provider
    provider = add_provider(store)
    unused = store.save_connection(Connection(name='Unused', base_url='http://unused.test/v1', model='unused', free=True, priority=90).model_dump(), user_id=store.user_id)
    await execute(store, make_run(store), lambda request: sse_response('hello'))
    rows = {c['id']: c for c in store.with_usage(store.list_connections(store.user_id))}
    assert rows[provider['id']]['calls'] == 1 and rows[provider['id']]['avg_output_tokens'] is not None
    assert rows[unused['id']]['calls'] == 0 and rows[unused['id']]['avg_output_tokens'] is None
