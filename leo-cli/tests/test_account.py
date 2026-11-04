"""L.E.O CLI's account against the real server app, in-process: device login, sync, usage reports, sign-out."""
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.auth import Auth
from backend.config import Config
from backend.schemas import Connection, RunInput
from backend.store import Store, calls
from leo_cli.account import Account, AccountError, SignedOut


@pytest.fixture
def server(tmp_path):
    """A L.E.O. server with one user who has a model with a key and a 9Router (Chat only) model."""
    store = Store(Config(data_dir=tmp_path / 'server'))
    user = store.create_user('ann', Auth.hash_password('ann-password'))
    store.save_connection(Connection(name='OpenRouter · free', base_url='https://openrouter.example/v1', model='m-free', free=True,
                                     tools=True, api_key='sk-or-1').model_dump(), user_id=user['id'])
    planner = store.save_connection(Connection(name='Strong', base_url='https://openrouter.example/v1', model='m-strong', free=False,
                                               api_key='sk-or-2').model_dump(), user_id=user['id'])
    store.set_preferences({**store.preferences(), 'planner_id': planner['id']})
    store.save_router(user['id'], 'http://leo-router-a:20128', 'p' * 24)
    store.save_connection(Connection(name='Codex', base_url='http://leo-router-a:20128/v1', model='cx/gpt', free=True, api_key='sk-9r').model_dump(),
                          user_id=user['id'])
    app = create_app(store.config, store)
    browser = TestClient(app)
    browser.cookies.set('leo_session', Auth(store, store.config).create_session(user['id']))
    yield {'store': store, 'app': app, 'browser': browser, 'transport': httpx.ASGITransport(app=app)}
    browser.close()
    store.engine.dispose()


@pytest.fixture
def account(tmp_path, monkeypatch, server):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    return Account(tmp_path / 'cli-home', transport=server['transport'])


async def no_wait(seconds):
    pass


def approver(server, answer=True):
    def show(code, url):
        assert url == f'http://testserver/cli?code={code}'
        assert server['browser'].post(f'/api/cli/device/{code}', json={'approve': answer}).status_code == 200
    return show


async def test_login_syncs_models_keys_and_preferences(account, server):
    summary = await account.login('http://testserver', 'test-laptop', show=approver(server), sleep=no_wait)
    assert summary == {'models': 2, 'chat_only': 1, 'username': 'ann'}
    store = account.store()
    synced = {c['model']: c for c in store.list_connections(account.user_id, private=True)}
    assert set(synced) == {'m-free', 'm-strong'}, 'the 9Router model stays Chat only'
    assert store.decrypt(synced['m-free']) == 'sk-or-1' and synced['m-free']['tools'] is True
    assert store.preferences()['planner_id'] == synced['m-strong']['id'], 'role choices follow the models to their local ids'
    assert account.config()['chat_only'] == [{'name': 'Codex', 'model': 'cx/gpt'}]
    assert (account.home / 'secrets.json').stat().st_mode & 0o777 == 0o600
    assert 'sk-or-1' not in (account.home / 'leo.db').read_bytes().decode('latin-1'), 'keys are encrypted at rest'


async def test_a_denied_login_saves_nothing(account, server):
    with pytest.raises(AccountError, match='denied'):
        await account.login('http://testserver', show=approver(server, answer=False), sleep=no_wait)
    assert account.secrets.get('device-token') is None and account.server is None


async def test_sync_after_revocation_says_to_sign_in_again(account, server):
    await account.login('http://testserver', show=approver(server), sleep=no_wait)
    [device] = server['browser'].get('/api/cli/devices').json()
    server['browser'].delete(f"/api/cli/devices/{device['id']}")
    with pytest.raises(SignedOut, match='leo login'):
        await account.sync()


async def test_usage_is_reported_once_with_totals_only(account, server):
    await account.login('http://testserver', show=approver(server), sleep=no_wait)
    store = account.store()
    run = store.create_run(RunInput(prompt='secret plan', mode='quick', request_id=str(uuid.uuid4())), account.user_id)
    with store.engine.begin() as db:
        for tokens in (100, 300):
            db.execute(calls.insert().values(id=str(uuid.uuid4()), run_id=run['id'], stage='generating', provider='OpenRouter · free', model='m-free',
                                             status='completed', input_tokens=tokens, output_tokens=10, reserved_tokens=0, reserved_cost=0,
                                             cost=0.001, estimated=False, created_at=time.time(), elapsed_ms=5))
    assert await account.report_usage() == 1
    assert await account.report_usage() == 0, 'nothing is sent twice'
    report = server['browser'].get('/api/usage').json()['cli']
    assert report['totals'] == {'calls': 2, 'input_tokens': 400, 'output_tokens': 20, 'cost': 0.002}


async def test_logout_revokes_the_device_and_removes_local_keys(account, server):
    await account.login('http://testserver', show=approver(server), sleep=no_wait)
    await account.logout()
    assert account.store().list_connections(account.user_id) == [] and account.secrets.get('device-token') is None
    assert server['browser'].get('/api/cli/devices').json() == []
