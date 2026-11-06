"""The tree of agents through real sessions, a real folder, and fake models: one per strength, scripted per task."""
import asyncio
import hashlib
import itertools
import json

import httpx
import pytest

from backend.schemas import Connection
from leo_cli.account import Account
from leo_cli.sessions import Sessions
from test_sessions import project, reply  # noqa: F401 (fixtures)

ALL = ('strong-m', 'standard-m', 'light-m')


class Child:
    """Stands for the agent ID of a child, known once the delegate call for its task exists."""

    def __init__(self, task):
        self.task = task


class Team:
    """Answers each request by its task: a script is a list of turns. A string is a final message; a list of
    (tool, arguments) is a turn of tool calls. When a script runs out, the agent reports `finished`."""

    def __init__(self, scripts, fail_models=(), fail_tasks=()):
        self.scripts = {key: list(steps) for key, steps in scripts.items()}
        self.fail_models, self.fail_tasks, self.calls, self.ids = fail_models, fail_tasks, [], itertools.count(1)
        self.children = {}
        self.transport = httpx.MockTransport(self)

    def resolve(self, value):
        return 'sub-' + hashlib.sha1(self.children[value.task].encode()).hexdigest()[:8] if isinstance(value, Child) else value

    def __call__(self, request):
        body = json.loads(request.content)
        task, model = body['messages'][1]['content'], body['model']
        self.calls.append({'model': model, 'task': task, 'tools': [t['function']['name'] for t in body.get('tools') or []], 'body': body})
        if model in self.fail_models:
            return httpx.Response(500, text='this model is broken')
        if any(key in task for key in self.fail_tasks):
            return httpx.Response(400, text='no access')
        key = next((k for k in self.scripts if k in task), None)
        step = self.scripts[key].pop(0) if key and self.scripts[key] else 'finished'
        if isinstance(step, str):
            return reply(text=step)
        calls = []
        for name, arguments in step:
            ident = f'call-{next(self.ids)}'
            if name == 'delegate':
                self.children[arguments['task']] = ident
            calls.append((ident, name, {k: self.resolve(v) for k, v in arguments.items()}))
        return reply(calls)

    def models_of(self, task):
        return [c['model'] for c in self.calls if task in c['task']]

    def tools_of(self, task):
        return next(c['tools'] for c in self.calls if task in c['task'])


def delegate(task, kind='worker', agent='coding'):
    return ('delegate', {'agent': agent, 'task': task, 'kind': kind})


def write(path):
    return ('write_file', {'path': path, 'content': 'hi\n'})


@pytest.fixture
def members(tmp_path, monkeypatch):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')

    def make(models=ALL, **preferences):
        account = Account(tmp_path / 'home')
        store = account.store()
        for model in models:
            store.save_connection(Connection(name=model, base_url=f'http://{model}.test/v1', model=model, free=True, tools=True,
                                             tier=model.split('-')[0]).model_dump(), user_id=account.user_id)
        store.set_preferences({**store.preferences(), **preferences})
        return account
    return make


async def run(account, project, team, prompt='ROOT task', autorun=True):
    sessions = Sessions(account, project, autorun=autorun, transport=team.transport)
    session = await sessions.open()
    await sessions.send(session, prompt)
    await asyncio.wait_for(session.task, 30)
    return sessions, session


def nodes(session):
    return session.run['state']['agent']['tree']['nodes']


def by_task(session, task):
    return next(n for n in nodes(session).values() if task in (n['task'] or ''))


async def test_a_question_gets_an_answer_and_no_tree(members, project):
    team = Team({'ROOT': ['It does nothing.']})
    _, session = await run(members(), project, team)
    assert session.run['output'] == 'It does nothing.'
    assert list(nodes(session)) == ['main'] and nodes(session)['main']['status'] == 'done' and team.models_of('ROOT') == ['strong-m']


