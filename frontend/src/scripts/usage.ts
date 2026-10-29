import { LEVELS, breakdowns, levelMeter, levelOf, money, node, percent, seconds, share, shareBar, successRate, tokens, type Report } from './usage-ui';

const el = (id: string) => document.getElementById(id) as HTMLElement;
const time = (value: number) => new Date(value * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

function row(cells: (string | HTMLElement)[], numeric: number[] = []) {
  const tr = document.createElement('tr');
  cells.forEach((cell, index) => { const td = document.createElement('td'); if (numeric.includes(index)) td.className = 'num'; typeof cell === 'string' ? td.textContent = cell : td.append(cell); tr.append(td); });
  return tr;
}
function empty(body: HTMLElement, columns: number, text: string) {
  const tr = document.createElement('tr'); const td = node('td', text, 'empty-cell') as HTMLTableCellElement; td.colSpan = columns; tr.append(td); body.replaceChildren(tr);
}
function modelCell(provider: string, model: string) {
  const wrap = node('div', '', 'model-cell'); wrap.append(node('strong', model), node('small', provider)); return wrap;
}
function inlineShare(value: number, series: string) {
  const wrap = node('div', '', 'inline-share');
  const bar = node('span', '', 'inline-bar'); bar.dataset.series = series;
  const fill = node('span'); fill.style.width = `${Math.min(value, 100)}%`; bar.append(fill);
  wrap.append(bar, node('strong', percent(value))); return wrap;
}

function renderTiles(report: Report) {
  const t = report.totals, all = t.input_tokens + t.output_tokens;
  const chats = report.conversations.filter(c => c.context);
  const busiest = chats.reduce<number>((max, c) => Math.max(max, c.context!.percent), 0);
  const tiles: [string, string, string][] = [
    ['Tokens', tokens(all, t.estimated), `${percent(share(t.input_tokens, all))} input, ${percent(share(t.output_tokens, all))} output`],
    ['Call success', percent(successRate(t)), `${t.completed} of ${t.calls - (t.in_progress ?? 0)} finished calls completed${t.uncertain ? `, ${t.uncertain} uncertain` : ''}${t.in_progress ? `, ${t.in_progress} running` : ''}`],
    ['Fullest context', chats.length ? percent(busiest) : '—', chats.length ? `${levelOf(busiest).name} level, ${chats.length} chat${chats.length === 1 ? '' : 's'} measured` : 'No completed calls yet'],
    ['Estimated cost', money(t.cost), `${report.models.length} model${report.models.length === 1 ? '' : 's'}, ${t.completed ? seconds(Math.round(t.elapsed_ms / t.completed)) : '—'} average call`],
  ];
  el('usage-tiles').replaceChildren(...tiles.map(([label, value, detail]) => {
    const tile = node('div', '', 'usage-tile');
    tile.append(node('span', label, 'tile-label'), node('strong', value, 'tile-value'), node('small', detail)); return tile;
  }));
}
function renderOverview(report: Report) {
  // Percentage of conversations at each context level.
  const levels = LEVELS.map(l => {
    const chats = report.conversations.filter(c => c.context && levelOf(c.context.percent).key === l.key).length;
    return { label: l.name, value: chats, detail: `${chats} chat${chats === 1 ? '' : 's'} with context ${l.range}`, series: `lv-${l.key}` };
  });
  el('usage-overview').replaceChildren(shareBar('Chats by context level', levels, 'chats', true), ...breakdowns(report));
}
function renderModels(report: Report) {
  const body = el('models-body'), all = report.totals.input_tokens + report.totals.output_tokens;
  if (!report.models.length) return empty(body, 8, 'No model calls yet. Send a message to see usage.');
  const ranked = [...report.models].sort((a, b) => (b.input_tokens + b.output_tokens) - (a.input_tokens + a.output_tokens));
  body.replaceChildren(...ranked.map((m, i) => row([
    modelCell(m.provider, m.model),
    Object.entries(m.roles).map(([role, count]) => `${role} ×${count}`).join(', '),
    inlineShare(share(m.input_tokens + m.output_tokens, all), String(i % 6)),
    `${percent(successRate(m))} (${m.completed}/${m.calls - (m.in_progress ?? 0)})`,
    tokens(m.input_tokens, m.estimated), tokens(m.output_tokens, m.estimated),
    m.completed ? seconds(Math.round(m.elapsed_ms / m.completed)) : '—', money(m.cost),
  ], [3, 4, 5, 6, 7])));
}
function renderContext(report: Report) {
  const list = el('context-list');
  if (!report.conversations.length) { list.replaceChildren(node('p', 'No conversations with model calls yet.', 'field-hint')); return; }
  list.replaceChildren(...report.conversations.map(chat => {
    const card = document.createElement('a'); card.className = 'context-card'; card.href = '/';
    card.addEventListener('click', () => { try { localStorage.setItem('leo-active-chat', chat.id); } catch {} });
    const heading = node('div', '', 'context-heading');
    heading.append(node('strong', chat.title || 'Untitled conversation'), node('span', `${chat.runs} run${chat.runs === 1 ? '' : 's'}, ${chat.calls} calls, ${money(chat.cost)}`));
    const all = chat.input_tokens + chat.output_tokens;
    card.append(heading, levelMeter(chat.context),
      node('small', `${tokens(all, chat.estimated)} tokens this chat: ${percent(share(chat.input_tokens, all))} input, ${percent(share(chat.output_tokens, all))} output. ${percent(share(chat.completed, chat.calls))} of calls completed.`));
    return card;
  }));
}
function renderCalls(report: Report) {
  const body = el('calls-body');
  if (!report.recent_calls.length) return empty(body, 7, 'No calls recorded yet.');
  body.replaceChildren(...report.recent_calls.map(c => {
    const status = node('span', c.status, 'call-status'); status.dataset.status = c.status;
    return row([time(c.created_at), c.conversation || '—', `${c.stage} · ${c.role}`, modelCell(c.provider, c.model), status,
      c.status === 'completed' ? `${tokens(c.input_tokens, c.estimated)} / ${tokens(c.output_tokens, c.estimated)}` : 'reserved', c.elapsed_ms ? seconds(c.elapsed_ms) : '—'], [5, 6]);
  }));
}

let timer: ReturnType<typeof setTimeout> | undefined;
async function load() {
  clearTimeout(timer);
  try {
    const response = await fetch('/api/usage', { headers: { 'Content-Type': 'application/json' } });
    if (response.status === 401) throw new Error('Sign in from the chat page to view usage');
    if (!response.ok) throw new Error(`Usage request failed (${response.status})`);
    const report: Report = await response.json();
    renderTiles(report); renderOverview(report); renderModels(report); renderContext(report); renderCalls(report);
    el('usage-updated').textContent = `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
    el('toast').hidden = true;
  } catch (error) {
    el('toast').textContent = error instanceof Error ? error.message : 'Unable to load usage'; el('toast').hidden = false;
  }
  // Refresh while visible so running conversations update without polling in the background.
  if (!document.hidden) timer = setTimeout(load, 5000);
}
el('usage-refresh').addEventListener('click', () => void load());
document.addEventListener('visibilitychange', () => { if (!document.hidden) void load(); });
void load();
