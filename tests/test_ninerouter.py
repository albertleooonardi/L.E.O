import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.api as api_module
from backend.api import create_app
from backend.ninerouter import CODEX_REDIRECT, NineRouter, RouterError, SignIns, serve_callback

MODELS = {'object': 'list', 'data': [
    {'id': 'cx/gpt-5.5', 'owned_by': 'cx', 'capabilities': {'reasoning': True, 'contextWindow': 400000}},
    {'id': 'cx/gpt-5.5-mini', 'owned_by': 'cx', 'capabilities': {'reasoning': False, 'contextWindow': 400000}},
    {'id': 'gh/gpt-4.1', 'owned_by': 'gh', 'capabilities': {'reasoning': False, 'contextWindow': 128000}},
]}


class FakeRouter:
    """Just enough of 9Router's dashboard API, recording each call."""

    def __init__(self):
        self.calls, self.accounts, self.logins, self.pending = [], [], 0, True
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        path, body = request.url.path, json.loads(request.content or b'{}')
        self.calls.append((request.method, path, body))
        if path == '/api/auth/login':
            self.logins += 1
            ok = body.get('password') == 'secret'
            return httpx.Response(200 if ok else 401, json={'success': ok}, headers={'set-cookie': 'auth_token=t; Path=/'} if ok else {})
        if 'auth_token=t' not in request.headers.get('cookie', '') and not path.startswith('/v1'):
            return httpx.Response(401, json={'error': 'Unauthorized'})
        if path == '/api/providers':
            return httpx.Response(200, json={'connections': self.accounts})
        if request.method == 'DELETE' and path.startswith('/api/providers/'):
            self.accounts = [a for a in self.accounts if a['id'] != path.rsplit('/', 1)[1]]
            return httpx.Response(200, json={'success': True})
        if path == '/api/keys':
            return httpx.Response(201, json={'key': 'sk-9r', 'name': 'L.E.O.', 'id': 'k1'})
        if path == '/api/oauth/codex/authorize':
            assert request.url.params['redirect_uri'] == CODEX_REDIRECT
            return httpx.Response(200, json={'authUrl': 'https://auth.openai.com/oauth/authorize?x=1', 'state': 'st8', 'codeVerifier': 'ver', 'flowType': 'authorization_code_pkce'})
        if path == '/api/oauth/codex/exchange':
            assert body == {'code': 'c0de', 'redirectUri': CODEX_REDIRECT, 'codeVerifier': 'ver', 'state': 'st8'}
            self.accounts.append({'id': 'a1', 'provider': 'codex', 'email': 'me@example.com'})
            return httpx.Response(200, json={'success': True, 'connection': {'id': 'a1', 'provider': 'codex', 'email': 'me@example.com'}})
        if path == '/api/oauth/github/device-code':
            return httpx.Response(200, json={'device_code': 'dev', 'user_code': 'AB-12', 'verification_uri': 'https://github.com/login/device', 'interval': 5, 'codeVerifier': 'v2'})
        if path == '/api/oauth/github/poll':
            if self.pending:
                return httpx.Response(200, json={'success': False, 'error': 'authorization_pending', 'pending': True})
            self.accounts.append({'id': 'a2', 'provider': 'github', 'displayName': 'octo'})
            return httpx.Response(200, json={'success': True, 'connection': {'id': 'a2', 'provider': 'github'}})
        if path == '/v1/models':
            assert request.headers['authorization'] == 'Bearer sk-9r'
            return httpx.Response(200, json=MODELS)
        return httpx.Response(404, json={'error': 'Not found'})


@pytest.fixture
def fake(monkeypatch):
    value = FakeRouter()
    real_init = NineRouter.__init__
    monkeypatch.setattr(NineRouter, '__init__', lambda self, url, password, transport=None: real_init(self, url, password, value.transport))
    real_discover = api_module.discover
    monkeypatch.setattr(api_module, 'discover', lambda connection, key, transport=None: real_discover(connection, key, value.transport))
    return value


@pytest.fixture
def router_client(store, fake):
    store.config.router9_url, store.config.router9_password, store.config.router9_callback_port = 'http://9router:20128', 'secret', 0
    with TestClient(create_app(store.config, store)) as value:
        value.cookies.set('leo_session', store.user_session_token)
        yield value


def test_accounts_off_without_router(client):
    data = client.get('/api/accounts').json()
    assert data['available'] is False and 'LEO_9ROUTER_URL' in data['error']
    assert 'claude' not in {p['id'] for p in data['providers']}, 'Claude subscription sign-in is never offered'
    assert client.post('/api/accounts/signin', json={'provider': 'github'}).status_code == 400


def test_risky_provider_needs_consent(router_client):
    response = router_client.post('/api/accounts/signin', json={'provider': 'codex'})
    assert response.status_code == 400 and 'banned' in response.json()['detail']
    assert router_client.post('/api/accounts/signin', json={'provider': 'nope', 'accept_risk': True}).status_code == 400


