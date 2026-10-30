import json

import httpx
import pytest

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
