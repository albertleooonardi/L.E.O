"""The `leo` command and its terminal UI: one-shot runs, approvals, slash commands, and event rendering."""
import asyncio
import io
import itertools
import json
import re
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from sqlalchemy import update

from backend.store import runs
from leo_cli import app
from leo_cli.sessions import Sessions
from leo_cli.ui import BANNER, App, Renderer, approval_view, choose_answer, parse_answer, split_blocks, suggested_prefix
from test_sessions import Model, account, project, reply, writes_hello  # noqa: F401 (fixtures)


class Answers(io.StringIO):
    """A stdin that is (or is not) a terminal, with typed lines."""

    def __init__(self, text='', tty=True):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


def args(*argv):
    return app.parser().parse_args(list(argv))


def text_of(console):
    return console.export_text(clear=False)


# -- the command -----------------------------------------------------------------

def test_unknown_commands_are_refused_not_run_as_tasks(capsys):
    with pytest.raises(SystemExit) as stop:
        app.main(['modles'])
    assert stop.value.code == 2 and 'unknown command' in capsys.readouterr().err


def test_a_folder_argument_is_the_project(tmp_path, monkeypatch):
    seen = []

    async def dispatch(arguments, account=None, transport=None):
        seen.append((arguments.project, arguments.command))
        return 0

    monkeypatch.setattr(app, 'dispatch', dispatch)
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'proj').mkdir()
    assert app.main([str(tmp_path / 'proj')]) == 0 and app.main(['~/proj', '--autorun']) == 0
    assert seen == [(str(tmp_path / 'proj'), []), (str(tmp_path / 'proj'), [])]


async def test_no_models_means_sign_in_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    from leo_cli.account import Account
    assert await app.dispatch(args('-p', 'hi', '--project', str(tmp_path)), Account(tmp_path / 'empty')) == 1
    assert 'leocode login' in capsys.readouterr().out


DOWN, ENTER = '\x1b[B', '\r'


async def test_the_first_run_signs_in_inside_leo(tmp_path, monkeypatch):
    """No `leocode login` step: plain `leo` on a terminal shows the welcome screen; quitting there exits cleanly."""
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    monkeypatch.setattr(app.sys, 'stdin', Answers(tty=True))
    from leo_cli.account import Account
    calls = []

    async def first_sign_in(account, console, server=None, name=None):
        calls.append(server)
        return False  # Quit.

    monkeypatch.setattr(app, 'first_sign_in', first_sign_in)
    assert await app.dispatch(args('--project', str(tmp_path), '--server', 'http://leo.test'), Account(tmp_path / 'home')) == 0
    assert calls == ['http://leo.test']


@pytest.fixture
def signing_in(tmp_path, monkeypatch):
    """A device that is not signed in, with `login` replaced by a recorder (the real one is tested in test_account)."""
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    monkeypatch.delenv('LEO_SERVER', raising=False)
    from leo_cli.account import Account
    used = []

    async def login(account, console, server=None, name=None, inside=False):
        used.append((server, inside))
        return 0

    monkeypatch.setattr(app, 'login', login)
    return Account(tmp_path / 'home'), used, Console(record=True, width=120, color_system=None)


async def test_the_first_run_picks_a_theme_then_signs_in_on_the_website(signing_in):
    account, used, console = signing_in
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(DOWN + ENTER)  # Theme: Light.
        await asyncio.sleep(0.3)
        typed.send_text(ENTER)  # Sign in (the first option).
        assert await app.first_sign_in(account, console) is True
    assert used == [(app.DEFAULT_SERVER, True)]
    assert account.config()['theme'] == 'light'
    shown = text_of(console)
    assert BANNER[4] in shown and 'Theme: Light' in shown


async def test_quit_on_the_welcome_screen_signs_nothing_in(signing_in):
    account, used, console = signing_in
    account.save(theme='mono')  # Chosen before: not asked again.
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(DOWN + DOWN + ENTER)  # Quit.
        assert await app.first_sign_in(account, console) is False
    assert used == [] and 'Theme:' not in text_of(console)


async def test_another_server_can_be_chosen_before_signing_in(signing_in):
    account, used, console = signing_in
    account.save(theme='dark')
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(DOWN + ENTER)  # Use another L.E.O. server.
        await asyncio.sleep(0.3)
        typed.send_text('\x15https://leo.example.com' + ENTER)  # Ctrl-U clears the suggested address.
        await asyncio.sleep(0.3)
        typed.send_text(ENTER)  # Sign in.
        assert await app.first_sign_in(account, console) is True
    assert used == [('https://leo.example.com', True)]


def test_every_theme_renders(tmp_path):
    from leo_cli import theme
    from leo_cli.ui import banner, theme_preview
    try:
        for name in theme.THEMES:
            console = Console(record=True, width=100, color_system='truecolor', theme=theme.use(name))
            console.print(banner(), theme_preview(), approval_view({'kind': 'command', 'message': 'Run this?', 'detail': {'command': 'ls'}}))
            assert f"Theme: {theme.THEMES[name]['label']}" in console.export_text()
    finally:
        theme.use(theme.DEFAULT)


async def test_the_theme_command_switches_and_remembers(ui, account):
    from leo_cli import theme
    try:
        await ui.command('/theme contrast')
        assert theme.current() == 'contrast' and account.config()['theme'] == 'contrast'
        await ui.command('/theme sparkly')
        assert 'Unknown theme' in text_of(ui.console) and theme.current() == 'contrast'
    finally:
        theme.use(theme.DEFAULT)


