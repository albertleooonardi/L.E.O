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

    def __init__(self, scripts, fail_models=(), fail_tasks=(), hook=None):
        self.scripts, self.hook = {key: list(steps) for key, steps in scripts.items()}, hook
        self.fail_models, self.fail_tasks, self.calls, self.ids = fail_models, fail_tasks, [], itertools.count(1)
        self.children = {}
        self.transport = httpx.MockTransport(self)

    def resolve(self, value):
        return 'sub-' + hashlib.sha1(self.children[value.task].encode()).hexdigest()[:8] if isinstance(value, Child) else value

    def __call__(self, request):
        body = json.loads(request.content)
        task, model = body['messages'][1]['content'], body['model']
        self.calls.append({'model': model, 'task': task, 'tools': [t['function']['name'] for t in body.get('tools') or []], 'body': body})
        if self.hook:
            self.hook(self.calls[-1])
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


# -- review ------------------------------------------------------------------------------------

def accept(task):
    return ('review_work', {'agent': Child(task), 'verdict': 'accept'})


def reject(task, feedback='Not good enough'):
    return ('review_work', {'agent': Child(task), 'verdict': 'reject', 'feedback': feedback})


def results(team, task='ROOT'):
    """The tool results in the latest request of this task."""
    latest = next(c for c in reversed(team.calls) if task in c['task'])
    return [m['content'] for m in latest['body']['messages'] if m['role'] == 'tool']


def messages(team, task='ROOT'):
    return next(c for c in reversed(team.calls) if task in c['task'])['body']['messages']


async def test_evidence_comes_from_what_ran_not_the_report(members, project):
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'],
                 'child work': [[write('a.txt'), ('bash', {'command': 'echo boom; exit 1'})], 'All tests pass!']})
    await run(members(), project, team)
    delegated = results(team)[0]
    assert 'All tests pass!' in delegated and 'a.txt' in delegated and 'echo boom; exit 1' in delegated and 'exit code 1' in delegated and 'boom' in delegated


async def test_long_evidence_is_cut_and_says_so(members, project):
    many = [write(f'{"long-file-name-" * 4}{n}.txt') for n in range(120)]
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'], 'child work': [many, 'Wrote them.']})
    await run(members(), project, team)
    evidence = results(team)[0].split('Evidence the engine collected:\n')[1].split('\n\nReview it')[0]
    assert evidence.endswith('… (evidence cut; read the files to check more)') and len(evidence) <= 4000 + 60


async def test_two_workers_changing_a_file_warns_the_parent(members, project):
    team = Team({'ROOT': [[delegate('first part')], [accept('first part')], [delegate('second part')], [accept('second part')], 'Done.'],
                 'first part': [[write('same.txt')], 'ok'], 'second part': [[write('same.txt')], 'ok']})
    sessions, session = await run(members(), project, team)
    warnings = [e['data']['message'] for e in sessions.store.events_of(session.run_id, 'warning')]
    assert any('same.txt' in w for w in warnings)
    assert any('Changed by another agent too: same.txt' in r for r in results(team))


async def test_accept_marks_it_accepted(members, project):
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'], 'child work': ['ok']})
    _, session = await run(members(), project, team)
    assert by_task(session, 'child work')['status'] == 'accepted' and session.run['output'] == 'Done.'


async def test_a_reject_retries_with_the_feedback(members, project):
    team = Team({'ROOT': [[delegate('child work')], [reject('child work', 'Add the tests')], [accept('child work')], 'Done.'],
                 'child work': ['first try', 'second try']})
    _, session = await run(members(), project, team)
    assert team.models_of('child work') == ['standard-m', 'standard-m'] and messages(team, 'child work')[-1]['content'].endswith('Add the tests')
    assert 'second try' in results(team)[1] and by_task(session, 'child work')['attempt'] == 2


