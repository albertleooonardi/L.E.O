import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.chat_tools import LIMIT_NOTE
from backend.worker import process_one
from .conftest import add_provider, make_run
from .test_agent import tool_reply
from .test_connectors import MCP_URL, FakeMCP

LINEAR_TOOLS = [
    {'name': 'create_issue', 'description': 'Create an issue', 'read_only': False, 'destructive': False,
     'input_schema': {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title']}},
    {'name': 'search_issues', 'description': 'Find issues', 'read_only': True, 'destructive': False,
     'input_schema': {'type': 'object', 'properties': {'q': {'type': 'string'}}}},
]


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))])


def linear(store, **permissions):
    return store.create_connector(store.user_id, {'name': 'Linear', 'url': MCP_URL, 'auth': 'none', 'enabled': True, 'status': 'connected',
                                                  'tools': LINEAR_TOOLS, 'permissions': {'create_issue': 'ask', 'search_issues': 'ask', **permissions}})


class World:
    """A tool-calling model at local.test and the Linear MCP server, behind one transport."""

    def __init__(self, model):
        self.mcp, self.model, self.requests = FakeMCP(), model, []
        self.transport = httpx.MockTransport(self)

    def __call__(self, request):
        if request.url.host == 'mcp.example.com':
            return self.mcp(request)
        body = json.loads(request.content)
        self.requests.append(body)
        return self.model(body)


def tool_results(body):
    return [m for m in body['messages'] if m['role'] == 'tool']


def files_an_issue(body):
    """Asks to create one issue, then answers with what the tool returned."""
    done = tool_results(body)
    if not done:
        return tool_reply([('call_1', 'linear__create_issue', {'title': 'Fix login'})])
    return tool_reply(text=f"Result: {done[-1]['content']}")


async def work(store, world):
    await process_one(store, 'w1', world.transport, None)


async def test_quick_mode_calls_an_allowed_tool_and_answers(store):
    add_provider(store, tools=True)
    connector = linear(store, create_issue='allow')
    run = make_run(store, prompt='File a Linear issue: fix login', connectors=[connector['id']])
    world = World(files_an_issue)
    await work(store, world)
    done = store.get_run(run['id'], store.user_id, True)
    assert done['status'] == 'completed' and done['output'] == 'Result: Created LIN-7: Fix login'
    assert world.mcp.calls == [('create_issue', {'title': 'Fix login'})]
    assert [t['function']['name'] for t in world.requests[0]['tools']] == ['linear__create_issue', 'linear__search_issues']
    assert done['state']['tool_log'][0] | {'result': ''} == {'stage': 'generating', 'connector': 'Linear', 'tool': 'create_issue',
                                                            'arguments': {'title': 'Fix login'}, 'status': 'done', 'result': ''}


async def test_ask_pauses_for_approval_and_resumes_without_repeating_anything(store):
    add_provider(store, tools=True)
    connector = linear(store)
    run = make_run(store, prompt='File a Linear issue: fix login', connectors=[connector['id']])
    world = World(files_an_issue)
    await work(store, world)
    waiting = store.get_run(run['id'], store.user_id, True)
    approval = waiting['state']['pending_approval']
    assert waiting['status'] == 'waiting' and approval['kind'] == 'mcp_tool' and world.mcp.calls == []
    assert approval['detail'] == {'connector_id': connector['id'], 'connector': 'Linear', 'tool': 'create_issue',
                                  'arguments': {'title': 'Fix login'}, 'read_only': False}
    store.resolve_approval(run['id'], approval['id'], True, store.user_id)
    await work(store, world)
    done = store.get_run(run['id'], store.user_id, True)
    assert done['status'] == 'completed' and world.mcp.calls == [('create_issue', {'title': 'Fix login'})]
    assert len(world.requests) == 2, 'the turn that asked for the tool is not sent again'
    # Allow once does not change the saved permission.
    assert store.get_connector(connector['id'], store.user_id)['permissions']['create_issue'] == 'ask'


async def test_a_declined_call_is_reported_to_the_model(store):
    add_provider(store, tools=True)
    run = make_run(store, prompt='File a Linear issue: fix login', connectors=[linear(store)['id']])
    world = World(files_an_issue)
    await work(store, world)
    store.resolve_approval(run['id'], store.get_run(run['id'], store.user_id, True)['state']['pending_approval']['id'], False, store.user_id)
    await work(store, world)
    done = store.get_run(run['id'], store.user_id, True)
    assert done['status'] == 'completed' and world.mcp.calls == [] and 'declined' in done['output']
    assert done['state']['tool_log'][0]['status'] == 'denied'


def test_always_allow_saves_the_permission(store):
    add_provider(store, tools=True)
    connector = linear(store)
    run = make_run(store, prompt='File a Linear issue', connectors=[connector['id']])
    approval = {'id': 'mcp:generating:call_1', 'kind': 'mcp_tool', 'message': 'Allow?', 'detail': {'connector_id': connector['id'], 'tool': 'create_issue'}}
    store.claim('w1')
    store.wait(run['id'], 'w1', {**store.get_run(run['id'], store.user_id, True)['state'], 'pending_approval': approval}, approval)
    with TestClient(create_app(store.config, store)) as client:
        client.cookies.set('leo_session', store.user_session_token)
        assert client.post(f"/api/runs/{run['id']}/approve", json={'approval_id': approval['id'], 'approved': True, 'remember': True}).status_code == 200
    assert store.get_connector(connector['id'], store.user_id)['permissions']['create_issue'] == 'allow'