async def test_without_a_usable_model_leo_sets_one_up_inside(tmp_path, monkeypatch):
    """Signed in, no model this computer can use: leo offers to add one instead of stopping."""
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    from leo_cli import models
    from leo_cli.account import Account
    from backend.schemas import Connection
    account = Account(tmp_path / 'home')
    account.secrets.set('device-token', 't' * 43)
    account.save(server='http://leo.test', synced_at=time.time())
    added = []

    async def discover(connection, key, transport=None):
        assert connection['base_url'] == 'http://localhost:11434/v1' and key is None
        return {'models': [{'model': 'qwen3-coder', 'name': 'qwen3-coder', 'tier': 'standard', 'skills': ['coding'], 'reasoning': False,
                            'tools': True, 'context_window': 65536, 'free': True}]}

    async def add_connection(body, sync=True):  # Saved on the account and synced down (tested in test_account).
        added.append(body)
        account.store().save_connection(Connection(**body).model_dump(), user_id=account.user_id)

    async def sync():
        return {}

    monkeypatch.setattr(models, 'discover', discover)
    monkeypatch.setattr(account, 'add_connection', add_connection)
    monkeypatch.setattr(account, 'sync', sync)
    console = Console(record=True, width=120, color_system=None)
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        for keys in (ENTER, ENTER, ENTER, ENTER, ENTER):  # Add a model, Ollama, its URL, no key, qwen3-coder.
            typed.send_text(keys)
            await asyncio.sleep(0.3)
        assert await asyncio.wait_for(app.ready(account, console, interactive=True), 10) is True
    [body] = added
    assert body['model'] == 'qwen3-coder' and body['tools'] is True and body['local'] is True and body['api_key'] is None
    assert 'Your account has no models this computer can use yet.' in text_of(console)


async def test_a_signed_out_device_is_offered_a_fresh_sign_in(tmp_path, monkeypatch):
    """The server revoked this device (or was reset): the menu offers Sign in again, not a sync that cannot work."""
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    from backend.schemas import Connection
    from leo_cli.account import Account, SignedOut
    account = Account(tmp_path / 'home')
    account.secrets.set('device-token', 't' * 43)
    account.save(server='http://leo.test')
    signed = []

    async def sync():
        raise SignedOut('This device was signed out')

    async def login(account_, console, server=None, name=None, inside=False):
        signed.append(inside)
        account.save(synced_at=time.time())
        account.store().save_connection(Connection(name='Local', base_url='http://local.test/v1', model='m', free=True, local=True,
                                                   tools=True).model_dump(), user_id=account.user_id)

    monkeypatch.setattr(account, 'sync', sync)
    monkeypatch.setattr(app, 'login', login)
    console = theme_console()
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(ENTER)  # Sign in again (the first option).
        assert await asyncio.wait_for(app.ready(account, console, interactive=True), 10) is True
    assert signed == [True] and 'sign in again to get your models' in text_of(console)


def theme_console():
    from leo_cli import theme
    return theme.apply(Console(record=True, width=120, color_system=None))


async def test_without_a_terminal_leo_says_how_to_add_a_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    from leo_cli.account import Account
    account = Account(tmp_path / 'home')
    account.secrets.set('device-token', 't' * 43)
    assert await app.dispatch(args('-p', 'hi', '--project', str(tmp_path)), account) == 1
    assert 'tool calling' in capsys.readouterr().out


async def test_models_lists_what_was_synced(account, capsys):
    assert await app.dispatch(args('models'), account) == 0
    out = capsys.readouterr().out
    assert 'Local test' in out and 'yes' in out


async def test_one_shot_prints_the_answer_and_exit_code(account, project, capsys):
    model = Model(writes_hello)
    code = await app.dispatch(args('-p', 'Create hello.txt', '--autorun', '--project', str(project)), account, model.transport)
    captured = capsys.readouterr()
    assert code == 0 and (project / 'hello.txt').read_text() == 'hi from leo\n'
    assert captured.out.strip().startswith('Done: Wrote 12 bytes'), 'only the answer goes to stdout'
    assert 'write_file' in captured.err, 'progress goes to stderr'


async def test_one_shot_without_a_terminal_declines_approvals(account, project, capsys):
    sessions = Sessions(account, project, transport=Model(writes_hello).transport)
    assert await app.one_shot(sessions, 'Create hello.txt', stdin=Answers(tty=False)) == 0
    assert not (project / 'hello.txt').exists()
    captured = capsys.readouterr()
    assert 'declined' in captured.out and 'Use --autorun' in captured.err


async def test_one_shot_asks_on_the_terminal(account, project, capsys):
    sessions = Sessions(account, project, transport=Model(writes_hello).transport)
    assert await app.one_shot(sessions, 'Create hello.txt', stdin=Answers('maybe\ny\n')) == 0
    assert (project / 'hello.txt').exists()
    assert 'Write hello.txt?' in capsys.readouterr().err


async def test_a_failed_run_exits_non_zero(account, project, capsys):
    import httpx
    failing = httpx.MockTransport(lambda request: httpx.Response(404, json={'error': {'message': 'no such model'}}))
    assert await app.one_shot(Sessions(account, project, autorun=True, transport=failing), 'Anything') == 1


# -- sessions: a closed window's claim --------------------------------------------

async def test_a_session_left_by_a_closed_window_is_taken_over(account, project):
    notes = []
    sessions = Sessions(account, project, autorun=True, transport=Model(writes_hello).transport, notify=lambda s, m: notes.append(m))
    session = await sessions.open()
    run = await sessions.send(session, 'Create hello.txt')
    session.task.cancel()
    await asyncio.gather(session.task, return_exceptions=True)
    with sessions.store.engine.begin() as db:  # Another (now closed) window holds the run for two more seconds.
        db.execute(update(runs).where(runs.c.id == run['id']).values(status='running', lease_owner='closed-window', lease_until=time.time() + 2))
    await sessions.drive(session, lease_wait=10)
    assert session.status == 'completed' and 'Waiting for the previous leocode window' in notes[0]


async def test_a_session_held_by_an_open_window_is_left_alone(account, project):
    notes = []
    sessions = Sessions(account, project, transport=Model(writes_hello).transport, notify=lambda s, m: notes.append(m))
    session = await sessions.open()
    run = await sessions.send(session, 'Create hello.txt')
    session.task.cancel()
    await asyncio.gather(session.task, return_exceptions=True)
    with sessions.store.engine.begin() as db:
        db.execute(update(runs).where(runs.c.id == run['id']).values(status='running', lease_owner='open-window', lease_until=time.time() + 60))
    await sessions.drive(session, lease_wait=1)
    assert session.status == 'running' and 'another leocode window' in notes[-1]


# -- interactive commands (without the prompt) ----------------------------------------

@pytest.fixture
def ui(account, project):
    console = Console(record=True, width=120, color_system=None)
    sessions = Sessions(account, project, transport=Model(writes_hello).transport)
    return App(sessions, account, console)


