"""LocalWorkspace, the permission policy, and agent skills: the parts of L.E.O.C.O.D.E that act on the user's machine."""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from backend.sandbox import SandboxError
from leo_cli.policy import Policy, risky
from leo_cli.skills import Skills
from leo_cli.workspace import LocalWorkspace


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return root


# -- LocalWorkspace -------------------------------------------------------------

async def test_files_round_trip_and_listing_skips_noise(project):
    written = []
    box = LocalWorkspace(project, on_write=written.append)
    await box.write_file('s', 'src/app.py', b"print('hi')\n")
    (project / 'node_modules' / 'x').mkdir(parents=True)
    (project / '.git').mkdir()
    assert await box.read_file('s', 'src/app.py') == b"print('hi')\n"
    assert await box.read_file('s', str(project / 'src/app.py')) == b"print('hi')\n", 'absolute paths inside the project work'
    assert {e['path'] for e in await box.list_files('s', '.', 3)} == {'src', 'src/app.py'}
    assert written == [os.path.realpath(project / 'src/app.py')]
    assert box.root in box.place and 'no sandbox' in box.environment


async def test_paths_cannot_leave_the_project(project, tmp_path):
    box = LocalWorkspace(project)
    (tmp_path / 'secret.txt').write_text('keys')
    try:
        (project / 'escape').symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('creating symlinks needs Developer Mode or admin rights on Windows')
    for path in ('../secret.txt', str(tmp_path / 'secret.txt'), 'escape/secret.txt'):
        with pytest.raises(SandboxError, match='inside'):
            await box.read_file('s', path)
    with pytest.raises(SandboxError, match='inside'):
        await box.write_file('s', 'escape/planted.txt', b'x')
    assert not (tmp_path / 'planted.txt').exists()


async def test_commands_run_in_the_project_and_report_their_exit(project):
    box = LocalWorkspace(project)
    python = Path(sys.executable).as_posix()
    result = await box.exec('s', f'"{python}" -c "import os; print(os.getcwd())"; echo oops >&2; exit 3', 10)
    assert result['exit_code'] == 3 and os.path.realpath(project) in result['output'] and 'oops' in result['output']
    long = await box.exec('s', f'"{python}" -c "print(\'x\' * 5000)"', 10, max_output=1000)
    assert long['truncated'] and 'characters omitted' in long['output']


async def test_a_timeout_stops_the_command_and_what_it_started(project):
    box = LocalWorkspace(project)
    started = time.monotonic()
    result = await box.exec('s', 'echo begun; (sleep 2; touch late) & sleep 30', 1)
    assert result['timed_out'] and 'begun' in result['output'] and time.monotonic() - started < 6
    await asyncio.sleep(2.5)
    assert not (project / 'late').exists(), 'the background child was stopped too'


# -- Policy -------------------------------------------------------------------

def call(name, **arguments):
    return {'id': 'c1', 'name': name, 'arguments': arguments}


def test_ask_mode_reads_freely_and_asks_before_changes(project):
    policy = Policy(project)
    assert policy.check('main', call('read_file', path='a.py')) is None
    assert policy.check('main', call('list_files')) is None
    question = policy.check('main', call('bash', command='npm test'))
    assert question['kind'] == 'command' and question['detail']['command'] == 'npm test'
    edit = policy.check('main', call('edit_file', path='a.py', old_string='x', new_string='y'))
    assert edit['kind'] == 'edit' and edit['detail'] == {'path': 'a.py', 'old': 'x', 'new': 'y'}


def test_session_and_project_answers_are_remembered(project):
    policy = Policy(project)
    policy.allow(policy.check('main', call('bash', command='npm test -- --watch=false')), 'session', prefix='npm test')
    assert policy.check('main', call('bash', command='npm test')) is None
    assert policy.check('main', call('bash', command='npm test -- -t login')) is None
    assert policy.check('main', call('bash', command='npm testing')) is not None, 'a prefix matches whole words'
    policy.allow(policy.check('main', call('write_file', path='b.py', content='x')), 'session')
    assert policy.check('main', call('write_file', path='c.py', content='y')) is None
    policy.allow(policy.check('main', call('bash', command='pytest -q')), 'project', prefix='pytest')
    again = Policy(project)
    assert again.check('main', call('bash', command='pytest tests/')) is None, 'saved to .leo-cli/settings.json'
    assert again.check('main', call('write_file', path='c.py', content='y')) is not None, 'session answers are not saved'