async def test_do_it_myself_gives_the_root_worker_tools(members, project):
    team = Team({'ROOT': [[('do_it_myself', {})], [write('hello.txt')], 'Done.']})
    _, session = await run(members(), project, team)
    first, second = [c['tools'] for c in team.calls][:2]
    assert 'write_file' not in first and 'bash' not in first and 'do_it_myself' in first
    assert 'write_file' in second and 'bash' in second and 'delegate' not in second and 'do_it_myself' not in second
    assert (project / 'hello.txt').exists() and set(team.models_of('ROOT')) == {'strong-m'} and session.status == 'completed'


async def test_two_levels_when_two_strengths_exist(members, project):
    team = Team({'ROOT': [[delegate('child work')], 'Done.'], 'child work': [[write('a.txt')], 'wrote it']})
    _, session = await run(members(('strong-m', 'light-m')), project, team)
    assert set(team.models_of('child work')) == {'light-m'} and team.models_of('ROOT')[0] == 'strong-m'
    assert by_task(session, 'child work')['kind'] == 'worker' and by_task(session, 'child work')['level'] == 1


async def test_one_strength_means_the_root_works_alone(members, project):
    team = Team({'ROOT': [[write('a.txt')], 'Done.']})
    await run(members(('standard-m',)), project, team)
    tools = team.tools_of('ROOT')
    assert 'write_file' in tools and 'todo_write' in tools and 'delegate' not in tools and 'do_it_myself' not in tools
    assert (project / 'a.txt').exists()


async def test_a_deep_branch_beside_a_shallow_one(members, project):
    team = Team({'ROOT': [[delegate('big part', 'manager'), delegate('small part')], 'Done.'],
                 'big part': [[delegate('leaf part')], 'report of big'], 'leaf part': [[write('leaf.txt')], 'leaf done'],
                 'small part': [[write('small.txt')], 'small done']})
    _, session = await run(members(), project, team)
    assert set(team.models_of('big part')) == {'standard-m'} and set(team.models_of('small part')) == {'standard-m'}
    assert set(team.models_of('leaf part')) == {'light-m'} and (project / 'leaf.txt').exists() and (project / 'small.txt').exists()
    big, small, leaf = by_task(session, 'big part'), by_task(session, 'small part'), by_task(session, 'leaf part')
    assert (big['kind'], small['kind'], leaf['kind']) == ('manager', 'worker', 'worker') and leaf['parent'] == big['id'] and leaf['level'] == 2


async def test_parents_cannot_work_and_workers_cannot_delegate(members, project):
    team = Team({'ROOT': [[write('nope.txt')], [delegate('big part', 'manager')], 'Done.'], 'big part': [[delegate('leaf part')], 'report'],
                 'leaf part': ['leaf done']})
    await run(members(), project, team)
    for parent in ('ROOT', 'big part'):
        assert not {'write_file', 'edit_file', 'bash'} & set(team.tools_of(parent)) and 'delegate' in team.tools_of(parent)
    assert 'delegate' not in team.tools_of('leaf part') and 'write_file' in team.tools_of('leaf part')
    assert not (project / 'nope.txt').exists()
    second = next(c for c in team.calls if 'ROOT' in c['task'] and c['body']['messages'][-1]['role'] == 'tool')
    assert 'there is no tool called write_file' in second['body']['messages'][-1]['content']


def kinds_offered(call):
    delegating = next(t for t in call['body']['tools'] if t['function']['name'] == 'delegate')
    return delegating['function']['parameters']['properties']['kind']['enum']


async def test_manager_kind_is_only_offered_on_three_levels(members, project, tmp_path):
    team = Team({'ROOT': ['Nothing to do.']})
    await run(members(), project, team)
    assert kinds_offered(team.calls[0]) == ['worker', 'manager']


async def test_two_levels_offer_only_workers(members, project):
    team = Team({'ROOT': ['Nothing to do.']})
    await run(members(('strong-m', 'standard-m')), project, team)
    assert kinds_offered(team.calls[0]) == ['worker']