async def test_autorun_new_sessions_and_unknown_commands(ui, project):
    await ui.command('/autorun on --save')
    assert ui.sessions.policy.autorun and '"autorun": true' in (project / '.leo-cli' / 'settings.json').read_text()
    await ui.command('/new')
    await ui.command('/new')
    await ui.command('/sessions')
    await ui.command('/nonsense')
    out = text_of(ui.console)
    rows = [line.split() for line in out.splitlines() if 'new session' in line]
    assert rows == [['1', 'new', 'session', ui.g.idle, 'idle'], [ui.g.prompt, '2', 'new', 'session', ui.g.idle, 'idle']], 'the current one is marked'
    assert 'Unknown command /nonsense' in out
    assert await ui.command('/exit') == 'exit'


async def test_a_skill_command_starts_a_task_and_cost_reports_it(ui, project):
    folder = project / '.leo-cli' / 'skills' / 'greet'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text('---\nname: greet\ndescription: Say hello\n---\nWrite hello.txt.\n')
    ui.sessions.policy.set_autorun(True)
    await ui.command('/help')
    assert '/greet' in text_of(ui.console) and 'Say hello' in text_of(ui.console)
    await ui.command('/greet in English')
    session = ui.sessions.current
    await session.task
    assert session.run['prompt'] == 'Use the greet skill. in English' and (project / 'hello.txt').exists()
    ui.follow(session)
    await ui.command('/cost')
    out = text_of(ui.console)
    assert 'write_file(hello.txt)' in out and 'Done: Wrote 12 bytes' in out and '2 calls' in out


async def test_a_message_sent_as_an_approval_arrives_steers_instead_of_crashing(ui):
    """Enter pressed just as the agent asks: the run waits for an answer, so the message steers it (no second run)."""
    session = await ui.sessions.open()
    await ui.sessions.send(session, 'Create hello.txt')
    await session.task
    assert session.approval
    await ui.submit('and make it uppercase')
    assert [e['data']['message'] for e in ui.sessions.store.events_of(session.run_id, 'steer')] == ['and make it uppercase']
    assert session.approval, 'still waiting for the answer'


async def test_a_failing_command_reports_instead_of_closing_the_window(ui, monkeypatch):
    async def broken(arguments):
        raise RuntimeError('disk full')

    said = []
    monkeypatch.setattr(ui, 'cmd_sync', broken)
    monkeypatch.setattr(ui, 'say', lambda text, style='leo.muted': said.append(text))
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        running = asyncio.create_task(ui.run())
        await until(lambda: ui.prompt is not None and ui.prompt.app.is_running)
        typed.send_text('/sync\r')
        await until(lambda: said)
        await until(lambda: ui.prompt.app.is_running)
        typed.send_text('/exit\r')
        await asyncio.wait_for(running, 10)
    assert said == ['/sync failed: disk full']


async def test_a_message_attaches_the_files_it_mentions(ui, project):
    (project / 'notes.md').write_text('use uppercase')
    await ui.submit('Create hello.txt as @notes.md says')
    session = ui.sessions.current
    await session.task
    assert '<file path="notes.md">\nuse uppercase' in session.run['prompt']
    assert 'attached notes.md · 1 line' in text_of(ui.console)
    assert session.title == 'Create hello.txt as @notes.md says', 'the title is what was typed'
    await ui.sessions.close()


async def test_cd_moves_the_window_to_another_folder(ui, project, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'readme.md').write_text('hi')
    await ui.command('/new')
    await ui.command('/cd ../nowhere')
    await ui.command('/cd ../other')
    out = text_of(ui.console).replace('\n', '')  # Long temporary paths wrap.
    assert 'No such folder' in out and f'Now working in {other.resolve()}' in out
    assert ui.sessions.project == str(other.resolve()) and ui.sessions.items == [] and ui.root() == str(other.resolve())
    from prompt_toolkit.document import Document
    from leo_cli.ui import SlashCompleter
    assert [c.text for c in SlashCompleter(ui).get_completions(Document('look at @re'), None)] == ['@readme.md'], '@paths complete in the new folder'


async def test_cd_waits_for_working_sessions(ui, project, tmp_path):
    session = await ui.sessions.open()
    await ui.sessions.send(session, 'Create hello.txt')
    await session.task
    await ui.command(f'/cd {tmp_path}')
    assert 'still working' in text_of(ui.console) and ui.sessions.project == str(project.resolve())
    await ui.sessions.close()


async def test_typing_while_the_agent_works_steers_it(ui):
    session = await ui.sessions.open()
    await ui.sessions.send(session, 'Create hello.txt')
    await ui.submit('use uppercase')
    assert [e['data']['message'] for e in ui.sessions.store.events_of(session.run_id, 'steer')] == ['use uppercase']
    await ui.sessions.close()


async def test_resume_lists_and_reopens_earlier_sessions(ui, account, project):
    first = Sessions(account, project, autorun=True, transport=Model(writes_hello).transport)
    earlier = await first.open()
    await first.send(earlier, 'Create hello.txt')
    await earlier.task
    await ui.command('/resume')
    await ui.command('/resume 1')
    out = text_of(ui.console)
    assert re.search(r'^1\s+Create hello\.txt\s+\d\d \w{3} \d\d:\d\d', out, re.M), 'numbered, with when it was last active'
    assert 'Resumed: Create hello.txt' in out and 'Done: Wrote' in out
    assert ui.sessions.current.id == earlier.id


# -- rendering ------------------------------------------------------------------

def test_events_render_as_one_line_per_tool_call():
    console = Console(record=True, width=120, color_system=None)
    renderer = Renderer(console)
    renderer.event('agent_step', {'agent': 'main', 'text': 'Checking the tests.', 'tools': [{'id': 'b', 'name': 'bash', 'summary': 'pytest -q'}]})
    renderer.event('agent_tool', {'agent': 'main', 'name': 'bash', 'ok': True, 'output': 'exit code 1\nF.\nFAILED test_x\n1 failed'})
    renderer.event('agent_step', {'agent': 'sub-1a2b3c4d', 'text': '', 'tools': [{'id': 'r', 'name': 'read_file', 'summary': 'app.py'}]})
    renderer.event('agent_tool', {'agent': 'sub-1a2b3c4d', 'name': 'read_file', 'ok': False, 'output': 'Error: No such file: app.py'})
    renderer.event('todos', {'items': [{'content': 'Fix test', 'status': 'in_progress'}, {'content': 'Ship', 'status': 'pending'}]})
    renderer.event('done', {'status': 'failed', 'error': 'Model call limit reached', 'usage': {'calls': 3, 'input_tokens': 1500, 'output_tokens': 200}})
    out = text_of(console)
    assert 'bash(pytest -q)' in out and 'exit code 1 · 3 lines of output' in out and 'FAILED test_x' in out
    assert f'{renderer.g.rail} #1a2b {renderer.g.dot} read_file(app.py)' in out and 'No such file' in out, 'sub-agent steps sit on its rail'
    assert f'{renderer.g.now} Fix test' in out and f'{renderer.g.idle} Ship' in out and '0/2' in out
    assert 'Model call limit reached' in out and '3 calls · 1.7K tokens' in out


