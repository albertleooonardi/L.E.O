"""L.E.O CLI's account against the real server app, in-process: device login, sync, usage reports, sign-out."""
import asyncio
import json
import os
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
    seen = []

    def router(request):  # The user's 9Router, which only the server can reach.
        seen.append(request)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              content=b'data: {"choices":[{"delta":{"content":"hi from codex"}}]}\n\ndata: [DONE]\n\n')

    app = create_app(store.config, store, relay_transport=httpx.MockTransport(router))
    browser = TestClient(app)
    browser.cookies.set('leo_session', Auth(store, store.config).create_session(user['id']))
    yield {'store': store, 'app': app, 'browser': browser, 'transport': httpx.ASGITransport(app=app), 'user': user, 'router_calls': seen}
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
        assert url.startswith(f'http://testserver/cli?code={code}&port=')
        assert server['browser'].post(f'/api/cli/device/{code}', json={'approve': answer}).status_code == 200
    return show


async def test_login_syncs_models_keys_and_preferences(account, server):
    summary = await account.login('http://testserver', 'test-laptop', show=approver(server), sleep=no_wait)
    assert summary == {'models': 3, 'relayed': 1, 'username': 'ann'}
    store = account.store()
    synced = {c['model']: c for c in store.list_connections(account.user_id, private=True)}
    assert set(synced) == {'m-free', 'm-strong', 'cx/gpt'}
    # The 9Router model is reached through the server, signed with the device token; the router key stays on the server.
    assert synced['cx/gpt']['base_url'].startswith('http://testserver/api/cli/relay/')
    assert store.decrypt(synced['cx/gpt']) == account.secrets.get('device-token')
    assert store.decrypt(synced['m-free']) == 'sk-or-1' and synced['m-free']['tools'] is True
    assert store.preferences()['planner_id'] == synced['m-strong']['id'], 'role choices follow the models to their local ids'
    if os.name != 'nt':  # Windows has no mode bits; the file sits in the user's own %APPDATA%.
        assert (account.home / 'secrets.json').stat().st_mode & 0o777 == 0o600
    database = (account.home / 'leo.db').read_bytes().decode('latin-1')
    assert 'sk-or-1' not in database and 'sk-9r' not in database, 'keys are encrypted at rest; the router key never comes down'


async def test_chat_connectors_come_down_and_run_through_the_server(tmp_path, monkeypatch, server):
    """Chat's connectors: their tools come down for the CLI's agent and /mcp; each call is made by the server, which keeps
    the sign-in. Tokens never reach the laptop, and a tool Chat denies cannot be called."""
    import socket
    from backend.mcp_client import MCPError
    from leo_cli.mcp import chat_connectors
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))])
    seen = []

    def linear(request):
        body = json.loads(request.content)
        if 'id' not in body:
            return httpx.Response(202)
        if body['method'] == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}}
        else:
            seen.append((body['params']['name'], body['params']['arguments'], request.headers['authorization']))
            result = {'content': [{'type': 'text', 'text': 'Created LIN-7'}]}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})

    schema = {'type': 'object', 'properties': {'title': {'type': 'string'}}}
    server['store'].create_connector(server['user']['id'], {
        'name': 'Linear', 'url': 'https://mcp.linear.example/mcp', 'enabled': True, 'auth': 'token', 'status': 'connected',
        'tools': [{'name': 'create_issue', 'description': 'New issue', 'input_schema': schema, 'read_only': False},
                  {'name': 'delete_all', 'description': 'Delete everything', 'input_schema': schema, 'read_only': False}],
        'permissions': {'create_issue': 'ask', 'delete_all': 'deny'}}, secret={'token': 'lin-secret-token'})
    app = create_app(server['store'].config, server['store'], mcp_transport=httpx.MockTransport(linear))
    account = Account(tmp_path / 'cli-home-2', transport=httpx.ASGITransport(app=app))
    await account.login('http://testserver', show=approver(server), sleep=no_wait)
    [found] = chat_connectors(account.home)
    assert found['name'] == 'Linear' and found['status'] == 'connected' and found['signed_in'] is True
    assert [t['name'] for t in found['tools']] == ['create_issue', 'delete_all'] and found['tools'][0]['input_schema'] == schema
    assert 'lin-secret-token' not in (account.home / 'chat-connectors.json').read_text()
    assert await account.call_connector(found['id'], 'create_issue', {'title': 'Bug'}) == {'text': 'Created LIN-7', 'is_error': False}
    assert seen == [('create_issue', {'title': 'Bug'}, 'Bearer lin-secret-token')], 'the server signs the call'
    with pytest.raises(MCPError, match='not allowed'):
        await account.call_connector(found['id'], 'delete_all', {})
    await account.logout()
    assert chat_connectors(account.home) is None, 'signing out removes them'


