import io
import json
import tarfile
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import egress
from backend.providers import ToolCalls, decode_chunk, make_request, stream
from backend.sandbox import SandboxError, files_tar, rebuild_tar, tar_to_zip, workspace_path, zip_to_tar
from backend.sandboxd import Settings, create_app


# -- tool calling across protocols -------------------------------------------

TRANSCRIPT = [
    {'role': 'system', 'content': 'S'},
    {'role': 'user', 'content': 'Fix it'},
    {'role': 'assistant', 'content': 'Looking', 'tool_calls': [{'id': 'c1', 'name': 'bash', 'arguments': {'command': 'ls'}},
                                                               {'id': 'c2', 'name': 'read_file', 'arguments': {'path': 'a.py'}}]},
    {'role': 'tool', 'tool_call_id': 'c1', 'name': 'bash', 'content': 'a.py'},
    {'role': 'tool', 'tool_call_id': 'c2', 'name': 'read_file', 'content': '1\tprint(1)'},
]
TOOLS = [{'name': 'bash', 'description': 'Run', 'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}}}]


def test_openai_transcript_and_tools():
    _, payload = make_request({'protocol': 'openai', 'model': 'm', 'base_url': 'http://x'}, TRANSCRIPT, 100, tools=TOOLS)
    assistant = payload['messages'][2]
    assert assistant['tool_calls'][0] == {'id': 'c1', 'type': 'function', 'function': {'name': 'bash', 'arguments': '{"command": "ls"}'}}
    assert payload['messages'][3] == {'role': 'tool', 'tool_call_id': 'c1', 'content': 'a.py'}
    assert payload['tools'] == [{'type': 'function', 'function': TOOLS[0]}]


def test_anthropic_groups_tool_results_in_one_user_turn():
    _, payload = make_request({'protocol': 'anthropic', 'model': 'm', 'base_url': 'http://x'}, TRANSCRIPT, 100, tools=TOOLS)
    assert payload['system'] == 'S'
    assistant, results = payload['messages'][1], payload['messages'][2]
    assert assistant['content'][0] == {'type': 'text', 'text': 'Looking'}
    assert assistant['content'][1] == {'type': 'tool_use', 'id': 'c1', 'name': 'bash', 'input': {'command': 'ls'}}
    assert results['role'] == 'user' and [b['tool_use_id'] for b in results['content']] == ['c1', 'c2']
    assert payload['tools'][0]['input_schema'] == TOOLS[0]['parameters']


def test_responses_uses_function_call_items():
    _, payload = make_request({'protocol': 'responses', 'model': 'm', 'base_url': 'http://x'}, TRANSCRIPT, 100, tools=TOOLS)
    kinds = [item.get('type', item.get('role')) for item in payload['input']]
    assert kinds == ['system', 'user', 'assistant', 'function_call', 'function_call', 'function_call_output', 'function_call_output']
    assert payload['tools'][0] == {'type': 'function', **TOOLS[0]}


def test_streamed_tool_call_fragments_assemble():
    calls = ToolCalls()
    calls.add(decode_chunk('openai', {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 'bash', 'arguments': '{"comm'}}]}}]}).calls)
    calls.add(decode_chunk('openai', {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': 'and": "ls"}'}}]}}]}).calls)
    calls.add(decode_chunk('anthropic', {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'b', 'name': 'read_file'}}).calls)
    calls.add(decode_chunk('anthropic', {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"path": "x"}'}}).calls)
    assert calls.result() == [{'id': 'a', 'name': 'bash', 'arguments': {'command': 'ls'}}, {'id': 'b', 'name': 'read_file', 'arguments': {'path': 'x'}}]
    done = decode_chunk('responses', {'type': 'response.output_item.done', 'output_index': 0,
                                      'item': {'type': 'function_call', 'call_id': 'r', 'name': 'bash', 'arguments': '{}'}})
    assert done.calls[0]['id'] == 'r'


def test_indexless_calls_and_bad_json_are_kept_apart():
    calls = ToolCalls()
    calls.add([{'id': 'x', 'name': 'bash', 'arguments': '{"command": "a"}'}])
    calls.add([{'id': 'y', 'name': 'bash', 'arguments': 'not json'}])
    first, second = calls.result()
    assert first['arguments'] == {'command': 'a'} and '_invalid_json' in second['arguments']


async def test_stream_sends_tools_and_yields_calls():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        body = ('data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'bash', 'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}]}) +
                '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, text=body, headers={'content-type': 'text/event-stream'})
    provider = {'protocol': 'openai', 'base_url': 'http://x/v1', 'model': 'm'}
    chunks = [c async for c in stream(provider, '', TRANSCRIPT, 64, 10, httpx.MockTransport(handler), tools=TOOLS)]
    assert seen[0]['tools'] and any(c.calls for c in chunks)


# -- archives ----------------------------------------------------------------

def test_workspace_paths_stay_inside():
    assert workspace_path('src/app.py') == '/workspace/src/app.py'
    assert workspace_path('/workspace/x') == '/workspace/x'
    for bad in ('../etc/passwd', '/etc/passwd', 'a/../../b'):
        with pytest.raises(SandboxError):
            workspace_path(bad)


def make_zip(entries, links=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        for name, target in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, target)
    return buffer.getvalue()


def members(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {m.name: m for m in archive}


def test_zip_upload_strips_wrapper_folder_and_drops_unsafe_entries():
    data = make_zip({'repo-main/app.py': 'print(1)', 'repo-main/src/x.py': 'x', '../evil.py': 'boom'}, links=[('repo-main/link', '/etc')])
    found = members(zip_to_tar(data))
    assert set(found) == {'app.py', 'src', 'src/x.py'}
    assert all(m.uid == 1000 for m in found.values())


def test_written_files_get_the_current_time():
    # mtime 0 on every write lets Python reuse a stale __pycache__ after a same-length edit.
    found = members(files_tar({'pkg/mod.py': b'VALUE = 1\n'}))
    assert all(abs(m.mtime - time.time()) < 60 for m in found.values())


def test_zip_without_wrapper_keeps_paths():
    assert set(members(zip_to_tar(make_zip({'a.py': '1', 'b/c.py': '2'})))) == {'a.py', 'b', 'b/c.py'}


def test_rebuild_tar_rejects_escapes_and_contains_links():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, kind, link in [('ok.txt', tarfile.REGTYPE, ''), ('/abs.txt', tarfile.REGTYPE, ''), ('../up.txt', tarfile.REGTYPE, ''),
                                 ('inside', tarfile.SYMTYPE, 'ok.txt'), ('outside', tarfile.SYMTYPE, '../../etc'), ('rooted', tarfile.SYMTYPE, '/etc'),
                                 ('dev', tarfile.CHRTYPE, ''), ('hard', tarfile.LNKTYPE, 'ok.txt')]:
            info = tarfile.TarInfo(name)
            info.type, info.linkname, info.mode, info.uid = kind, link, 0o4755, 0
            archive.addfile(info, io.BytesIO(b'') if kind == tarfile.REGTYPE else None)
    found = members(rebuild_tar(buffer.getvalue(), 'stage'))
    assert set(found) == {'stage', 'stage/ok.txt', 'stage/inside'}
    assert found['stage/ok.txt'].mode & 0o4000 == 0 and found['stage/ok.txt'].uid == 1000


def test_export_becomes_zip():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, data in [('workspace/a.txt', b'hello'), ('workspace/sub/b.txt', b'b')]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(tar_to_zip(buffer.getvalue()))) as archive:
        assert archive.read('a.txt') == b'hello' and archive.namelist() == ['a.txt', 'sub/b.txt']


# -- sandboxd against a fake Docker API ---------------------------------------

def frame(stream_type, text):
    data = text.encode()
    return bytes([stream_type, 0, 0, 0]) + len(data).to_bytes(4, 'big') + data


class FakeDocker:
    def __init__(self):
        self.calls, self.containers, self.archives = [], {}, []
        self.exec_specs, self.exec_starts, self.exit_code = [], [], 0

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if method == 'GET' and path.endswith('/json') and path.startswith('/containers/'):
            name = path.split('/')[2]
            return httpx.Response(200, json=self.containers[name]) if name in self.containers else httpx.Response(404, json={'message': 'no such container'})
        if path == '/volumes/create':
            return httpx.Response(201, json={})
        if path == '/containers/create':
            self.spec = json.loads(request.content)
            self.containers[request.url.params['name']] = {'State': {'Running': False}}
            return httpx.Response(201, json={'Id': 'abc'})
        if path.endswith('/start') and path.startswith('/containers/'):
            self.containers[path.split('/')[2]]['State']['Running'] = True
            return httpx.Response(204)
        if path.endswith('/exec'):
            self.exec_spec = json.loads(request.content)
            self.exec_specs.append(self.exec_spec)
            return httpx.Response(201, json={'Id': 'e1'})
        if path == '/exec/e1/start':
            self.exec_starts.append(json.loads(request.content))
            return httpx.Response(200, content=frame(1, 'hello ') + frame(2, 'warning\n'))
        if path == '/exec/e1/json':
            return httpx.Response(200, json={'ExitCode': self.exit_code, 'Running': False})
        if path.endswith('/archive') and method == 'PUT':
            self.archives.append((request.url.params['path'], request.content))
            return httpx.Response(200)
        return httpx.Response(404, json={'message': f'unexpected {method} {path}'})


@pytest.fixture
def sandboxd(monkeypatch):
    monkeypatch.setenv('LEO_SANDBOX_TOKEN', 'secret-token')
    docker = FakeDocker()
    with TestClient(create_app(Settings(), httpx.MockTransport(docker))) as client:
        client.headers['Authorization'] = 'Bearer secret-token'
        yield client, docker


SID = '0b1c2d3e-0000-4000-8000-000000000001'


def test_sandboxd_requires_token(sandboxd):
    client, _ = sandboxd
    assert client.put(f'/sessions/{SID}', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert client.get('/health', headers={'Authorization': ''}).status_code == 200
    assert client.put('/sessions/not_valid!').status_code in (400, 404)


def test_sandboxd_creates_locked_down_container(sandboxd):
    client, docker = sandboxd
    assert client.put(f'/sessions/{SID}').json()['status'] == 'running'
    host = docker.spec['HostConfig']
    assert host['CapDrop'] == ['ALL'] and host['SecurityOpt'] == ['no-new-privileges']
    assert host['NetworkMode'] == 'leo-sandbox' and host['PidsLimit'] == 512 and host['Memory'] == 2 * 1024 ** 3
    assert docker.spec['User'] == '1000:1000'
    assert 'HTTPS_PROXY=http://egress:3128' in docker.spec['Env'] and 'PYTHONDONTWRITEBYTECODE=1' in docker.spec['Env']
    client.put(f'/sessions/{SID}')
    assert docker.calls.count(('POST', '/containers/create')) == 1  # Idempotent.


def test_sandboxd_exec_demultiplexes_output_and_runs_unprivileged(sandboxd):
    client, docker = sandboxd
    result = client.post(f'/sessions/{SID}/exec', json={'command': 'echo hello', 'timeout': 30}).json()
    assert result == {'exit_code': 0, 'output': 'hello warning\n', 'timed_out': False, 'truncated': False}
    assert docker.exec_spec['User'] == '1000:1000'
    assert docker.exec_spec['Cmd'][:4] == ['timeout', '--kill-after=5', '30', 'bash']
    assert len(docker.exec_specs) == 1  # No cleanup when the command finished on its own.


def test_sandboxd_timeout_kills_every_process_the_command_started(sandboxd):
    client, docker = sandboxd
    docker.exit_code = 124
    result = client.post(f'/sessions/{SID}/exec', json={'command': 'sleep 300 &', 'timeout': 3}).json()
    assert result['timed_out']
    command, cleanup = docker.exec_specs
    marker = next(e for e in command['Env'] if e.startswith('LEO_EXEC='))
    # The cleanup finds processes by the marker in their environment (it survives setsid and job control),
    # runs as the sandbox user, and finishes before exec returns.
    assert marker.removeprefix('LEO_EXEC=') in cleanup['Cmd'] and cleanup['User'] == '1000:1000'
    assert not any(e.startswith('LEO_EXEC=') for e in cleanup.get('Env') or [])
    assert docker.exec_starts[-1]['Detach'] is False


def test_sandboxd_imports_through_a_fresh_staging_folder(sandboxd):
    client, docker = sandboxd
    assert client.put(f'/sessions/{SID}/import', content=files_tar({'src/app.py': b'print(1)'})).status_code == 200
    path, archive = docker.archives[-1]
    assert path == '/tmp'
    names = list(members(archive))
    staging = names[0]
    assert staging.startswith('leo-import-') and f'{staging}/src/app.py' in names
    assert 'cp -a' in docker.exec_spec['Cmd'][-1] and docker.exec_spec['User'] == '1000:1000'


# -- egress proxy ------------------------------------------------------------

def test_egress_allowlist_and_request_parsing():
    rules = ['pypi.org', '.npmjs.org']
    assert egress.allowed('pypi.org', rules) and egress.allowed('registry.npmjs.org', rules)
    assert not egress.allowed('evil-pypi.org', rules) and not egress.allowed('example.com', rules)
    assert egress.parse_target('CONNECT pypi.org:443 HTTP/1.1') == ('CONNECT', 'pypi.org', 443, None)
    assert egress.parse_target('GET http://pypi.org/simple/x HTTP/1.1') == ('GET', 'pypi.org', 80, '/simple/x')
    assert egress.parse_target('GET /relative HTTP/1.1') is None


async def test_egress_refuses_hosts_outside_the_allowlist():
    class Writer:
        data = b''

        def write(self, value):
            self.data += value

        def close(self):
            pass

    import asyncio
    reader = asyncio.StreamReader()
    reader.feed_data(b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n')
    writer = Writer()
    await egress.handle(reader, writer, ['pypi.org'])
    assert writer.data.startswith(b'HTTP/1.1 403') and b'allowlist' in writer.data
