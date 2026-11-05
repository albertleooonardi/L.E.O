"""The `leo` command and its terminal UI: one-shot runs, approvals, slash commands, and event rendering."""
import asyncio
import io
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from sqlalchemy import update

from backend.store import runs
from leo_cli import app
from leo_cli.sessions import Sessions
from leo_cli.ui import App, Renderer, approval_view, suggested_prefix
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


async def test_no_models_means_sign_in_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    from leo_cli.account import Account
    assert await app.dispatch(args('-p', 'hi', '--project', str(tmp_path)), Account(tmp_path / 'empty')) == 1
    assert 'leo login' in capsys.readouterr().out


DOWN, ENTER = '\x1b[B', '\r'


async def test_the_first_run_signs_in_inside_leo(tmp_path, monkeypatch):
    """No `leo login` step: plain `leo` on a terminal shows the welcome screen; quitting there exits cleanly."""
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
    assert '███████╗██╗███████╗██╗╚██████╔╝' in shown and 'Theme: Light' in shown


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
    added = []

    async def discover(connection, key, transport=None):
        assert connection['base_url'] == 'http://localhost:11434/v1' and key is None
        return {'models': [{'model': 'qwen3-coder', 'name': 'qwen3-coder', 'tier': 'standard', 'skills': ['coding'], 'reasoning': False,
                            'tools': True, 'context_window': 65536, 'free': True}]}

    async def add_connection(body):  # Saved on the account and synced down (tested in test_account).
        added.append(body)
        account.store().save_connection(Connection(**body).model_dump(), user_id=account.user_id)

    monkeypatch.setattr(models, 'discover', discover)
    monkeypatch.setattr(account, 'add_connection', add_connection)
    console = Console(record=True, width=120, color_system=None)
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        for keys in (ENTER, ENTER, ENTER, ENTER, ENTER):  # Add a model, Ollama, its URL, no key, qwen3-coder.
            typed.send_text(keys)
            await asyncio.sleep(0.3)
        assert await asyncio.wait_for(app.ready(account, console, interactive=True), 10) is True
    [body] = added
    assert body['model'] == 'qwen3-coder' and body['tools'] is True and body['local'] is True and body['api_key'] is None
    assert 'Your account has no models this computer can use yet.' in text_of(console)


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
    assert session.status == 'completed' and 'Waiting for the previous leo window' in notes[0]


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
    assert session.status == 'running' and 'another leo window' in notes[-1]


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
    assert '[1] new session' in out and '› [2] new session' in out and 'Unknown command /nonsense' in out
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
    assert '1. Create hello.txt' in out and 'Resumed: Create hello.txt' in out and 'Done: Wrote' in out
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
    assert '1a2b3c4d' in out and 'read_file(app.py)' in out and 'No such file' in out
    assert '[>] Fix test' in out and '[ ] Ship' in out
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
        await asyncio.sleep(0.2)
        typed.send_text('y\r')
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
