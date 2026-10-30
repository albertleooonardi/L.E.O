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
    assert await process_one(store, 'test-worker', httpx.MockTransport(handler))
    return store.get_run(run['id'])


async def test_quick_stream_persist_and_replay(client, store):
    add_provider(store)
    run = make_run(store)
    result = await execute(store, run, lambda r: sse_response('```python\ndef add(a, b):\n    return a + b\n```'))
    assert result['status'] == 'completed'
    assert result['usage']['calls'] == 1
    assert result['usage']['input_tokens'] == 25
    assert result['usage']['static_checks'][0]['passed']
    chat = store.conversation(run['conversation_id'])
    assert len(chat['messages']) == 2
    assert chat['active_run'] is None
    events = client.get(f"/api/runs/{run['id']}/events")
    assert 'event: token' in events.text and 'event: done' in events.text
    cursor = store.get_events(run['id'])[-1]['id']
    assert client.get(f"/api/runs/{run['id']}/events?after={cursor}").text == ''


async def test_review_and_repair_use_assigned_models(store):
    generator = add_provider(store)
    reviewer = store.save_connection(Connection(name='Reviewer', base_url='http://review.test/v1', model='review', free=True).model_dump())
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
    second = store.save_connection(Connection(name='Backup', base_url='http://backup.test/v1', model='backup', free=True, priority=20).model_dump())
    def handler(request):
        if request.url.host == 'local.test':
            return httpx.Response(429, headers={'retry-after': '90'})
        return sse_response('Recovered')
    result = await execute(store, make_run(store), handler)
    assert result['output'] == 'Recovered'
    assert result['usage']['calls'] == 2
    assert result['usage']['uncertain_calls'] == 1
    assert store.get_connection(first['id'])['cooldown_until'] > time.time() + 80


async def test_paid_budget_blocks_before_network(store):
    store.save_connection(Connection(name='Paid', base_url='http://paid.test/v1', model='paid', input_price=100, output_price=100).model_dump())
    store.set_preferences({**store.preferences(), 'allow_paid': True, 'max_cost': 0.001})
    contacted = []
    result = await execute(store, make_run(store), lambda r: contacted.append(r))
    assert result['status'] == 'failed'
    assert 'spending limit' in result['error']
    assert not contacted


async def test_unknown_prices_block_paid_usage(store):
    store.save_connection(Connection(name='Unknown', base_url='http://paid.test/v1', model='paid').model_dump())
    store.set_preferences({**store.preferences(), 'allow_paid': True})
    result = await execute(store, make_run(store), lambda r: pytest.fail('Must not contact unpriced provider'))
    assert result['status'] == 'failed' and 'prices' in result['error']


async def test_call_limit_preserves_answer_but_marks_review_incomplete(store):
    add_provider(store)
    store.set_preferences({**store.preferences(), 'max_calls': 1})
    result = await execute(store, make_run(store, mode='reviewed'), lambda r: sse_response('Draft answer'))
    assert result['status'] == 'failed'
    assert result['output'] == 'Draft answer'
    assert result['usage']['calls'] == 1
    assert 'call limit' in result['error']


async def test_malformed_review_is_not_a_pass(store):
    add_provider(store)
    store.set_preferences({**store.preferences(), 'max_repairs': 0})
    result = await execute(store, make_run(store, mode='reviewed'), lambda r: sse_response('Looks good!'))
    assert result['status'] == 'completed'
    assert result['usage']['review_passed'] is False