async def test_a_second_reject_moves_to_a_stronger_model(members, project):
    team = Team({'ROOT': [[delegate('child work')], [reject('child work')], [reject('child work')], [accept('child work')], 'Done.'],
                 'child work': ['one', 'two', 'three']})
    _, session = await run(members(), project, team)
    assert team.models_of('child work') == ['standard-m', 'standard-m', 'strong-m']
    assert [m['content'] for m in messages(team, 'child work') if m['role'] == 'assistant'] == ['one', 'two'], 'it keeps the conversation'
    assert by_task(session, 'child work')['attempt'] == 3


async def test_a_third_reject_fails_and_reaches_the_answer(members, project):
    team = Team({'ROOT': [[delegate('child work')], [reject('child work')], [reject('child work')], [reject('child work')], 'All done!'],
                 'child work': ['one', 'two', 'three']})
    _, session = await run(members(), project, team)
    assert by_task(session, 'child work')['status'] == 'failed' and 'no attempts' in results(team)[-1]
    assert len(team.models_of('child work')) == 3
    assert session.run['output'] == 'All done!\n\n**Not finished:**\n- child work (failed)'


async def test_a_reject_needs_feedback(members, project):
    team = Team({'ROOT': [[delegate('child work')], [('review_work', {'agent': Child('child work'), 'verdict': 'reject'})], [accept('child work')], 'Done.'],
                 'child work': ['ok']})
    _, session = await run(members(), project, team)
    assert results(team)[1].startswith('Error') and by_task(session, 'child work')['attempt'] == 1


async def test_reviewing_someone_elses_child_is_refused(members, project):
    team = Team({'ROOT': [[delegate('boss part', 'manager')], [accept('leaf part'), ('review_work', {'agent': 'sub-nobody', 'verdict': 'accept'}),
                                                                ('review_work', {'agent': Child('boss part'), 'verdict': 'maybe'})],
                          [accept('boss part')], 'Done.'],
                 'boss part': [[delegate('leaf part')], [accept('leaf part')], 'report'], 'leaf part': ['ok']})
    await run(members(), project, team)
    verdicts = results(team)[1:4]
    assert all(r.startswith('Error') for r in verdicts), verdicts


async def test_a_parent_cannot_finish_without_reviewing(members, project):
    team = Team({'ROOT': [[delegate('child work')], 'Done too soon.', [accept('child work')], 'Done.'], 'child work': ['ok']})
    _, session = await run(members(), project, team)
    reminder = [m['content'] for m in messages(team) if m['role'] == 'user'][-1]
    assert 'child work' in reminder and 'review_work' in reminder and session.run['output'] == 'Done.'


async def test_two_reminders_then_not_reviewed(members, project):
    team = Team({'ROOT': [[delegate('child work')], 'one', 'two', 'three'], 'child work': ['ok']})
    sessions, session = await run(members(), project, team)
    assert by_task(session, 'child work')['status'] == 'not_reviewed' and team.models_of('ROOT').count('strong-m') == 4
    assert session.run['output'] == 'three\n\n**Not finished:**\n- child work (not_reviewed)'
    assert any('child work' in e['data']['message'] for e in sessions.store.events_of(session.run_id, 'warning'))


async def test_a_manager_is_reviewed_by_the_root_with_its_childrens_evidence(members, project):
    team = Team({'ROOT': [[delegate('boss part', 'manager')], [accept('boss part')], 'Done.'],
                 'boss part': [[delegate('leaf part')], [accept('leaf part')], 'report of boss'], 'leaf part': [[write('leaf.txt')], 'ok']})
    _, session = await run(members(), project, team)
    assert 'report of boss' in results(team)[0] and 'leaf.txt' in results(team)[0]
    assert by_task(session, 'boss part')['status'] == 'accepted'


# -- pausing, restarting, stopping -------------------------------------------------------------

