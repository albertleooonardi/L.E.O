// Shared usage visuals: context levels and percentage breakdowns. Everything is built with textContent.

export type Totals = { calls: number; completed: number; uncertain: number; in_progress?: number; input_tokens: number; output_tokens: number; reserved_tokens: number; cost: number; estimated: boolean; elapsed_ms: number; role_tokens?: Record<string, number> };
export type ModelUsage = Totals & { provider: string; model: string; roles: Record<string, number>; context_window: number | null };
export type Context = { tokens: number; allowance: number; model_window: number | null; percent: number; provider: string; model: string; stage: string; estimated: boolean };
export type Call = { stage: string; role: string; provider: string; model: string; status: string; input_tokens: number; output_tokens: number; estimated: boolean; elapsed_ms: number; created_at: number; conversation?: string };
export type Report = { totals: Totals; context_tokens: number; models: ModelUsage[]; recent_calls: Call[];
  conversations: (Totals & { id: string; title: string; runs: number; context: Context | null; updated_at: number })[] };

// Context levels: where the latest call sits against the trimming allowance.
export const LEVELS = [
  { key: 'comfortable', name: 'Comfortable', range: 'under 50%', from: 0, hint: 'Plenty of room for longer chats.' },
  { key: 'moderate', name: 'Moderate', range: '50–69%', from: 50, hint: 'Room remains; long replies will fill it faster.' },
  { key: 'high', name: 'High', range: '70–89%', from: 70, hint: 'Older turns will soon be trimmed.' },
  { key: 'limit', name: 'Near limit', range: '90% and above', from: 90, hint: 'Older turns are being trimmed. Start a new chat or raise Context tokens.' },
] as const;
export const levelOf = (percent: number) => [...LEVELS].reverse().find(l => percent >= l.from)!;

const number = new Intl.NumberFormat();
export const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
export const tokens = (value: number, estimated = false) => `${estimated ? '~' : ''}${number.format(value)}`;
export const money = (value: number) => `$${value.toFixed(value && value < 0.01 ? 5 : 2)}`;
export const share = (part: number, whole: number) => whole > 0 ? Math.round(1000 * part / whole) / 10 : 0;
/** Completed share of calls that have finished; running calls are not counted against success. */
export const successRate = (t: { calls: number; completed: number; in_progress?: number }) => t.calls - (t.in_progress ?? 0) > 0 ? share(t.completed, t.calls - (t.in_progress ?? 0)) : null;
export const percent = (value: number | null) => value === null ? '—' : `${value % 1 ? value.toFixed(1) : value}%`;
export const seconds = (ms: number) => ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
const ROLE_NAMES: Record<string, string> = { generator: 'Generator', planner: 'Planner', reviewer: 'Reviewer' };

export function node(tag: string, text = '', className = '') {
  const item = document.createElement(tag); item.textContent = text; if (className) item.className = className; return item;
}

/** A four-zone meter: the zones mark each level's range, the fill and label show where this chat is. */
export function levelMeter(context: Context | null) {
  const wrap = node('div', '', 'level-meter');
  if (!context) { wrap.append(node('p', 'No completed call yet. Context appears after the first answer.', 'level-empty')); return wrap; }
  const level = levelOf(context.percent);
  wrap.dataset.level = level.key;
  const head = node('div', '', 'level-head');
  head.append(node('strong', level.name, 'level-name'), node('span', percent(context.percent), 'level-percent'));
  const track = node('div', '', 'level-track');
  track.setAttribute('role', 'meter'); track.setAttribute('aria-label', `Context ${level.name}, ${percent(context.percent)} used`);
  track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.setAttribute('aria-valuenow', String(Math.min(context.percent, 100)));
  LEVELS.forEach((l, i) => {
    const zone = node('span', '', 'level-zone'); zone.dataset.level = l.key;
    zone.style.flexBasis = `${(LEVELS[i + 1]?.from ?? 100) - l.from}%`; track.append(zone);
  });
  const fill = node('span', '', 'level-fill'); fill.style.width = `${Math.min(context.percent, 100)}%`; track.append(fill);
  const scale = node('div', '', 'level-scale');
  for (const l of LEVELS) { const tick = node('span', `${l.from}%`); tick.style.left = `${l.from}%`; scale.append(tick); }
  wrap.append(head, track, scale,
    node('small', `${tokens(context.tokens, context.estimated)} of ${number.format(context.allowance)} tokens. ${level.hint}`),
    node('small', `Latest call: ${context.stage} on ${context.model}${context.model_window ? `, model window ${compact.format(context.model_window)}` : ''}.`, 'level-detail'));
  return wrap;
}

