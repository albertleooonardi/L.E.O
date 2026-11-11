"""Audit routing invariants and optionally exercise 9Router and a real three-level agent tree.

Run from the repository root: python -m benchmarks.algorithm_qa [--live] [--output DIRECTORY].
Live mode uses existing free models, copies credentials into an isolated temporary store, and keeps local usage and
settings separate from the user's account. It consumes provider quota. The output report contains no credentials.
"""
import argparse
import asyncio
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from backend.context import parse_plan
from backend.orchestrator import Orchestrator
from backend.providers import ProviderError, ToolCalls, stream
from backend.router import rank
from backend.schemas import Connection, RunInput
from backend.worker import process_one
from leo_cli.account import Account
from leo_cli.sessions import Sessions


def audit(output):
    previous = os.environ.get('LEO_CLI_KEYRING')
    os.environ['LEO_CLI_KEYRING'] = 'file'
    account = Account(Path(tempfile.mkdtemp(prefix='audit-', dir=output)))
    store, uid = account.store(), account.user_id
    common = {'free': True, 'local': True, 'tools': True, 'base_url': 'http://qa.invalid/v1'}
    light = store.save_connection(Connection(name='Light specialist', model='light', tier='light', skills=['coding'], **common).model_dump(), user_id=uid)
    middle = store.save_connection(Connection(name='Standard generalist', model='standard', tier='standard', **common).model_dump(), user_id=uid)
    ordered, reason = rank([light, middle], 'coding', 'strong', {'routing': 'auto'})
    cases = [{'name': 'strongest_fallback_when_no_model_meets_difficulty', 'passed': ordered[0]['id'] == middle['id'],
              'expected': 'Standard generalist', 'actual': ordered[0]['name'], 'reason': reason}]
    writer = store.save_connection(Connection(name='Writer', model='writer', tier='strong', **common).model_dump(), user_id=uid)
    run = store.create_run(RunInput(prompt='Review a function', mode='reviewed', request_id=str(uuid.uuid4())), uid)
    run = store.get_run(run['id'], uid, True)
    run['state']['last_generator'] = 'writer'
    run['state']['preferences']['reviewer_id'] = writer['id']
    candidates, reason = Orchestrator(store, run, 'qa').candidates('reviewer')
    cases.append({'name': 'assigned_reviewer_must_not_be_the_writer', 'passed': candidates[0]['model'] != 'writer',
                  'expected': 'a different model', 'actual': candidates[0]['model'], 'reason': reason})
    all_models = [light, middle, writer]
    orders = {tuple(m['id'] for m in rank(list(p), 'coding', 'standard', {'routing': 'auto'})[0]) for p in itertools.permutations(all_models)}
    cases.append({'name': 'ranking_is_independent_of_input_order', 'passed': len(orders) == 1})
    malformed = json.dumps({'tasks': [{'instruction': 'First', 'depends_on': [2]}, {'instruction': 'Second', 'depends_on': [1]}]})
    parsed = parse_plan(malformed)
    cases.append({'name': 'plan_contains_only_backward_dependencies', 'passed': all(d < t['id'] for t in parsed for d in t['depends_on']), 'parsed': parsed})
    valid = parse_plan(json.dumps({'tasks': [{'instruction': 'Choose inputs'}, {'instruction': 'Build output', 'depends_on': [1]}]}))
    cases.append({'name': 'valid_dependency_plan_is_preserved', 'passed': len(valid) == 2 and valid[1]['depends_on'] == [1]})
    store.engine.dispose()
    account.secrets.file.unlink(missing_ok=True)
    if previous is None:
        os.environ.pop('LEO_CLI_KEYRING', None)
    else:
        os.environ['LEO_CLI_KEYRING'] = previous
    return cases