async def test_an_approval_deep_in_the_tree_pauses_and_resumes(members, project):
    team = Team({'ROOT': [[delegate('boss part', 'manager')], [accept('boss part')], 'Done.'],
                 'boss part': [[delegate('leaf part')], [accept('leaf part')], 'report'], 'leaf part': [[write('leaf.txt')], 'ok']})
    sessions, session = await run(members(), project, team, autorun=False)
    assert session.status == 'waiting' and session.approval['kind'] == 'edit' and not (project / 'leaf.txt').exists()
    calls = len(team.calls)
    sessions.answer(session, 'once')
    await asyncio.wait_for(session.task, 30)
    assert session.status == 'completed' and (project / 'leaf.txt').exists()
    assert team.models_of('leaf part').count('light-m') == 2, 'the step that asked is not repeated'
    assert len(team.calls) == calls + 5, 'leaf report, boss review, boss report, root review, root answer'


async def test_a_repeated_review_call_does_not_apply_twice(members, project):
    team = Team({'ROOT': [[delegate('child work')], [reject('child work', 'Write the file')], [accept('child work')], 'Done.'],
                 'child work': ['first try', [write('a.txt')], 'wrote it']})
    sessions, session = await run(members(), project, team, autorun=False)
    assert session.approval['kind'] == 'edit', 'the retry asks, in the middle of the review call'
    sessions.answer(session, 'once')
    await asyncio.wait_for(session.task, 30)
    assert by_task(session, 'child work')['attempt'] == 2 and session.status == 'completed'
    asked = [m for m in messages(team, 'child work') if m['role'] == 'user' and 'Write the file' in m['content']]
    assert len(asked) == 1


async def test_a_restart_repeats_no_finished_work(members, project):
    from sqlalchemy import update
    from backend.store import runs
    holder = {}

    def close_window(call):  # The window goes away as the root gets its child's report.
        if 'ROOT' in call['task'] and len([c for c in team.calls if 'ROOT' in c['task']]) == 2:
            holder['session'].task.cancel()

    team = Team({'ROOT': [[delegate('child work')], 'unused after the restart', [accept('child work')], 'Done.'],
                 'child work': [[write('a.txt')], 'ok']}, hook=close_window)
    account = members()
    sessions = Sessions(account, project, autorun=True, transport=team.transport)
    holder['session'] = session = await sessions.open()
    await sessions.send(session, 'ROOT task')
    await asyncio.gather(session.task, return_exceptions=True)
    await sessions.close()
    with sessions.store.engine.begin() as db:
        db.execute(update(runs).where(runs.c.id == session.run_id).values(lease_until=0))
    team.hook = None
    child_calls = len(team.models_of('child work'))
    again = Sessions(account, project, autorun=True, transport=team.transport)
    resumed = again.resume(session.id)
    await asyncio.wait_for(resumed.task, 30)
    assert resumed.status == 'completed' and len(team.models_of('child work')) == child_calls, 'the finished child ran nothing more'
    await again.close()


async def test_stopping_stops_every_node(members, project):
    holder = {}

    def stop_once(call):
        if 'part a' in call['task'] and 'stopped' not in holder:
            holder['stopped'] = holder['sessions'].stop(holder['session']) or True

    team = Team({'ROOT': [[delegate('part a'), delegate('part b')], 'unused'], 'part a': ['a', 'a'], 'part b': ['b', 'b']}, hook=stop_once)
    account = members()
    holder['sessions'] = sessions = Sessions(account, project, autorun=True, transport=team.transport)
    holder['session'] = session = await sessions.open()
    await sessions.send(session, 'ROOT task')
    await asyncio.wait_for(session.task, 30)
    assert session.status == 'cancelled' and session.task.done()
    team.hook = None
    team.scripts['ROOT'] = ['Second answer.']  # The new message's history holds the first task.
    await sessions.send(session, 'second task')
    await asyncio.wait_for(session.task, 30)
    assert session.status == 'completed' and session.run['output'] == 'Second answer.'
    await sessions.close()


async def test_the_scrollback_says_who_each_agent_is(members, project):
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'], 'child work': ['My report.']})
    sessions, session = await run(members(), project, team)
    events = [e['data'] for e in sessions.store.events_of(session.run_id, 'subagent')]
    assert [e['status'] for e in events] == ['started', 'completed'] and events[0]['task'] == 'child work' and events[1]['report'] == 'My report.'
