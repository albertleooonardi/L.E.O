import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.api as api_module
from backend.api import create_app
from backend.auth import Auth
from backend.ninerouter import CODEX_REDIRECT, NineRouter, RouterError, SignIns, serve_callback

MODELS = {'object': 'list', 'data': [
    {'id': 'cx/gpt-5.5', 'owned_by': 'cx', 'capabilities': {'reasoning': True, 'contextWindow': 400000}},
    {'id': 'cx/gpt-5.5-mini', 'owned_by': 'cx', 'capabilities': {'reasoning': False, 'contextWindow': 400000}},
    {'id': 'gh/gpt-4.1', 'owned_by': 'gh', 'capabilities': {'reasoning': False, 'contextWindow': 128000}},
]}


class FakeRouter:
    """Just enough of 9Router's dashboard API, recording each call. Every router address (one per user) keeps its
    own signed-in accounts, and accepts the passwords sandboxd was given."""

    def __init__(self):
        self.calls, self.by_host, self.logins, self.pending, self.passwords = [], {}, 0, True, {'secret'}
        self.transport = httpx.MockTransport(self.handle)

    @property
    def accounts(self):
        return [a for found in self.by_host.values() for a in found]

    def handle(self, request):
        path, body = request.url.path, json.loads(request.content or b'{}')
        accounts = self.by_host.setdefault(request.url.host, [])
        self.calls.append((request.method, path, body))
        if path == '/api/health':
            return httpx.Response(200, json={'ok': True})
        if path == '/api/auth/login':
            self.logins += 1
            ok = body.get('password') in self.passwords
            return httpx.Response(200 if ok else 401, json={'success': ok}, headers={'set-cookie': 'auth_token=t; Path=/'} if ok else {})
        if 'auth_token=t' not in request.headers.get('cookie', '') and not path.startswith('/v1'):
            return httpx.Response(401, json={'error': 'Unauthorized'})
        if path == '/api/providers' and request.method == 'GET':
            return httpx.Response(200, json={'connections': accounts})
        if request.method == 'DELETE' and path.startswith('/api/providers/'):
            accounts[:] = [a for a in accounts if a['id'] != path.rsplit('/', 1)[1]]
            return httpx.Response(200, json={'success': True})
        if path == '/api/keys':
            return httpx.Response(201, json={'key': 'sk-9r', 'name': 'L.E.O.', 'id': 'k1'})
        if path == '/api/providers/validate':
            return httpx.Response(200, json={'valid': body.get('apiKey') != 'bad'})
        if path == '/api/providers' and request.method == 'POST':
            connection = {'id': f'key-{body["provider"]}', 'provider': body['provider'], 'authType': 'apikey',
                          'name': body['name'], 'isActive': True}
            accounts.append(connection)
            return httpx.Response(201, json={'connection': connection})
        if path.startswith('/api/providers/') and path.endswith('/models'):
            return httpx.Response(200, json={'models': [
                {'id': 'cohere/free:free', 'name': 'Free model', 'context_length': 128000, 'pricing': {'prompt': '0', 'completion': '0'}},
                {'id': 'vendor/paid', 'name': 'Paid model', 'pricing': {'prompt': '0.000002', 'completion': '0.000008'}},
                {'id': 'embed-small', 'name': 'Embeddings'},
                {'id': 'art/flux', 'name': 'Flux', 'architecture': {'output_modalities': ['image']}},
            ]})
        if path.startswith('/api/providers/') and request.method == 'PUT':
            connection = next(a for a in accounts if a['id'] == path.rsplit('/', 1)[1])
            return httpx.Response(200, json={'connection': connection})
        if path == '/api/oauth/codex/authorize':
            assert request.url.params['redirect_uri'] == CODEX_REDIRECT
            return httpx.Response(200, json={'authUrl': 'https://auth.openai.com/oauth/authorize?x=1', 'state': 'st8', 'codeVerifier': 'ver', 'flowType': 'authorization_code_pkce'})
        if path == '/api/oauth/codex/exchange':
            assert body == {'code': 'c0de', 'redirectUri': CODEX_REDIRECT, 'codeVerifier': 'ver', 'state': 'st8'}
            accounts.append({'id': 'a1', 'provider': 'codex', 'email': 'me@example.com'})
            return httpx.Response(200, json={'success': True, 'connection': {'id': 'a1', 'provider': 'codex', 'email': 'me@example.com'}})
        if path == '/api/oauth/github/device-code':
            return httpx.Response(200, json={'device_code': 'dev', 'user_code': 'AB-12', 'verification_uri': 'https://github.com/login/device', 'interval': 5, 'codeVerifier': 'v2'})
        if path == '/api/oauth/github/poll':
            if self.pending:
                return httpx.Response(200, json={'success': False, 'error': 'authorization_pending', 'pending': True})
            accounts.append({'id': 'a2', 'provider': 'github', 'displayName': 'octo'})
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


