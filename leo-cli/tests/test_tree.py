"""The tree of agents through real sessions, a real folder, and fake models: one per strength, scripted per task."""
import asyncio
import hashlib
import itertools
import json
import re
import time

import httpx
import pytest

from backend.schemas import Connection, RunInput
from leo_cli.account import Account
from leo_cli.sessions import Sessions
from test_sessions import project, reply  # noqa: F401 (fixtures)

ALL = ('strong-m', 'standard-m', 'light-m')


class LastChild:
    """The ID of the child whose report came last, for providers whose call IDs say nothing."""


class Child:
    """Stands for the agent ID of a child, known once the delegate call for its task exists."""

    def __init__(self, task):
        self.task = task


class Team:
    """Answers each request by its task: a script is a list of turns. A string is a final message; a list of
    (tool, arguments) is a turn of tool calls. When a script runs out, the agent reports `finished`."""

    def __init__(self, scripts, fail_models=(), fail_tasks=(), hook=None, plain_ids=False, fail_status=500, limited=()):
        self.scripts, self.hook, self.limited = {key: list(steps) for key, steps in scripts.items()}, hook, set(limited)
        self.plain_ids, self.fail_status = plain_ids, fail_status  # plain_ids: every turn's first call is call_0, as without provider IDs.
        self.fail_models, self.fail_tasks, self.calls, self.ids = fail_models, fail_tasks, [], itertools.count(1)
        self.children = {}
        self.transport = httpx.MockTransport(self)

    def resolve(self, value, body=None):
        if isinstance(value, LastChild):
            return re.findall(r'Report from (sub-[0-9a-f]+)', ''.join(m.get('content') or '' for m in body['messages'] if m['role'] == 'tool'))[-1]
        return 'sub-' + hashlib.sha1(self.children[value.task].encode()).hexdigest()[:8] if isinstance(value, Child) else value

    def __call__(self, request):
        body = json.loads(request.content)
        task, model = body['messages'][1]['content'], body['model']
        self.calls.append({'model': model, 'task': task, 'tools': [t['function']['name'] for t in body.get('tools') or []], 'body': body})
        if self.hook:
            self.hook(self.calls[-1])
        if model in self.limited:
            return httpx.Response(429, json={'error': {'message': 'Rate limit exceeded: free-models-per-day.'}},
                                  headers={'x-ratelimit-limit': '50', 'x-ratelimit-reset': str(int((time.time() + 3600) * 1000))})
        if model in self.fail_models:
            return httpx.Response(self.fail_status, text='this model is broken')
        if any(key in task for key in self.fail_tasks):
            return httpx.Response(400, text='no access')
        key = next((k for k in self.scripts if k in task), None)
        step = self.scripts[key].pop(0) if key and self.scripts[key] else 'finished'
        if isinstance(step, str):
            return reply(text=step)
        calls = []
        for index, (name, arguments) in enumerate(step):
            ident = f'call_{index}' if self.plain_ids else f'call-{next(self.ids)}'
            if name == 'delegate' and isinstance(arguments['task'], str):
                self.children[arguments['task']] = ident
            calls.append((ident, name, {k: self.resolve(v, body) for k, v in arguments.items()}))
        return reply(calls)

    def models_of(self, task):
        return [c['model'] for c in self.calls if task in c['task']]

    def tools_of(self, task):
        return next(c['tools'] for c in self.calls if task in c['task'])


def delegate(task, kind='worker', agent='coding', size='normal'):
    return ('delegate', {'agent': agent, 'task': task, 'kind': kind, 'size': size})


def write(path):
    return ('write_file', {'path': path, 'content': 'hi\n'})