async def test_truncated_stream_is_not_a_completed_answer(store):
    add_provider(store)
    result = await execute(store, make_run(store), lambda r: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'))
    assert result['status'] == 'failed'
    assert 'ended before completion' in result['error']
    assert result['usage']['uncertain_calls'] == 1


async def test_recovery_reuses_completed_stage(store):
    add_provider(store)
    run = make_run(store)
    claimed = store.claim('old-worker')
    state = claimed['state']
    state['completed']['generating'] = 'Already generated'
    store.checkpoint(run['id'], 'old-worker', state=state)
    with store.engine.begin() as db:
        db.execute(update(runs).where(runs.c.id == run['id']).values(lease_until=0))
    result = await execute(store, run, lambda r: pytest.fail('Checkpoint must prevent repeated generation'))
    assert result['output'] == 'Already generated'
    with pytest.raises(LostLease):
        store.checkpoint(run['id'], 'old-worker', output='stale write')


async def test_cancellation_aborts_upstream(store):
    add_provider(store)
    run = make_run(store)
    started = asyncio.Event()
    closed = asyncio.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.sleep(20)
            yield b'data: [DONE]\n\n'
        async def aclose(self):
            closed.set()
    task = asyncio.create_task(process_one(store, 'worker', httpx.MockTransport(lambda r: httpx.Response(200, stream=SlowStream()))))
    await asyncio.wait_for(started.wait(), 2)
    store.cancel(run['id'])
    await asyncio.wait_for(task, 2)
    assert store.get_run(run['id'])['status'] == 'cancelled'
    assert closed.is_set()
    ledger = store.call_rows(run['id'])
    assert [c['status'] for c in ledger] == ['interrupted'] and ledger[0]['reserved_tokens'] > 0


async def test_disabled_snapshot_connection_is_not_used(store):
    provider = add_provider(store)
    run = make_run(store)
    config = Connection(name='Disabled', base_url='http://local.test/v1', model='test-model', free=True, enabled=False)
    store.save_connection(config.model_dump(), provider['id'])
    result = await execute(store, run, lambda r: pytest.fail('Disabled connection used'))
    assert result['status'] == 'failed'


async def test_task_dependencies_and_synthesis(store):
    add_provider(store)
    seen = []
    def handler(request):
        body = json.loads(request.content)
        system, prompt = body['messages'][0]['content'], body['messages'][-1]['content']
        seen.append(prompt)
        if 'execution plan' in system:
            return sse_response(json.dumps({'tasks': [
                {'instruction': 'Define interface', 'depends_on': []},
                {'instruction': 'Implement interface', 'depends_on': [1]},
            ]}))
        if 'Return only JSON' in system:
            return sse_response('{"passed": true, "issues": []}')
        if 'YOUR SUBTASK:\nDefine interface' in prompt:
            return sse_response('INTERFACE_RESULT')
        if 'YOUR SUBTASK:\nImplement interface' in prompt:
            assert 'Task 1:\nINTERFACE_RESULT' in prompt and 'final answer the user sees' in prompt
            return sse_response('IMPLEMENTATION_RESULT')
        raise AssertionError('A chain of subtasks needs no synthesis call')
    result = await execute(store, make_run(store, mode='deep'), handler)
    assert result['status'] == 'completed'
    assert result['usage']['calls'] == 4, 'plan + 2 tasks + review; the last task is the answer'
    assert result['output'] == 'IMPLEMENTATION_RESULT'
    assert any('intermediate step' in p for p in seen), 'earlier tasks are asked to stay brief'
    events = store.get_events(result['id'])
    assert len([e for e in events if e['kind'] == 'task']) == 2
    assert any(e['kind'] == 'token' and 'IMPLEMENTATION_RESULT' in e['data']['text'] for e in events), 'the final task streams as the answer'


async def test_total_token_budget_blocks_excess_calls(store):
    add_provider(store)
    store.set_preferences({**store.preferences(), 'token_budget': 2048, 'max_output_tokens': 2048})
    result = await execute(store, make_run(store), lambda r: pytest.fail('Token allowance exceeded'))
    assert result['status'] == 'failed' and 'token allowance' in result['error']


def crash_after(store, run, completed, in_flight=None):
    """Simulate a worker that checkpointed some stages, then lost its lease mid-call."""
    claimed = store.claim('crashed-worker')
    state = claimed['state']
    state['completed'].update(completed)
    store.checkpoint(run['id'], 'crashed-worker', state=state)
    if in_flight:
        store.add_call(dict(id=str(uuid.uuid4()), run_id=run['id'], stage=in_flight, provider='Local test', model='test-model',
                            status='started', input_tokens=100, output_tokens=0, reserved_tokens=2148, reserved_cost=0,
                            cost=0, estimated=True, created_at=time.time(), elapsed_ms=0))
    with store.engine.begin() as db:
        db.execute(update(runs).where(runs.c.id == run['id']).values(lease_until=0))


async def test_deep_recovery_resumes_remaining_tasks_only(store):
    add_provider(store)
    run = make_run(store, mode='deep')
    plan = json.dumps({'tasks': [{'instruction': 'Define interface', 'depends_on': []},
                                 {'instruction': 'Implement interface', 'depends_on': [1]}]})
    crash_after(store, run, {'planning': plan, 'task_1': 'INTERFACE_RESULT'}, in_flight='task_2')
    seen = []
    def handler(request):
        body = json.loads(request.content)
        system, prompt = body['messages'][0]['content'], body['messages'][-1]['content']
        assert 'execution plan' not in system, 'Checkpointed plan must not be requested again'
        assert 'YOUR SUBTASK:\nDefine interface' not in prompt, 'Checkpointed task must not run again'
        if 'Return only JSON' in system:
            seen.append('review')
            return sse_response('{"passed": true, "issues": []}')
        if 'YOUR SUBTASK:\nImplement interface' in prompt:
            assert 'Task 1:\nINTERFACE_RESULT' in prompt
            seen.append('task_2')
            return sse_response('IMPLEMENTATION_RESULT')
        seen.append('synthesis')
        return sse_response('Final combined answer')
    result = await execute(store, run, handler)
    assert result['status'] == 'completed'
    assert seen == ['task_2', 'review']
    assert result['output'] == 'IMPLEMENTATION_RESULT'
    ledger = store.call_rows(run['id'])
    assert [c['status'] for c in ledger].count('interrupted') == 1
    assert result['usage']['calls'] == 3 and result['usage']['uncertain_calls'] == 1
    kinds = [e['kind'] for e in store.get_events(run['id'])]
    assert 'plan' not in kinds, 'Replayed plan must not be republished'
    assert [e['data']['id'] for e in store.get_events(run['id']) if e['kind'] == 'task'] == [2]
    assert any('Resumed after a worker interruption' in e['data'].get('message', '') for e in store.get_events(run['id']) if e['kind'] == 'warning')


async def test_reviewed_recovery_continues_repair_loop(store):
    add_provider(store)
    run = make_run(store, mode='reviewed')
    crash_after(store, run, {'generating': '```python\ndef add(a,b):\n    return a - b\n```',
                             'reviewing_0': json.dumps({'passed': False, 'issues': ['Use addition']})})
    seen = []
    def handler(request):
        body = json.loads(request.content)
        system, prompt = body['messages'][0]['content'], body['messages'][-1]['content']
        if 'Return only JSON' in system:
            seen.append('review')
            assert 'return a + b' in prompt
            return sse_response('{"passed": true, "issues": []}')
        assert 'PREVIOUS ANSWER' in prompt and 'Use addition' in prompt
        seen.append('repair')
        return sse_response('```python\ndef add(a,b):\n    return a + b\n```')
    result = await execute(store, run, handler)
    assert result['status'] == 'completed'
    assert seen == ['repair', 'review']
    assert result['usage']['review_passed'] is True
    assert 'return a + b' in result['output']
    reviews = [e['data']['passed'] for e in store.get_events(run['id']) if e['kind'] == 'review']
    assert reviews == [True], 'Checkpointed review verdict must not be republished'


async def test_fresh_claim_is_not_reported_as_recovery(store):
    add_provider(store)
    run = make_run(store)
    await execute(store, run, lambda r: sse_response('Done'))
    assert not any(e['kind'] == 'warning' for e in store.get_events(run['id']))


async def test_reasoning_only_output_reports_exhausted_limit(store):
    add_provider(store)
    store.set_preferences({**store.preferences(), 'max_output_tokens': 4096})
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body['max_tokens'])
        content = 'data: ' + json.dumps({'choices': [{'delta': {'reasoning': 'thinking...'}, 'finish_reason': 'length'}]}) + '\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=content)
    result = await execute(store, make_run(store, mode='reviewed'), handler)
    assert result['status'] == 'failed'
    assert 'output limit without answer text' in result['error']
    assert seen == [4096], 'Every stage uses the configured output allowance'
    warnings = [e['data']['message'] for e in store.get_events(result['id']) if e['kind'] == 'warning']
    assert warnings and '..' not in warnings[0] and 'Trying the next' not in warnings[0]