def test_device_sign_in_adds_models_with_one_key(router_client, store, fake):
    started = router_client.post('/api/accounts/signin', json={'provider': 'github', 'accept_risk': True}).json()
    assert started['user_code'] == 'AB-12' and started['url'] == 'https://github.com/login/device' and started['status'] == 'waiting'
    assert router_client.get(f"/api/accounts/signin/{started['id']}").json()['status'] == 'waiting'
    fake.pending = False
    done = router_client.get(f"/api/accounts/signin/{started['id']}").json()
    assert done['status'] == 'done' and done['result'] == {'account': '', 'added': 1}
    poll = next(body for method, path, body in fake.calls if path == '/api/oauth/github/poll')
    assert poll == {'deviceCode': 'dev', 'codeVerifier': 'v2', 'extraData': None}
    [model] = store.list_connections(store.user_id)
    assert model['model'] == 'gh/gpt-4.1' and model['name'] == 'GitHub Copilot · gpt-4.1' and model['has_key'] and model['free']
    assert model['base_url'] == 'http://9router:20128/v1'
    accounts = router_client.get('/api/accounts').json()
    assert accounts['available'] and accounts['accounts'] == [{'id': 'a2', 'provider': 'github', 'name': 'GitHub Copilot', 'label': 'octo'}]


def test_codex_redirect_exchanges_code_and_reuses_key(router_client, store, fake):
    started = router_client.post('/api/accounts/signin', json={'provider': 'codex', 'accept_risk': True}).json()
    assert started['url'].startswith('https://auth.openai.com/') and started['user_code'] is None
    flows = router_client.app.state.signins
    ok, message = asyncio.run(flows.callback({'code': 'c0de', 'state': 'st8'}))
    assert ok and 'ChatGPT (Codex)' in message
    again, _ = asyncio.run(flows.callback({'code': 'c0de', 'state': 'st8'}))
    assert not again, 'a single-use code is exchanged once'
    status = router_client.get(f"/api/accounts/signin/{started['id']}").json()
    assert status['status'] == 'done' and status['result'] == {'account': 'me@example.com', 'added': 2}
    models = {c['model']: c for c in store.list_connections(store.user_id)}
    assert set(models) == {'cx/gpt-5.5', 'cx/gpt-5.5-mini'} and all(c['has_key'] for c in models.values())
    assert models['cx/gpt-5.5']['reasoning'] is True and models['cx/gpt-5.5']['context_window'] == 400000
    assert sum(1 for _, path, _ in fake.calls if path == '/api/keys') == 1, 'one 9Router key serves every model'


def test_disconnect_removes_account_and_its_models(router_client, store, fake):
    started = router_client.post('/api/accounts/signin', json={'provider': 'codex', 'accept_risk': True}).json()
    asyncio.run(router_client.app.state.signins.callback({'code': 'c0de', 'state': 'st8'}))
    assert router_client.get(f"/api/accounts/signin/{started['id']}").json()['status'] == 'done'
    response = router_client.delete('/api/accounts/a1')
    assert response.json() == {'ok': True, 'removed_models': 2}
    assert fake.accounts == [] and store.list_connections(store.user_id) == []
    assert router_client.delete('/api/accounts/a1').status_code == 404


def test_callback_rejects_unknown_state_and_reports_provider_errors(router_client):
    flows = router_client.app.state.signins
    assert asyncio.run(flows.callback({'code': 'x', 'state': 'forged'}))[0] is False
    started = router_client.post('/api/accounts/signin', json={'provider': 'codex', 'accept_risk': True}).json()
    ok, message = asyncio.run(flows.callback({'state': 'st8', 'error': 'access_denied', 'error_description': 'User cancelled'}))
    assert not ok and message == 'User cancelled'
    status = router_client.get(f"/api/accounts/signin/{started['id']}").json()
    assert status['status'] == 'error' and status['error'] == 'User cancelled'


async def test_router_logs_in_again_when_its_session_lapses(fake):
    router = NineRouter('http://9router:20128', 'secret', fake.transport)
    assert await router.accounts() == []
    router.cookies = httpx.Cookies({'auth_token': 'expired'})
    assert await router.accounts() == [] and fake.logins == 2
    with pytest.raises(RouterError, match='LEO_9ROUTER_PASSWORD'):
        await NineRouter('http://9router:20128', 'wrong', fake.transport).accounts()


async def test_router_unreachable_is_a_clear_error():
    def refuse(request):
        raise httpx.ConnectError('refused')
    with pytest.raises(RouterError, match='not reachable'):
        await NineRouter('http://9router:20128', 'secret', httpx.MockTransport(refuse)).accounts()


async def test_callback_listener_serves_the_redirect():
    seen = []

    class Flows:
        async def callback(self, query):
            seen.append(query)
            return True, 'Signed in <ok>'
    server = await serve_callback(Flows(), 0, '127.0.0.1')
    port = server.sockets[0].getsockname()[1]
    async with httpx.AsyncClient() as client:
        page = await client.get(f'http://127.0.0.1:{port}/auth/callback?code=c&state=s')
        missing = await client.get(f'http://127.0.0.1:{port}/other')
    server.close()
    assert page.status_code == 200 and 'Signed in &lt;ok&gt;' in page.text and seen == [{'code': 'c', 'state': 's'}]
    assert missing.status_code == 404