class FakeSandbox:
    """sandboxd's router endpoints: records which users' routers were started and removed."""

    def __init__(self, router):
        self.router, self.ensured, self.deleted = router, [], []

    async def ensure_router(self, user_id, password, adopt_legacy=False):
        self.ensured.append((user_id, password) if not adopt_legacy else (user_id, password, 'adopt'))
        self.router.passwords.add(password)
        return f'http://leo-router-{user_id}:20128'

    async def delete_router(self, user_id):
        self.deleted.append(user_id)


@pytest.fixture
def sandbox(fake):
    return FakeSandbox(fake)


@pytest.fixture
def router_client(store, sandbox):
    store.config.router9_callback_port = 0
    with TestClient(create_app(store.config, store, sandbox=sandbox)) as value:
        value.cookies.set('leo_session', store.user_session_token)
        yield value


def test_models_on_the_old_shared_router_move_to_the_users_own(store, sandbox):
    """Upgrading from the shared 9router service: at start, the user gets their own router started from a copy of it
    (same password, accounts, and keys), and their models follow; other models stay where they are."""
    from backend.schemas import Connection
    old = store.save_connection(Connection(name='Codex', base_url='http://9router:20128/v1', model='cx/gpt', free=True,
                                           api_key='sk-9r').model_dump(), user_id=store.user_id)
    other = store.save_connection(Connection(name='Groq', base_url='https://api.groq.com/openai/v1', model='llama', free=True,
                                             api_key='gsk').model_dump(), user_id=store.user_id)
    store.config.router9_callback_port = 0
    with TestClient(create_app(store.config, store, sandbox=sandbox)) as client:
        async def moved():
            await client.app.state.adopting

        client.portal.call(moved)
    assert sandbox.ensured == [(store.user_id, 'leo-local-development', 'adopt')]
    url = f'http://leo-router-{store.user_id}:20128'
    assert store.get_router(store.user_id) == {'url': url, 'password': 'leo-local-development'}
    moved = store.get_connection(old['id'], store.user_id, private=True)
    assert moved['base_url'] == f'{url}/v1' and store.decrypt(moved) == 'sk-9r', 'the key stays valid on the copy'
    assert store.get_connection(other['id'], store.user_id)['base_url'] == 'https://api.groq.com/openai/v1'


def test_accounts_off_without_sandbox(client):
    data = client.get('/api/accounts').json()
    assert data['available'] is False and 'sandbox' in data['error']
    assert 'claude' not in {p['id'] for p in data['providers']}, 'Claude subscription sign-in is never offered'
    assert client.post('/api/accounts/signin', json={'provider': 'github'}).status_code == 503


@pytest.mark.parametrize('path', ['/api/accounts', '/api/cli/accounts'])
def test_account_list_restores_a_missing_router(router_client, store, fake, sandbox, monkeypatch, path):
    from backend.ninerouter import RouterUnavailable
    store.save_router(store.user_id, f'http://leo-router-{store.user_id}:20128', 'secret')
    original = NineRouter.accounts
    attempts = []

    async def accounts(router):
        attempts.append(router.url)
        if len(attempts) == 1:
            raise RouterUnavailable('Container is missing')
        return await original(router)

    monkeypatch.setattr(NineRouter, 'accounts', accounts)
    headers = {}
    if path.startswith('/api/cli/'):
        device = router_client.post('/api/cli/device', json={'device_name': 'laptop'}).json()
        router_client.post(f"/api/cli/device/{device['user_code']}", json={'approve': True})
        token = router_client.post('/api/cli/token', json={'device_code': device['device_code']}).json()['token']
        headers = {'Authorization': f'Bearer {token}'}
    response = router_client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json()['available'] is True
    assert sandbox.ensured == [(store.user_id, 'secret')]
    assert len(attempts) == 2


