"""The server side of L.E.O CLI: device login, model sync, usage reports, and device revocation."""
import time

from fastapi.testclient import TestClient

from backend.api import create_app
from backend.schemas import Connection
from .conftest import add_provider


def anonymous(store):
    return TestClient(create_app(store.config, store))


def sign_in_device(client, store, name='laptop'):
    """Run the device flow: the CLI asks for a code, the signed-in user allows it, the CLI collects its token."""
    with anonymous(store) as cli:
        started = cli.post('/api/cli/device', json={'device_name': name}).json()
        assert cli.post('/api/cli/token', json={'device_code': started['device_code']}).json() == {'status': 'pending'}
        assert client.get(f"/api/cli/device/{started['user_code']}").json()['device_name'] == name
        assert client.post(f"/api/cli/device/{started['user_code']}", json={'approve': True}).status_code == 200
        token = cli.post('/api/cli/token', json={'device_code': started['device_code']}).json()
        assert token['status'] == 'approved' and len(token['token']) > 30
        assert cli.post('/api/cli/token', json={'device_code': started['device_code']}).json() == {'status': 'expired'}, 'single use'
    return started, token['token']


def test_device_login_shows_a_readable_code_and_link(client, store):
    with anonymous(store) as cli:
        started = cli.post('/api/cli/device', json={'device_name': 'macbook'}).json()
    code = started['user_code']
    assert len(code) == 9 and code[4] == '-' and not set(code) & set('01IO')
    assert started['verification_url'] == f'http://testserver/cli?code={code}' and started['expires_in'] == 600


def test_device_login_allow_then_sync_models_with_keys(client, store):
    add_provider(store, api_key='sk-local-test')
    _, token = sign_in_device(client, store)
    with anonymous(store) as cli:
        synced = cli.get('/api/cli/sync', headers={'Authorization': f'Bearer {token}'}).json()
    [model] = synced['connections']
    assert model['api_key'] == 'sk-local-test' and model['model'] == 'test-model' and 'secret' not in model
    assert synced['device']['name'] == 'laptop' and synced['user']['username'] == 'testuser'
    assert synced['preferences']['routing'] and {s['id'] for s in synced['skills']} >= {'general', 'coding'}
    assert [d['name'] for d in client.get('/api/cli/devices').json()] == ['laptop']


def test_denied_and_expired_logins_give_no_token(client, store):
    with anonymous(store) as cli:
        denied = cli.post('/api/cli/device', json={'device_name': 'stranger'}).json()
        client.post(f"/api/cli/device/{denied['user_code']}", json={'approve': False})
        assert cli.post('/api/cli/token', json={'device_code': denied['device_code']}).json() == {'status': 'denied'}
        assert client.post(f"/api/cli/device/{denied['user_code']}", json={'approve': True}).status_code == 409
        late = cli.post('/api/cli/device', json={'device_name': 'slow'}).json()
        store.create_cli_login('unused-digest', 'WXYZ-2345', 'x', time.time() + 600)
        from backend.store import cli_logins
        from sqlalchemy import update
        with store.engine.begin() as db:
            db.execute(update(cli_logins).values(expires_at=time.time() - 1))
        assert cli.post('/api/cli/token', json={'device_code': late['device_code']}).json() == {'status': 'expired'}
        assert client.post(f"/api/cli/device/{late['user_code']}", json={'approve': True}).status_code == 404
        assert cli.post('/api/cli/token', json={'device_code': 'not-a-real-device-code'}).json() == {'status': 'expired'}


def test_a_device_token_only_opens_the_cli_endpoints(client, store):
    _, token = sign_in_device(client, store)
    with anonymous(store) as cli:
        bearer = {'Authorization': f'Bearer {token}'}
        assert cli.get('/api/conversations', headers=bearer).status_code == 401
        assert cli.get('/api/auth/me', headers=bearer).status_code == 401
        assert cli.get('/api/cli/sync').status_code == 401
        assert cli.get('/api/cli/sync', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    # A browser session is not a device either.
    assert client.get('/api/cli/sync').status_code == 401


def test_per_user_router_models_stay_chat_only(client, store):
    store.save_router(store.user_id, 'http://leo-router-x:20128', 'p' * 24)
    store.save_connection(Connection(name='ChatGPT (Codex) · gpt-5', base_url='http://leo-router-x:20128/v1', model='cx/gpt-5', free=True,
                                     api_key='sk-9r').model_dump(), user_id=store.user_id)
    add_provider(store, api_key='sk-local-test')
    _, token = sign_in_device(client, store)
    with anonymous(store) as cli:
        synced = cli.get('/api/cli/sync', headers={'Authorization': f'Bearer {token}'}).json()
    assert [c['model'] for c in synced['connections']] == ['test-model']
    assert synced['chat_only'] == [{'name': 'ChatGPT (Codex) · gpt-5', 'model': 'cx/gpt-5'}]


def test_usage_reports_reach_the_usage_page(client, store):
    _, token = sign_in_device(client, store)
    entry = {'session': 's1', 'model': 'gpt-x', 'connection': 'OpenRouter', 'calls': 3, 'input_tokens': 900, 'output_tokens': 120,
             'cost': 0.004, 'estimated': False, 'at': time.time()}
    with anonymous(store) as cli:
        assert cli.post('/api/cli/usage', json={'entries': [entry]}, headers={'Authorization': f'Bearer {token}'}).json() == {'ok': True, 'received': 1}
    cli_usage = client.get('/api/usage').json()['cli']
    assert cli_usage['totals'] == {'calls': 3, 'input_tokens': 900, 'output_tokens': 120, 'cost': 0.004}
    assert cli_usage['models'][0]['model'] == 'gpt-x'


def test_revoking_a_device_signs_it_out(client, store):
    _, token = sign_in_device(client, store)
    [device] = client.get('/api/cli/devices').json()
    assert client.delete(f"/api/cli/devices/{device['id']}").status_code == 200
    with anonymous(store) as cli:
        refused = cli.get('/api/cli/sync', headers={'Authorization': f'Bearer {token}'})
        assert refused.status_code == 401 and 'leo login' in refused.json()['detail']
    _, second = sign_in_device(client, store, 'desktop')
    with anonymous(store) as cli:
        assert cli.post('/api/cli/logout', headers={'Authorization': f'Bearer {second}'}).status_code == 200
        assert cli.get('/api/cli/sync', headers={'Authorization': f'Bearer {second}'}).status_code == 401
    assert client.get('/api/cli/devices').json() == []