@pytest.fixture
def members(tmp_path, monkeypatch):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')

    def make(models=ALL, shared_host=False, **preferences):
        account = Account(tmp_path / 'home')
        store = account.store()
        for model in models:
            store.save_connection(Connection(name=model, base_url='http://one.test/v1' if shared_host else f'http://{model}.test/v1', model=model, free=True, tools=True,
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


async def test_an_easy_task_goes_to_the_cheapest_level(members, project):
    team = Team({'ROOT': [[delegate('easy part', size='small')], [accept('easy part')], 'Done.'], 'easy part': [[write('hello.txt')], 'wrote it']})
    _, session = await run(members(), project, team)
    root = team.tools_of('ROOT')
    assert 'write_file' not in root and 'bash' not in root and 'do_it_myself' not in root
    assert set(team.models_of('easy part')) == {'light-m'} and (project / 'hello.txt').exists() and session.status == 'completed'
    assert by_task(session, 'easy part')['parent'] == 'main' and by_task(session, 'easy part')['kind'] == 'worker'


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
    assert '… (evidence cut; read the files to check more)' in evidence and len(evidence) <= 4000 + 60


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


# -- found in the final review ------------------------------------------------------------------

async def test_a_provider_without_call_ids_keeps_every_subtask(members, project):
    team = Team({'ROOT': [[delegate('first part')], [('review_work', {'agent': LastChild(), 'verdict': 'accept'})], [delegate('second part')],
                          [('review_work', {'agent': LastChild(), 'verdict': 'accept'})], 'Done.'],
                 'first part': [[write('first.txt')], 'one'], 'second part': [[write('second.txt')], 'two']}, plain_ids=True)
    _, session = await run(members(), project, team)
    assert (project / 'first.txt').exists() and (project / 'second.txt').exists()
    assert by_task(session, 'first part')['status'] == by_task(session, 'second part')['status'] == 'accepted'


async def test_a_manager_with_the_same_call_id_as_its_parent_does_not_hang(members, project):
    team = Team({'ROOT': [[delegate('boss part', 'manager')], [('review_work', {'agent': LastChild(), 'verdict': 'accept'})], 'Done.'],
                 'boss part': [[delegate('leaf part')], [('review_work', {'agent': LastChild(), 'verdict': 'accept'})], 'report'],
                 'leaf part': [[write('leaf.txt')], 'ok']}, plain_ids=True)
    _, session = await run(members(), project, team)
    assert (project / 'leaf.txt').exists() and session.status == 'completed'


@pytest.mark.parametrize('tree', [True, False])
async def test_a_once_approval_does_not_cover_a_later_call_with_the_same_id(members, project, tree):
    team = Team({'ROOT': [[write('first.txt')], [write('second.txt')], 'Done.']}, plain_ids=True)
    sessions = Sessions(members(('strong-m',)) if tree else members(), project, transport=team.transport)
    session = await sessions.open()
    session.workspace.tree = tree
    await sessions.send(session, 'ROOT task')
    await asyncio.wait_for(session.task, 30)
    assert session.approval['detail']['path'] == 'first.txt'
    sessions.answer(session, 'once')
    await asyncio.wait_for(session.task, 30)
    assert session.status == 'waiting' and session.approval['detail']['path'] == 'second.txt', 'it must ask again'
    assert not (project / 'second.txt').exists()
    await sessions.close()


async def test_stopping_kills_the_commands_that_are_running(members, project):
    team = Team({'ROOT': [[delegate('part a'), delegate('part b')], 'unused'],
                 'part a': [[('bash', {'command': 'sleep 1; touch a-finished'})], 'a'], 'part b': [[('bash', {'command': 'sleep 1; touch b-finished'})], 'b']})
    sessions = Sessions(members(), project, autorun=True, transport=team.transport)
    session = await sessions.open()
    await sessions.send(session, 'ROOT task')
    await asyncio.sleep(0.4)
    sessions.stop(session)
    await asyncio.wait_for(session.task, 10)
    await asyncio.sleep(1.2)
    assert session.status == 'cancelled' and not (project / 'a-finished').exists() and not (project / 'b-finished').exists()


@pytest.mark.parametrize('task', [None, 42, ['a list']])
async def test_a_task_that_is_not_text_gets_a_readable_error(members, project, task):
    team = Team({'ROOT': [[('delegate', {'agent': 'coding', 'task': task})], 'Done.']})
    _, session = await run(members(), project, team)
    assert session.status == 'completed' and results(team)[0].startswith('Error') and list(nodes(session)) == ['main']


async def test_a_review_of_something_odd_gets_a_readable_error(members, project):
    team = Team({'ROOT': [[('review_work', {'agent': ['x'], 'verdict': 'accept'}), ('review_work', {'agent': None, 'verdict': ['accept']})], 'Done.']})
    _, session = await run(members(), project, team)
    assert session.status == 'completed' and all(r.startswith('Error') for r in results(team))


async def test_a_manager_stopped_by_its_step_limit_hides_nothing(members, project):
    busy = [[('list_files', {})] for _ in range(5)]
    team = Team({'ROOT': [[delegate('boss part', 'manager')], [accept('boss part')], 'All good.'],
                 'boss part': [[delegate('leaf part')], *busy], 'leaf part': [[write('leaf.txt')], 'ok']})
    _, session = await run(members(agent_max_steps=5), project, team)
    assert by_task(session, 'leaf part')['status'] == 'not_reviewed'
    assert 'leaf.txt' in results(team)[0], 'its work still shows in the evidence'
    assert session.run['output'].startswith('All good.\n\n**Not finished:**') and 'leaf part' in session.run['output']


async def test_the_evidence_cut_keeps_the_latest_commands(members, project):
    noisy = [[('bash', {'command': f'echo {n} {"x" * 280}'})] for n in range(30)]
    team = Team({'ROOT': [[delegate('child work')], [reject('child work', 'Run the tests')], [accept('child work')], 'Done.'],
                 'child work': [*noisy, 'Wrote it.', [write('a.txt'), ('bash', {'command': 'echo FINAL TEST RUN'})], 'Tests pass.']})
    await run(members(), project, team)
    assert 'FINAL TEST RUN' in results(team)[1] and 'evidence cut' in results(team)[1]


async def test_a_model_that_answers_with_an_error_is_tried_once_per_run(members, project):
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'],
                 'child work': [[('list_files', {})], [('list_files', {})], [('list_files', {})], 'ok']}, fail_models={'standard-m'}, fail_status=400)
    await run(members(), project, team)
    assert team.models_of('child work').count('standard-m') == 1 and team.models_of('child work')[-1] == 'strong-m'


