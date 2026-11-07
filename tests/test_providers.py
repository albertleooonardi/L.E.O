import json
import time

import httpx
import pytest

from backend import providers
from backend.context import prepare_context, static_review, token_bound
from backend.providers import make_request, stream
from .conftest import sse_response


@pytest.mark.parametrize('protocol,path', [('openai', '/chat/completions'), ('responses', '/responses'), ('anthropic', '/messages')])
async def test_protocol_streaming_and_usage(protocol, path):
    captured = []
    def handler(request):
        captured.append(request)
        return sse_response('Hello', protocol)
    provider = {'protocol': protocol, 'base_url': 'https://api.test/v1', 'model': 'test'}
    chunks = [c async for c in stream(provider, 'private-key', [{'role': 'system', 'content': 'Be useful'}, {'role': 'user', 'content': 'Hi'}], 128, 10, httpx.MockTransport(handler))]
    assert ''.join(c.text for c in chunks) == 'Hello'
    assert max(c.output_tokens or 0 for c in chunks) == 12
    assert captured[0].url.path == '/v1' + path
    if protocol == 'anthropic':
        assert captured[0].headers['x-api-key'] == 'private-key' and 'authorization' not in captured[0].headers
    else:
        assert captured[0].headers['authorization'] == 'Bearer private-key'
    payload = json.loads(captured[0].content)
    if protocol == 'anthropic':
        assert payload['system'] == 'Be useful'
        assert payload['messages'][0]['role'] == 'user'
    if protocol == 'responses':
        assert payload['store'] is False


def test_compaction_keeps_complete_recent_turns_and_latest_request():
    history = [{'role': 'user', 'content': 'old' * 600}, {'role': 'assistant', 'content': 'old answer'}, {'role': 'user', 'content': 'recent'}, {'role': 'assistant', 'content': 'recent answer'}]
    result, omitted = prepare_context(history, 'system', 'required latest', 500)
    assert omitted == 2
    assert result[0]['content'] == 'system' and result[-1]['content'] == 'required latest'
    assert result[1]['content'] == 'recent'
    assert token_bound(result) <= 500
    with pytest.raises(ValueError):
        prepare_context([], 'system', 'too long' * 1000, 500)


def test_static_checks_detect_syntax_without_executing_code(tmp_path):
    target = tmp_path / 'should-not-exist'
    result = static_review(f'```python\nopen({str(target)!r}, "w").write("oops")\n```\n```json\n{{bad}}\n```')
    assert result[0]['passed'] is True and result[1]['passed'] is False
    assert not target.exists()


def test_planner_rejects_cycles_and_excessive_tasks():
    from backend.context import parse_plan
    assert parse_plan('{"tasks":[{"instruction":"First","depends_on":[1]}]}') == []
    assert parse_plan('{"tasks":[{"instruction":"First","depends_on":[]}]}')[0]['id'] == 1
    assert parse_plan(json.dumps({'tasks': [{'instruction': 'a'}] * 4})) == []


async def test_provider_error_detail_is_reported_without_key():
    from backend.providers import ProviderError
    body = {'error': {'code': 429, 'message': 'Provider returned error', 'metadata': {'raw': 'model is temporarily rate-limited upstream'}}}
    handler = lambda r: httpx.Response(429, json=body, headers={'retry-after': '12'})
    provider = {'protocol': 'openai', 'base_url': 'https://api.test/v1', 'model': 'test'}
    with pytest.raises(ProviderError) as caught:
        [c async for c in stream(provider, 'private-key', [{'role': 'user', 'content': 'Hi'}], 128, 10, httpx.MockTransport(handler))]
    assert 'HTTP 429' in str(caught.value) and 'rate-limited upstream' in str(caught.value)
    assert 'private-key' not in str(caught.value)
    assert caught.value.retryable and caught.value.retry_after == 12


async def test_catalog_normalizes_gemini_model_names():
    from backend.providers import list_models
    handler = lambda r: httpx.Response(200, json={'object': 'list', 'data': [{'id': 'models/gemini-2.5-flash'}, {'id': 'plain-model'}]})
    provider = {'protocol': 'openai', 'base_url': 'https://gemini.test/v1beta/openai', 'model': 'gemini-2.5-flash'}
    assert await list_models(provider, 'key', httpx.MockTransport(handler)) == ['gemini-2.5-flash', 'plain-model']


def test_hidden_thinking_tokens_are_counted_from_total():
    from backend.providers import usage_chunk
    chunk = usage_chunk({'prompt_tokens': 16, 'completion_tokens': 239, 'total_tokens': 1432})
    assert chunk.input_tokens == 16 and chunk.output_tokens == 1416
    assert usage_chunk({'prompt_tokens': 10, 'completion_tokens': 50, 'total_tokens': 60}).output_tokens == 50
    assert usage_chunk({'input_tokens': 5, 'output_tokens': 7}).output_tokens == 7


def test_effort_is_sent_only_to_reasoning_models_in_each_api_form():
    messages = [{'role': 'user', 'content': 'Hi'}]
    gemini = {'protocol': 'openai', 'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai', 'model': 'g', 'reasoning': True}
    assert make_request(gemini, messages, 100, 'low')[1]['reasoning_effort'] == 'low'
    router = {**gemini, 'base_url': 'https://openrouter.ai/api/v1'}
    payload = make_request(router, messages, 100, 'medium')[1]
    assert payload['reasoning'] == {'effort': 'medium'} and 'reasoning_effort' not in payload
    responses = {**gemini, 'protocol': 'responses'}
    assert make_request(responses, messages, 100, 'low')[1]['reasoning'] == {'effort': 'low'}
    plain = {**gemini, 'reasoning': False}
    assert 'reasoning_effort' not in make_request(plain, messages, 100, 'low')[1]



