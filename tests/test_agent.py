import io
import json
import tarfile
import uuid
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.sandbox import SandboxError, workspace_path
from backend.schemas import Connection
from backend.worker import process_one
from .conftest import add_provider, make_run


class FakeSandbox:
    """In-memory stand-in for sandboxd: files are a dict, commands are logged and answered by a handler."""

    def __init__(self, on_exec=None):
        self.files, self.commands, self.ensured, self.pushes, self.deleted = {}, [], [], [], []
        self.on_exec = on_exec or (lambda command: (0, f'ran: {command}'))

    async def ensure(self, sid):
        self.ensured.append(sid)
        return {'id': sid, 'status': 'running'}

    async def delete(self, sid):
        self.deleted.append(sid)

    async def exec(self, sid, command, timeout=120, cwd=None, max_output=30000):
        self.commands.append(command)
        code, output = self.on_exec(command)
        return {'exit_code': code, 'output': output, 'timed_out': False, 'truncated': False}

    async def read_file(self, sid, path, with_mode=False):
        key = workspace_path(path)
        if key not in self.files:
            raise SandboxError(f'No such file or folder: {path}')
        return (self.files[key], 0o755) if with_mode else self.files[key]

    async def write_file(self, sid, path, data, mode=0o644):
        self.files[workspace_path(path)] = data

    async def list_files(self, sid, path='.', depth=3, limit=2000):
        return [{'path': k.removeprefix('/workspace/'), 'type': 'file', 'size': len(v)} for k, v in sorted(self.files.items())]

    async def import_tar(self, sid, data):
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive:
                if member.isfile():
                    self.files[workspace_path(member.name)] = archive.extractfile(member).read()

    async def export(self, sid, path='/workspace'):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            for key, data in self.files.items():
                info = tarfile.TarInfo('workspace/' + key.removeprefix('/workspace/'))
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    async def clone(self, sid, url, branch=None, token=None):
        self.files['/workspace/README.md'] = b'cloned'
        return {'ok': True, 'branch': branch or 'main', 'created_branch': False}

    async def push(self, sid, url, branch, token, message):
        self.pushes.append((url, branch, token, message))
        return {'ok': True, 'branch': branch, 'commit': 'abc1234', 'output': 'pushed'}