async def probe(source, connection):
    start, calls, text = time.monotonic(), ToolCalls(), ''
    schema = {'name': 'report_probe', 'description': 'Report the connectivity probe result.',
              'parameters': {'type': 'object', 'properties': {'answer': {'type': 'string', 'enum': ['OK']}}, 'required': ['answer'], 'additionalProperties': False}}
    try:
        async for chunk in stream(connection, source.store().decrypt(connection),
                                  [{'role': 'user', 'content': 'Call report_probe exactly once with answer="OK". Do not answer with text.'}],
                                  256, 35, tools=[schema]):
            calls.add(chunk.calls)
            text += chunk.text
        found = calls.result()
        passed = any(c['name'] == 'report_probe' and c['arguments'] == {'answer': 'OK'} for c in found)
        return {'model': connection['model'], 'tier': connection['tier'], 'passed': passed, 'tool_calls': found,
                'text': text[:300], 'seconds': round(time.monotonic() - start, 2)}
    except (ProviderError, ValueError) as exc:
        return {'model': connection['model'], 'tier': connection['tier'], 'passed': False, 'error': str(exc), 'seconds': round(time.monotonic() - start, 2)}


TASK = '''This is a bounded QA test of the full three-level agent tree. Use one root -> one manager -> one worker.
First call delegate with kind="manager", size="normal", agent="coding". Give that manager this entire task:
Delegate exactly one coding worker with size="normal" to create clamp.py with clamp(value, lower, upper). Return lower
when value < lower, upper when value > upper, otherwise value. Raise ValueError when lower > upper. Add test_clamp.py
using unittest, including boundaries and invalid bounds. Run python -m unittest -v. The worker should report the result.
The manager must inspect the evidence and call review_work to accept or reject its worker before reporting to the root.
The root must call review_work on the manager before its final answer. Keep all messages brief. No dependencies, no
network, no extra files. Only workers edit files or run commands. Use the tool names exactly as offered.'''