async def test_an_unpriced_paid_model_is_skipped_for_another(members, project):
    account = members(('strong-m', 'light-m'), allow_paid=True)
    account.store().save_connection(Connection(name='unpriced', base_url='http://standard-unpriced.test/v1', model='standard-unpriced', free=False, tools=True,
                                               tier='standard').model_dump(), user_id=account.user_id)
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'], 'child work': ['ok']})
    _, session = await run(account, project, team)
    assert by_task(session, 'child work')['status'] == 'accepted' and 'standard-unpriced' not in team.models_of('child work')


async def test_free_first_keeps_paid_models_as_a_backup(members, project):
    account = members(('standard-m', 'light-m'), allow_paid=True, routing='free_first')
    account.store().save_connection(Connection(name='paid', base_url='http://strong-paid.test/v1', model='strong-paid', free=False, tools=True, tier='strong',
                                               input_price=1, output_price=1).model_dump(), user_id=account.user_id)
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'], 'child work': ['ok']})
    _, session = await run(account, project, team)
    assert 'strong-paid' not in [c['model'] for c in team.calls] and team.models_of('ROOT')[0] == 'standard-m' and team.models_of('child work')[0] == 'light-m'


# -- found by the QA on real models -------------------------------------------------------------

@pytest.fixture
def no_waiting(monkeypatch):
    from backend import tree
    monkeypatch.setattr(tree, 'ROOT_RETRY_SECONDS', 0)


def root_fails_after(team, marker, recover_after=None):
    """From the request whose last message holds `marker`, the root's calls fail; optionally they come back."""
    seen = []

    def hook(call):
        last = call['body']['messages'][-1].get('content') or ''
        if 'ROOT' in call['task'] and marker in last:
            seen.append(call)
            team.fail_tasks = {} if recover_after and len(seen) > recover_after else {'ROOT'}
    team.hook = hook


async def test_finished_work_survives_a_final_message_that_fails(members, project, no_waiting):
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'never written'], 'child work': [[write('a.txt')], 'wrote it']})
    root_fails_after(team, 'Accepted the work')
    _, session = await run(members(), project, team)
    output = session.run['output']
    assert session.status == 'completed' and (project / 'a.txt').exists()
    assert 'could not write' in output and 'child work' in output and 'a.txt' in output and 'Check' in output and len(output) < 1500


async def test_the_final_message_is_tried_again_first(members, project, no_waiting):
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'The real answer.'], 'child work': ['ok']})
    root_fails_after(team, 'Accepted the work', recover_after=3)
    _, session = await run(members(), project, team)
    assert session.status == 'completed' and session.run['output'] == 'The real answer.'


async def test_unreviewed_work_is_kept_but_the_run_says_it_failed(members, project, no_waiting):
    team = Team({'ROOT': [[delegate('child work')], 'never written'], 'child work': [[write('a.txt')], 'wrote it']})
    root_fails_after(team, 'Report from')
    _, session = await run(members(), project, team)
    assert session.status == 'failed' and 'child work' in session.run['error'] and 'a.txt' in session.run['error']
    assert len(session.run['error']) < 1500 and by_task(session, 'child work')['status'] == 'not_reviewed'


async def test_a_root_that_did_nothing_fails_with_a_short_error(members, project, no_waiting):
    team = Team({'ROOT': ['never']}, fail_tasks={'ROOT'})
    _, session = await run(members(), project, team)
    assert session.status == 'failed' and len(session.run['error']) < 700
    assert len(team.calls) <= 6, 'one more pass over the models, no more'


