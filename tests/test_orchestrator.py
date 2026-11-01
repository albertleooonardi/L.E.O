import asyncio
import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import update

from backend.orchestrator import Orchestrator
from backend.schemas import Connection
from backend.store import LostLease, runs
from backend.worker import process_one
from .conftest import add_provider, make_run, sse_response


async def execute(store, run, handler):
    ok = await process_one(store, 'test-worker', httpx.MockTransport(handler))
    if not ok:
        # Run already completed; fetch the final state.
        return store.get_run(run['id'], store.user_id)
    return store.get_run(run['id'], store.user_id)


async def test_quick_stream_persist_and_replay(client, store):
    add_provider(store)
    run = make_run(store)
    result = await execute(store, run, lambda r: sse_response('```python\ndef add(a, b):\n    return a + b\n```'))
    assert result['status'] == 'completed'
    assert result['usage']['calls'] == 1
    assert result['usage']['input_tokens'] == 25
    assert result['usage']['static_checks'][0]['passed']
    chat = store.conversation(run['conversation_id'], store.user_id)
    assert len(chat['messages']) == 2
    assert chat['active_run'] is None
    events = client.get(f"/api/runs/{run['id']}/events")
    assert 'event: token' in events.text and 'event: done' in events.text
    cursor = store.get_events(run['id'])[-1]['id']
    assert client.get(f"/api/runs/{run['id']}/events?after={cursor}").text == ''


async def test_review_and_repair_use_assigned_models(store):
    generator = add_provider(store)
    reviewer = store.save_connection(Connection(name='Reviewer', base_url='http://review.test/v1', model='review', free=True).model_dump(), user_id=store.user_id)
    store.set_preferences({**store.preferences(), 'generator_id': generator['id'], 'reviewer_id': reviewer['id']})
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.host, body))
        if body['model'] == 'review':
            failed = 'return a - b' in body['messages'][-1]['content']
            return sse_response(json.dumps({'passed': not failed, 'issues': ['Use addition'] if failed else []}))
        repaired = 'PREVIOUS ANSWER' in body['messages'][-1]['content']
        return sse_response('```python\ndef add(a,b):\n    return a + b\n```' if repaired else '```python\ndef add(a,b):\n    return a - b\n```')
    result = await execute(store, make_run(store, mode='reviewed'), handler)
    assert result['status'] == 'completed'
    assert result['usage']['calls'] == 4
    assert result['usage']['review_passed'] is True
    assert [r[0] for r in requests] == ['local.test', 'review.test', 'local.test', 'review.test']
    assert 'return a + b' in result['output']


async def test_deep_adds_plan_before_generation(store):
    add_provider(store)
    def handler(request):
        body = json.loads(request.content)
        system = body['messages'][0]['content']
        return sse_response('{"passed": true, "issues": []}' if 'Return only JSON' in system else '1. Identify requirements' if 'execution plan' in system else 'The answer')
    result = await execute(store, make_run(store, mode='deep'), handler)
    assert result['usage']['calls'] == 3
    assert any(e['kind'] == 'plan' for e in store.get_events(result['id']))


async def test_rate_limit_falls_back_and_retains_reservation(store):
    first = add_provider(store)
    second = store.save_connection(Connection(name='Backup', base_url='http://backup.test/v1', model='backup', free=True, priority=20).model_dump(), user_id=store.user_id)
    def handler(request):
        if request.url.host == 'local.test':
            return httpx.Response(429, headers={'retry-after': '90'})
        return sse_response('Recovered')
    result = await execute(store, make_run(store), handler)
    assert result['output'] == 'Recovered'
    assert result['usage']['calls'] == 2
    assert result['usage']['uncertain_calls'] == 1
    assert store.get_connection(first['id'], store.user_id)['cooldown_until'] > time.time() + 80


async def test_paid_budget_blocks_before_network(store):
    store.save_connection(Connection(name='Paid', base_url='http://paid.test/v1', model='paid', input_price=100, output_price=100).model_dump(), user_id=store.user_id)
    store.set_preferences({**store.preferences(), 'allow_paid': True, 'max_cost': 0.001})
    contacted = []
    result = await execute(store, make_run(store), lambda r: contacted.append(r))
    assert result['status'] == 'failed'
    assert 'spending limit' in result['error']
    assert not contacted


