"""Terminal UX boundaries: narrow layouts, keyboard cancellation, and managed provider setup."""
import asyncio
import pytest

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from rich.console import Console

from leo_cli import models, theme
from leo_cli.ui import fit, glyphs, pick, pick_many
from leo_cli.views import ModelList
from leo_cli.account import Account, AccountError


async def test_escape_backs_out_of_a_menu():
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text('\x1b')
        assert await asyncio.wait_for(pick('Choose', [('a', 'First'), ('b', 'Second')]), 3) is None


@pytest.mark.parametrize('server', ['localhost:8000', 'https://', 'http://localhost:abc', 'ftp://leo.example', 'http://user:secret@localhost'])
async def test_invalid_server_addresses_have_an_actionable_error(tmp_path, monkeypatch, server):
    monkeypatch.setenv('LEO_CLI_KEYRING', 'file')
    account = Account(tmp_path / 'account')
    with pytest.raises(AccountError, match='Use a server address'):
        await account.login(server)


async def test_checklist_bulk_selection_is_limited_and_survives_search():
    choices = [('a', 'Alpha'), ('b', 'Beta'), ('c', 'Gamma')]
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text('\x01\r')  # Ctrl-A selects visible, limited to two.
        assert await asyncio.wait_for(pick_many('Models', choices, limit=2), 3) == ['a', 'b']
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        typed.send_text('Gam \x15\r')  # Filter, tick Gamma, clear filter, keep the tick.
        assert await asyncio.wait_for(pick_many('Models', choices), 3) == ['c']


def test_fragment_clipping_handles_tiny_terminals_and_wide_characters():
    for width in range(0, 20):
        result = fit([('', '代码 /help and more')], width)
        assert get_cwidth(''.join(text for _, text in result)) <= width


def test_model_list_is_readable_at_small_widths_and_groups_relayed_models():
    connections = [{'name': f'OpenRouter · Model {n}', 'model': f'openrouter/vendor/model-{n}',
                    'base_url': f'http://leo.test/api/cli/relay/{n}', 'enabled': n == 0, 'free': True,
                    'local': False, 'tier': 'standard', 'tools': True, 'skills': ['coding']} for n in range(2)]
    for width in (24, 36, 60, 80, 120):
        console = theme.apply(Console(record=True, width=width, color_system=None))
        console.print(ModelList(connections, glyphs(unicode=True)))
        text = console.export_text()
        assert all(get_cwidth(line) <= width for line in text.splitlines())
        assert text.count('OpenRouter') == 1 and 'Model 0' in text and 'Model 1' in text
        assert 'http://' not in text and 'off' in text and 'yes' in text
    console = theme.apply(Console(record=True, width=80, color_system=None))
    console.print(ModelList(connections, glyphs(), 'not-here'))
    assert 'No models match' in console.export_text()


class ManagedAccount:
    def __init__(self):
        self.connected, self.selected = [], []

    async def connect_model_provider(self, provider, key):
        self.connected.append((provider, key))

    async def provider_models(self, provider):
        return {'name': 'OpenRouter', 'models': [
            {'model': 'openrouter/vendor/new:free', 'name': 'New model', 'tools': True, 'free': True},
            {'model': 'openrouter/vendor/old:free', 'name': 'Existing model', 'tools': True, 'free': True, 'added': True},
            {'model': 'openrouter/vendor/paid', 'name': 'Paid model', 'tools': True, 'free': False},
        ]}

    async def select_provider_models(self, provider, selected):
        self.selected.append((provider, selected))
        return {'created': [{'id': 'new'}]}


async def test_cli_key_setup_uses_router_and_selects_only_new_free_models(monkeypatch):
    account, choices = ManagedAccount(), iter(['key', 'openrouter', 'free'])

    async def choose(*args, **kwargs):
        return next(choices)

    async def key(*args, **kwargs):
        assert kwargs.get('secret') is True
        return 'upstream-secret'

    async def checklist(title, options, **kwargs):
        assert kwargs['limit'] == 60
        assert [value for value, _ in options] == ['openrouter/vendor/new:free']
        return [options[0][0]]

    monkeypatch.setattr(models, 'pick', choose)
    monkeypatch.setattr(models, 'ask', key)
    monkeypatch.setattr(models, 'pick_many', checklist)
    console = theme.apply(Console(record=True, width=80, color_system=None))
    offered = {'available': True, 'accounts': [], 'key_providers': [{'id': 'openrouter', 'name': 'OpenRouter'}]}
    assert await models.connect_models(account, console, offered)
    assert account.connected == [('openrouter', 'upstream-secret')]
    assert account.selected == [('openrouter', ['openrouter/vendor/new:free'])]
    assert 'upstream-secret' not in console.export_text()


async def test_cancelled_key_entry_does_not_connect_anything(monkeypatch):
    account, choices = ManagedAccount(), iter(['key', 'openrouter'])

    async def choose(*args, **kwargs):
        return next(choices)

    async def cancel(*args, **kwargs):
        return None

    monkeypatch.setattr(models, 'pick', choose)
    monkeypatch.setattr(models, 'ask', cancel)
    console = theme.apply(Console())
    assert not await models.connect_models(account, console, {'available': True, 'accounts': [], 'key_providers': [{'id': 'openrouter', 'name': 'OpenRouter'}]})
    assert account.connected == [] and account.selected == []
