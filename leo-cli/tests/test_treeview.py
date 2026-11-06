"""Drawing the tree of agents: in the prompt while it works, and in full in /usage."""
from prompt_toolkit.utils import get_cwidth
from rich.console import Console

from leo_cli import theme, treeview
from leo_cli.ui import glyphs

G = glyphs(unicode=True)


def node(node_id, parent='main', status='working', task=None, model='light-m', kind='worker'):
    return {'id': node_id, 'parent': parent, 'kind': kind, 'level': 1, 'task': task or f'task {node_id}', 'model': model, 'status': status, 'attempt': 1}


def tree(*children, root_status='planning'):
    nodes = {'main': node('main', None, root_status, 'the task', 'strong-m', 'root')}
    for child in children:
        nodes[child['id']] = child
    return nodes


def lines(fragments):
    text = ''.join(t for _, t in fragments)
    assert not fragments or text.endswith('\n')
    return text.splitlines()


def full_text(nodes, usage=None, **kwargs):
    console = theme.apply(Console(record=True, width=120, color_system=None))
    console.print(treeview.full(nodes, usage or {}, G, **kwargs))
    return console.export_text()


def test_rows_nest_with_branches_and_order():
    nodes = tree(node('a', status='accepted', task='Auth module', kind='manager'), node('a1', 'a', 'accepted', 'Login route'), node('a2', 'a', 'working', 'Tests'),
                 node('b', status='waiting', task='Docs'))
    out = lines(treeview.live(nodes, {}, 100, 60, G, 0, {'a2': 'bash(pytest)'}))
    assert out[0].startswith(f'{G.agent} strong-m')
    assert out[1].startswith(f'{G.tee} {G.ok} Auth module') and out[2].startswith(f'{G.pipe}{G.tee} {G.ok} Login route')
    assert out[3].startswith(f'{G.pipe}{G.elbow} ') and 'Tests' in out[3] and 'bash(pytest)' in out[3]
    assert out[4].startswith(f'{G.elbow} {G.idle} Docs') and 'waiting' in out[4]


def test_a_parent_with_work_to_judge_reads_reviewing():
    out = lines(treeview.live(tree(node('a', status='in_review')), {}, 100, 60, G, 0))
    assert 'reviewing' in out[0] and 'in review' in out[1]


def test_usage_appears_on_the_rows():
    out = lines(treeview.live(tree(node('a')), {'a': {'calls': 2, 'input_tokens': 3000, 'output_tokens': 400, 'cost': 0}}, 100, 60, G, 0))
    assert '3.4K' in out[1]


def test_finished_branches_collapse_first():
    many = [node(f'f{n}', status='accepted') for n in range(3)] + [node(f'w{n}', status='waiting') for n in range(2)] + [node('now')]
    out = lines(treeview.live(tree(*many), {}, 100, 15, G, 0))  # 5 lines.
    assert len(out) == 5 and '3 finished' in out[1] and 'task w0' in out[2] and 'task now' in out[4]
    assert not any('task f0' in line for line in out)
    out = lines(treeview.live(tree(*many), {}, 100, 12, G, 0))  # 4 lines: the waiting ones go too.
    assert len(out) == 4 and '3 finished' in out[1] and '2 waiting' in out[2] and 'task now' in out[3]


def test_a_finished_parent_of_working_children_stays():
    out = lines(treeview.live(tree(node('a', status='planning', kind='manager'), node('a1', 'a', 'accepted'), node('a2', 'a', 'accepted'), node('a3', 'a', 'working')), {}, 100, 12, G, 0))
    assert any('task a3' in line for line in out) and any('task a ' in line or line.endswith('planning') for line in out)


def test_the_tree_fits_short_and_narrow_terminals():
    nodes = tree(*[node(f'n{i}', task='a very long task description that cannot possibly fit on the line') for i in range(12)])
    out = lines(treeview.live(nodes, {'n0': {'calls': 1, 'input_tokens': 900, 'output_tokens': 9, 'cost': 0}}, 50, 10, G, 0, {'n0': 'bash(a very long command that also cannot fit at all)'}))
    assert len(out) == 3 and all(get_cwidth(line) <= 50 for line in out), out
    assert 'more' in out[-1]


def test_a_narrow_row_keeps_its_status_and_clips_the_task():
    out = lines(treeview.live(tree(node('a', status='retrying', task='x' * 80)), {}, 50, 60, G, 0))
    assert 'retrying' in out[1] and 'xxx' in out[1] and G.ellipsis in out[1] and get_cwidth(out[1]) <= 50


def test_never_more_than_ten_lines():
    assert len(lines(treeview.live(tree(*[node(f'n{i}') for i in range(12)]), {}, 100, 100, G, 0))) == 10


def test_only_a_root_shows_nothing():
    assert treeview.live(tree(), {}, 100, 60, G, 0) == []


def test_ascii_terminals_get_ascii():
    plain = glyphs(unicode=False)
    nodes = tree(node('a', status='accepted'), node('b', status='failed'), node('c', status='waiting'), node('d'), node('e', status='retrying'))
    text = ''.join(t for _, t in treeview.live(nodes, {}, 100, 60, plain, 3))
    assert text.isascii(), text
    console = theme.apply(Console(record=True, width=120, color_system=None))
    console.print(treeview.full(nodes, {}, plain))
    assert console.export_text().isascii()


def test_model_names_are_shortened():
    assert treeview.short_model('nvidia/nemotron-3-ultra:free') == 'nemotron-3-ultra' and treeview.short_model(None) == ''


def test_usage_adds_up_from_the_ledger():
    def row(stage, status='completed', tokens=(100, 10), cost=0.5):
        return {'stage': stage, 'status': status, 'input_tokens': tokens[0], 'output_tokens': tokens[1], 'cost': cost}

    usage = treeview.agent_usage([row('agent:main:1'), row('agent:main:2'), row('agent:sub-1:1'), row('agent:compact:1'), row('agent:sub-1:2', 'failed')])
    assert usage['main'] == {'calls': 2, 'input_tokens': 200, 'output_tokens': 20, 'cost': 1.0}
    assert usage['sub-1']['calls'] == 1 and usage['compact']['calls'] == 1, 'a failed call is not counted'


def test_the_full_view_shows_every_agent_with_its_usage():
    many = [node(f'n{i}', status='accepted', task=f'piece {i}') for i in range(12)]
    usage = {'n3': {'calls': 2, 'input_tokens': 3000, 'output_tokens': 400, 'cost': 0.25}, 'compact': {'calls': 1, 'input_tokens': 10, 'output_tokens': 5, 'cost': 0}}
    out = full_text(tree(*many, root_status='done'), usage)
    assert all(f'piece {i}' in out for i in range(12)) and '3.4K' in out and '$0.2500' in out and 'context summaries' in out


def test_a_stopped_run_does_not_show_working():
    nodes = tree(node('a'), node('b', status='retrying'), node('c', status='accepted'))
    out = full_text(nodes, run_status='cancelled')
    assert 'stopped' in out and 'working' not in out and 'retrying' not in out and 'accepted' in out
