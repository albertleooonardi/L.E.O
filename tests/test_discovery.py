import json

import httpx

from backend.discovery import check_access, discover, profile
from backend.schemas import Connection
from .conftest import sse_response

OPENROUTER = {'data': [
    {'id': 'big/ultra-550b:free', 'name': 'Ultra (free)', 'description': 'A frontier reasoning model.', 'context_length': 1000000,
     'pricing': {'prompt': '0', 'completion': '0'}, 'architecture': {'output_modalities': ['text']}, 'reasoning': {'mandatory': False, 'default_enabled': True}},
    {'id': 'pool/laguna-xs:free', 'name': 'Laguna XS (free)', 'description': 'The latest coding agent model.', 'context_length': 262144,
     'pricing': {'prompt': '0', 'completion': '0'}, 'architecture': {'output_modalities': ['text']}, 'reasoning': {'mandatory': True}},
    {'id': 'g/gemma-31b-it:free', 'name': 'Gemma 31B (free)', 'description': 'Instruction tuned.', 'context_length': 131072,
     'pricing': {'prompt': '0', 'completion': '0'}, 'architecture': {'output_modalities': ['text']}, 'reasoning': {'default_enabled': False}},
    {'id': 'liquid/lfm-2.6b:free', 'name': 'LFM', 'description': 'A compact model. Liquid advises against using it for agentic coding.',
     'pricing': {'prompt': '0', 'completion': '0'}, 'architecture': {'output_modalities': ['text']}},
    {'id': 'paid/model', 'name': 'Paid', 'pricing': {'prompt': '0.000001', 'completion': '0.000002'}, 'architecture': {'output_modalities': ['text']}},
    {'id': 'g/lyria:free', 'name': 'Lyria', 'pricing': {'prompt': '0', 'completion': '0'}, 'architecture': {'output_modalities': ['text', 'audio']}},
    {'id': 'openrouter/free', 'name': 'Free router', 'pricing': {'prompt': '0', 'completion': '0'}, 'architecture': {'output_modalities': ['text']}},
]}
GEMINI = {'models': [
    {'name': 'models/gemini-9-pro', 'displayName': 'Gemini 9 Pro', 'supportedGenerationMethods': ['generateContent'], 'inputTokenLimit': 1048576, 'outputTokenLimit': 65536, 'thinking': True},
    {'name': 'models/gemini-9-flash-lite', 'supportedGenerationMethods': ['generateContent'], 'inputTokenLimit': 1048576, 'outputTokenLimit': 65536, 'thinking': True},
    {'name': 'models/gemini-9-flash-tts', 'supportedGenerationMethods': ['generateContent'], 'outputTokenLimit': 16384},
    {'name': 'models/gemini-flash-latest', 'supportedGenerationMethods': ['generateContent'], 'outputTokenLimit': 65536},
    {'name': 'models/text-embedding', 'supportedGenerationMethods': ['embedContent'], 'outputTokenLimit': 1},
]}


def conn(url, **kw):
    return {'id': 'src', 'name': 'Provider', 'protocol': 'openai', 'base_url': url, 'model': 'x', 'free': True, 'local': False, **kw}


def test_profiles_come_from_size_family_and_description():
    assert profile('big/ultra-550b', 'frontier reasoning model')['tier'] == 'strong'
    assert profile('x/model-2.6b')['tier'] == 'light' and profile('gemini-2.5-flash-lite')['tier'] == 'light'
    assert profile('gemini-9-flash-lite')['tier'] == 'standard', 'newer Flash-Lite generations are standard'
    assert profile('gemini-9-flash-lite')['skills'] == ['frontend', 'design', 'general']
    assert profile('gemini-9-pro')['tier'] == 'strong' and profile('gemini-9-flash')['tier'] == 'standard'
    assert profile('m/something', 'MoE with 16B active parameters')['tier'] == 'standard', 'active parameters are not total size'
    assert profile('p/laguna-s')['skills'] == ['coding', 'backend', 'frontend']
    assert profile('l/lfm', 'Liquid advises against using it for agentic coding.')['skills'] == []


async def test_openrouter_discovery_offers_free_text_models_only():
    found = await discover(conn('https://openrouter.ai/api/v1'), 'k', httpx.MockTransport(lambda r: httpx.Response(200, json=OPENROUTER)))
    by_id = {m['model']: m for m in found['models']}
    assert set(by_id) == {'big/ultra-550b:free', 'pool/laguna-xs:free', 'g/gemma-31b-it:free', 'liquid/lfm-2.6b:free'}
    assert by_id['big/ultra-550b:free']['tier'] == 'strong' and by_id['big/ultra-550b:free']['reasoning'] is True
    assert by_id['pool/laguna-xs:free']['skills'][0] == 'coding' and by_id['pool/laguna-xs:free']['tier'] == 'light'
    assert by_id['g/gemma-31b-it:free']['reasoning'] is False and by_id['g/gemma-31b-it:free']['free'] is True
    assert {s['model'] for s in found['skipped']} == {'g/lyria:free', 'openrouter/free'}, 'paid models are not even listed'


