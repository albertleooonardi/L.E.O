import base64
import hashlib
import json
import re
import socket
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.auth import Auth
from backend.connectors import tool_specs
from backend.mcp_client import result_text

MCP_URL = 'https://mcp.example.com/mcp'
CHALLENGE = 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"'
TOOLS = [
    {'name': 'search_issues', 'description': 'Find issues', 'inputSchema': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
     'annotations': {'readOnlyHint': True}},
    {'name': 'create_issue', 'description': 'Create an issue', 'inputSchema': {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title']}},
    {'name': 'odd', 'description': 'A tool with a schema providers reject', 'inputSchema': {'type': 'string'}},
]


class FakeMCP:
    """A remote MCP server (with paging, sessions, JSON or SSE answers) and its OAuth sign-in service."""

    def __init__(self, auth=None, sse=False, registration=True):
        self.auth, self.sse, self.registration = auth, sse, registration
        self.valid, self.calls, self.challenge, self.registered, self.refreshes = {'tok-123'}, [], None, [], 0

    def answer(self, ident, result, **headers):
        message = {'jsonrpc': '2.0', 'id': ident, 'result': result}
        if self.sse:
            return httpx.Response(200, text=f'event: message\ndata: {json.dumps(message)}\n\n', headers={'content-type': 'text/event-stream', **headers})
        return httpx.Response(200, json=message, headers=headers)

    def __call__(self, request):
        url, path = str(request.url), request.url.path
        if request.url.host == 'mcp.example.com':
            if path == '/.well-known/oauth-protected-resource/mcp':
                return httpx.Response(200, json={'resource': MCP_URL, 'authorization_servers': ['https://auth.example.com']})
            if self.auth and request.headers.get('authorization', '').removeprefix('Bearer ') not in self.valid:
                return httpx.Response(401, headers={'www-authenticate': CHALLENGE})
            body = json.loads(request.content)
            if body['method'] == 'initialize':
                assert body['params']['clientInfo']['name'] == 'L.E.O.'
                return self.answer(body['id'], {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}}, **{'mcp-session-id': 'session-1'})
            assert request.headers['mcp-session-id'] == 'session-1'
            if 'id' not in body:
                return httpx.Response(202)
            if body['method'] == 'tools/list':
                cursor = body['params'].get('cursor')
                return self.answer(body['id'], {'tools': TOOLS[2:]} if cursor else {'tools': TOOLS[:2], 'nextCursor': 'page-2'})
            if body['method'] == 'tools/call':
                name, args = body['params']['name'], body['params']['arguments']
                self.calls.append((name, args))
                if name == 'create_issue':
                    return self.answer(body['id'], {'content': [{'type': 'text', 'text': f"Created LIN-7: {args['title']}"}]})
                return self.answer(body['id'], {'content': [{'type': 'text', 'text': '2 issues found'}, {'type': 'image', 'mimeType': 'image/png', 'data': 'AAA'}]})
        if request.url.host == 'auth.example.com':
            if path == '/.well-known/oauth-authorization-server':
                meta = {'issuer': 'https://auth.example.com', 'authorization_endpoint': 'https://auth.example.com/authorize',
                        'token_endpoint': 'https://auth.example.com/token', 'code_challenge_methods_supported': ['S256']}
                if self.registration:
                    meta['registration_endpoint'] = 'https://auth.example.com/register'
                return httpx.Response(200, json=meta)
            if path == '/register':
                self.registered.append(json.loads(request.content))
                return httpx.Response(201, json={'client_id': 'dynamic-client'})
            if path == '/token':
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                assert form['resource'] == MCP_URL
                if form['grant_type'] == 'refresh_token':
                    assert form['refresh_token'] == 'refresh-1'
                    self.refreshes += 1
                    self.valid.add('access-2')
                    return httpx.Response(200, json={'access_token': 'access-2', 'expires_in': 3600})
                if base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'].encode()).digest()).rstrip(b'=').decode() != self.challenge:
                    return httpx.Response(400, json={'error': 'invalid_grant'})
                self.valid.add('access-1')
                return httpx.Response(200, json={'access_token': 'access-1', 'refresh_token': 'refresh-1', 'expires_in': 3600})
        return httpx.Response(404, json={'error': f'unexpected {url}'})


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Public names resolve to a public address; intranet names to a private one."""
    def resolve(host, port, *args, **kwargs):
        address = '10.0.0.5' if host.endswith('.corp') else '169.254.169.254' if host == 'metadata.example.com' else '93.184.216.34'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 0))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)


def connector_client(store, server, session=None):
    client = TestClient(create_app(store.config, store, mcp_transport=httpx.MockTransport(server)))
    client.cookies.set('leo_session', session or store.user_session_token)
    return client


def navigation(page):
    return re.search(r'url=([^"]+)"', page.text)[1].replace('&amp;', '&')


@pytest.mark.parametrize('sse', [False, True])
def test_token_connector_lists_every_page_of_tools(store, sse):
    server = FakeMCP(auth='token', sse=sse)
    with connector_client(store, server) as client:
        made = client.post('/api/connectors', json={'name': 'Linear', 'url': MCP_URL, 'auth': 'token', 'token': 'tok-123'}).json()
    assert made['status'] == 'connected' and made['signed_in'] and 'secret' not in made and 'tok-123' not in json.dumps(made)
    assert [t['name'] for t in made['tools']] == ['search_issues', 'create_issue', 'odd']
    assert made['tools'][0]['read_only'] and not made['tools'][1]['read_only']
    assert made['permissions'] == {'search_issues': 'ask', 'create_issue': 'ask', 'odd': 'ask'}


def test_a_rejected_token_is_saved_with_an_error(store):
    with connector_client(store, FakeMCP(auth='token')) as client:
        made = client.post('/api/connectors', json={'name': 'Linear', 'url': MCP_URL, 'auth': 'token', 'token': 'wrong'}).json()
        assert made['status'] == 'error' and 'refused the token' in made['error']
        assert client.put(f"/api/connectors/{made['id']}", json={'token': 'tok-123'}).status_code == 200
        assert client.post(f"/api/connectors/{made['id']}/refresh").json()['status'] == 'connected'


def test_connector_addresses_must_be_public_https(store):
    with connector_client(store, FakeMCP()) as client:
        for url in ('http://mcp.example.com/mcp', 'https://wiki.corp/mcp', 'https://metadata.example.com/mcp', 'https://user:pw@mcp.example.com/mcp'):
            assert client.post('/api/connectors', json={'name': 'X', 'url': url}).status_code == 400, url
    store.config.mcp_allow_private = True
    with connector_client(store, FakeMCP()) as client:
        assert client.post('/api/connectors', json={'name': 'Wiki', 'url': 'http://wiki.corp/mcp'}).status_code == 201


def test_oauth_connector_registers_signs_in_and_refreshes(store):
    server = FakeMCP(auth='oauth')
    with connector_client(store, server) as client:
        made = client.post('/api/connectors', json={'name': 'Notion', 'url': MCP_URL, 'auth': 'oauth'}).json()
        assert made['status'] == 'sign_in' and not made['signed_in']
        start = client.get(f"/api/connectors/{made['id']}/oauth/start", follow_redirects=False)
        assert start.status_code == 307
        query = parse_qs(urlsplit(start.headers['location']).query)
        assert start.headers['location'].startswith('https://auth.example.com/authorize?')
        assert query['client_id'] == ['dynamic-client'] and query['resource'] == [MCP_URL] and query['code_challenge_method'] == ['S256']
        assert query['redirect_uri'] == ['http://testserver/api/connectors/oauth/callback']
        assert server.registered[0]['redirect_uris'] == ['http://testserver/api/connectors/oauth/callback']
        server.challenge = query['code_challenge'][0]
        # The provider's redirect carries no SameSite=Strict session cookie; the encrypted sign-in cookie identifies the user.
        client.cookies.delete('leo_session')
        page = client.get('/api/connectors/oauth/callback', params={'code': 'c0de', 'state': query['state'][0]})
        assert navigation(page) == f"/?settings=connectors&connected={made['id']}"
        client.cookies.set('leo_session', store.user_session_token)
        listed = client.get('/api/connectors').json()[0]
    assert listed['status'] == 'connected' and listed['signed_in'] and len(listed['tools']) == 3
    # An access token the server stops accepting is refreshed once, transparently.
    server.valid.discard('access-1')
    with connector_client(store, server) as client:
        assert client.post(f"/api/connectors/{made['id']}/refresh").json()['status'] == 'connected'
    assert server.refreshes == 1


def test_oauth_without_registration_asks_for_a_client_id(store):
    server = FakeMCP(auth='oauth', registration=False)
    with connector_client(store, server) as client:
        made = client.post('/api/connectors', json={'name': 'Corp', 'url': MCP_URL, 'auth': 'oauth'}).json()
        page = client.get(f"/api/connectors/{made['id']}/oauth/start", follow_redirects=False)
        assert navigation(page) == '/?settings=connectors&error=needs_client'
        assert client.get('/api/connectors').json()[0]['needs_client'] is True
        client.put(f"/api/connectors/{made['id']}", json={'client_id': 'pasted-id', 'client_secret': 'pasted-secret'})
        start = client.get(f"/api/connectors/{made['id']}/oauth/start", follow_redirects=False)
        assert parse_qs(urlsplit(start.headers['location']).query)['client_id'] == ['pasted-id']


def test_a_forged_connector_callback_is_refused(store):
    with connector_client(store, FakeMCP(auth='oauth')) as client:
        made = client.post('/api/connectors', json={'name': 'Notion', 'url': MCP_URL, 'auth': 'oauth'}).json()
        client.get(f"/api/connectors/{made['id']}/oauth/start", follow_redirects=False)
        page = client.get('/api/connectors/oauth/callback', params={'code': 'c0de', 'state': 'forged'})
        assert navigation(page) == '/?settings=connectors&error=failed'
        assert client.get('/api/connectors').json()[0]['signed_in'] is False


def test_permissions_and_isolation(store):
    server = FakeMCP()
    with connector_client(store, server) as client:
        made = client.post('/api/connectors', json={'name': 'Linear', 'url': MCP_URL}).json()
        edited = client.put(f"/api/connectors/{made['id']}", json={'permissions': {'search_issues': 'allow', 'odd': 'deny'}}).json()
        assert edited['permissions'] == {'search_issues': 'allow', 'create_issue': 'ask', 'odd': 'deny'}
        assert client.put(f"/api/connectors/{made['id']}", json={'permissions': {'nope': 'allow'}}).status_code == 400
    other = store.create_user('colleague', Auth.hash_password('colleague-pass'))
    with connector_client(store, server, Auth(store, store.config).create_session(other['id'])) as stranger:
        assert stranger.get('/api/connectors').json() == []
        assert stranger.put(f"/api/connectors/{made['id']}", json={'enabled': False}).status_code == 404
        assert stranger.delete(f"/api/connectors/{made['id']}").status_code == 404
        assert stranger.post('/api/runs', json={'prompt': 'hi', 'connectors': [made['id']], 'request_id': 'stranger-run'}).status_code == 400


def test_tool_specs_namespace_and_skip_denied_or_unusable_tools(store):
    with connector_client(store, FakeMCP()) as client:
        made = client.post('/api/connectors', json={'name': 'Linear Prod!', 'url': MCP_URL}).json()
        client.put(f"/api/connectors/{made['id']}", json={'permissions': {'search_issues': 'deny'}})
    specs, index, skipped = tool_specs([store.get_connector(made['id'], store.user_id)])
    assert [s['name'] for s in specs] == ['linear_prod__create_issue'] and specs[0]['description'].startswith('[Linear Prod!]')
    assert index == {'linear_prod__create_issue': (made['id'], 'create_issue')} and skipped == ['Linear Prod! · odd']


def test_tool_results_become_text():
    assert result_text({'content': [{'type': 'text', 'text': 'hi'}, {'type': 'image', 'mimeType': 'image/png'}]}) == 'hi\n[image (image/png) omitted]'
    assert result_text({'content': [], 'structuredContent': {'n': 1}}) == '{"n": 1}'
    assert result_text({'content': [{'type': 'text', 'text': 'x' * 30000}]}).endswith('[10000 characters omitted]')