def test_approvals_show_the_command_or_the_change():
    from leo_cli import theme
    console = theme.apply(Console(record=True, width=100, color_system=None))
    console.print(approval_view({'kind': 'edit', 'message': 'Edit a.py?', 'detail': {'path': 'a.py', 'old': 'x = 1', 'new': 'x = 2'}}))
    console.print(approval_view({'kind': 'command', 'message': 'Run this command?', 'detail': {'command': 'sudo rm x', 'risky': True}}))
    out = text_of(console)
    assert '-x = 1' in out and '+x = 2' in out and 'sudo rm x' in out and 'never remembered' in out


def test_suggested_prefixes_keep_the_program_and_subcommand():
    assert suggested_prefix('npm test -- --watch=false') == 'npm test'
    assert suggested_prefix('pytest -q tests/') == 'pytest'
    assert suggested_prefix('python ./manage.py migrate') == 'python'
    assert suggested_prefix('python3 greet.py && ls -la') == 'python3 greet.py && ls -la', 'a chained command is remembered whole'


def test_approval_answers_are_whole_words_never_a_first_letter():
    options = {'y': 'once', 's': 'session', 'p': 'project', 'n': 'deny'}
    assert [parse_answer(t, options) for t in ('y', ' Y\n', 'yes', 'project', 'n', 'no', '1', '3', '4')] == ['y', 'y', 'y', 'p', 'n', 'n', 'y', 'p', 'n']
    assert parse_answer('5', options) is None and parse_answer('0', options) is None
    assert parse_answer('please also run the tests', options) is None, 'a steer message typed as the question appeared'
    assert parse_answer('yes please', options) is None and parse_answer('', options) is None
    assert parse_answer('s', {'y': 'once', 'n': 'deny'}) is None, 'only the keys this approval offers'


def test_parallel_results_say_which_call_they_belong_to_and_the_plan_shows_once():
    console = Console(record=True, width=120, color_system=None)
    renderer = Renderer(console)
    renderer.event('agent_step', {'agent': 'main', 'tools': [{'name': 'todo_write', 'summary': '2 items'}]})
    renderer.event('todos', {'items': [{'content': 'Read', 'status': 'in_progress'}, {'content': 'Ship', 'status': 'pending'}]})
    renderer.event('agent_tool', {'agent': 'main', 'name': 'todo_write', 'ok': True, 'output': 'Todo list updated: 0 of 2 completed.'})
    renderer.event('agent_step', {'agent': 'main', 'tools': [{'name': 'read_file', 'summary': 'a.py'}, {'name': 'list_files', 'summary': '.'}]})
    renderer.event('agent_tool', {'agent': 'main', 'name': 'read_file', 'summary': 'a.py', 'ok': True, 'output': '1\tx = 1'})
    renderer.event('agent_tool', {'agent': 'main', 'name': 'list_files', 'summary': '.', 'ok': True, 'output': 'a.py (6 bytes)\nb/'})
    renderer.event('agent_step', {'agent': 'main', 'tools': [{'name': 'bash', 'summary': 'pytest -q'}]})
    renderer.event('agent_tool', {'agent': 'main', 'name': 'bash', 'ok': True, 'output': 'exit code 0\n..\n\n2 passed in 0.1s'})
    out = text_of(console)
    assert 'todo_write' not in out and 'Todo list updated' not in out and '0/2' in out, 'the plan, not the tool call that set it'
    assert f'{renderer.g.hook} a.py {renderer.g.sep} 1 line' in out and f'{renderer.g.hook} list_files {renderer.g.sep} 2 entries' in out
    assert f'{renderer.g.hook} exit code 0 {renderer.g.sep} 2 lines of output' in out and '2 passed in 0.1s' in out, 'the end of a success shows'


# -- the interactive prompt, typed into through a pipe ------------------------------------