async def test_the_device_can_ask_for_account_sign_in(account, server):
    """The sign-in routes of Settings → Models answer a device token too; this server has no sandbox, and says so."""
    with pytest.raises(SignedOut):
        await account.model_accounts()
    await account.login('http://testserver', show=approver(server), sleep=no_wait)
    offered = await account.model_accounts()
    assert offered['available'] is False and 'sandbox service' in offered['error'] and {'codex', 'github'} <= {p['id'] for p in offered['providers']}
    with pytest.raises(AccountError, match='sandbox service'):
        await account.start_sign_in('kimi')


async def test_chatgpt_models_on_the_server_work_through_the_relay(account, server):
    """A model call from the laptop goes to the server, which signs it with the router key and streams the answer back."""
    from backend import providers
    await account.login('http://testserver', show=approver(server), sleep=no_wait)
    store = account.store()
    codex = next(c for c in store.list_connections(account.user_id, private=True) if c['model'] == 'cx/gpt')
    text = ''
    async for chunk in providers.stream(codex, store.decrypt(codex), [{'role': 'user', 'content': 'hi'}], 100, 30, server['transport']):
        text += chunk.text
    assert text == 'hi from codex'
    [call] = server['router_calls']
    assert str(call.url) == 'http://leo-router-a:20128/v1/chat/completions' and call.headers['authorization'] == 'Bearer sk-9r'


async def test_the_relay_only_reaches_the_users_own_router_and_only_for_model_calls(account, server):
    await account.login('http://testserver', show=approver(server), sleep=no_wait)
    token = account.secrets.get('device-token')
    ids = {c['model']: c['id'] for c in server['store'].list_connections(server['user']['id'])}
    async with httpx.AsyncClient(transport=server['transport'], base_url='http://testserver') as http:
        signed = {'Authorization': f'Bearer {token}'}
        assert (await http.post(f"/api/cli/relay/{ids['cx/gpt']}/chat/completions", json={})).status_code == 401, 'device token required'
        assert (await http.post(f"/api/cli/relay/{ids['cx/gpt']}/v1/admin", json={}, headers=signed)).status_code == 404, 'model calls only'
        assert (await http.post(f"/api/cli/relay/{ids['m-free']}/chat/completions", json={}, headers=signed)).status_code == 404,             'only models on the 9Router; others are called directly'
        other = server['store'].create_user('bob', Auth.hash_password('bob-password'))
        theirs = server['store'].save_connection(Connection(name='x', base_url='http://leo-router-a:20128/v1', model='cx/x').model_dump(),
                                                 user_id=other['id'])
        assert (await http.post(f"/api/cli/relay/{theirs['id']}/chat/completions", json={}, headers=signed)).status_code == 404
    assert server['router_calls'] == []


async def test_a_model_added_in_the_cli_is_saved_on_the_account(account, server):
    await account.login('http://testserver', show=approver(server), sleep=no_wait)
    await account.add_connection({'name': 'Groq · llama', 'base_url': 'https://api.groq.com/openai/v1', 'model': 'llama', 'api_key': 'gsk-1',
                                  'free': True, 'tools': True})
    on_server = {c['model']: c for c in server['store'].list_connections(server['user']['id'], private=True)}
    assert server['store'].decrypt(on_server['llama']) == 'gsk-1', 'like Settings → Models: the website sees it too'
    assert 'llama' in {c['model'] for c in account.store().list_connections(account.user_id)}, 'and it is synced down at once'


async def test_the_browser_coming_back_ends_the_wait_at_once(account, server):
    """After Allow, the page sends the browser to the terminal's listener on 127.0.0.1; no poll interval to wait out."""
    pages, returns = [], []

    async def never(seconds):
        await asyncio.sleep(3600)

    async def come_back(port):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(b'GET /callback?status=approved HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        pages.append(await reader.read())
        writer.close()

    def show(code, url):
        assert server['browser'].post(f'/api/cli/device/{code}', json={'approve': True}).status_code == 200
        returns.append(asyncio.ensure_future(come_back(int(url.rsplit('port=', 1)[1]))))

    summary = await asyncio.wait_for(account.login('http://testserver', show=show, sleep=never), 10)
    await returns[0]
    assert summary['username'] == 'ann'
    assert pages[0].startswith(b'HTTP/1.1 200') and b'L.E.O CLI is signed in' in pages[0]


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