async def test_denied_tools_are_never_offered(store):
    add_provider(store, tools=True)
    run = make_run(store, prompt='Look up issues', connectors=[linear(store, create_issue='deny', search_issues='allow')['id']])
    world = World(lambda body: tool_reply(text='Nothing to do'))
    await work(store, world)
    assert [t['function']['name'] for t in world.requests[0]['tools']] == ['linear__search_issues']
    assert store.get_run(run['id'], store.user_id, True)['status'] == 'completed'


async def test_the_tool_round_limit_forces_an_answer(store):
    add_provider(store, tools=True)
    store.set_preferences({**store.preferences(), 'max_tool_calls': 2})
    run = make_run(store, prompt='Search issues until done', connectors=[linear(store, search_issues='allow')['id']])

    def keeps_searching(body):
        if 'tools' not in body:
            assert body['messages'][-1]['content'] == LIMIT_NOTE
            return tool_reply(text='Here is what I found.')
        return tool_reply([(f"call_{len(tool_results(body))}", 'linear__search_issues', {'q': 'login'})])
    world = World(keeps_searching)
    await work(store, world)
    done = store.get_run(run['id'], store.user_id, True)
    assert done['status'] == 'completed' and done['output'] == 'Here is what I found.' and len(world.mcp.calls) == 2


async def test_deep_mode_gives_tools_only_to_the_assigned_task(store):
    add_provider(store, tools=True)
    run = make_run(store, mode='deep', prompt='Plan the login fix and file it in Linear', connectors=[linear(store, create_issue='allow')['id']])

    def model(body):
        system = body['messages'][0]['content']
        if system.startswith('Return only JSON: {"passed"'):
            assert 'tools' not in body
            return tool_reply(text=json.dumps({'passed': True, 'issues': []}))
        if 'Create a concise execution plan' in system:
            assert 'tools' not in body and 'Connectors: linear (Linear: create_issue, search_issues)' in system
            return tool_reply(text=json.dumps({'tasks': [
                {'instruction': 'Write the fix plan', 'depends_on': [], 'skill': 'general', 'difficulty': 'light'},
                {'instruction': 'File the plan as a Linear issue', 'depends_on': [1], 'skill': 'general', 'difficulty': 'light', 'connectors': ['linear']}]}))
        task = body['messages'][-1]['content'] if not tool_results(body) else ''
        if 'Write the fix plan' in task:
            assert 'tools' not in body
            return tool_reply(text='Plan: patch the session check')
        assert body.get('tools'), 'the filing task gets the connector'
        return files_an_issue(body)
    world = World(model)
    await work(store, world)
    done = store.get_run(run['id'], store.user_id, True)
    assert done['status'] == 'completed' and done['output'] == 'Result: Created LIN-7: Fix login' and len(world.mcp.calls) == 1


async def test_review_and_repair_never_call_tools_but_see_the_log(store):
    add_provider(store, tools=True)
    run = make_run(store, mode='reviewed', prompt='File a Linear issue: fix login', connectors=[linear(store, create_issue='allow')['id']])
    reviews = []

    def model(body):
        system = body['messages'][0]['content']
        if system.startswith('Return only JSON: {"passed"'):
            assert 'tools' not in body
            reviews.append(body['messages'][-1]['content'])
            return tool_reply(text=json.dumps({'passed': len(reviews) > 1, 'issues': [] if len(reviews) > 1 else ['Mention the issue key']}))
        if 'Fix these review findings' in body['messages'][-1]['content']:
            assert 'tools' not in body
            return tool_reply(text='Created LIN-7 (Fix login).')
        return files_an_issue(body)
    world = World(model)
    await work(store, world)
    done = store.get_run(run['id'], store.user_id, True)
    assert done['status'] == 'completed' and done['output'] == 'Created LIN-7 (Fix login).' and len(world.mcp.calls) == 1
    assert 'TOOL CALLS MADE' in reviews[0] and 'create_issue' in reviews[0]


async def test_without_a_tool_capable_model_the_answer_comes_without_tools(store):
    add_provider(store, tools=False)
    run = make_run(store, prompt='File a Linear issue: fix login', connectors=[linear(store, create_issue='allow')['id']])
    world = World(lambda body: tool_reply(text='I cannot reach Linear from here.'))
    await work(store, world)
    done = store.get_run(run['id'], store.user_id, True)
    assert done['status'] == 'completed' and 'tools' not in world.requests[0] and world.mcp.calls == []
    assert any('tool calls' in e['data']['message'] for e in store.events_of(run['id'], 'warning'))


async def test_a_follow_up_in_the_same_chat_uses_tools_again(store):
    """Earlier answers in the history are context, not this turn's answer, and do not count as tool rounds."""
    add_provider(store, tools=True)
    store.set_preferences({**store.preferences(), 'max_tool_calls': 1})
    connector = linear(store, create_issue='allow')
    first = make_run(store, prompt='File a Linear issue: fix login', connectors=[connector['id']])
    world = World(files_an_issue)
    await work(store, world)
    second = make_run(store, prompt='File another Linear issue: fix logout', conversation_id=first['conversation_id'], connectors=[connector['id']])
    await work(store, world)
    done = store.get_run(second['id'], store.user_id, True)
    assert done['status'] == 'completed' and len(world.mcp.calls) == 2 and len(world.requests) == 4
    assert world.requests[2]['tools'], 'the follow-up still gets its tools'