def test_account_list_reports_a_failed_router_recovery(router_client, store, sandbox, monkeypatch):
    from backend.ninerouter import RouterUnavailable
    from backend.sandbox import SandboxError
    store.save_router(store.user_id, f'http://leo-router-{store.user_id}:20128', 'secret')

    async def unavailable(router):
        raise RouterUnavailable('Container is missing')

    async def cannot_start(*args, **kwargs):
        raise SandboxError('Cannot reach the sandbox service')

    monkeypatch.setattr(NineRouter, 'accounts', unavailable)
    monkeypatch.setattr(sandbox, 'ensure_router', cannot_start)
    response = router_client.get('/api/accounts')
    assert response.status_code == 200
    assert response.json()['available'] is False
    assert response.json()['error'] == 'Cannot reach the sandbox service'


def test_api_key_connects_via_router_without_creating_a_placeholder_model(router_client, store, fake):
    response = router_client.post('/api/model-providers/connect', json={'provider': 'openrouter', 'api_key': 'upstream-secret'})
    assert response.status_code == 201 and response.json()['connected']
    assert store.list_connections(store.user_id) == []
    accounts = router_client.get('/api/accounts').json()
    assert accounts['accounts'][0]['provider'] == 'openrouter' and 'upstream-secret' not in str(accounts)
    found = router_client.get('/api/model-providers/openrouter/models').json()
    assert {m['model'] for m in found['models']} == {'openrouter/cohere/free:free', 'openrouter/vendor/paid'}
    paid = next(m for m in found['models'] if not m['free'])
    assert paid['input_price'] == 2 and paid['output_price'] == 8
    added = router_client.post('/api/model-providers/openrouter/models', json={'models': ['openrouter/cohere/free:free']})
    assert added.status_code == 201
    [model] = store.list_connections(store.user_id, True)
    assert model['base_url'] == f'http://leo-router-{store.user_id}:20128/v1' and model['free']
    assert store.decrypt(model) == 'sk-9r', 'L.E.O. calls the router with its key, not the upstream credential'
    again = router_client.post('/api/model-providers/openrouter/models', json={'models': [model['model']]})
    assert again.json()['created'] == []
    assert router_client.post('/api/model-providers/openrouter/models', json={'models': ['cx/gpt-5.5']}).status_code == 502
    assert router_client.delete('/api/accounts/key-openrouter').json()['removed_models'] == 1
    assert store.list_connections(store.user_id) == []


def test_rejected_key_is_not_saved_and_unconnected_catalog_is_blocked(router_client, store, fake):
    assert router_client.post('/api/model-providers/connect', json={'provider': 'openrouter', 'api_key': 'bad'}).status_code == 502
    assert not any(method == 'POST' and path == '/api/providers' for method, path, body in fake.calls)
    assert router_client.get('/api/model-providers/openrouter/models').status_code == 502
    assert router_client.post('/api/model-providers/connect', json={'provider': 'unknown', 'api_key': 'key'}).status_code == 400
    assert router_client.post('/api/model-providers/connect', json={'provider': 'openrouter', 'api_key': '   '}).status_code == 422


def test_cli_provider_setup_is_scoped_to_its_device_and_keeps_router_keys_on_server(router_client, store, fake, tmp_path, monkeypatch):
    from leo_cli.account import Account
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    device = router_client.post('/api/cli/device', json={'device_name': 'CLI QA'}).json()
    router_client.post(f"/api/cli/device/{device['user_code']}", json={'approve': True})
    token = router_client.post('/api/cli/token', json={'device_code': device['device_code']}).json()['token']
    headers = {'Authorization': f'Bearer {token}'}
    assert router_client.post('/api/cli/model-providers/connect', json={'provider': 'openrouter', 'api_key': 'secret'}).status_code == 401
    account = Account(tmp_path / 'cli-managed', transport=httpx.ASGITransport(app=router_client.app))
    account.save(server='http://testserver')
    account.secrets.set('device-token', token)

    async def connect_and_sync():
        await account.connect_model_provider('openrouter', 'upstream-secret')
        catalog = await account.provider_models('openrouter')
        assert 'openrouter/cohere/free:free' in {m['model'] for m in catalog['models']}
        return await account.select_provider_models('openrouter', ['openrouter/cohere/free:free'])

    result = asyncio.run(connect_and_sync())
    assert len(result['created']) == 1
    [local] = account.store().list_connections(account.user_id, True)
    assert '/api/cli/relay/' in local['base_url'] and account.store().decrypt(local) == token
    assert 'sk-9r' not in (account.home / 'leo.db').read_bytes().decode('latin-1')
    router_client.cookies.clear()
    assert router_client.get('/api/model-providers/openrouter/models', headers=headers).status_code == 401


