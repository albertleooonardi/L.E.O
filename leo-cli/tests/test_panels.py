"""/usage, /context, and /mcp: what models cost, how full the context is, and which MCP servers there are."""
import json
import sys

from rich.console import Console

from leo_cli import mcp, theme
from leo_cli.panels import context_view, usage_view
from leo_cli.sessions import Sessions
from leo_cli.ui import glyphs
from test_sessions import Model, account, project, writes_hello  # noqa: F401 (fixtures)


def shown(renderable, width=140):
    console = theme.apply(Console(record=True, width=width, color_system=None))
    console.print(renderable)
    return console.export_text()


async def finished_session(account, project):
    sessions = Sessions(account, project, autorun=True, transport=Model(writes_hello).transport)
    session = await sessions.open()
    await sessions.send(session, 'Create hello.txt')
    await session.task
    return sessions, session


async def test_usage_lists_every_model_and_the_totals(account, project):
    g = glyphs(unicode=True)
    assert 'No model calls on this device yet' in shown(usage_view(account.store(), account.user_id, g=g))
    sessions, session = await finished_session(account, project)
    out = shown(usage_view(account.store(), account.user_id, session.id, [c['id'] for c in sessions.history()], g))
    assert 'test-model' in out and 'Local test' in out and '100%' in out and 'free' in out
    assert 'this session  2 calls · 240 tokens (200 in, 40 out) · free' in out
    assert 'this folder' in out and 'this device' in out and '~ estimated' not in out


async def test_context_shows_what_fills_the_window(account, project):
    g = glyphs(unicode=True)
    empty = shown(context_view(None, account.store(), account.user_id, g=g))
    assert 'No task in this session yet' in empty and 'tool definitions' in empty
    sessions, session = await finished_session(account, project)
    out = shown(context_view(session.run, account.store(), account.user_id, 'test-model', g))
    assert 'test-model' in out and 'system prompt' in out and 'tool results' in out and 'free' in out
    rows = {line.split()[0]: line for line in out.splitlines() if line.strip()}
    assert not rows['system'].split()[2].startswith('0'), 'the system prompt is counted'
    assert 'smaller than the point where L.E.O. summarizes' in out, 'a 32K model under the default allowance says so'


SERVER = """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        result = {'protocolVersion': '2025-06-18', 'capabilities': {}, 'serverInfo': {'name': 'fake'}}
    elif message.get('method') == 'tools/list':
        result = {'tools': [{'name': 'echo'}, {'name': 'add'}]}
    else:
        continue
    print('starting up...', flush=True)
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
"""


async def test_mcp_lists_cli_servers_and_checks_them(tmp_path):
    home, project = tmp_path / 'home', tmp_path / 'project'
    (project / '.leo-cli').mkdir(parents=True)
    home.mkdir()
    (tmp_path / 'server.py').write_text(SERVER)
    (home / 'mcp.json').write_text(json.dumps({'mcpServers': {'mine': {'command': sys.executable, 'args': [str(tmp_path / 'server.py')]},
                                                             'shared': {'url': 'https://old.example/mcp'}}}))
    (project / '.leo-cli' / 'mcp.json').write_text(json.dumps({'mcpServers': {'shared': {'command': 'no-such-command-here'},
                                                                              'empty': {}}}))
    servers = mcp.configured(project, home)
    assert [(s['name'], s['scope']) for s in servers] == [('mine', 'you'), ('shared', 'project'), ('empty', 'project')], 'the project wins'
    results = [await mcp.check(s) for s in servers]
    assert results[0] == {'ok': True, 'tools': ['echo', 'add'], 'error': None}, 'lines that are not JSON-RPC are skipped'
    assert results[1]['error'] == 'command not found: no-such-command-here'
    assert 'needs a command' in results[2]['error']
    (home / 'chat-connectors.json').write_text(json.dumps([{'name': 'Notion', 'url': 'https://mcp.notion.example/mcp', 'enabled': True,
                                                            'auth': 'oauth', 'status': 'sign_in', 'signed_in': False, 'tools': []}]))
    out = shown(mcp.view(mcp.chat_connectors(home), servers, results, glyphs(unicode=True), home, project))
    assert 'From L.E.O Chat' in out and 'Notion' in out and 'sign in on the website' in out
    assert 'mine' in out and 'ready' in out and 'echo, add' in out and '.leo-cli/mcp.json' in out and 'command not found' in out


def test_mcp_says_how_to_add_servers(tmp_path):
    out = shown(mcp.view(None, [], [], glyphs(unicode=True), tmp_path, tmp_path))
    assert 'run /sync' in out and 'mcpServers' in out and 'mcp.json' in out


async def test_usage_starts_with_the_latest_tree(tmp_path, monkeypatch, project):
    from leo_cli.ui import App
    from test_tree import ALL, Team, accept, delegate
    from backend.schemas import Connection
    from leo_cli.account import Account
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    home = Account(tmp_path / 'home-2')
    for model in ALL:
        home.store().save_connection(Connection(name=model, base_url=f'http://{model}.test/v1', model=model, free=True, tools=True,
                                                tier=model.split('-')[0]).model_dump(), user_id=home.user_id)
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'], 'child work': ['ok'], 'plain': ['Just an answer.']})
    console = theme.apply(Console(record=True, width=140, color_system=None))
    ui = App(Sessions(home, project, autorun=True, transport=team.transport), home, console)
    session = await ui.sessions.open()
    await ui.sessions.send(session, 'ROOT task')
    await session.task
    await ui.command('/usage')
    out = console.export_text()
    assert 'child work' in out and out.index('child work') < out.index('Model usage on this device') and 'accepted' in out
    await ui.command('/new')
    await ui.sessions.send(ui.sessions.current, 'plain question')
    await ui.sessions.current.task
    await ui.command('/usage')
    assert 'child work' not in console.export_text(), 'a tree of one is no tree'
    await ui.sessions.close()


async def test_print_mode_shows_the_tree_on_stderr(tmp_path, monkeypatch, project, capsys):
    from leo_cli import app
    from test_tree import ALL, Team, accept, delegate
    from backend.schemas import Connection
    from leo_cli.account import Account
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    home = Account(tmp_path / 'home-3')
    for model in ALL:
        home.store().save_connection(Connection(name=model, base_url=f'http://{model}.test/v1', model=model, free=True, tools=True,
                                                tier=model.split('-')[0]).model_dump(), user_id=home.user_id)
    team = Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'], 'child work': ['ok']})
    sessions = Sessions(home, project, autorun=True, transport=team.transport)
    assert await app.one_shot(sessions, 'ROOT task') == 0
    captured = capsys.readouterr()
    assert captured.out == 'Done.\n' and 'child work' in captured.err and 'accepted' in captured.err