async def test_an_account_limit_stops_every_model_on_it_at_once(members, project):
    free = ('strong-a:free', 'strong-b:free', 'standard-a:free', 'light-a:free')
    team = Team({'ROOT': ['Answered by the other one.']}, limited=free)
    account = members((*free, 'standard-other'), shared_host=True)
    sessions, session = await run(account, project, team)
    assert session.run['output'] == 'Answered by the other one.'
    assert sum(c['model'] in free for c in team.calls) == 1, 'one refusal is enough to know'
    warnings = [e['data']['message'] for e in sessions.store.events_of(session.run_id, 'warning')]
    assert len(warnings) == 1 and '3 other models' in warnings[0] and '60 minutes' in warnings[0]
    before = len(team.calls)
    await sessions.send(session, 'ROOT again')
    await asyncio.wait_for(session.task, 30)
    assert all(c['model'] == 'standard-other' for c in team.calls[before:]), 'the next task does not try them either'


async def test_an_account_limit_comes_back_at_its_time(members, project):
    team = Team({'ROOT': ['ok']}, limited={'strong-a:free'})
    account = members(('strong-a:free', 'standard-other'), shared_host=True)
    sessions, session = await run(account, project, team)
    assert sessions.store.blocked_quotas()
    from sqlalchemy import update
    from backend.store import quotas
    with sessions.store.engine.begin() as db:
        db.execute(update(quotas).values(reset_at=0))
    assert not sessions.store.blocked_quotas()


async def test_all_models_at_their_limits_gives_one_short_error(members, project):
    free = ('strong-a:free', 'standard-a:free', 'light-a:free')
    team = Team({'ROOT': ['never']}, limited=free)
    _, session = await run(members(free, shared_host=True), project, team)
    assert session.status == 'failed' and 'limit' in session.run['error'] and len(session.run['error']) < 400


async def test_many_failing_models_give_a_short_error(members, project):
    many = tuple(f'standard-{n}' for n in range(12))
    team = Team({'ROOT': ['never']}, fail_models=set(many), fail_status=400)
    _, session = await run(members(many), project, team)
    assert session.status == 'failed' and len(session.run['error']) < 900 and 'and 9 more' in session.run['error']


def quota(account, used, limit=50):
    """This account has hit the free limit before, and has made `used` requests today."""
    from sqlalchemy import insert
    from backend.store import calls
    import uuid
    store = account.store()
    store.note_quota('one.test:free', limit, 0)
    run = store.create_run(RunInput(prompt='earlier', mode='quick', request_id=str(uuid.uuid4())), account.user_id)
    with store.engine.begin() as db:
        for _ in range(used):
            db.execute(insert(calls).values(id=str(uuid.uuid4()), run_id=run['id'], stage='generating', provider='x', model='strong-a:free', status='completed',
                                            input_tokens=1, output_tokens=1, reserved_tokens=0, reserved_cost=0, cost=0, estimated=False, created_at=time.time(), elapsed_ms=1))
    store.cancel(run['id'], account.user_id)


FREE = ('strong-a:free', 'standard-a:free', 'light-a:free')


async def test_with_little_quota_left_the_root_works_alone(members, project):
    account = members(FREE, shared_host=True)
    quota(account, used=40)
    team = Team({'ROOT': [[write('a.txt')], 'Done.']})
    sessions, session = await run(account, project, team)
    assert 'write_file' in team.tools_of('ROOT') and 'delegate' not in team.tools_of('ROOT') and (project / 'a.txt').exists()
    assert list(nodes(session)) == ['main']
    assert any('10' in e['data']['message'] and 'alone' in e['data']['message'] for e in sessions.store.events_of(session.run_id, 'warning'))


async def test_with_plenty_of_quota_the_tree_is_normal_and_the_root_knows(members, project):
    account = members(FREE, shared_host=True)
    quota(account, used=5)
    team = Team({'ROOT': ['Nothing to do.']})
    await run(account, project, team)
    assert 'delegate' in team.tools_of('ROOT')
    assert 'About 45' in team.calls[0]['body']['messages'][0]['content']


async def test_quota_running_out_mid_tree_stops_new_children(members, project):
    account = members(FREE, shared_host=True)
    quota(account, used=30)
    team = Team({'ROOT': [[delegate('first part')], [accept('first part')], [delegate('second part')], [write('b.txt')], 'Done.'],
                 'first part': [[('list_files', {})]] * 14 + ['done']})
    _, session = await run(account, project, team)
    refused = [r for r in results(team) if r.startswith('Error') and 'requests' in r]
    assert refused and 'second part' not in [n['task'] for n in nodes(session).values()] and (project / 'b.txt').exists()