def test_claude_effort_goes_in_output_config():
    claude = {'protocol': 'anthropic', 'base_url': 'https://api.anthropic.com/v1', 'model': 'claude-haiku-5-5', 'reasoning': True}
    payload = make_request(claude, [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'Hi'}], 100, 'low')[1]
    assert payload['output_config'] == {'effort': 'low'} and payload['system'] == 'S' and 'temperature' not in payload


async def test_exhausted_credits_are_named_plainly():
    from backend.providers import ProviderError
    body = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API.'}}
    provider = {'protocol': 'anthropic', 'base_url': 'https://api.anthropic.com/v1', 'model': 'claude-haiku-5-5'}
    with pytest.raises(ProviderError, match='credits or billing are exhausted') as caught:
        [c async for c in stream(provider, 'k', [{'role': 'user', 'content': 'Hi'}], 64, 10, httpx.MockTransport(lambda r: httpx.Response(400, json=body)))]
    assert not caught.value.retryable


async def test_free_tier_quota_is_not_reported_as_billing():
    from backend.providers import ProviderError
    body = [{'error': {'code': 429, 'message': 'You exceeded your current quota, please check your plan and billing details. Please retry in 41s.'}}]
    provider = {'protocol': 'openai', 'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai', 'model': 'gemini-2.5-flash'}
    with pytest.raises(ProviderError, match='quota reached') as caught:
        [c async for c in stream(provider, 'k', [{'role': 'user', 'content': 'Hi'}], 64, 10, httpx.MockTransport(lambda r: httpx.Response(429, json=body)))]
    assert caught.value.retryable and caught.value.retry_after == 41


def limited(status, text, **headers):
    request = httpx.Request('POST', 'http://x.test')
    return httpx.Response(status, text=text, headers=headers, request=request)


def test_a_limit_shared_by_many_models_says_when_it_comes_back():
    day = limited(429, '{"error":{"message":"Rate limit exceeded: free-models-per-day. Add 10 credits"}}', **{'x-ratelimit-limit': '50', 'x-ratelimit-reset': str(int((time.time() + 7200) * 1000))})
    with pytest.raises(providers.ProviderError) as found:
        providers.check_status(day)
    assert 7100 < found.value.shared_seconds <= 7200 and found.value.limit == 50, 'beyond the 1 hour cap of ordinary waits'
    router = limited(503, '{"error":{"message":"[codex/gpt-6-sol] [429]: The usage limit has been reached (reset after 24m 7s)"}}')
    with pytest.raises(providers.ProviderError) as found:
        providers.check_status(router)
    assert found.value.shared_seconds == 24 * 60 + 7 and found.value.limit is None
    with pytest.raises(providers.ProviderError) as found:
        providers.check_status(limited(429, '{"error":{"message":"Slow down"}}'))
    assert found.value.shared_seconds is None, 'an ordinary rate limit belongs to the model'


def test_models_on_one_limit_share_a_group():
    group = providers.quota_group
    free = {'base_url': 'https://openrouter.ai/api/v1', 'model': 'nvidia/nemotron:free'}
    assert group(free) == group({**free, 'model': 'google/gemma:free'}), 'every free model of a key'
    assert group(free) != group({**free, 'model': 'stealth/crouton-alpha'}), 'a stealth model is not one'
    relay = {'base_url': 'http://localhost:8000/api/cli/relay/1', 'model': 'cx/gpt-6-sol'}
    assert group(relay) == group({'base_url': 'http://localhost:8000/api/cli/relay/2', 'model': 'cx/gpt-5.6-luna'}), 'one account through a router'
    assert group(relay) != group({**relay, 'model': 'gh/gpt-4.1'}), 'another account on the same router'


def test_a_relay_error_shows_its_reason_and_a_down_router_is_unreachable():
    """L.E.O.'s relay answers like FastAPI ({"detail": ...}); its reason must reach the user, not just "HTTP 502"."""
    with pytest.raises(providers.ProviderError) as found:
        providers.check_status(limited(502, '{"detail":"Your 9Router is not reachable from the L.E.O. server"}'))
    assert 'Your 9Router is not reachable' in str(found.value) and found.value.unreachable
    with pytest.raises(providers.ProviderError) as found:
        providers.check_status(limited(502, '{"error":{"message":"Upstream provider returned an error"}}'))
    assert not found.value.unreachable, 'one model failing upstream says nothing about its neighbours'


async def test_nothing_answering_at_an_address_is_unreachable():
    def refuse(request):
        raise httpx.ConnectError('Connection refused', request=request)
    provider = {'protocol': 'openai', 'base_url': 'http://router.test/v1', 'model': 'cx/gpt'}
    with pytest.raises(providers.ProviderError) as found:
        [c async for c in stream(provider, 'k', [{'role': 'user', 'content': 'Hi'}], 64, 10, httpx.MockTransport(refuse))]
    assert found.value.unreachable and found.value.retryable
