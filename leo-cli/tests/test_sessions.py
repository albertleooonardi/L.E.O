"""Sessions end to end in-process: the shared agent engine, a fake tool-calling model, and a real project folder."""
import asyncio
import json
import subprocess

import httpx
import pytest

from backend.schemas import Connection
from leo_cli.account import Account
from leo_cli.sessions import Sessions


def reply(calls=(), text=''):
    """A streamed Chat Completions answer, with tool calls when given."""
    events = [{'choices': [{'delta': {'content': text}, 'finish_reason': None}]}] if text else []
    if calls:
        events.append({'choices': [{'delta': {'tool_calls': [{'index': n, 'id': c[0], 'function': {'name': c[1], 'arguments': json.dumps(c[2])}}
                                                              for n, c in enumerate(calls)]}, 'finish_reason': None}]})
    events += [{'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if calls else 'stop'}]},
               {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}]
    return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n',
                          headers={'content-type': 'text/event-stream'})


def results(body):
    return [m for m in body['messages'] if m['role'] == 'tool']


class Model:
    """Plays the lead agent: `script(body, done_results)` answers each turn."""

    def __init__(self, script):
        self.script, self.requests = script, []
        self.transport = httpx.MockTransport(self)

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        return self.script(body, results(body))


def writes_hello(body, done):
    if not done:
        return reply([('w1', 'write_file', {'path': 'hello.txt', 'content': 'hi from leo\n'})])
    return reply(text=f"Done: {done[-1]['content']}")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return root


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    value = Account(tmp_path / 'home')
    value.store().save_connection(Connection(name='Local test', base_url='http://local.test/v1', model='test-model', free=True, local=True,
                                             tools=True).model_dump(), user_id=value.user_id)
    return value


async def settle(session):
    await session.task


async def test_an_edit_waits_for_approval_then_runs(account, project):
    model = Model(writes_hello)
    sessions = Sessions(account, project, transport=model.transport)
    session = await sessions.open()
    await sessions.send(session, 'Create hello.txt')
    await settle(session)
    assert session.status == 'waiting' and session.approval['kind'] == 'edit' and session.approval['detail']['path'] == 'hello.txt'
    assert not (project / 'hello.txt').exists()
    assert sessions.answer(session, 'once')
    await settle(session)
    assert session.status == 'completed' and (project / 'hello.txt').read_text() == 'hi from leo\n'
    assert 'Wrote 12 bytes to ' + str((project / 'hello.txt').resolve()) in session.run['output']
    lead = model.requests[0]['messages'][0]['content']
    assert str(project.resolve()) in lead and "user's own computer" in lead and 'Linux sandbox' not in lead


async def test_autorun_and_a_denied_edit(account, project):
    model = Model(writes_hello)
    sessions = Sessions(account, project, autorun=True, transport=model.transport)
    session = await sessions.open()
    await sessions.send(session, 'Create hello.txt')
    await settle(session)
    assert session.status == 'completed' and (project / 'hello.txt').exists()
    careful = Sessions(account, project, autorun=False, transport=Model(writes_hello).transport)
    other = await careful.open()
    await careful.send(other, 'Create hello.txt again')
    await settle(other)
    careful.answer(other, 'deny')
    await settle(other)
    assert other.status == 'completed' and 'declined' in other.run['output']


async def test_a_project_answer_is_remembered_for_later_commands(account, project):
    def runs_tests(body, done):
        return reply([('b1', 'bash', {'command': 'echo testing'})]) if not done else reply(text=done[-1]['content'])
    sessions = Sessions(account, project, transport=Model(runs_tests).transport)
    session = await sessions.open()
    await sessions.send(session, 'Run the tests')
    await settle(session)
    sessions.answer(session, 'project', prefix='echo')
    await settle(session)
    assert 'testing' in session.run['output']
    await sessions.send(session, 'Run them again')
    await settle(session)
    assert session.status == 'completed', 'the saved project rule lets it run without asking'
    assert json.loads((project / '.leo-cli' / 'settings.json').read_text())['allow']['commands'] == ['echo']


async def test_the_agent_uses_a_skill_and_a_command_expands(account, project):
    skill = project / '.leo-cli' / 'skills' / 'release'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: release\ndescription: Cut a release\n---\nBump the version in VERSION.\n')

    def uses_skill(body, done):
        assert any(t['function']['name'] == 'use_skill' for t in body['tools'])
        if not done:
            return reply([('s1', 'use_skill', {'skill': 'release'})])
        return reply(text='Loaded: ' + done[-1]['content'][:2000])
    model = Model(uses_skill)
    sessions = Sessions(account, project, transport=model.transport)
    session = await sessions.open()
    await sessions.send(session, sessions.skills.expand('release', 'patch'))
    await settle(session)
    assert session.status == 'completed' and 'Bump the version' in session.run['output']
    assert '- release: Cut a release' in model.requests[0]['messages'][0]['content']


async def test_two_sessions_run_at_once_and_warn_about_the_same_file(account, project):
    gate = asyncio.Event()
    notes = []

    def slow_writer(body, done):
        if not done:
            return reply([('w1', 'write_file', {'path': 'shared.txt', 'content': body['messages'][-1]['content'][-1]})])
        return reply(text='ok')
    sessions = Sessions(account, project, autorun=True, transport=Model(slow_writer).transport, notify=lambda s, m: notes.append(m))
    first = await sessions.open()
    second = await sessions.open()
    await sessions.send(first, 'Write shared.txt with 1')
    await sessions.send(second, 'Write shared.txt with 2')
    await asyncio.gather(settle(first), settle(second))
    assert first.status == second.status == 'completed'
    assert notes == [f'Sessions {first.number} and {second.number} both changed shared.txt'] or \
           notes == [f'Sessions {second.number} and {first.number} both changed shared.txt']


async def test_a_worktree_session_works_on_its_own_branch(account, project):
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'start'], check=True)
    sessions = Sessions(account, project, autorun=True, transport=Model(writes_hello).transport)
    isolated = await sessions.open(worktree=True)
    assert isolated.branch.startswith('leo/session-1-') and '.leo-cli/worktrees/' in isolated.root
    await sessions.send(isolated, 'Create hello.txt')
    await settle(isolated)
    assert (project / '.leo-cli' / 'worktrees').exists() and not (project / 'hello.txt').exists()
    assert (await_path := __import__('pathlib').Path(isolated.root) / 'hello.txt').read_text() == 'hi from leo\n'
    assert sessions.history()[0]['id'] == isolated.id


async def test_a_worktree_outside_git_falls_back_to_the_folder(account, project):
    notes = []
    sessions = Sessions(account, project, notify=lambda s, m: notes.append(m))
    session = await sessions.open(worktree=True)
    assert session.root == str(project.resolve()) and session.branch is None and 'Could not create a worktree' in notes[0]


async def test_resume_continues_an_interrupted_run(account, project):
    model = Model(writes_hello)
    sessions = Sessions(account, project, autorun=True, transport=model.transport)
    session = await sessions.open()
    run = account.store().create_run(__import__('backend.schemas', fromlist=['RunInput']).RunInput(
        prompt='Create hello.txt', mode='agent', request_id='resume-test-1'), account.user_id)
    account.store().update_workspace(run['conversation_id'], project=str(project.resolve()), path=str(project.resolve()))
    again = Sessions(account, project, autorun=True, transport=model.transport)
    resumed = again.resume(run['conversation_id'])
    await settle(resumed)
    assert resumed.status == 'completed' and (project / 'hello.txt').exists()