async def until(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        await asyncio.sleep(0.05)


async def test_interactive_task_approval_and_exit(account, project):
    sessions = Sessions(account, project, transport=Model(writes_hello).transport)
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        ui = App(sessions, account, Console(color_system=None))
        running = asyncio.create_task(ui.run())
        await until(lambda: ui.prompt is not None and ui.prompt.app.is_running)
        typed.send_text('Create hello.txt\r')
        # The approval question takes over the prompt as soon as the agent asks it.
        await until(lambda: sessions.current is not None and sessions.current.approval is not None)
        await until(lambda: sessions.current.approval['id'] in ui.notified)
        await asyncio.sleep(0.6)  # Keys in the first moment of a question are ignored.
        typed.send_text('1')  # A number chooses at once: 1. Yes.
        await until(lambda: sessions.current.status == 'completed')
        assert (project / 'hello.txt').read_text() == 'hi from leo\n'
        await until(lambda: ui.prompt.app.is_running)
        typed.send_text('/exit\r')
        await asyncio.wait_for(running, 10)


def test_update_upgrades_both_packages_with_uv(monkeypatch):
    from leo_cli import theme, update
    console = theme.apply(Console(record=True, width=120, color_system=None))
    ran = []
    monkeypatch.setattr(update, 'find_uv', lambda: 'uv')
    assert update.update(console, run=lambda command: ran.append(command) or 0) == 0
    assert ran == [['uv', 'tool', 'upgrade', 'leo-cli', '--reinstall-package', 'leo-cli', '--reinstall-package', 'leo-orchestrator']]
    assert 'up to date' in text_of(console)
    monkeypatch.setattr(update, 'find_uv', lambda: None)
    assert update.update(console, run=lambda command: 0) == 1 and 'npm install -g leo-orchestrator' in text_of(console)


# -- the redesigned screens -------------------------------------------------------------

async def test_help_and_model_names_keep_their_brackets(ui, account):
    """Text from commands and models is never read as rich markup: "[add]" and "[local]" show as typed."""
    from backend.schemas import Connection
    account.store().save_connection(Connection(name='Ollama · qwen3 [local]', protocol='openai', base_url='http://localhost:11434/v1',
                                               model='qwen3', local=True, free=True).model_dump(), user_id=account.user_id)
    await ui.command('/help')
    await ui.command('/models')
    out = text_of(ui.console)
    for usage in ('/models [add]', '/login [server]', '/new [--worktree]', '/theme [name]', '/autorun on|off [--save]'):
        assert usage in out, usage
    assert 'Sessions' in out and 'Keys' in out, 'grouped'
    assert 'Ollama · qwen3 [local]' in out


async def test_one_shot_shows_which_keys_answer_an_approval(account, project, capsys):
    sessions = Sessions(account, project, transport=Model(writes_hello).transport)
    assert await app.one_shot(sessions, 'Create hello.txt', stdin=Answers('maybe\n1\n')) == 0
    err = capsys.readouterr().err
    assert ' 1  yes' in err and ' 2  yes, this session' in err and ' 3  yes, always in this project' in err and ' 4  no' in err
    assert 'Answer with a number, 1 to 4.' in err, 'a wrong answer says what to type'
    assert re.search(r'done . \d+ calls . ', err), 'stderr ends with what the run cost; the answer alone goes to stdout'


def test_without_unicode_everything_is_plain_ascii():
    console = Console(record=True, width=100, color_system=None)
    renderer = Renderer(console, unicode=False)
    renderer.event('todos', {'items': [{'content': 'A', 'status': 'completed'}, {'content': 'B', 'status': 'in_progress'}]})
    renderer.event('agent_step', {'agent': 'main', 'tools': [{'name': 'bash', 'summary': 'ls'}]})
    renderer.event('agent_tool', {'agent': 'main', 'name': 'bash', 'ok': False, 'output': 'boom'})
    renderer.event('agent_step', {'agent': 'sub-1a2b3c4d', 'tools': [{'name': 'read_file', 'summary': 'a.py'}]})
    renderer.event('warning', {'message': 'slow'})
    renderer.event('done', {'status': 'completed', 'output': 'ok', 'usage': {'calls': 1, 'input_tokens': 5, 'output_tokens': 5}})
    console.print(approval_view({'kind': 'command', 'message': 'Run?', 'detail': {'command': 'ls'}}, unicode=False))
    out = text_of(console)
    assert out.isascii(), [c for c in out if not c.isascii()]
    assert '* bash(ls)' in out and '| #1a2b * read_file(a.py)' in out and '! slow' in out


def test_slash_commands_complete_with_what_they_do(ui, project):
    from prompt_toolkit.document import Document
    from leo_cli.ui import SlashCompleter
    folder = project / '.leo-cli' / 'skills' / 'greet'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text('---\nname: greet\ndescription: Say hello\n---\nWrite hello.txt.\n')

    def complete(text):
        return [(c.text, c.display_meta_text) for c in SlashCompleter(ui).get_completions(Document(text), None)]

    assert ('/models', 'list your models, or add some (account sign-in or API key)') in complete('/mo')
    assert [text for text, _ in complete('/models ')] == ['add', 'rerank'] and [text for text, _ in complete('/model a')] == ['add'], 'and what follows it'
    assert complete('/gr') == [('/greet', 'Say hello')], 'your skills complete too'
    assert complete('fix the tests') == [] and complete('/models add ') == [], 'only at the start of the message'


async def test_the_status_bar_lists_sessions_and_the_keys_that_matter(ui):
    def bar():
        return ''.join(text for _, text in ui.toolbar())

    assert 'no session yet' in bar() and '/help' in bar()
    await ui.command('/new')
    assert f'{ui.g.idle} 1 new · idle' in bar() and 'asks before edits' in bar()
    ui.sessions.policy.set_autorun(True)
    assert 'autorun on' in bar()


async def test_the_status_bar_fits_a_narrow_terminal(ui, monkeypatch):
    from prompt_toolkit.utils import get_cwidth
    from leo_cli import ui as module
    monkeypatch.setattr(module, 'get_app', lambda: (_ for _ in ()).throw(RuntimeError()))
    session = await ui.command('/new') or ui.sessions.current
    session.title = 'A long title that would never fit on a narrow terminal at all'
    for width in (50, 64, 80, 120):
        monkeypatch.setattr(module.shutil, 'get_terminal_size', lambda width=width: SimpleNamespace(columns=width))
        bar = ''.join(text for _, text in ui.toolbar())
        assert get_cwidth(bar) <= width, (width, bar)
        assert '/help' in bar and 'tab complete' in bar, 'the keys stay visible'


def test_the_welcome_card_fits_the_terminal(account, project):
    from leo_cli import theme
    sessions = Sessions(account, project)

    def shown(width):
        console = theme.apply(Console(record=True, width=width, color_system=None))
        console.print(app.welcome(account, sessions, width=width))
        return text_of(console)

    wide, narrow = shown(120), shown(36)
    logo = BANNER[4]
    assert logo in wide and 'project' in wide and 'models' in wide and 'mode' in wide
    assert logo not in narrow and 'L.E.O.C.O.D.E' in narrow, 'no logo when it would not fit'
    assert all(len(line) <= 36 for line in narrow.splitlines())
    assert all(len(line) <= 64 for line in shown(64).splitlines()), 'the key tips stay on one line'
    assert project.name in narrow and project.name in wide, 'a long project path keeps its folder name'


# -- streaming, the live status line, and the approval menu ------------------------------------

def test_streamed_markdown_is_split_only_where_it_is_safe():
    assert split_blocks('Hello\n\nWor') == ('Hello\n\n', 'Wor')
    assert split_blocks('a\n\n```py\nx = 1\n\ny = 2\n') == ('a\n\n', '```py\nx = 1\n\ny = 2\n'), 'never inside a code block'
    assert split_blocks('```py\nx = 1\n```\nafter') == ('```py\nx = 1\n```\n', 'after'), 'a closed code block is finished'
    assert split_blocks('no blank line yet\n') == ('', 'no blank line yet\n')


def test_the_answer_streams_in_once():
    console = Console(record=True, width=100, color_system=None)
    renderer = Renderer(console)
    for chunk in ('Checking the', ' tests.\n\nThey', ' pass now.'):
        renderer.event('agent_text', {'stage': 'agent:main:1', 'text': chunk})
    assert 'Checking the tests.' in text_of(console) and 'They pass' not in text_of(console), 'only finished blocks print'
    assert renderer.preview == 'They pass now.' and renderer.phase == 'writing'
    renderer.event('agent_step', {'agent': 'main', 'step': 1, 'text': 'Checking the tests.\n\nThey pass now.', 'tools': []})
    renderer.event('done', {'status': 'completed', 'output': 'Checking the tests.\n\nThey pass now.', 'usage': {}})
    out = text_of(console)
    assert out.count('Checking the tests.') == 1 and out.count('They pass now.') == 1, 'streamed, never printed again'
    assert renderer.preview == '' and renderer.answer is None


def test_text_before_tool_calls_streams_and_a_fallback_starts_over():
    console = Console(record=True, width=100, color_system=None)
    renderer = Renderer(console)
    renderer.event('agent_text', {'stage': 'agent:main:1', 'text': 'Let me look.\n\n'})
    renderer.event('agent_text', {'stage': 'agent:main:1', 'reset': True})
    renderer.event('agent_text', {'stage': 'agent:main:1', 'text': 'Looking now.'})
    renderer.event('agent_step', {'agent': 'main', 'step': 1, 'text': 'Looking now.', 'tools': [{'name': 'read_file', 'summary': 'a.py'}]})
    renderer.event('done', {'status': 'completed', 'output': 'All good.', 'usage': {}})
    out = text_of(console)
    assert 'another one starts the answer again' in out and out.count('Looking now.') == 1 and 'read_file(a.py)' in out
    assert 'All good.' in out, 'an answer that did not stream is printed at the end'


async def test_the_status_bar_shows_a_live_line_while_the_agent_works(ui, monkeypatch):
    from leo_cli import ui as module
    monkeypatch.setattr(module, 'get_app', lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(module.shutil, 'get_terminal_size', lambda: SimpleNamespace(columns=100))
    session = await ui.sessions.open()
    monkeypatch.setattr(type(session), 'busy', property(lambda self: True))
    ui.renderer.started = time.monotonic() - 5
    ui.renderer.event('agent_text', {'stage': 'agent:main:1', 'text': 'Writing the fix'})
    live, bar = ''.join(text for _, text in ui.toolbar()).split('\n')
    assert 'Writing…' in live and '5s' in live and 'Writing the fix' in live and any(c in live for c in '▁▂▃▄▅▆▇█')
    assert 'esc' in bar and 'steer' in bar
    ui.renderer.event('agent_step', {'agent': 'main', 'step': 1, 'tools': [{'name': 'bash', 'summary': 'pytest -q'}]})
    assert 'Running bash(pytest -q)…' in ''.join(text for _, text in ui.toolbar())


async def test_approvals_are_a_menu_that_ignores_keys_typed_too_soon():
    approval = {'kind': 'command', 'detail': {'command': 'npm test -- --watch'}}

    async def answer(*steps):
        """Keys typed after the given delays, from when the menu appears."""
        with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
            asking = asyncio.create_task(choose_answer(approval, 'npm test', guard=0.3))
            for delay, key in steps:
                await asyncio.sleep(delay)
                typed.send_text(key)
            return await asyncio.wait_for(asking, 5)

    assert await answer((0.05, '\r'), (0.4, '\x1b[B'), (0.05, '\r')) == 'session', 'an Enter in the first moment does nothing'
    assert await answer((0.05, '4'), (0.4, '3')) == 'project', 'a number chooses at once, once the moment has passed'
    assert await answer((0.05, '\x1b')) == 'deny', 'Esc says no'
    from leo_cli.ui import menu_labels
    assert dict(menu_labels(approval, 'npm test'))['session'] == 'Yes, and allow commands starting with `npm test` for the rest of this session'
    assert [d for d, _ in menu_labels({'kind': 'command', 'detail': {'command': 'sudo x', 'risky': True}})] == ['once', 'deny']


# -- adding models, as on the website ---------------------------------------------------------

async def tick(options, *keys, **kwargs):
    from leo_cli.ui import pick_many
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        for key in keys:
            typed.send_text(key)
        return await asyncio.wait_for(pick_many('Which?', options, **kwargs), 5)


async def test_the_checklist_ticks_filters_and_cancels():
    options = [('a/one', 'One'), ('a/two', 'Two'), ('b/three', 'Three'), ('type', 'Type a model ID')]
    assert await tick(options, ENTER) == ['a/one'], 'nothing ticked: the highlighted row'
    assert await tick(options, ' ', DOWN, DOWN, ' ', ENTER) == ['a/one', 'b/three']
    assert await tick(options, ' ', ' ', DOWN, ENTER) == ['a/two'], 'space unticks'
    assert await tick(options, 'thr', ' ', '\x7f\x7f\x7f', ENTER) == ['b/three'], 'a tick survives changing the filter'
    assert await tick(options, 'zzz', ENTER, keep='type') == ['type'], 'the kept row stays visible'
    assert await tick(options, ' ', '\x1b') is None, 'Esc cancels, ticks and all'


def discovered(*names):
    return {'models': [{'model': n, 'name': f'{n} (free)', 'tier': 'standard', 'skills': [], 'reasoning': False, 'tools': True,
                        'context_window': 65536, 'free': True} for n in names]}


async def test_more_models_reuse_the_saved_key_and_sync_once(tmp_path, monkeypatch):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    from backend.schemas import Connection
    from leo_cli import models
    from leo_cli.account import Account
    account = Account(tmp_path / 'home')
    account.store().save_connection(Connection(name='OpenRouter · First', base_url='https://openrouter.example/v1', model='m/first', free=True,
                                                tools=True, api_key='sk-saved').model_dump(), user_id=account.user_id)
    account.store().save_connection(Connection(name='Codex', base_url='http://leo.test/api/cli/relay/1', model='cx/gpt', free=True,
                                                api_key='device-token').model_dump(), user_id=account.user_id)
    added, synced = [], []

    async def discover(connection, key, transport=None):
        assert key == 'sk-saved' and connection['base_url'] == 'https://openrouter.example/v1'
        return discovered('m/first', 'm/second', 'm/third', 'm/fourth')

    async def add_connection(body, sync=True):
        assert sync is False
        added.append(body)

    async def sync():
        synced.append(True)

    monkeypatch.setattr(models, 'discover', discover)
    monkeypatch.setattr(account, 'add_connection', add_connection)
    monkeypatch.setattr(account, 'sync', sync)
    console = theme_console()
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(DOWN + ENTER)  # Account sign-in, then OpenRouter's saved key (relayed models have none here).
        await asyncio.sleep(0.3)
        typed.send_text(' ' + DOWN + ' ' + ENTER)  # m/fourth and m/second, in the order of the list.
        assert await asyncio.wait_for(models.add_model(account, console), 10) is True
    assert [b['model'] for b in added] == ['m/fourth', 'm/second'], 'what is already added is not offered'
    assert added[0]['name'] == 'OpenRouter · m/fourth' and added[0]['api_key'] == 'sk-saved' and added[0]['free'] is True
    assert synced == [True] and 'Added to your L.E.O. account: OpenRouter · m/fourth, OpenRouter · m/second.' in text_of(console)


class FakeAccounts:
    """What the server answers for account sign-in."""

    def __init__(self, available=True, finish='done'):
        self.available, self.finish, self.polls, self.started, self.synced, self.cancelled = available, finish, 0, [], 0, []

    async def model_accounts(self):
        return {'available': self.available, 'error': None if self.available else 'Model-account sign-in needs the sandbox service',
                'providers': [{'id': 'kimi', 'name': 'Kimi', 'flow': 'device', 'risk': None},
                              {'id': 'github', 'name': 'GitHub Copilot', 'flow': 'device', 'risk': 'May be restricted.'}],
                'accounts': []}

    async def start_sign_in(self, provider, accept_risk=False):
        self.started.append((provider, accept_risk))
        return {'id': 's1', 'provider': provider, 'status': 'waiting', 'error': None, 'url': 'https://sign.example/device', 'user_code': 'ABCD-1234', 'result': None}

    async def sign_in_status(self, session_id):
        self.polls += 1
        if self.polls < 2:
            return {'id': 's1', 'status': 'waiting'}
        if self.finish == 'hang':
            await asyncio.sleep(30)
        return {'id': 's1', 'status': self.finish, 'error': 'It was denied' if self.finish == 'error' else None,
                'result': {'account': 'me@example.com', 'added': 4}}

    async def cancel_sign_in(self, session_id):
        self.cancelled.append(session_id)

    async def sync(self):
        self.synced += 1


async def test_signing_in_with_an_account_from_the_cli(monkeypatch):
    from leo_cli import models
    opened = []
    monkeypatch.setattr(models.webbrowser, 'open', lambda url: opened.append(url) or True)
    fake, console = FakeAccounts(), theme_console()
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(ENTER)  # Kimi.
        assert await asyncio.wait_for(models.sign_in_account(fake, console, poll_seconds=0), 10) is True
    out = text_of(console)
    assert fake.started == [('kimi', False)] and opened == ['https://sign.example/device'] and fake.synced == 1
    assert 'ABCD-1234' in out and 'https://sign.example/device' in out and 'Signed in to Kimi as me@example.com: 4 models added.' in out


async def test_account_sign_in_needs_the_risk_accepted_and_the_sandbox():
    from leo_cli import models
    fake, console = FakeAccounts(), theme_console()
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(DOWN + ENTER)  # GitHub Copilot.
        await asyncio.sleep(0.3)
        typed.send_text(ENTER)  # The default answer: No.
        assert await asyncio.wait_for(models.sign_in_account(fake, console), 10) is False
    assert fake.started == [] and 'May be restricted.' in text_of(console)
    fake, console = FakeAccounts(available=False), theme_console()
    assert await models.sign_in_account(fake, console) is False and 'needs the sandbox service' in text_of(console)


async def test_a_failed_or_cancelled_sign_in_says_so(monkeypatch):
    from leo_cli import models
    monkeypatch.setattr(models.webbrowser, 'open', lambda url: True)
    fake, console = FakeAccounts(finish='error'), theme_console()
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(ENTER)
        assert await asyncio.wait_for(models.sign_in_account(fake, console, poll_seconds=0), 10) is False
    assert 'It was denied' in text_of(console) and fake.synced == 0
    fake, console = FakeAccounts(finish='hang'), theme_console()
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text(ENTER)
        task = asyncio.create_task(models.sign_in_account(fake, console, poll_seconds=0))
        await until(lambda: fake.polls == 2)
        task.cancel()  # Ctrl-C.
        assert await task is False
    assert fake.cancelled == ['s1'] and 'Sign-in cancelled.' in text_of(console)


async def test_model_and_models_list_and_point_at_adding(ui):
    await ui.command('/model')
    await ui.command('/models')
    out = text_of(ui.console)
    assert 'Unknown command' not in out and out.count('/models add') == 2


# -- the tree of agents on the screen --------------------------------------------------------

async def test_the_prompt_shows_the_tree_while_working(ui, monkeypatch):
    from leo_cli import ui as module
    monkeypatch.setattr(module, 'get_app', lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(module.shutil, 'get_terminal_size', lambda: SimpleNamespace(columns=100, lines=40))
    session = await ui.sessions.open()
    ui.renderer.event('tree', {'id': 'main', 'parent': None, 'kind': 'root', 'task': 'all of it', 'model': 'strong-m', 'status': 'planning'})
    ui.renderer.event('tree', {'id': 'sub-1', 'parent': 'main', 'kind': 'worker', 'task': 'Write the tests', 'model': 'light-m', 'status': 'working'})
    idle = ''.join(text for _, text in ui.message())
    assert 'Write the tests' not in idle
    monkeypatch.setattr(type(session), 'busy', property(lambda self: True))
    busy = ''.join(text for _, text in ui.message())
    assert 'Write the tests' in busy and busy.endswith(f'steer {ui.g.prompt} ') and busy.index('Write the tests') < busy.index('steer')


def test_the_answer_block_is_not_printed_twice():
    console = Console(record=True, width=100, color_system=None)
    renderer = Renderer(console)
    renderer.event('agent_text', {'stage': 'agent:main:1', 'text': 'The work is done.'})
    renderer.event('agent_step', {'agent': 'main', 'step': 1, 'text': 'The work is done.', 'tools': []})
    renderer.event('done', {'status': 'completed', 'output': 'The work is done.\n\n**Not finished:**\n- Docs (failed)', 'usage': {}})
    out = text_of(console)
    assert out.count('The work is done.') == 1 and out.count('Not finished:') == 1 and 'Docs (failed)' in out


def test_a_finished_run_forgets_its_tree():
    renderer = Renderer(Console(file=io.StringIO()))
    renderer.event('tree', {'id': 'main', 'parent': None, 'kind': 'root', 'task': 'x', 'status': 'planning'})
    renderer.event('done', {'status': 'completed', 'output': 'ok', 'usage': {}})
    assert renderer.tree == {}


# -- the name ----------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def test_leocode_and_leo_are_both_commands():
    scripts = tomllib.loads((ROOT / 'leo-cli' / 'pyproject.toml').read_text())['project']['scripts']
    assert scripts['leocode'] == scripts['leo'] == 'leo_cli.app:main'
    bins = json.loads((ROOT / 'leo-cli' / 'npm' / 'package.json').read_text())['bin']
    assert bins['leocode'] == bins['leo'] == 'bin/leo.js'
    assert app.parser().prog == 'leocode'


def test_the_logo_fits_at_eighty_columns(account, project):
    from leo_cli import theme
    from leo_cli.ui import BANNER
    sessions = Sessions(account, project)

    def shown(width):
        console = theme.apply(Console(record=True, width=width, color_system=None))
        console.print(app.welcome(account, sessions, width=width))
        return text_of(console)

    assert len(BANNER) == 6 and max(len(line) for line in BANNER) <= 78
    wide, narrow = shown(80), shown(79)
    assert BANNER[4] in wide and all(len(line) <= 80 for line in wide.splitlines())
    assert BANNER[4] not in narrow and 'L.E.O.C.O.D.E' in narrow


def test_nothing_says_the_old_name():
    files = [*(ROOT / 'leo-cli' / 'leo_cli').glob('*.py'), *(ROOT / 'backend').glob('*.py'), ROOT / 'frontend/src/components/AuthPage.astro', ROOT / 'frontend/src/scripts/chat.ts',
             *[ROOT / 'frontend/src/pages' / name for name in ('cli.astro', 'usage.astro', 'index.astro')], ROOT / 'leo-cli/npm/bin/leo.js', ROOT / 'leo-cli/npm/README.md']
    for path in files:
        assert 'L.E.O CLI' not in path.read_text(), path
    assert 'Soon' not in (ROOT / 'frontend/src/components/AuthPage.astro').read_text()


async def test_the_completion_menu_keeps_no_room_while_the_agent_works(ui, monkeypatch):
    """prompt_toolkit keeps 8 rows free while it completes as you type, which on a short terminal would clip the tree."""
    session = await ui.sessions.open()
    assert ui.completes_while_typing() is True
    monkeypatch.setattr(type(session), 'busy', property(lambda self: True))
    assert ui.completes_while_typing() is False


# -- the tree as a graph ------------------------------------------------------------------------

@pytest.fixture
def tree_ui(tmp_path, monkeypatch, project):
    from test_tree import ALL
    from backend.schemas import Connection
    from leo_cli.account import Account
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    home = Account(tmp_path / 'home-tree')
    for model in ALL:
        home.store().save_connection(Connection(name=model, base_url=f'http://{model}.test/v1', model=model, free=True, tools=True,
                                                tier=model.split('-')[0]).model_dump(), user_id=home.user_id)

    def make(team):
        return App(Sessions(home, project, autorun=True, transport=team.transport), home, Console(record=True, width=120, color_system=None))
    return make


async def test_tree_command_draws_the_graph_with_a_note_for_a_root_alone(tree_ui):
    from test_tree import Team
    ui = tree_ui(Team({'ROOT': ['An answer.']}))
    await ui.command('/tree')
    assert 'No task yet' in text_of(ui.console)
    session = await ui.sessions.open()
    await ui.sessions.send(session, 'ROOT task')
    await session.task
    await ui.command('/tree')
    out = text_of(ui.console)
    assert 'ROOT' in out and '+--' in out and 'answered this one itself' in out
    await ui.command('/usage')
    assert text_of(ui.console).count('answered this one itself') == 2, '/usage shows it too'
    await ui.sessions.close()


async def test_a_question_says_the_root_answered_it(tree_ui):
    from test_tree import Team
    ui = tree_ui(Team({'ROOT': ['An answer.']}))
    session = await ui.sessions.open()
    await ui.sessions.send(session, 'ROOT task')
    await session.task
    await ui.command('/tree')
    assert 'answered this one itself' in text_of(ui.console)
    await ui.sessions.close()


async def test_a_real_tree_prints_its_graph_when_the_task_ends(tree_ui):
    from test_tree import Team, accept, delegate
    ui = tree_ui(Team({'ROOT': [[delegate('child work')], [accept('child work')], 'Done.'], 'child work': ['ok']}))
    session = await ui.sessions.open()
    await ui.sessions.send(session, 'ROOT task')
    await session.task
    ui.follow(session)
    out = text_of(ui.console)
    assert out.count('child work') >= 1 and 'WORKER' in out and 'accepted' in out and out.index('Done.') < out.index('WORKER')
    await ui.sessions.close()


async def test_a_root_alone_prints_no_graph_by_itself(tree_ui):
    from test_tree import Team
    ui = tree_ui(Team({'ROOT': ['An answer.']}))
    session = await ui.sessions.open()
    await ui.sessions.send(session, 'ROOT task')
    await session.task
    ui.follow(session)
    assert 'ROOT' not in text_of(ui.console) and '+--' not in text_of(ui.console)
    await ui.sessions.close()


async def test_the_root_row_names_its_model_before_its_first_step_ends():
    renderer = Renderer(Console(file=io.StringIO()))
    renderer.event('tree', {'id': 'main', 'parent': None, 'kind': 'root', 'task': 'x', 'status': 'planning', 'model': None})
    renderer.event('model', {'stage': 'agent:main:1', 'model': 'vendor/strong-m:free'})
    assert renderer.tree['main']['model'] == 'vendor/strong-m:free'


async def test_models_rerank_asks_the_server_then_syncs(ui, account, monkeypatch):
    asked = []

    async def rerank():
        asked.append(True)
        return {'changed': 3}

    monkeypatch.setattr(account, 'rerank_models', rerank)
    await ui.command('/models rerank')
    assert asked == [True] and '3 models' in text_of(ui.console)