async def live(output, extend_budget_once=False):
    source = Account()
    choices = source.store().list_connections(source.user_id, private=True)
    wanted = ['openrouter/nvidia/nemotron-3.5-super-vl:free', 'openrouter/google/gemma-4-31b-it:free', 'openrouter/cohere/north-mini-code:free']
    selected = [next(c for c in choices if c['model'] == model and c['free'] and c['enabled']) for model in wanted]
    report = {'probes': []}
    for c in selected:
        try:
            result = await asyncio.wait_for(probe(source, c), 50)
        except TimeoutError:
            result = {'model': c['model'], 'tier': c['tier'], 'passed': False, 'error': 'Probe exceeded 50 seconds'}
        report['probes'].append(result)
        print(json.dumps({'probe': result}), flush=True)
    failed = {p['model'] for p in report['probes'] if not p['passed']}
    if wanted[1] in failed:
        fallback = next((c for c in choices if c['model'] == 'openrouter/recursive/flint-v1.0:free' and c['free'] and c['enabled']), None)
        if fallback:
            result = await asyncio.wait_for(probe(source, fallback), 50)
            report['probes'].append(result)
            print(json.dumps({'fallback_probe': result}), flush=True)
            if result['passed']:
                selected.append(fallback)
    passed_tiers = {p['tier'] for p in report['probes'] if p['passed']}
    if passed_tiers != {'strong', 'standard', 'light'}:
        report['tree'] = {'status': 'not_started', 'reason': 'One of the representative models failed native tool calling.'}
        return report
    # Resolve upstream credentials before selecting the isolated file-backed secret store.
    copied = [{**{k: c[k] for k in Connection.model_fields if k in c}, 'api_key': source.store().decrypt(c),
               'priority': 0 if c['model'] in failed else 10} for c in selected]
    previous_keyring = os.environ.get('LEO_CLI_KEYRING')
    os.environ['LEO_CLI_KEYRING'] = 'file'
    home = Account(Path(tempfile.mkdtemp(prefix='live-', dir=output)))
    for c in copied:
        home.store().save_connection(Connection(**c).model_dump(), user_id=home.user_id)
    home.store().set_preferences({**home.store().preferences(), 'agent_max_calls': 14, 'agent_token_budget': 100000,
        'agent_max_cost': .05, 'agent_max_run_seconds': 300, 'max_output_tokens': 1600, 'context_tokens': 24000,
        'timeout_seconds': 40, 'agent_parallel': 2, 'agent_command_timeout': 15})
    workspace = home.home / 'workspace'
    workspace.mkdir()
    sessions = Sessions(home, str(workspace), autorun=True)
    session = await sessions.open()
    cursor = 0
    try:
        await sessions.send(session, TASK)
        deadline = time.monotonic() + 300
        while session.task and not session.task.done():
            for event in sessions.events(session, cursor):
                cursor = event['id']
                if event['kind'] in ('tree', 'warning', 'error'):
                    data = event['data']
                    if event['kind'] == 'tree':
                        data = {k: data.get(k) for k in ('id', 'parent', 'kind', 'tier', 'model', 'status', 'attempt')}
                    print(json.dumps({'event': event['kind'], 'data': data}), flush=True)
            if time.monotonic() > deadline:
                sessions.stop(session)
                break
            await asyncio.sleep(.5)
        if session.task:
            await session.task
        if extend_budget_once and session.approval and session.approval['kind'] == 'budget':
            before = home.store().call_rows(session.run_id)
            sessions.answer(session, 'once')
            await asyncio.wait_for(session.task, 90)
            after = home.store().call_rows(session.run_id)
            report['resume'] = {'before_status': 'waiting', 'after_status': session.status, 'calls_before': len(before),
                                'calls_after': len(after), 'new_worker_calls': sum(c['stage'].startswith('agent:sub-') for c in after[len(before):])}
        run = session.run
        nodes = ((run['state'].get('agent') or {}).get('tree') or {}).get('nodes') or {}
        rows = home.store().call_rows(session.run_id)
        report['tree'] = {'status': session.status, 'output': run.get('output'),
            'nodes': [{k: n.get(k) for k in ('id', 'parent', 'kind', 'level', 'tier', 'model', 'status', 'attempt', 'reviews', 'files', 'commands')} for n in nodes.values()],
            'calls': [{k: c.get(k) for k in ('model', 'stage', 'status', 'input_tokens', 'output_tokens', 'elapsed_ms')} for c in rows],
            'usage': run.get('usage')}
        # Independent acceptance checks are authored by the QA harness, not by any model.
        hidden = workspace / 'qa_acceptance.py'
        hidden.write_text('''import unittest
from clamp import clamp
class Acceptance(unittest.TestCase):
 def test_cases(self):
  for x,lo,hi,want in [(-1,0,5,0),(8,0,5,5),(3,0,5,3),(0,0,5,0),(5,0,5,5),(-3,-5,-1,-3),(3,3,3,3),(1.5,1,2,1.5)]:
   with self.subTest(x=x): self.assertEqual(clamp(x,lo,hi),want)
 def test_invalid(self):
  with self.assertRaises(ValueError): clamp(3,5,1)
unittest.main()
''')
        checked = subprocess.run([sys.executable, str(hidden)], cwd=workspace, capture_output=True, text=True, timeout=15)
        report['acceptance'] = {'passed': checked.returncode == 0, 'returncode': checked.returncode, 'output': checked.stdout + checked.stderr}
    finally:
        await sessions.close()
        home.store().engine.dispose()
        home.secrets.file.unlink(missing_ok=True)  # Do not leave a reusable copy of device credentials in a QA artifact.
        if previous_keyring is None:
            os.environ.pop('LEO_CLI_KEYRING', None)
        else:
            os.environ['LEO_CLI_KEYRING'] = previous_keyring
    return report