async def test_usage_report_shows_routing_per_model_and_context(client, store):
    generator = add_provider(store, context_window=32768)
    reviewer = store.save_connection(Connection(name='Reviewer', base_url='http://review.test/v1', model='review', free=True, priority=50).model_dump())
    store.set_preferences({**store.preferences(), 'reviewer_id': reviewer['id'], 'context_tokens': 16000})
    def handler(request):
        body = json.loads(request.content)
        if request.url.host == 'review.test' and 'Return only JSON' not in body['messages'][0]['content']:
            return httpx.Response(429, json={'error': {'message': 'busy'}})
        if body['model'] == 'review':
            return sse_response('{"passed": true, "issues": []}')
        return sse_response('Answer')
    run = make_run(store, mode='reviewed')
    assert (await execute(store, run, handler))['status'] == 'completed'
    report = client.get('/api/usage').json()
    by_model = {m['model']: m for m in report['models']}
    assert by_model['test-model']['roles'] == {'generator': 1}
    assert by_model['review']['roles'] == {'reviewer': 1}
    assert by_model['test-model']['input_tokens'] == 25 and by_model['test-model']['output_tokens'] == 12
    assert report['totals']['calls'] == 2 and report['totals']['uncertain'] == 0
    assert report['totals']['role_tokens'] == {'generator': 37, 'reviewer': 37}
    chat = report['conversations'][0]
    assert chat['id'] == run['conversation_id'] and chat['runs'] == 1
    assert chat['context']['model'] == 'review' and chat['context']['tokens'] == 37
    assert chat['context']['allowance'] == 16000 and chat['context']['percent'] == 0.2
    assert [c['role'] for c in report['recent_calls']] == ['reviewer', 'generator']
    assert 'secret' not in json.dumps(report)


