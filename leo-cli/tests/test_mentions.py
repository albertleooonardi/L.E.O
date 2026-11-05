"""@path in a message: files and folders attached to what the agent receives, and @path completion."""
from leo_cli.mentions import attach, complete


def test_a_file_mention_attaches_its_text(tmp_path):
    (tmp_path / 'app.py').write_text('x = 1\nprint(x)\n')
    text, notes = attach('fix the bug in @app.py, please', tmp_path)
    assert text.startswith('fix the bug in @app.py, please\n\nAttached by the user with @')
    assert '<file path="app.py">\nx = 1\nprint(x)\n' in text and notes == ['app.py · 2 lines'], 'trailing punctuation is not part of the path'


def test_a_folder_mention_attaches_its_listing_without_noise(tmp_path):
    (tmp_path / 'src' / 'lib').mkdir(parents=True)
    (tmp_path / 'src' / 'main.py').write_text('')
    (tmp_path / 'src' / 'lib' / 'util.py').write_text('')
    (tmp_path / 'src' / 'node_modules').mkdir()
    text, notes = attach('what is in @src/', tmp_path)
    assert '<folder path="src">\nlib/\nlib/util.py\nmain.py\n</folder>' in text and notes == ['src/ · 3 entries']


def test_paths_with_spaces_home_and_outside_the_folder(tmp_path, monkeypatch):
    project, outside = tmp_path / 'project', tmp_path / 'notes'
    project.mkdir()
    outside.mkdir()
    (project / 'my file.txt').write_text('hello')
    (outside / 'spec.md').write_text('# Spec')
    monkeypatch.setenv('HOME', str(tmp_path))
    text, notes = attach('read @"my file.txt" and @~/notes/spec.md', project)
    assert '<file path="my file.txt">\nhello\n</file>' in text
    assert f'<file path="{outside.resolve() / "spec.md"}">\n# Spec' in text, 'outside the folder: attached read-only, by its full path'


def test_what_is_not_a_path_is_left_alone(tmp_path):
    (tmp_path / 'a.py').write_text('a')
    (tmp_path / 'b.bin').write_bytes(b'\0\1\2')
    message = 'mail me@example.com, ping @everyone, see @missing.py and @a.py and @a.py again; @b.bin'
    text, notes = attach(message, tmp_path)
    assert text.count('<file path="a.py">') == 1, 'attached once'
    assert notes == ['a.py · 1 line', 'b.bin · binary file, not attached']
    assert attach('no mentions here', tmp_path) == ('no mentions here', [])


def test_long_files_are_cut_and_say_where_to_read_on(tmp_path, monkeypatch):
    from leo_cli import mentions
    monkeypatch.setattr(mentions, 'FILE_CHARS', 20)
    (tmp_path / 'big.txt').write_text('line\n' * 100)
    text, notes = attach('@big.txt', tmp_path)
    assert 'read the rest with read_file' in text and notes[0].endswith('(cut)')


def test_completion_lists_folders_first_and_skips_noise(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'setup.py').write_text('')
    (tmp_path / '.env').write_text('')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'src' / 'server.py').write_text('')
    (tmp_path / 'some dir').mkdir()
    assert complete('@s', tmp_path) == [('@"some dir/"', 'some dir/'), ('@src/', 'src/'), ('@setup.py', 'setup.py')]
    assert complete('@src/se', tmp_path) == [('@src/server.py', 'server.py')]
    assert complete('@', tmp_path)[0][0] == '@"some dir/"' and all('.env' not in c for c, _ in complete('@', tmp_path))
    assert complete('@.e', tmp_path) == [('@.env', '.env')], 'hidden files when asked for'
    assert complete('@nowhere/x', tmp_path) == []