async def test_gemini_discovery_uses_native_catalog_and_skips_non_chat():
    seen = []
    def handler(request):
        seen.append((request.url.path, request.headers.get('x-goog-api-key')))
        return httpx.Response(200, json=GEMINI)
    found = await discover(conn('https://generativelanguage.googleapis.com/v1beta/openai', free=True), 'gkey', httpx.MockTransport(handler))
    assert seen == [('/v1beta/models', 'gkey')]
    assert [m['model'] for m in found['models']] == ['gemini-9-flash-lite', 'gemini-9-pro']
    assert {s['reason'] for s in found['skipped']} == {'not a text chat model', 'alias of another model'}


async def test_check_access_reports_unusable_models():
    def handler(request):
        model = json.loads(request.content)['model']
        if model == 'denied':
            return httpx.Response(403, json={'error': {'message': 'only available on agentic harnesses'}})
        return sse_response('OK')
    result = await check_access(conn('https://openrouter.ai/api/v1'), 'k', [{'model': 'ok', 'reasoning': True}, {'model': 'denied', 'reasoning': False}],
                                httpx.MockTransport(handler))
    assert result['ok'] == {'reason': None, 'reasoning': True} and 'HTTP 403' in result['denied']['reason']


async def test_check_access_drops_effort_a_model_rejects():
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(400) if 'reasoning_effort' in body else sse_response('OK')
    result = await check_access(conn('https://generativelanguage.googleapis.com/v1beta/openai'), 'k', [{'model': 'gemma', 'reasoning': True}],
                                httpx.MockTransport(handler))
    assert result['gemma'] == {'reason': None, 'reasoning': False}


def test_discover_and_adopt_api_reuses_key(client, store, monkeypatch):
    import backend.discovery as discovery
    source = store.save_connection(Connection(name='OpenRouter · First', base_url='https://openrouter.ai/api/v1', model='first', api_key='or-key', free=True).model_dump())
    async def fake_discover(connection, key, transport=None):
        assert key == 'or-key'
        return {'provider': 'openrouter', 'skipped': [], 'models': [profile('first', free=True), profile('pool/laguna-xs:free', 'coding agent model', True, 262144, True)]}
    async def fake_check(connection, key, models, transport=None, concurrency=3):
        return {m['model']: {'reason': None, 'reasoning': m['reasoning']} for m in models}
    monkeypatch.setattr('backend.api.discover', fake_discover)
    monkeypatch.setattr('backend.api.check_access', fake_check)
    listed = client.post(f"/api/connections/{source['id']}/discover").json()
    assert [m['added'] for m in listed['models']] == [True, False]
    response = client.post(f"/api/connections/{source['id']}/adopt", json={'models': [listed['models'][1], listed['models'][0]], 'check': True})
    body = response.json()
    assert response.status_code == 201 and [c['model'] for c in body['created']] == ['pool/laguna-xs:free']
    assert body['skipped'] == [{'model': 'first', 'reason': 'already added'}]
    added = store.get_connection(body['created'][0]['id'], True)
    assert store.decrypt(added) == 'or-key' and added['skills'][0] == 'coding' and added['free'] is True
    assert 'or-key' not in response.text


async def test_anthropic_discovery_profiles_claude_with_suggested_prices():
    listing = {'data': [
        {'id': 'claude-opus-5-5', 'display_name': 'Claude Opus 5.5', 'max_input_tokens': 1000000},
        {'id': 'claude-sonnet-5-5', 'display_name': 'Claude Sonnet 5.5', 'max_input_tokens': 1000000},
        {'id': 'claude-haiku-5-5', 'display_name': 'Claude Haiku 5.5', 'max_input_tokens': 1000000},
        {'id': 'claude-haiku-4-5', 'display_name': 'Claude Haiku 4.5', 'max_input_tokens': 200000},
        {'id': 'claude-3-haiku', 'display_name': 'Old'},
    ], 'has_more': False}
    seen = []
    def handler(request):
        seen.append((request.url.path, request.url.params.get('limit'), request.headers.get('x-api-key'), request.headers.get('authorization')))
        return httpx.Response(200, json=listing)
    found = await discover({**conn('https://api.anthropic.com/v1', free=False), 'protocol': 'anthropic'}, 'ak', httpx.MockTransport(handler))
    assert seen == [('/v1/models', '1000', 'ak', None)]
    by_id = {m['model']: m for m in found['models']}
    assert by_id['claude-opus-5-5']['tier'] == 'strong' and by_id['claude-opus-5-5']['input_price'] == 4 and by_id['claude-opus-5-5']['output_price'] == 20
    assert by_id['claude-sonnet-5-5']['tier'] == 'standard' and by_id['claude-haiku-5-5']['tier'] == 'light' and by_id['claude-haiku-5-5']['input_price'] == 0.10
    assert by_id['claude-haiku-5-5']['reasoning'] is True and by_id['claude-haiku-4-5']['reasoning'] is False, 'effort only from 4.6 on'
    assert all(m['free'] is False for m in found['models']) and [s['model'] for s in found['skipped']] == ['claude-3-haiku']