async def test_read_only_mcp_tools_reach_parents(members, project, tmp_path):
    from test_mcp_agent import notes_server
    notes_server(project, tmp_path)
    team = Team({'ROOT': [[delegate('child work')], 'Done.'], 'child work': ['done']})
    await run(members(('strong-m', 'light-m')), project, team)
    assert 'notes__lookup' in team.tools_of('ROOT') and 'notes__save_note' not in team.tools_of('ROOT')
    assert {'notes__lookup', 'notes__save_note'} <= set(team.tools_of('child work'))


async def test_the_thirteenth_agent_is_refused(members, project):
    tasks = [f'part {chr(97 + n)}' for n in range(13)]
    team = Team({'ROOT': [[delegate(task) for task in tasks], 'Done.'], **{task: ['ok'] for task in tasks}})
    _, session = await run(members(), project, team)
    assert len(nodes(session)) == 13, 'the root and 12 children'
    results = [m for m in next(c for c in reversed(team.calls) if 'ROOT' in c['task'])['body']['messages'] if m['role'] == 'tool']
    refused = [m['content'] for m in results if m['content'].startswith('Error')]
    assert len(refused) == 1 and '12' in refused[0]


async def test_a_level_of_managers_does_not_deadlock(members, project):
    scripts = {'ROOT': [[delegate(f'boss {n}', 'manager') for n in 'abc'], 'Done.']}
    for n in 'abc':
        scripts[f'boss {n}'] = [[delegate(f'leaf {n}1'), delegate(f'leaf {n}2')], 'report']
    team = Team(scripts)
    _, session = await run(members(agent_parallel=1), project, team)
    assert session.status == 'completed' and len(nodes(session)) == 10


async def test_bad_delegate_arguments_get_a_readable_error(members, project):
    team = Team({'ROOT': [[delegate('odd kind', 'boss'), delegate('   '), delegate('odd agent', agent='nobody')], 'Done.']})
    _, session = await run(members(), project, team)
    results = [m['content'] for m in team.calls[-1]['body']['messages'] if m['role'] == 'tool']
    assert len(results) == 3 and all(r.startswith('Error') for r in results), results
    assert session.status == 'completed' and list(nodes(session)) == ['main']


async def test_a_broken_model_falls_through_to_another(members, project):
    team = Team({'ROOT': [[delegate('child work')], 'Done.'], 'child work': ['done']}, fail_models={'standard-m'})
    _, session = await run(members(), project, team)
    assert team.models_of('child work')[0] == 'standard-m' and team.models_of('child work')[-1] == 'strong-m'
    assert session.status == 'completed'


async def test_a_child_with_no_model_left_fails_alone(members, project):
    team = Team({'ROOT': [[delegate('doomed part'), delegate('fine part')], 'Done.'], 'fine part': [[write('fine.txt')], 'ok']},
                fail_tasks={'doomed part'})
    _, session = await run(members(), project, team)
    assert by_task(session, 'doomed part')['status'] == 'failed' and (project / 'fine.txt').exists() and session.status == 'completed'
    results = [m['content'] for m in team.calls[-1]['body']['messages'] if m['role'] == 'tool']
    assert any(r.startswith('Error: this subtask failed') for r in results)


async def test_the_server_agent_stays_flat(members, project):
    team = Team({'ROOT': ['Nothing to do.']})
    sessions = Sessions(members(), project, autorun=True, transport=team.transport)
    session = await sessions.open()
    session.workspace.tree = False
    await sessions.send(session, 'ROOT task')
    await asyncio.wait_for(session.task, 30)
    assert 'tree' not in session.run['state']['agent']
    tool = next(t for t in team.calls[0]['body']['tools'] if t['function']['name'] == 'delegate')
    assert 'difficulty' in tool['function']['parameters']['properties'] and 'kind' not in tool['function']['parameters']['properties']
