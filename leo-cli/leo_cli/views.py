"""Small terminal views that adapt to the width available at render time."""
from collections import OrderedDict

from rich.console import Group
from rich.table import Table
from rich.text import Text


class ModelList:
    def __init__(self, connections, glyphs, query=''):
        self.connections, self.g, self.query = connections, glyphs, query.strip().lower()

    def __rich_console__(self, console, options):
        width, g = options.max_width, self.g
        models = [c for c in self.connections if self.query in f"{c['name']} {c['model']} {' '.join(c.get('skills') or [])}".lower()]
        enabled = sum(c['enabled'] for c in models)
        yield Text.assemble(('Models', 'leo.title'), (f'  {enabled} enabled / {len(models)} total', 'leo.muted'))
        if not models:
            yield Text('No models match that search.' if self.query else 'No models connected. Use /models add to connect a provider.', style='leo.muted')
            return
        groups = OrderedDict()
        for c in sorted(models, key=lambda c: (c['name'].split(' · ')[0].lower(), not c['enabled'], c['name'].lower())):
            route = '9Router' if '/api/cli/relay/' in c['base_url'] else 'local' if c.get('local') else 'direct'
            provider = c['name'].split(' · ')[0]
            groups.setdefault((provider, route), []).append(c)
        for (provider, route), group in groups.items():
            yield Text('')
            yield Text.assemble((provider, 'bold'), (f'  {route} · {len(group)} model' + ('s' if len(group) != 1 else ''), 'leo.muted'))
            table = Table(box=None, show_header=width >= 72, padding=(0, 1), expand=True, header_style='leo.muted')
            table.add_column('Model', ratio=1, overflow='fold')
            if width >= 72:
                for name in ('Strength', 'Tools', 'Cost'):
                    table.add_column(name, no_wrap=True)
            for c in group:
                label = c['name'].partition(' · ')[2] or c['model']
                name = Text.assemble((f'{g.dot if c["enabled"] else g.idle} ', 'leo.ok' if c['enabled'] else 'leo.muted'),
                                     (label, '' if c['enabled'] else 'leo.muted'))
                if not c['enabled']:
                    name.append(' · off', style='leo.muted')
                tools = 'yes' if c.get('tools') else 'no' if c.get('tools') is False else 'untested'
                cost = ('free' if c.get('free') or c.get('local') else
                        f"${c.get('input_price') or 0:g}/${c.get('output_price') or 0:g}" if c.get('input_price') is not None and c.get('output_price') is not None else 'price unknown')
                if width >= 72:
                    table.add_row(name, Text(c.get('tier', 'standard'), style='leo.muted'),
                                  Text(tools, style='leo.ok' if tools == 'yes' else 'leo.warn' if tools == 'no' else 'leo.muted'),
                                  Text(cost, style='leo.ok' if cost == 'free' else 'leo.muted'))
                else:
                    name.append(f"\n  {c.get('tier', 'standard')} · tools {tools} · {cost}", style='leo.muted')
                    table.add_row(name)
            yield table
        if any('/api/cli/relay/' in c['base_url'] for c in models):
            yield Text('\n9Router models run via your L.E.O. server.', style='leo.muted')
        if any(not c.get('free') and not c.get('local') and c.get('input_price') is not None for c in models):
            yield Text('Paid prices: input / output per 1M tokens.', style='leo.muted')


def help_section(title, rows, width):
    """Long command syntax gets its own line in small terminals, instead of crushing the explanation."""
    if width < 76:
        parts = [Text(title, style='leo.title')]
        for command, description in rows:
            parts.extend([Text(command, style='leo.accent'), Text('  ' + description, style='leo.muted')])
        return Group(*parts)
    table = Table.grid(padding=(0, 3))
    table.add_column(min_width=26, style='leo.accent')
    table.add_column(style='leo.muted')
    for command, description in rows:
        table.add_row(Text(command), Text(description))
    return Group(Text(title, style='leo.title'), table)