def tool_reply(calls=(), text=''):
    """A streamed Chat Completions answer with tool calls."""
    deltas = [{'index': n, 'id': c[0], 'function': {'name': c[1], 'arguments': json.dumps(c[2])}} for n, c in enumerate(calls)]
    events = []
    if text:
        events.append({'choices': [{'delta': {'content': text}, 'finish_reason': None}]})
    if deltas:
        events.append({'choices': [{'delta': {'tool_calls': deltas}, 'finish_reason': None}]})
    events.append({'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if deltas else 'stop'}]})
    events.append({'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    body = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n'
    return httpx.Response(200, text=body, headers={'content-type': 'text/event-stream'})


def results(body):
    return [m for m in body['messages'] if m['role'] == 'tool']


async def run_agent(store, sandbox, handler, run=None, prompt='Build a calculator with tests'):
    run = run or make_run(store, mode='agent', prompt=prompt)
    await process_one(store, 'w1', httpx.MockTransport(handler), sandbox)
    return run, store.get_run(run['id'], store.user_id, True)


def lead(body):
    return body['messages'][0]['content'].startswith("You are L.E.O.'s lead agent")


async def test_agent_builds_delegates_in_parallel_and_verifies(store):
    add_provider(store)
    sandbox = FakeSandbox(lambda c: (0, '3 passed') if 'pytest' in c else (0, ''))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body['tools']
        done = results(body)
        if not lead(body):
            # A sub-agent: write its file, then report.
            task = body['messages'][1]['content']
            if not done:
                target = 'test_calc.py' if 'tests' in task else 'README.md'
                return tool_reply([('w-' + target, 'write_file', {'path': target, 'content': 'x'})])
            return tool_reply(text=f'Done: {task}')
        if not done:
            return tool_reply([('t1', 'todo_write', {'todos': [{'content': 'Write calc', 'status': 'in_progress'}]}),
                               ('w1', 'write_file', {'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'})])
        if len(done) == 2:
            return tool_reply([('d1', 'delegate', {'agent': 'coding', 'task': 'Write tests for calc.py'}),
                               ('d2', 'delegate', {'agent': 'general', 'task': 'Write a README'})])
        if len(done) == 4:
            return tool_reply([('b1', 'bash', {'command': 'python -m pytest -q'})])
        return tool_reply(text='Built calc.py with tests; 3 passed.')

    run, result = await run_agent(store, sandbox, handler)
    assert result['status'] == 'completed', result['error']
    assert result['output'] == 'Built calc.py with tests; 3 passed.'
    assert set(sandbox.files) == {'/workspace/calc.py', '/workspace/test_calc.py', '/workspace/README.md'}
    assert sandbox.commands == ['python -m pytest -q']
    kinds = [e['kind'] for e in store.get_events(run['id'])]
    assert kinds.count('subagent') == 4 and 'todos' in kinds and kinds.count('agent_step') >= 4
    reports = [m['content'] for m in results(seen[-1])]
    assert any('Report from the coding sub-agent' in r for r in reports)
    roles = {r['role'] for r in store.usage_report(user_id=store.user_id)['recent_calls']}
    assert roles == {'agent', 'subagent'}
    assert store.get_workspace(run['conversation_id'])['source'] == 'empty'


async def test_tool_errors_go_back_to_the_model(store):
    add_provider(store)
    sandbox = FakeSandbox()

    def handler(request):
        done = results(json.loads(request.content))
        if not done:
            return tool_reply([('r', 'read_file', {'path': 'missing.py'}), ('x', 'rm_rf', {}), ('e', 'read_file', {'path': '../../etc/passwd'})])
        assert done[0]['content'].startswith('Error: No such file') and 'no tool called rm_rf' in done[1]['content']
        assert 'inside /workspace' in done[2]['content']
        return tool_reply(text='Reported the problems.')

    _, result = await run_agent(store, sandbox, handler)
    assert result['status'] == 'completed', result['error']


async def test_edit_file_requires_a_unique_match(store):
    add_provider(store)
    sandbox = FakeSandbox()
    sandbox.files['/workspace/a.py'] = b'x = 1\nx = 1\n'

    def handler(request):
        done = results(json.loads(request.content))
        if not done:
            return tool_reply([('e1', 'edit_file', {'path': 'a.py', 'old_string': 'x = 1', 'new_string': 'x = 2'})])
        if len(done) == 1:
            assert 'appears 2 times' in done[0]['content']
            return tool_reply([('e2', 'edit_file', {'path': 'a.py', 'old_string': 'x = 1', 'new_string': 'x = 2', 'replace_all': True})])
        return tool_reply(text='ok')

    await run_agent(store, sandbox, handler)
    assert sandbox.files['/workspace/a.py'] == b'x = 2\nx = 2\n'


async def test_git_push_waits_for_approval_then_resumes(store, client):
    add_provider(store)
    sandbox = FakeSandbox()
    workspace = store.create_workspace({'name': 'repo', 'source': 'git', 'repo_url': 'https://github.com/me/repo', 'branch': 'feature',
                                        'token': 'ghp_secret', 'status': 'ready'}, store.user_id)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert any(t['function']['name'] == 'git_push' for t in body['tools'])
        done = results(body)
        if not done:
            return tool_reply([('p1', 'git_push', {'message': 'Add feature'})])
        return tool_reply(text=f"Push result: {done[0]['content']}")

    run = make_run(store, mode='agent', prompt='Push it', conversation_id=workspace['id'])
    _, result = await run_agent(store, sandbox, handler, run)
    assert result['status'] == 'waiting' and not sandbox.pushes
    approval = [e for e in store.get_events(run['id']) if e['kind'] == 'approval'][0]['data']
    assert approval['kind'] == 'git_push' and approval['detail']['branch'] == 'feature'
    assert client.post(f"/api/runs/{run['id']}/approve", json={'approval_id': 'wrong', 'approved': True}).status_code == 409
    assert client.post(f"/api/runs/{run['id']}/approve", json={'approval_id': approval['id'], 'approved': True}).status_code == 200
    assert client.post(f"/api/runs/{run['id']}/approve", json={'approval_id': approval['id'], 'approved': True}).status_code == 409
    await process_one(store, 'w2', httpx.MockTransport(handler), sandbox)
    result = store.get_run(run['id'], store.user_id)
    assert result['status'] == 'completed' and 'Pushed commit abc1234 to feature' in result['output']
    assert sandbox.pushes == [('https://github.com/me/repo', 'feature', 'ghp_secret', 'Add feature')]
    assert len(calls) == 2  # The first step was not repeated after resuming.
    assert 'ghp_secret' not in json.dumps(calls)


async def test_declined_push_is_reported_to_the_agent(store, client):
    add_provider(store)
    sandbox = FakeSandbox()
    workspace = store.create_workspace({'source': 'git', 'repo_url': 'https://github.com/me/repo', 'status': 'ready'}, store.user_id)

    def handler(request):
        done = results(json.loads(request.content))
        return tool_reply([('p1', 'git_push', {'message': 'x'})]) if not done else tool_reply(text=done[0]['content'])

    run = make_run(store, mode='agent', prompt='Push', conversation_id=workspace['id'])
    await run_agent(store, sandbox, handler, run)
    client.post(f"/api/runs/{run['id']}/approve", json={'approval_id': 'push:p1', 'approved': False})
    await process_one(store, 'w2', httpx.MockTransport(handler), sandbox)
    assert 'declined' in store.get_run(run['id'], store.user_id)['output'] and not sandbox.pushes


async def test_budget_limit_asks_to_extend(store, client):
    add_provider(store)
    store.set_preferences({**store.preferences(), 'agent_max_calls': 10})
    sandbox = FakeSandbox()
    steps = []

    def handler(request):
        steps.append(1)
        if len(steps) < 12:
            return tool_reply([(f'b{len(steps)}', 'bash', {'command': f'echo {len(steps)}'})])
        return tool_reply(text='Finished after many steps.')

    run, result = await run_agent(store, sandbox, handler)
    assert result['status'] == 'waiting' and len(steps) == 10
    assert result['state']['pending_approval']['kind'] == 'budget'
    client.post(f"/api/runs/{run['id']}/approve", json={'approval_id': 'budget:0', 'approved': True})
    await process_one(store, 'w2', httpx.MockTransport(handler), sandbox)
    assert store.get_run(run['id'], store.user_id)['output'] == 'Finished after many steps.'


async def test_steering_message_reaches_the_agent(store, client):
    add_provider(store)
    sandbox = FakeSandbox()
    run = make_run(store, mode='agent', prompt='Write a script')
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            # The user writes while the first step is running.
            assert client.post(f"/api/runs/{run['id']}/steer", json={'message': 'Use Python, not Bash'}).status_code == 200
            return tool_reply([('w', 'write_file', {'path': 's.sh', 'content': 'echo'})])
        return tool_reply(text='Switched to Python.')

    await run_agent(store, sandbox, handler, run)
    assert 'Use Python, not Bash' in seen[1]['messages'][-1]['content']
    assert client.post(f"/api/runs/{run['id']}/steer", json={'message': 'late'}).status_code == 409


async def test_worker_crash_resumes_from_the_saved_transcript(store):
    add_provider(store)
    from sqlalchemy import update
    from backend.store import runs
    sandbox = FakeSandbox()
    seen = []
    run = make_run(store, mode='agent')

    def handler(request):
        body = json.loads(request.content)
        done = results(body)
        seen.append(len(done))
        if len(seen) == 2:
            # The worker dies during its second step: its lease expires and another worker may claim the run.
            with store.engine.begin() as db:
                db.execute(update(runs).where(runs.c.id == run['id']).values(lease_owner='dead-worker', lease_until=0))
        if not done:
            return tool_reply([('w', 'write_file', {'path': 'a.py', 'content': '1'})])
        return tool_reply(text='Resumed and finished.')

    await process_one(store, 'w1', httpx.MockTransport(handler), sandbox)
    first = store.get_run(run['id'], store.user_id, True)
    assert first['status'] == 'running'
    await process_one(store, 'w2', httpx.MockTransport(handler), sandbox)
    result = store.get_run(run['id'], store.user_id)
    assert first['state']['agent']['transcripts']['main'][-1]['role'] == 'tool'
    assert result['status'] == 'completed' and result['output'] == 'Resumed and finished.'
    assert seen.count(0) == 1  # The write step ran once.


async def test_models_without_tool_support_are_skipped_and_learned(store):
    plain = store.save_connection(Connection(name='No tools', base_url='http://plain.test/v1', model='plain', free=True, priority=1).model_dump(), user_id=store.user_id)
    add_provider(store, priority=50)
    known_bad = store.save_connection(Connection(name='Known', base_url='http://bad.test/v1', model='bad', free=True, tools=False, priority=0).model_dump(), user_id=store.user_id)
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == 'plain.test':
            return httpx.Response(400, json={'error': {'message': 'This model does not support tools'}})
        return tool_reply(text='Done')

    _, result = await run_agent(store, FakeSandbox(), handler)
    assert result['status'] == 'completed'
    assert 'bad.test' not in hosts and hosts[:2] == ['plain.test', 'local.test']
    assert store.get_connection(plain['id'])['tools'] is False
    assert store.get_connection(known_bad['id'])['tools'] is False


async def test_long_transcripts_are_compacted(store):
    add_provider(store, context_window=200000)
    store.set_preferences({**store.preferences(), 'context_tokens': 9000, 'max_output_tokens': 1000})
    sandbox = FakeSandbox(lambda c: (0, 'x' * 6000))
    steps = []

    def handler(request):
        body = json.loads(request.content)
        if 'work log' in body['messages'][0]['content']:
            return tool_reply(text='- ran 4 commands; all printed x')
        steps.append(body)
        if len(steps) < 6:
            return tool_reply([(f'b{len(steps)}', 'bash', {'command': 'cat big'})])
        return tool_reply(text='Done.')

    run, result = await run_agent(store, sandbox, handler)
    assert result['status'] == 'completed', result['error']
    assert any(e['kind'] == 'compacted' for e in store.get_events(run['id']))
    assert any('Summary of your earlier steps' in m['content'] for b in steps for m in b['messages'] if m['role'] == 'user')


async def test_agent_run_without_sandbox_fails_clearly(store):
    add_provider(store)
    _, result = await run_agent(store, None, lambda r: tool_reply(text='x'))
    assert result['status'] == 'failed' and 'sandbox service' in result['error']


# -- API ----------------------------------------------------------------------

@pytest.fixture
def agent_client(store):
    sandbox = FakeSandbox()
    store.config.server_agent = True
    with TestClient(create_app(store.config, store, sandbox)) as value:
        value.cookies.set('leo_session', store.user_session_token)
        value.sandbox = sandbox
        yield value


def test_workspace_lifecycle(agent_client, store):
    add_provider(store)
    created = agent_client.post('/api/workspaces', json={'source': 'git', 'repo_url': 'https://github.com/me/app.git', 'token': 'tok'}).json()
    assert created['status'] == 'ready' and created['name'] == 'app' and created['has_token'] and 'token' not in created
    wid = created['id']
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('proj/main.py', 'print("hi")')
    assert agent_client.put(f'/api/workspaces/{wid}/upload', content=archive.getvalue()).status_code == 200
    files = agent_client.get(f'/api/workspaces/{wid}/files').json()['entries']
    assert {f['path'] for f in files} == {'README.md', 'main.py'}
    assert agent_client.get(f'/api/workspaces/{wid}/file', params={'path': 'main.py'}).json()['content'] == 'print("hi")'
    download = agent_client.get(f'/api/workspaces/{wid}/download')
    assert download.headers['content-type'] == 'application/zip' and 'app.zip' in download.headers['content-disposition']
    assert set(zipfile.ZipFile(io.BytesIO(download.content)).namelist()) == {'README.md', 'main.py'}
    chats = agent_client.get('/api/conversations').json()
    assert chats[0]['workspace'] is True
    assert agent_client.get(f'/api/conversations/{wid}').json()['workspace']['repo_url'] == 'https://github.com/me/app.git'
    assert agent_client.delete(f'/api/conversations/{wid}').status_code == 200
    assert agent_client.sandbox.deleted == [wid]
    assert agent_client.get(f'/api/workspaces/{wid}').status_code == 404


def test_workspace_input_is_validated(agent_client):
    for body in [{'source': 'git', 'repo_url': 'http://github.com/a/b'}, {'source': 'git', 'repo_url': 'https://u:p@github.com/a/b'},
                 {'source': 'empty', 'repo_url': 'https://github.com/a/b'}, {'source': 'git', 'repo_url': 'https://github.com/a/b', 'branch': '../x'}]:
        assert agent_client.post('/api/workspaces', json=body).status_code == 422


def test_other_users_cannot_reach_a_workspace(agent_client, store):
    from backend.auth import Auth
    workspace = agent_client.post('/api/workspaces', json={}).json()
    other = store.create_user('someone', Auth.hash_password('password123'))
    token = Auth(store, store.config).create_session(other['id'])
    agent_client.cookies.set('leo_session', token)
    assert agent_client.get(f"/api/workspaces/{workspace['id']}/files").status_code == 404


def test_agent_mode_needs_the_sandbox_service(client, store):
    add_provider(store)
    store.config.server_agent = True
    response = client.post('/api/runs', json={'prompt': 'Build it', 'mode': 'agent', 'request_id': str(uuid.uuid4())})
    assert response.status_code == 400 and 'sandbox' in response.json()['detail']
    assert client.get('/api/health').json()['agent'] is False


def test_server_agent_mode_is_off_unless_enabled(store):
    """Agent work belongs to L.E.O.C.O.D.E; the web chat refuses it unless LEO_SERVER_AGENT turns it back on."""
    add_provider(store)
    with TestClient(create_app(store.config, store, FakeSandbox())) as client:
        client.cookies.set('leo_session', store.user_session_token)
        assert client.get('/api/health').json()['agent'] is False
        refused = client.post('/api/runs', json={'prompt': 'fix the bug', 'mode': 'agent', 'request_id': 'agent-off-1'})
        assert refused.status_code == 403 and 'L.E.O.C.O.D.E' in refused.json()['detail']
        assert client.post('/api/workspaces', json={'source': 'empty'}).status_code == 403
        assert client.post('/api/runs', json={'prompt': 'hello', 'mode': 'quick', 'request_id': 'agent-off-2'}).status_code == 202


def test_old_agent_chats_stay_readable_but_take_no_new_messages(store):
    add_provider(store)
    store.config.server_agent = True
    with TestClient(create_app(store.config, store, FakeSandbox())) as client:
        client.cookies.set('leo_session', store.user_session_token)
        run = client.post('/api/runs', json={'prompt': 'build it', 'mode': 'agent', 'request_id': 'agent-old-1'}).json()
    store.config.server_agent = False
    with TestClient(create_app(store.config, store, FakeSandbox())) as client:
        client.cookies.set('leo_session', store.user_session_token)
        assert client.get(f"/api/conversations/{run['conversation_id']}").status_code == 200
        later = client.post('/api/runs', json={'prompt': 'and now?', 'conversation_id': run['conversation_id'], 'request_id': 'agent-old-2'})
        assert later.status_code == 409 and 'L.E.O.C.O.D.E' in later.json()['detail']