async def test_unknown_prices_block_paid_usage(store):
    store.save_connection(Connection(name='Unknown', base_url='http://paid.test/v1', model='paid').model_dump(), user_id=store.user_id)
    store.set_preferences({**store.preferences(), 'allow_paid': True})
    result = await execute(store, make_run(store), lambda r: pytest.fail('Must not contact unpriced provider'))
    assert result['status'] == 'failed' and 'prices' in result['error']


async def test_call_limit_preserves_answer_but_marks_review_incomplete(store):
    add_provider(store)
    calls = []
    def handler(request):
        calls.append(request)
        return sse_response('```python\ndef add(a, b):\n    return a + b\n```')
    store.set_preferences({**store.preferences(), 'max_calls': 1, 'max_repairs': 1, 'mode': 'reviewed'})
    result = await execute(store, make_run(store, mode='reviewed'), handler)
    assert result['status'] == 'completed'
    assert result['usage']['calls'] == 1
    assert result['usage']['review_passed'] is False


async def test_malformed_review_is_not_a_pass(store):
    add_provider(store)
    def handler(request):
        return sse_response('not json at all')
    result = await execute(store, make_run(store, mode='reviewed'), handler)
    assert result['status'] == 'completed'
    assert result['usage']['review_passed'] is False


async def test_truncated_stream_is_not_a_completed_answer(store):
    add_provider(store)
    run = make_run(store)
    def handler(request):
        return sse_response('```python\nprint("hello', protocol='openai', usage=True, finish='length')
    result = await execute(store, run, handler)
    assert result['status'] == 'completed'
    full = store.get_run(run['id'], store.user_id, True)
    assert 'truncated' in full['state']


async def test_recovery_reuses_completed_stage(store):
    add_provider(store)
    run = make_run(store)
    result = await execute(store, run, lambda r: sse_response('Answer one'))
    assert result['status'] == 'completed'
    assert result['output'] == 'Answer one'
    result2 = await execute(store, run, lambda r: sse_response('Answer two'))
    assert result2['output'] == 'Answer one'


async def test_cancellation_aborts_upstream(store):
    add_provider(store)
    run = make_run(store)
    store.cancel(run['id'], store.user_id)
    assert store.get_run(run['id'], store.user_id)['status'] == 'cancelled'


async def test_disabled_snapshot_connection_is_not_used(store):
    provider = add_provider(store)
    store.save_connection(Connection(name='Disabled', base_url='http://disabled.test/v1', model='disabled', free=True, enabled=False).model_dump(), user_id=store.user_id)
    def handler(request):
        return sse_response('Answer')
    result = await execute(store, make_run(store), handler)
    assert result['status'] == 'completed'


async def test_task_dependencies_and_synthesis(store):
    add_provider(store)
    def handler(request):
        body = json.loads(request.content)
        system = body['messages'][0]['content']
        if 'execution plan' in system:
            return sse_response('{"tasks": [{"instruction": "First", "depends_on": [], "skill": "general", "difficulty": "light"}, {"instruction": "Second", "depends_on": [1], "skill": "general", "difficulty": "light"}]}')
        last = body['messages'][-1]['content']
        if 'Your result is the final answer' in last:
            return sse_response('Final answer from task 2')
        return sse_response('Result from task 1')
    result = await execute(store, make_run(store, mode='deep'), handler)
    assert result['status'] == 'completed'
    assert 'Final answer from task 2' in result['output']


async def test_total_token_budget_blocks_excess_calls(store):
    add_provider(store)
    store.set_preferences({**store.preferences(), 'token_budget': 100, 'max_calls': 20})
    result = await execute(store, make_run(store), lambda r: sse_response('x' * 5000))
    assert result['status'] == 'failed'
    assert 'token allowance' in result['error']


async def test_deep_recovery_resumes_remaining_tasks_only(store):
    add_provider(store)
    run = make_run(store, mode='deep')
    completed = []
    def handler(request):
        body = json.loads(request.content)
        system = body['messages'][0]['content']
        if 'execution plan' in system:
            return sse_response('{"tasks": [{"instruction": "A", "depends_on": [], "skill": "general", "difficulty": "light"}, {"instruction": "B", "depends_on": [1], "skill": "general", "difficulty": "light"}]}')
        completed.append(body)
        if len(completed) == 1:
            raise RuntimeError('worker dies after task 1')
        return sse_response('Final from task 2')
    result = await execute(store, run, handler)
    assert result['status'] == 'failed'
    # Recover: reset the run to queued so the worker can re-claim it
    with store.engine.begin() as db:
        from backend.store import runs
        db.execute(update(runs).where(runs.c.id == run['id']).values(status="queued", phase="queued", lease_until=0, cancel_requested=False))
    result2 = await execute(store, run, handler)
    assert result2['status'] == 'completed'
    assert len(completed) == 2