async def test_usage_report_counts_failed_calls_as_uncertain(client, store):
    add_provider(store, context_window=32768)
    store.save_connection(Connection(name='Backup', base_url='http://backup.test/v1', model='backup', free=True, priority=20).model_dump())
    handler = lambda r: httpx.Response(429) if r.url.host == 'local.test' else sse_response('Recovered')
    await execute(store, make_run(store), handler)
    report = client.get('/api/usage').json()
    by_model = {m['model']: m for m in report['models']}
    assert by_model['test-model']['uncertain'] == 1 and by_model['test-model']['completed'] == 0
    assert by_model['test-model']['reserved_tokens'] > 0
    assert by_model['backup']['completed'] == 1
    assert report['conversations'][0]['context']['model'] == 'backup'
    assert [c['status'] for c in report['recent_calls']] == ['completed', 'failed']


async def test_usage_report_filters_to_one_conversation(client, store):
    add_provider(store)
    first, second = make_run(store), make_run(store, prompt='Second chat')
    await execute(store, first, lambda r: sse_response('One'))
    await execute(store, second, lambda r: sse_response('Two'))
    report = client.get('/api/usage', params={'conversation_id': second['conversation_id']}).json()
    assert [c['id'] for c in report['conversations']] == [second['conversation_id']]
    assert report['totals']['calls'] == 1 and len(report['recent_calls']) == 1
    assert report['models'][0]['roles'] == {'generator': 1}
    assert client.get('/api/usage').json()['totals']['calls'] == 2


async def test_usage_report_separates_running_calls(client, store):
    add_provider(store)
    run = make_run(store)
    store.claim('worker')
    store.add_call(dict(id=str(uuid.uuid4()), run_id=run['id'], stage='generating', provider='Local test', model='test-model',
                        status='started', input_tokens=100, output_tokens=0, reserved_tokens=2148, reserved_cost=0,
                        cost=0, estimated=True, created_at=time.time(), elapsed_ms=0))
    totals = client.get('/api/usage').json()['totals']
    assert totals['in_progress'] == 1 and totals['uncertain'] == 0 and totals['completed'] == 0


async def test_usage_report_treats_started_calls_of_finished_runs_as_interrupted(client, store):
    add_provider(store)
    run = make_run(store)
    store.claim('worker')
    store.add_call(dict(id=str(uuid.uuid4()), run_id=run['id'], stage='generating', provider='Local test', model='test-model',
                        status='started', input_tokens=100, output_tokens=0, reserved_tokens=2148, reserved_cost=0,
                        cost=0, estimated=True, created_at=time.time(), elapsed_ms=0))
    with store.engine.begin() as db:
        db.execute(update(runs).where(runs.c.id == run['id']).values(status='cancelled'))
    report = client.get('/api/usage').json()
    assert report['totals']['in_progress'] == 0 and report['totals']['uncertain'] == 1
    assert report['recent_calls'][0]['status'] == 'interrupted'


async def test_repeated_rate_limits_back_off_exponentially(store):
    from backend import orchestrator
    orchestrator._strikes.clear()
    limited = add_provider(store)
    store.save_connection(Connection(name='Backup', base_url='http://backup.test/v1', model='backup', free=True, priority=50).model_dump())
    handler = lambda r: httpx.Response(429, headers={'retry-after': '1'}) if r.url.host == 'local.test' else sse_response('ok')
    cooldowns = []
    for _ in range(3):
        start = time.time()
        await execute(store, make_run(store), handler)
        cooldowns.append(round(store.get_connection(limited['id'])['cooldown_until'] - start))
        store.cooldown(limited['id'], -1)  # Let the next run try it again.
    assert [c // 10 for c in cooldowns] == [3, 6, 12], cooldowns
    await execute(store, make_run(store), lambda r: sse_response('recovered'))
    assert limited['id'] not in orchestrator._strikes, 'a success clears the strikes'


async def test_reasoning_models_get_low_effort_on_simple_work(store):
    store.save_connection(Connection(name='Thinker', base_url='http://think.test/v1', model='thinker', free=True, reasoning=True,
                                     skills=['general']).model_dump())
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return sse_response('Paris')
    await execute(store, make_run(store, mode='auto', prompt='What is the capital of France?'), handler)
    assert bodies[0]['reasoning_effort'] == 'low'


async def test_usage_separates_actual_and_reserved_tokens(store):
    add_provider(store)
    store.save_connection(Connection(name='Backup', base_url='http://backup.test/v1', model='backup', free=True, priority=50).model_dump())
    result = await execute(store, make_run(store), lambda r: httpx.Response(503) if r.url.host == 'local.test' else sse_response('ok'))
    usage = result['usage']
    assert usage['input_tokens'] == 25 and usage['output_tokens'] == 12, 'only the completed call counts as actual usage'
    assert usage['reserved_tokens'] > 0 and usage['uncertain_calls'] == 1