async def live_cascade(output):
    """Use the actual rate-limited model first; see whether the guarded loop reaches another live provider."""
    source = Account()
    mine = source.store().list_connections(source.user_id, private=True)
    wanted = ['openrouter/google/gemma-4-31b-it:free', 'openrouter/nvidia/nemotron-3.5-super-vl:free']
    copied = []
    for priority, model in enumerate(wanted):
        c = next(c for c in mine if c['model'] == model and c['free'] and c['enabled'])
        copied.append({**{k: c[k] for k in Connection.model_fields if k in c}, 'api_key': source.store().decrypt(c), 'priority': priority})
    previous_keyring = os.environ.get('LEO_CLI_KEYRING')
    os.environ['LEO_CLI_KEYRING'] = 'file'
    home = Account(Path(tempfile.mkdtemp(prefix='cascade-', dir=output)))
    try:
        for c in copied:
            home.store().save_connection(Connection(**c).model_dump(), user_id=home.user_id)
        home.store().set_preferences({**home.store().preferences(), 'max_calls': 4, 'max_output_tokens': 128,
                                     'token_budget': 20000, 'timeout_seconds': 35, 'web_search': 'off'})
        run = home.store().create_run(RunInput(prompt='Reply with exactly QA_OK.', mode='quick', request_id=str(uuid.uuid4())), home.user_id)
        await asyncio.wait_for(process_one(home.store(), 'live-cascade-qa'), 100)
        result = home.store().get_run(run['id'], home.user_id, True)
        calls = [{k: c.get(k) for k in ('model', 'stage', 'status', 'elapsed_ms')} for c in home.store().call_rows(run['id'])]
        return {'status': result['status'], 'output': result['output'], 'calls': calls,
                'warnings': [e['data'] for e in home.store().get_events(run['id']) if e['kind'] == 'warning'],
                'fallback_observed': len(calls) > 1 and calls[0]['status'] == 'failed' and calls[-1]['status'] == 'completed'}
    finally:
        home.store().engine.dispose()
        home.secrets.file.unlink(missing_ok=True)
        if previous_keyring is None:
            os.environ.pop('LEO_CLI_KEYRING', None)
        else:
            os.environ['LEO_CLI_KEYRING'] = previous_keyring


def assess_tree(result):
    tree = result.get('tree') or {}
    nodes = tree.get('nodes') or []
    lookup = {n['id']: n for n in nodes}
    children = [n for n in nodes if n.get('parent')]
    return [
        {'name': 'live_run_completed', 'passed': tree.get('status') == 'completed'},
        {'name': 'root_manager_worker_all_exercised', 'passed': {n['kind'] for n in nodes} == {'root', 'manager', 'worker'}},
        {'name': 'all_three_tiers_exercised', 'passed': {n['tier'] for n in nodes} == {'strong', 'standard', 'light'}},
        {'name': 'children_reviewed_and_accepted', 'passed': bool(children) and all(n['status'] == 'accepted' for n in children)},
        {'name': 'parent_roles_remained_read_only', 'passed': bool(nodes) and all(not n.get('files') and not n.get('commands') for n in nodes if n['kind'] != 'worker')},
        {'name': 'parent_links_form_a_tree', 'passed': bool(children) and all(n['parent'] in lookup and n['level'] > lookup[n['parent']]['level'] for n in children)},
        {'name': 'worker_ran_passing_tests', 'passed': any('unittest' in c.get('command', '') and c.get('exit') == 'exit code 0' for n in nodes for c in n.get('commands') or [])},
        {'name': 'independent_acceptance', 'passed': bool((result.get('acceptance') or {}).get('passed'))},
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--live-cascade', action='store_true')
    parser.add_argument('--extend-budget-once', action='store_true', help='Permit one QA-only budget extension, up to 28 tree calls total.')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix='leo-algorithm-qa-'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'audit': audit(output)}
    (output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    if args.live:
        report['live'] = asyncio.run(live(output, args.extend_budget_once))
        report['live']['checks'] = assess_tree(report['live'])
    if args.live_cascade:
        report['cascade'] = asyncio.run(live_cascade(output))
    (output / 'report.json').write_text(json.dumps(report, indent=2))
    print('Report:', output / 'report.json', flush=True)
    passed = all(c['passed'] for c in report['audit'] + (report.get('live') or {}).get('checks', []))
    if args.live_cascade:
        passed = passed and report['cascade']['status'] == 'completed' and report['cascade']['output'].strip() == 'QA_OK'
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