/** Stacked bar plus a legend that gives each part its percentage. */
export function shareBar(title: string, parts: { label: string; value: number; detail?: string; series?: string }[], unit = 'tokens', fixed = false) {
  const section = node('section', '', 'share');
  const total = parts.reduce((sum, p) => sum + p.value, 0);
  section.append(node('h3', title));
  if (!total) { section.append(node('p', `No ${unit} recorded yet.`, 'level-empty')); return section; }
  const bar = node('div', '', 'share-bar'); bar.setAttribute('role', 'img');
  bar.setAttribute('aria-label', parts.map(p => `${p.label} ${percent(share(p.value, total))}`).join(', '));
  const legend = node('ul', '', 'share-legend');
  // Fixed scales (like levels) keep their order and show empty parts at 0%; other breakdowns rank by size.
  (fixed ? parts : parts.filter(p => p.value > 0).sort((a, b) => b.value - a.value)).forEach((p, i) => {
    if (p.value > 0) { const slice = node('span', '', 'share-slice'); slice.dataset.series = p.series ?? String(i % 6); slice.style.flexGrow = String(p.value); bar.append(slice); }
    const row = node('li'); row.dataset.series = p.series ?? String(i % 6);
    const label = node('span', p.label, 'share-label');
    row.append(label, node('strong', percent(share(p.value, total)), 'share-percent'), node('small', p.detail ?? `${number.format(p.value)} ${unit}`, 'share-detail'));
    legend.append(row);
  });
  section.append(bar, legend);
  return section;
}

/** The breakdowns shared by the chat panel and the usage page. */
export function breakdowns(report: Report) {
  const t = report.totals;
  const models = report.models.map(m => ({ label: m.model, value: m.input_tokens + m.output_tokens,
    detail: `${compact.format(m.input_tokens + m.output_tokens)} tokens, ${percent(successRate(m))} of calls ok, ${m.completed ? seconds(Math.round(m.elapsed_ms / m.completed)) : '—'} avg` }));
  const roles = Object.entries(t.role_tokens ?? {}).map(([role, value]) => ({ label: ROLE_NAMES[role] ?? role, value,
    detail: `${compact.format(value)} tokens, ${report.models.reduce((n, m) => n + (m.roles[role] ?? 0), 0)} calls` }));
  return [
    shareBar('Input and output', [
      { label: 'Input (prompt and context)', value: t.input_tokens, detail: `${tokens(t.input_tokens, t.estimated)} tokens` },
      { label: 'Output (answers and reasoning)', value: t.output_tokens, detail: `${tokens(t.output_tokens, t.estimated)} tokens` },
    ]),
    shareBar('Tokens by model', models),
    ...(roles.length ? [shareBar('Tokens by role', roles)] : []),
    shareBar('Call outcomes', [
      { label: 'Completed', value: t.completed, detail: `${t.completed} calls`, series: 'ok' },
      { label: 'In progress', value: t.in_progress ?? 0, detail: `${t.in_progress ?? 0} running now`, series: '0' },
      { label: 'Uncertain (failed or interrupted)', value: t.uncertain, detail: `${t.uncertain} calls, may have been billed`, series: 'warn' },
    ], 'calls'),
  ];
}