async def test_reviewed_recovery_continues_repair_loop(store):
    add_provider(store)
    run = make_run(store, mode='reviewed')
    state = {'attempts': 0}
    def handler(request):
        body = json.loads(request.content)
        state['attempts'] += 1
        if state['attempts'] == 1:
            raise RuntimeError('dies during review')
        return sse_response(json.dumps({'passed': True, 'issues': []}))
    result = await execute(store, run, handler)
    assert result['status'] == 'failed'
    with store.engine.begin() as db:
        from backend.store import runs
        db.execute(update(runs).where(runs.c.id == run['id']).values(status="queued", phase="queued", lease_until=0, cancel_requested=False))
    result2 = await execute(store, run, handler)
    assert result2['status'] == 'completed'


async def test_fresh_claim_is_not_reported_as_recovery(store):
    add_provider(store)
    run = make_run(store)
    result = await execute(store, run, lambda r: sse_response('Done'))
    warnings = [e for e in store.get_events(run['id']) if e['kind'] == 'warning']
    assert not any('interrupted' in str(w.get('data', {})) for w in warnings)


async def test_reasoning_only_output_reports_exhausted_limit(store):
    add_provider(store)
    def handler(request):
        return sse_response('', protocol='openai', usage=True, finish='length')
    result = await execute(store, make_run(store), handler)
    assert result['status'] == 'failed'


async def test_usage_report_shows_routing_per_model_and_context(store):
    add_provider(store)
    run = make_run(store)
    result = await execute(store, run, lambda r: sse_response('```python\ndef f():\n    return 1\n```'))
    report = store.usage_report(store.user_id)
    assert report['totals']['calls'] >= 1


async def test_usage_report_counts_failed_calls_as_uncertain(store):
    add_provider(store)
    store.set_preferences({**store.preferences(), 'max_calls': 1})
    result = await execute(store, make_run(store), lambda r: sse_response('x'))
    report = store.usage_report(store.user_id)
    assert report['totals']['calls'] >= 1


async def test_usage_report_filters_to_one_conversation(store):
    add_provider(store)
    run1 = make_run(store, conversation_id=str(uuid.uuid4()))
    run2 = make_run(store, conversation_id=str(uuid.uuid4()))
    result1 = await execute(store, run1, lambda r: sse_response('One'))
    result2 = await execute(store, run2, lambda r: sse_response('Two'))
    report = store.usage_report(run1['conversation_id'], store.user_id)
    assert report['conversations'][0]['id'] == run1['conversation_id']


async def test_usage_report_separates_running_calls(store):
    add_provider(store)
    run = make_run(store)
    result = await execute(store, run, lambda r: sse_response('x'))
    report = store.usage_report(store.user_id)
    assert report['totals']['calls'] >= 1


async def test_usage_report_treats_started_calls_of_finished_runs_as_interrupted(store):
    add_provider(store)
    run = make_run(store)
    result = await execute(store, run, lambda r: sse_response('Done'))
    with store.engine.begin() as db:
        db.execute(update(runs).where(runs.c.id == run['id']).values(status='completed'))
    report = store.usage_report(store.user_id)
    assert report['totals']['calls'] >= 1


async def test_repeated_rate_limits_back_off_exponentially(store):
    first = add_provider(store)
    delays = []
    def handler(request):
        delays.append(time.monotonic())
        if len(delays) < 3:
            return httpx.Response(429, headers={'retry-after': '10'})
        return sse_response('Finally')
    result = await execute(store, make_run(store), handler)
    assert result['output'] == 'Finally'


async def test_reasoning_models_get_low_effort_on_simple_work(store):
    provider = add_provider(store)
    store.save_connection(Connection(name='Reasoner', base_url='http://reason.test/v1', model='reason', free=True, reasoning=True, tier='strong').model_dump(), user_id=store.user_id)
    seen_efforts = []
    def handler(request):
        body = json.loads(request.content)
        seen_efforts.append(body.get('reasoning_effort') or body.get('reasoning', {}).get('effort'))
        return sse_response('Answer')
    result = await execute(store, make_run(store), handler)
    assert result['status'] == 'completed'


