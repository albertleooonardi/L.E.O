"""The agent uses MCP tools in the CLI: a local server started once per window, with approvals like edits and commands."""
import json
import sys

from leo_cli.sessions import Sessions
from test_sessions import Model, account, project, reply  # noqa: F401 (fixtures)

SERVER = """
import json, sys
TOOLS = [{'name': 'lookup', 'description': 'Look a word up', 'annotations': {'readOnlyHint': True},
          'inputSchema': {'type': 'object', 'properties': {'word': {'type': 'string'}}, 'required': ['word']}},
         {'name': 'save note', 'description': 'Save a note',
          'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}},
         {'name': 'broken', 'description': 'No usable schema', 'inputSchema': {'type': 'string'}}]
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'notes'}}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'tools/call':
        args = message['params']['arguments']
        if message['params']['name'] == 'lookup':
            result = {'content': [{'type': 'text', 'text': args['word'] + ': a greeting'}]}
        else:
            open('notes.txt', 'a').write(args['text'] + '\\n')
            result = {'content': [{'type': 'text', 'text': 'saved'}]}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
"""


def notes_server(project, tmp_path):
    (tmp_path / 'notes_server.py').write_text(SERVER)
    (project / '.leo-cli').mkdir(exist_ok=True)
    (project / '.leo-cli' / 'mcp.json').write_text(json.dumps({'mcpServers': {'notes': {'command': sys.executable,
                                                                                        'args': [str(tmp_path / 'notes_server.py')]}}}))


def uses_notes(body, done):
    names = {tool['function']['name'] for tool in body.get('tools') or []}
    assert {'notes__lookup', 'notes__save_note'} <= names and 'notes__broken' not in names, names
    if not done:
        return reply([('m1', 'notes__lookup', {'word': 'hello'})])
    if len(done) == 1:
        return reply([('m2', 'notes__save_note', {'text': done[-1]['content'].splitlines()[-1]})])
    return reply(text=f"Done: {done[-1]['content'].splitlines()[-1]}")


async def test_the_agent_calls_mcp_tools_and_asks_before_changes(account, project, tmp_path):
    notes_server(project, tmp_path)
    sessions = Sessions(account, project, transport=Model(uses_notes).transport)
    session = await sessions.open()
    await sessions.send(session, 'Look up hello and save it as a note')
    await session.task
    lookup = [e['data'] for e in sessions.store.events_of(session.run_id, 'agent_tool') if e['data']['name'] == 'notes__lookup']
    assert lookup and lookup[0]['ok'] and 'hello: a greeting' in lookup[0]['output'], 'a read-only tool runs without asking'
    assert session.status == 'waiting' and session.approval['kind'] == 'mcp'
    assert session.approval['detail']['tool'] == 'save note' and '"hello: a greeting"' in session.approval['detail']['arguments']
    assert not (project / 'notes.txt').exists()
    assert sessions.answer(session, 'session')
    await session.task
    assert session.status == 'completed' and session.run['output'] == 'Done: saved'
    assert (project / 'notes.txt').read_text() == 'hello: a greeting\n', 'local servers run in the project folder'
    assert sessions.policy.check('main', {'name': 'notes__save_note', 'arguments': {'text': 'x'}}) is None, 'remembered for the session'
    process = sessions.mcp.clients['notes'].process
    await sessions.close()
    assert process.returncode is not None, 'the server stops with the window'


async def test_a_server_that_fails_is_left_out(account, project, tmp_path):
    (project / '.leo-cli').mkdir()
    (project / '.leo-cli' / 'mcp.json').write_text(json.dumps({'mcpServers': {'gone': {'command': 'no-such-mcp-server'}}}))

    def plain(body, done):
        assert not [t for t in body.get('tools') or [] if '__' in t['function']['name']]
        return reply(text='ok')

    sessions = Sessions(account, project, autorun=True, transport=Model(plain).transport)
    session = await sessions.open()
    await sessions.send(session, 'hi')
    await session.task
    assert session.run['output'] == 'ok' and 'gone' in sessions.mcp.failed
    await sessions.close()