def test_key_rotation_keeps_existing_models_and_one_managed_provider(router_client, store, fake):
    router_client.post('/api/model-providers/connect', json={'provider': 'openrouter', 'api_key': 'original-key'})
    router_client.post('/api/model-providers/openrouter/models', json={'models': ['openrouter/cohere/free:free']})
    [before] = store.list_connections(store.user_id)
    response = router_client.post('/api/model-providers/connect', json={'provider': 'openrouter', 'api_key': 'replacement-key'})
    assert response.status_code == 201
    assert len(fake.accounts) == 1
    assert store.list_connections(store.user_id)[0]['id'] == before['id']
    assert any(method == 'PUT' and body.get('apiKey') == 'replacement-key' for method, path, body in fake.calls)


def test_migration_leaves_distinct_provider_credentials_untouched(router_client, store, fake):
    from backend.schemas import Connection
    for name, key in [('Personal', 'key-1'), ('Work', 'key-2')]:
        store.save_connection(Connection(name=name, model='vendor/model', base_url='https://openrouter.ai/api/v1', api_key=key).model_dump(), user_id=store.user_id)
    assert router_client.post('/api/model-providers/migrate').json()['moved'] == 0
    assert all(c['base_url'] == 'https://openrouter.ai/api/v1' for c in store.list_connections(store.user_id))
    assert fake.accounts == []


def test_direct_openrouter_migrates_preserving_models_and_removing_only_the_placeholder(router_client, store, fake):
    from backend.schemas import Connection
    placeholder = store.save_connection(Connection(name='OpenRouter', model='Openrouter', base_url='https://openrouter.ai/api/v1', api_key='upstream-secret').model_dump(), user_id=store.user_id)
    real = store.save_connection(Connection(name='OpenRouter · Free model', model='cohere/free:free', base_url='https://openrouter.ai/api/v1', api_key='upstream-secret', free=True, priority=3).model_dump(), user_id=store.user_id)
    result = router_client.post('/api/model-providers/migrate')
    assert result.status_code == 200 and result.json()['moved'] == 1
    assert store.get_connection(placeholder['id'], store.user_id) is None
    moved = store.get_connection(real['id'], store.user_id, True)
    assert moved['model'] == 'openrouter/cohere/free:free' and moved['priority'] == 3 and moved['free']
    assert store.decrypt(moved) == 'sk-9r'
    assert moved['base_url'] == f'http://leo-router-{store.user_id}:20128/v1'
    assert router_client.post('/api/model-providers/migrate').json()['moved'] == 0


def test_other_users_cannot_see_or_select_router_models(router_client, store, fake):
    router_client.post('/api/model-providers/connect', json={'provider': 'openrouter', 'api_key': 'upstream-secret'})
    user = store.create_user('seconduser', Auth.hash_password('long-enough-password'))
    with TestClient(router_client.app) as other:
        other.cookies.set('leo_session', Auth(store, store.config).create_session(user['id']))
        assert other.get('/api/accounts').json()['accounts'] == []
        assert other.get('/api/model-providers/openrouter/models').status_code == 502
        assert other.post('/api/model-providers/openrouter/models', json={'models': ['openrouter/cohere/free:free']}).status_code == 502


def test_each_user_signs_in_on_their_own_router(router_client, store, fake, sandbox):
    assert router_client.get('/api/accounts').json()['accounts'] == [] and sandbox.ensured == [], 'no router until a sign-in'
    started = router_client.post('/api/accounts/signin', json={'provider': 'github', 'accept_risk': True}).json()
    fake.pending = False
    assert router_client.get(f"/api/accounts/signin/{started['id']}").json()['status'] == 'done'
    [(owner, password)] = sandbox.ensured
    assert owner == store.user_id and len(password) >= 24 and store.get_router(store.user_id)['password'] == password
    [model] = store.list_connections(store.user_id)
    assert model['base_url'] == f'http://leo-router-{store.user_id}:20128/v1'
    # Another user of the same server (same in-memory sign-ins) sees and controls none of it.
    other = store.create_user('colleague', Auth.hash_password('colleague-pass'))
    with TestClient(router_client.app) as other_client:
        other_client.cookies.set('leo_session', Auth(store, store.config).create_session(other['id']))
        assert other_client.get('/api/accounts').json()['accounts'] == []
        assert other_client.delete('/api/accounts/a2').status_code == 404
        assert other_client.get(f"/api/accounts/signin/{started['id']}").status_code == 404
    assert [uid for uid, _ in sandbox.ensured] == [store.user_id]