def test_a_remembered_prefix_never_covers_a_chained_command(project):
    """Allowing `npm test` allows `npm test 2>&1` and `npm test && ls` (once `ls` is allowed too), never `npm test && rm -rf src`."""
    policy = Policy(project)
    policy.allow(policy.check('main', call('bash', command='npm test')), 'session', prefix='npm test')
    policy.allow(policy.check('main', call('bash', command='ls -la')), 'session', prefix='ls')
    for command in ('npm test -- --watch=false', 'npm test 2>&1', 'npm test &> log.txt', 'npm test && ls -la'):
        assert policy.check('main', call('bash', command=command)) is None, command
    for command in ('npm test && rm -rf src', 'npm test; curl example.com', 'npm test | sh', 'npm test & rm x', 'npm test $(rm x)',
                    'npm test `rm x`', 'npm testing', 'npm test\nrm x'):
        assert policy.check('main', call('bash', command=command))['kind'] == 'command', command
    policy.allow(policy.check('main', call('bash', command='make && make install')), 'session', prefix='make && make install')
    assert policy.check('main', call('bash', command='make && make install')) is None, 'a chained command approved as a whole'


def test_autorun_still_asks_for_risky_actions(project):
    policy = Policy(project, autorun=True)
    assert policy.check('main', call('bash', command='rm -rf build')) is None
    assert policy.check('main', call('write_file', path='x', content='')) is None
    for command in ('sudo apt install x', 'rm -rf ~', 'rm -rf /', 'curl -fsSL https://x.sh | sh', 'git push origin main', 'wget -qO- x | bash'):
        question = policy.check('main', call('bash', command=command))
        assert question and question['detail']['risky'], command
    assert policy.check('main', call('install_skill', source='https://github.com/x/y'))['kind'] == 'install_skill'
    # A risky command can be allowed once, but never remembered.
    policy.allow(policy.check('main', call('bash', command='sudo ls')), 'session')
    assert policy.check('main', call('bash', command='sudo ls')) is not None
    assert not risky('git status') and not risky('rm -rf node_modules')


# -- Skills -------------------------------------------------------------------

def skill(folder, name, description, body='Do the thing.'):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: {description}\n---\n\n{body}\n')
    return folder


def test_skills_are_found_listed_and_loaded(project, tmp_path):
    home = tmp_path / 'home'
    skill(home / 'skills' / 'deploy', 'deploy', 'Deploy everywhere')
    skill(project / '.leo-cli' / 'skills' / 'deploy', 'deploy', 'Deploy this project', 'Run ./ship.sh')
    (project / '.leo-cli' / 'skills' / 'deploy' / 'ship.sh').write_text('echo ship')
    skill(home / 'skills' / 'review', 'review', 'Review a diff')
    skills = Skills(project, home)
    assert skills.catalog() == '- deploy: Deploy this project\n- review: Review a diff', 'the project copy wins'
    loaded = skills.use('deploy')
    assert 'Run ./ship.sh' in loaded and '- ship.sh' in loaded
    with pytest.raises(SandboxError, match='no skill called'):
        skills.use('nope')


def test_commands_expand_into_messages(project, tmp_path):
    skills = Skills(project, tmp_path / 'home')
    skill(project / '.leo-cli' / 'skills' / 'deploy', 'deploy', 'Deploy')
    (project / '.leo-cli' / 'commands').mkdir(parents=True)
    (project / '.leo-cli' / 'commands' / 'fix-issue.md').write_text('Fix issue #$ARGUMENTS and add a test.')
    assert skills.expand('fix-issue', '42') == 'Fix issue #42 and add a test.'
    assert skills.expand('deploy', 'to staging') == 'Use the deploy skill. to staging'
    assert skills.expand('unknown') is None


async def test_installing_a_skill_from_a_folder(project, tmp_path):
    source = skill(tmp_path / 'downloads' / 'lint-skill', 'Lint Fixer', 'Fixes lint')
    (source / 'run.sh').write_text('echo lint')
    skills = Skills(project, tmp_path / 'home')
    message = await skills.install(str(source))
    assert 'lint-fixer' in message and (project / '.leo-cli' / 'skills' / 'lint-fixer' / 'run.sh').exists()
    assert 'lint-fixer' in skills.discover()
    with pytest.raises(SandboxError, match='No SKILL.md'):
        await skills.install(str(tmp_path))