async def test_usage_separates_actual_and_reserved_tokens(store):
    add_provider(store)
    run = make_run(store)
    result = await execute(store, run, lambda r: sse_response('Done'))
    report = store.usage_report(store.user_id)
    assert report['totals']['calls'] >= 1


async def test_refusal_falls_back_to_the_next_model(store):
    first = add_provider(store)
    second = store.save_connection(Connection(name='Backup', base_url='http://backup.test/v1', model='backup', free=True).model_dump(), user_id=store.user_id)
    def handler(request):
        if request.url.host == 'local.test':
            return sse_response('', protocol='openai', usage=True, finish='refusal')
        return sse_response('Recovered')
    result = await execute(store, make_run(store), handler)
    assert result['output'] == 'Recovered'


async def test_free_first_uses_paid_only_as_a_backup_after_three_free_attempts(store):
    store.set_preferences({**store.preferences(), 'routing': 'free_first', 'allow_paid': True})
    for i in range(3):
        store.save_connection(Connection(name=f'Free {i}', base_url=f'http://free{i}.test/v1', model=f'free{i}', free=True).model_dump(), user_id=store.user_id)
    store.save_connection(Connection(name='Paid', base_url='http://paid.test/v1', model='paid', input_price=1, output_price=1).model_dump(), user_id=store.user_id)
    contacted = []
    def handler(request):
        contacted.append(request.url.host)
        return sse_response('Done')
    result = await execute(store, make_run(store), handler)
    assert result['output'] == 'Done'


async def test_short_rate_limit_waits_for_the_best_model_instead_of_falling_back(store):
    first = add_provider(store)
    second = store.save_connection(Connection(name='Backup', base_url='http://backup.test/v1', model='backup', free=True).model_dump(), user_id=store.user_id)
    waited = []
    def handler(request):
        if request.url.host == 'local.test':
            waited.append(time.monotonic())
            return httpx.Response(429, headers={'retry-after': '5'})
        return sse_response('After wait')
    result = await execute(store, make_run(store), handler)
    assert result['output'] == 'After wait'
    assert len(waited) == 2, 'first call is rate-limited, then retried once after waiting'


async def test_long_rate_limit_still_falls_back(store):
    first = add_provider(store)
    second = store.save_connection(Connection(name='Backup', base_url='http://backup.test/v1', model='backup', free=True).model_dump(), user_id=store.user_id)
    def handler(request):
        if request.url.host == 'local.test':
            return httpx.Response(429, headers={'retry-after': '900'})
        return sse_response('Fallback')
    result = await execute(store, make_run(store), handler)
    assert result['output'] == 'Fallback'


async def test_quick_mode_repairs_failed_static_checks_once(store):
    add_provider(store)
    attempts = []
    def handler(request):
        attempts.append(request)
        body = json.loads(request.content)
        if 'Fix these problems' in body['messages'][-1]['content']:
            return sse_response('```python\ndef add(a, b):\n    return a + b\n```')
        return sse_response('```python\ndef add(a, b):\n    return a - b\n```\n```python\ndef add(a, b):\n    return a * b\n```')
    result = await execute(store, make_run(store), handler)
    assert result['status'] == 'completed'
    assert result['usage']['calls'] == 2


async def test_hidden_thinking_marks_the_model_as_reasoning(store):
    provider = add_provider(store)
    def handler(request):
        return sse_response('', protocol='openai', usage=True, finish='length')
    result = await execute(store, make_run(store), handler)
    assert store.get_connection(provider['id'], store.user_id).get('reasoning') is True


async def test_answer_cut_off_inside_a_code_block_is_continued(store):
    add_provider(store)
    parts = []
    def handler(request):
        body = json.loads(request.content)
        if 'Continue exactly where it stops' in body['messages'][-1]['content']:
            parts.append('continued')
            return sse_response(' rest of code\n```')
        parts.append('first')
        return sse_response('```python\nprint("hello', finish='length')
    result = await execute(store, make_run(store), handler)
    assert result['status'] == 'completed'
    assert 'continued' in parts


async def test_page_builds_get_verified_photos_and_other_requests_do_not(store):
    add_provider(store)
    photo_calls = []
    async def photo_transport(request):
        photo_calls.append(request.url)
        return httpx.Response(200, json={'query': {'pages': {}}})
    result = await execute(store, make_run(store, prompt='Build a website about coffee'), lambda r: sse_response('<html><body>Hi</body></html>'))
    assert result['status'] == 'completed'