def test_disconnecting_the_last_account_stops_the_router_and_signing_in_again_reuses_it(router_client, store, fake, sandbox):
    started = router_client.post('/api/accounts/signin', json={'provider': 'codex', 'accept_risk': True}).json()
    asyncio.run(router_client.app.state.signins.callback({'code': 'c0de', 'state': 'st8'}))
    assert router_client.get(f"/api/accounts/signin/{started['id']}").json()['status'] == 'done'
    assert router_client.delete('/api/accounts/a1').status_code == 200
    assert sandbox.deleted == [store.user_id]
    router_client.post('/api/accounts/signin', json={'provider': 'codex', 'accept_risk': True})
    assert len(sandbox.ensured) == 2 and sandbox.ensured[0] == sandbox.ensured[1], 'same router, same saved password'


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
    assert model['base_url'] == f'http://leo-router-{store.user_id}:20128/v1'
    accounts = router_client.get('/api/accounts').json()
    assert accounts['available'] and accounts['accounts'] == [{'id': 'a2', 'provider': 'github', 'name': 'GitHub Copilot', 'label': 'octo', 'active': True, 'auth_type': 'oauth'}]


def test_the_cli_signs_in_to_an_account_with_its_device_token(router_client, store, fake):
    """/models add uses the sign-in of Settings → Models, signed with the device token; nothing else opens up for it."""
    started = router_client.post('/api/cli/device', json={'device_name': 'laptop'}).json()
    assert router_client.post(f"/api/cli/device/{started['user_code']}", json={'approve': True}).status_code == 200
    token = router_client.post('/api/cli/token', json={'device_code': started['device_code']}).json()['token']
    router_client.cookies.clear()
    device = {'Authorization': f'Bearer {token}'}
    assert router_client.get('/api/cli/accounts').status_code == 401, 'no device token, no answer'
    assert router_client.get('/api/accounts', headers=device).status_code == 401, 'the routes for the browser stay for the browser'
    offered = router_client.get('/api/cli/accounts', headers=device).json()
    assert offered['available'] is True and 'github' in {p['id'] for p in offered['providers']}
    assert router_client.post('/api/cli/accounts/signin', headers=device, json={'provider': 'github'}).status_code == 400, 'the risk must be accepted'
    begun = router_client.post('/api/cli/accounts/signin', headers=device, json={'provider': 'github', 'accept_risk': True})
    assert begun.status_code == 201 and begun.json()['user_code'] == 'AB-12'
    fake.pending = False
    done = router_client.get(f"/api/cli/accounts/signin/{begun.json()['id']}", headers=device).json()
    assert done['status'] == 'done' and [m['model'] for m in store.list_connections(store.user_id)] == ['gh/gpt-4.1']
    assert router_client.delete(f"/api/cli/accounts/signin/{begun.json()['id']}", headers=device).json() == {'ok': True}
    assert [m['model'] for m in router_client.get('/api/cli/sync', headers=device).json()['relayed']] == ['gh/gpt-4.1'], 'it syncs down, through the relay'


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
    with pytest.raises(RouterError, match='password'):
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


def test_the_cli_can_rerank_account_models_it_signed_in_earlier(router_client, store, fake):
    """Models signed in before the ranks were fixed keep their old rank until the owner asks; other models are left alone."""
    started = router_client.post('/api/cli/device', json={'device_name': 'laptop'}).json()
    router_client.post(f"/api/cli/device/{started['user_code']}", json={'approve': True})
    token = router_client.post('/api/cli/token', json={'device_code': started['device_code']}).json()['token']
    device = {'Authorization': f'Bearer {token}'}
    begun = router_client.post('/api/accounts/signin', json={'provider': 'codex', 'accept_risk': True}).json()
    router_client.get('/api/auth/oauth/none')  # Not a sign-in; the router is what matters here.
    from backend.schemas import Connection
    router_url = f'http://leo-router-{store.user_id}:20128/v1'
    old = store.save_connection(Connection(name='Old', base_url=router_url, model='cx/gpt-6-sol', free=True, tier='standard', api_key='k').model_dump(), user_id=store.user_id)
    mine = store.save_connection(Connection(name='Mine', base_url='http://elsewhere.test/v1', model='gpt-6-sol', free=True, tier='light', api_key='k').model_dump(), user_id=store.user_id)
    assert begun['id']
    answer = router_client.post('/api/cli/connections/rerank', headers=device)
    assert answer.status_code == 200 and answer.json()['changed'] == 1
    tiers = {c['id']: c['tier'] for c in store.list_connections(store.user_id)}
    assert tiers[old['id']] == 'strong' and tiers[mine['id']] == 'light'
    assert store.decrypt(store.get_connection(old['id'], store.user_id, True)) == 'k', 'its key stays'
