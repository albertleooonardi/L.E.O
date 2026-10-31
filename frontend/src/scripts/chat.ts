import { canvas, extractPage, streamingHtml } from './canvas';
import { renderMarkdown } from './markdown';
import { breakdowns, compact, levelMeter, levelOf, money, node, percent, seconds, successRate, tokens, type Report } from './usage-ui';

type Mode = 'auto' | 'quick' | 'reviewed' | 'deep';
type Provider = { id: string; name: string; protocol: string; base_url: string; model: string; local: boolean; free: boolean; enabled: boolean; has_key: boolean; priority: number; context_window: number; input_price: number | null; output_price: number | null; token_parameter: string; skills: string[]; tier: string; reasoning: boolean };
type Skill = { id: string; name: string; description: string; keywords: string[]; instructions: string; enabled: boolean };
type Preset = { id: string; name: string; protocol: string; base_url: string; local?: boolean; free?: boolean; hint: string; skills?: string[]; tier?: string; reasoning?: boolean };
type Preferences = { generator_id: string | null; planner_id: string | null; reviewer_id: string | null; mode: Mode; routing: string; allow_paid: boolean; max_cost: number; max_calls: number; max_repairs: number; max_output_tokens: number; context_tokens: number; token_budget: number; timeout_seconds: number; max_run_seconds: number; system_prompt: string; smart_routing?: boolean; web_search?: string; search_url?: string; search_pages?: number };
type Usage = { route?: RouteInfo; calls?: number; input_tokens?: number; output_tokens?: number; estimated_cost?: number; estimated?: boolean; uncertain_calls?: number; context_messages_omitted?: number; review_passed?: boolean | null };
type Run = { id: string; conversation_id: string; status: string; phase: string; output: string; error?: string; usage: Usage; mode: Mode; created_at?: number };
type Conversation = { id: string; title: string; updated_at: number; active_run: string | null };
type Message = { role: 'user' | 'assistant'; content: string; run_id?: string };
type FullConversation = Conversation & { messages: Message[]; runs: Run[] };

function el<T extends HTMLElement = HTMLElement>(id: string): T { return document.getElementById(id) as T; }
const field = (id: string) => el<HTMLInputElement>(id);
const select = (id: string) => el<HTMLSelectElement>(id);
const dialog = (id: string) => el<HTMLDialogElement>(id);
const prompt = el<HTMLTextAreaElement>('prompt');
let conversations: Conversation[] = [];
let connections: Provider[] = [];
let presets: Preset[] = [];
let skills: Skill[] = [];
let preferences: Preferences;
let current: FullConversation | null = null;
let activeId: string | null = null;
let activeRun: string | null = null;
let stream: EventSource | null = null;
let draft = '';
let submitting = false;
let ready = false;
let toastTimer: ReturnType<typeof setTimeout>;
let navigation = 0;

function toast(text: string) {
  el('toast').textContent = text; el('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el('toast').hidden = true, 5000);
}
function errorText(error: unknown) { return error instanceof Error ? error.message : 'Something went wrong'; }
async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init.headers } });
  const data = await response.json().catch(() => ({ detail: 'Server returned an unreadable response' }));
  if (response.status === 401) { if (!dialog('login-dialog').open) dialog('login-dialog').showModal(); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`);
  return data;
}
const send = <T>(path: string, method: string, body?: unknown) => api<T>(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
function rememberChat() { try { activeId ? localStorage.setItem('leo-active-chat', activeId) : localStorage.removeItem('leo-active-chat'); } catch {} }
function closeStream() { stream?.close(); stream = null; activeRun = null; draft = ''; clearInterval(liveClock); updateSend(); }
function updateSend() {
  const busy = Boolean(activeRun);
  const button = el<HTMLButtonElement>('send-button');
  button.disabled = submitting || (!busy && (!ready || !prompt.value.trim()));
  button.setAttribute('aria-label', busy ? 'Stop response' : 'Send message');
  button.title = busy ? 'Stop response' : 'Send message';
  button.querySelector<SVGElement>('svg')!.style.display = busy ? 'none' : '';
  button.querySelector<HTMLElement>('.stop-square')!.hidden = !busy;
  el('stop-run').hidden = !busy;
}
function resizePrompt() { prompt.style.height = 'auto'; prompt.style.height = `${Math.min(prompt.scrollHeight, 220)}px`; updateSend(); }
function scrollToLatest() { if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 350) requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight })); }

// Render generated text as DOM nodes only. Model output never becomes HTML.
function codeBlock(language: string, codeText: string) {
  const wrapper = node('div', '', 'code-block');
  const heading = node('div', '', 'code-heading');
  const button = node('button', 'Copy', 'code-copy'); button.setAttribute('aria-label', `Copy ${language} code`);
  button.addEventListener('click', async () => { await copy(codeText); button.textContent = 'Copied'; setTimeout(() => button.textContent = 'Copy', 1500); });
  heading.append(node('span', language), button);
  const pre = document.createElement('pre'); pre.tabIndex = 0; const code = document.createElement('code'); code.textContent = codeText; pre.append(code);
  wrapper.append(heading, pre); return wrapper;
}
// Render generated text as DOM nodes only (see markdown.ts). Model output never becomes HTML.
function renderContent(target: HTMLElement, text: string) { renderMarkdown(target, text, codeBlock); }
async function copy(text: string) { try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { toast('Select the text to copy it'); } }
function messageNode(message: Message, pending = false) {
  const article = document.createElement('article'); article.className = `message ${message.role}`;
  const content = document.createElement('div'); content.className = 'message-content';
  if (pending) { article.id = 'pending-response'; content.id = 'pending-content'; }
  if (message.role === 'assistant') {
    const name = document.createElement('div'); name.className = 'message-name'; if (pending) { name.textContent = 'L.E.O'; name.append(node('span', '', 'working-dot')); article.append(name); }
  }
  renderContent(content, message.content); article.append(content);
  if (message.role === 'assistant' && !pending) {
    const actions = document.createElement('div'); actions.className = 'message-actions';
    const button = document.createElement('button'); button.className = 'message-action'; button.textContent = 'Copy response'; button.addEventListener('click', () => copy(message.content)); actions.append(button);
    const page = extractPage(message.content);
    if (page) { const preview = document.createElement('button'); preview.className = 'message-action preview-action'; preview.textContent = 'Open preview'; preview.addEventListener('click', () => canvas.show(page)); actions.prepend(preview); }
    const run = current?.runs.find(r => r.id === message.run_id);
    if (run) { const label = document.createElement('span'); label.className = 'message-meta'; label.textContent = `${run.usage.route ? routeLabel(run.usage.route) : run.mode} · ${run.usage.calls || 0} calls${run.usage.review_passed === false ? ' · review unresolved' : ''}${run.status === 'failed' ? ' · incomplete run' : ''}`; actions.append(label); }
    article.append(actions);
  }
  return article;
}
function renderConversation() {
  const hasMessages = Boolean(current?.messages.length);
  document.body.classList.toggle('chat-active', hasMessages);
  el('welcome').hidden = hasMessages; el('messages').hidden = !hasMessages;
  el('messages').replaceChildren(...(current?.messages.map(m => messageNode(m)) || []));
  el('conversation-title').textContent = current?.title || '';
  document.title = current ? `${current.title} — L.E.O` : 'L.E.O';
  el('run-panel').hidden = true; el('run-events').replaceChildren();
  const last = current?.runs.at(-1);
  if (last && !current?.active_run) showRun(last);
  renderHistory(); updateSend(); void refreshUsage();
}
let usageTimer: ReturnType<typeof setTimeout> | undefined;
let usageRequest = 0;
// Per-chat usage, like a coding agent's session sidebar: context level, then each breakdown as percentages.
async function refreshUsage() {
  const id = activeId, request = ++usageRequest;
  clearTimeout(usageTimer);
  if (!id) { el('context-meter').hidden = true; el('usage-panel-body').hidden = true; el('usage-panel-empty').hidden = false; return; }
  try {
    const usage = await api<Report>(`/usage?conversation_id=${encodeURIComponent(id)}`);
    if (activeId !== id || request !== usageRequest) return;
    const context = usage.conversations[0]?.context ?? null, t = usage.totals;
    el('context-meter').hidden = !context;
    if (context) {
      const level = levelOf(context.percent);
      el('context-text').textContent = `${level.name} · ${percent(context.percent)} · ${compact.format(context.tokens)}/${compact.format(context.allowance)}`;
      const fill = el('context-meter').querySelector<HTMLElement>('.meter span')!;
      fill.style.width = `${Math.min(context.percent, 100)}%`; fill.dataset.level = level.key;
      el('context-meter').title = `Context ${level.name.toLowerCase()}: ${percent(context.percent)} of the allowance used by the latest call. Open usage for this chat.`;
    }
    const summary = node('div', '', 'panel-summary');
    for (const [label, value] of [['Tokens', tokens(t.input_tokens + t.output_tokens, t.estimated)], ['Calls', String(t.calls)], ['Success', percent(successRate(t))], ['Est. cost', money(t.cost)]]) {
      const item = node('div'); item.append(node('span', label), node('strong', value)); summary.append(item);
    }
    const contextSection = node('section', '', 'share'); contextSection.append(node('h3', 'Context'), levelMeter(context));
    const calls = node('section', '', 'share'); calls.append(node('h3', 'Calls, newest first'));
    const list = node('ol', '', 'panel-calls');
    for (const c of usage.recent_calls) {
      const item = node('li'); item.dataset.status = c.status;
      item.append(node('span', `${c.stage} · ${c.model}`), node('small', c.status === 'completed' ? `${tokens(c.input_tokens + c.output_tokens, c.estimated)} · ${seconds(c.elapsed_ms)}` : c.status));
      list.append(item);
    }
    calls.append(list);
    el('usage-panel-body').replaceChildren(summary, contextSection, ...breakdowns(usage), calls);
    el('usage-panel-body').hidden = false; el('usage-panel-empty').hidden = true;
  } catch { /* Usage is informational; chat keeps working without it. */ }
  // Keep the panel current while a run is producing calls.
  if (activeRun && activeId === id) usageTimer = setTimeout(() => void refreshUsage(), 3000);
}
document.addEventListener('leo:canvas-open', () => { if (!el('usage-panel').hidden) setUsagePanel(false, false); });
function setUsagePanel(open: boolean, remember = true) {
  if (open && canvas.open) canvas.close();
  el('usage-panel').hidden = !open; document.body.classList.toggle('usage-open', open);
  el('toggle-usage').setAttribute('aria-expanded', String(open)); el('context-meter').setAttribute('aria-expanded', String(open));
  if (remember) try { localStorage.setItem('leo-usage-panel', open ? 'open' : 'closed'); } catch {}
  if (open) void refreshUsage();
}
el('toggle-usage').addEventListener('click', () => setUsagePanel(el('usage-panel').hidden));
el('context-meter').addEventListener('click', () => setUsagePanel(el('usage-panel').hidden));
el('close-usage').addEventListener('click', () => setUsagePanel(false));
function usageText(usage: Usage) {
  if (!usage.calls) return '';
  const tokens = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  return `${usage.calls} model calls · ${usage.estimated ? '~' : ''}${tokens.toLocaleString()} tokens · $${(usage.estimated_cost || 0).toFixed(5)} estimated${usage.uncertain_calls ? ` · ${usage.uncertain_calls} uncertain calls reserved` : ''}${usage.context_messages_omitted ? ` · ${usage.context_messages_omitted} older messages omitted` : ''}`;
}
function showRun(run: Run) {
  el('run-panel').hidden = false;
  el('run-status').textContent = run.error || (run.status === 'completed' ? (run.usage.review_passed === false ? 'Finished · review unresolved' : 'Finished') : run.phase);
  el('run-usage').textContent = usageText(run.usage);
  el('status-dot').dataset.status = run.status;
}
function logActivity(message: string) {
  const li = document.createElement('li'); li.textContent = message; el('run-events').append(li);
  while (el('run-events').children.length > 80) el('run-events').firstChild?.remove();
}
async function refreshHistory() { conversations = await api<Conversation[]>('/conversations'); renderHistory(); }
function renderHistory() {
  el('history-list').replaceChildren(); el('history-empty').hidden = conversations.length > 0;
  for (const chat of conversations) {
    const row = document.createElement('div'); row.className = `history-item${chat.id === activeId ? ' active' : ''}`;
    const button = document.createElement('button'); button.className = 'history-open';
    const span = document.createElement('span'); span.textContent = `${chat.active_run ? '◌ ' : ''}${chat.title}`; button.append(span); button.title = chat.title;
    button.addEventListener('click', () => void openChat(chat.id).catch(e => toast(errorText(e))));
    const remove = document.createElement('button'); remove.className = 'history-delete'; remove.textContent = '×'; remove.setAttribute('aria-label', `Delete ${chat.title}`);
    remove.addEventListener('click', async () => {
      try { await send(`/conversations/${chat.id}`, 'DELETE'); if (activeId === chat.id) newChat(); await refreshHistory(); }
      catch (e) { toast(errorText(e)); }
    }); row.append(button, remove); el('history-list').append(row);
  }
}
async function openChat(id: string) {
  const nav = ++navigation;
  if (id !== activeId) canvas.hide();
  closeStream();
  const data = await api<FullConversation>(`/conversations/${id}`);
  if (nav !== navigation) return;
  activeId = id; current = data; rememberChat(); renderConversation(); closeSidebar(); dialog('search-dialog').close();
  if (data.active_run) watchRun(data.active_run);
}
function newChat() {
  navigation++; canvas.hide(); closeStream(); activeId = null; current = null; rememberChat(); renderConversation(); closeSidebar(); prompt.value = ''; resizePrompt(); prompt.focus(); window.scrollTo({ top: 0 });
}
// Live run view: the orchestration stages light up as the worker reports them.
const STAGES: Record<Mode, [string, string][]> = {
  auto: [['route', 'Route'], ['generate', 'Generate'], ['check', 'Check']],
  quick: [['generate', 'Generate'], ['check', 'Check']],
  reviewed: [['generate', 'Generate'], ['check', 'Check'], ['review', 'Review'], ['repair', 'Repair']],
  deep: [['plan', 'Plan'], ['tasks', 'Tasks'], ['generate', 'Generate'], ['check', 'Check'], ['review', 'Review'], ['repair', 'Repair']],
};
const ACTIVITY: Record<string, string> = { research: 'Searching the web', plan: 'Planning the steps', tasks: 'Working on the subtasks', generate: 'Writing the answer', review: 'Reviewing the answer', repair: 'Fixing review findings' };
let liveClock: ReturnType<typeof setInterval> | undefined;
function stageOf(phase: string) {
  return phase.startsWith('research_') ? 'research' : phase === 'planning' ? 'plan' : phase.startsWith('task_') ? 'tasks' : phase === 'generating' ? 'generate'
    : phase.startsWith('reviewing') ? 'review' : phase.startsWith('repairing') ? 'repair' : null;
}
function stageTrack(stages: [string, string][]) {
  const track = node('ol', '', 'stage-track'); track.setAttribute('aria-label', 'Run stages');
  for (const [key, label] of stages) { const step = node('li', label); step.dataset.stage = key; step.dataset.state = 'pending'; track.append(step); }
  return track;
}
type RouteInfo = { skill: string; difficulty: string; mode: Mode; auto: boolean; signals: string[]; search?: boolean };
function routeLabel(route: RouteInfo) { return `${route.auto ? 'Auto → ' : ''}${route.mode} · ${route.skill} · ${route.difficulty}`; }
function liveNode(mode: Mode, startedAt: number) {
  const live = node('div', '', 'run-live'); live.id = 'run-live';
  const track = stageTrack(STAGES[mode]);
  if (mode === 'auto') { track.querySelector<HTMLElement>('li')!.dataset.state = 'active'; }
  const status = node('p', '', 'live-status'); status.setAttribute('role', 'status');
  status.append(node('span', 'Waiting for a worker', 'live-text'), node('span', '0s', 'live-clock'));
  live.append(track, status);
  clearInterval(liveClock);
  const tick = () => {
    const clock = document.querySelector<HTMLElement>('#run-live .live-clock'); if (!clock) return clearInterval(liveClock);
    const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
    clock.textContent = elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${String(elapsed % 60).padStart(2, '0')}s` : `${elapsed}s`;
  };
  liveClock = setInterval(tick, 1000); requestAnimationFrame(tick);
  return live;
}
function setStage(key: string, state: 'active' | 'done' | 'issues' | 'skipped', note = '') {
  const steps = [...document.querySelectorAll<HTMLElement>('#run-live .stage-track li')];
  const index = steps.findIndex(s => s.dataset.stage === key); if (index < 0) return;
  if (state === 'active') {
    steps.forEach((s, i) => { if (s.dataset.state === 'active') s.dataset.state = 'done'; if (i < index && s.dataset.state === 'pending') s.dataset.state = 'done'; });
  }
  steps[index].dataset.state = state;
  if (note) steps[index].title = note;
}
function setActivity(text: string) { const target = document.querySelector('#run-live .live-text'); if (target) target.textContent = text; }
function renderDraft() {
  const target = document.getElementById('pending-content'); if (!target) return;
  renderContent(target, draft);
  if (!draft) { target.append(...[78, 92, 54].map(width => { const line = node('span', '', 'skeleton-line'); line.style.width = `${width}%`; return line; })); return; }
  const caret = node('span', '', 'stream-caret'); caret.setAttribute('aria-hidden', 'true');
  // Put the caret where the text is growing: the last paragraph, list item, or code line.
  let last = target.lastElementChild as HTMLElement | null;
  while (last && (last.tagName === 'UL' || last.tagName === 'OL' || last.tagName === 'BLOCKQUOTE') && last.lastElementChild) last = last.lastElementChild as HTMLElement;
  (last?.classList.contains('code-block') ? last.querySelector('code') : last ?? target)!.append(caret);
}
function watchRun(id: string) {
  closeStream(); activeRun = id; draft = ''; updateSend(); canvas.reset();
  el('run-panel').hidden = true; el('run-status').textContent = 'Queued';  // The live view shows progress; the summary returns when the run ends. el('run-usage').textContent = ''; el('status-dot').dataset.status = 'running'; el('run-events').replaceChildren();
  el('messages').append(messageNode({ role: 'assistant', content: '' }, true));
  const run = current?.runs.find(r => r.id === id);
  const mode = (run?.mode || select('composer-mode').value) as Mode;
  el('pending-response').insertBefore(liveNode(mode, run?.created_at ?? Date.now() / 1000), el('pending-content'));
  renderDraft();
  void refreshUsage();
  stream = new EventSource(`/api/runs/${id}/events`);
  const source = stream;
  const listen = (kind: string, handler: (data: any) => void) => source.addEventListener(kind, event => {
    if (activeRun !== id) return;
    try { handler(JSON.parse((event as MessageEvent).data)); } catch { toast('Unable to read a progress update'); }
  });
  let planned = 0, finished = 0;
  listen('route', (data: RouteInfo) => {
    // The head decided: rebuild the track for the chosen mode, keeping the decision visible.
    const stages = [...STAGES[data.mode]];
    if (data.search) stages.splice(data.mode === 'deep' ? 1 : 0, 0, ['research', 'Research']);
    const track = stageTrack([['route', routeLabel(data)], ...stages]);
    const first = track.querySelector<HTMLElement>('li')!; first.dataset.state = 'done';
    first.title = `Matched ${data.skill} at ${data.difficulty} difficulty${data.signals.length ? ` (${data.signals.join(', ')})` : ''}`;
    document.querySelector('#run-live .stage-track')?.replaceWith(track);
    setActivity(`Routed as ${data.skill} · ${data.difficulty}${data.auto ? `, chose ${data.mode} mode` : ''}`);
    logActivity(`Route: ${routeLabel(data)}${data.signals.length ? ` (${data.signals.join(', ')})` : ''}`);
  });
  listen('status', data => {
    el('run-status').textContent = data.message; logActivity(data.message);
    const stage = stageOf(data.phase || '');
    if (stage) { setStage(stage, 'active'); setActivity(ACTIVITY[stage]); }
  });
  listen('model', data => {
    el('composer-note').textContent = `${data.provider} · ${data.model}`;
    if (data.reason) logActivity(`${data.stage}${data.skill ? ` [${data.skill} · ${data.difficulty}]` : ''} → ${data.model}: ${data.reason}`);
    const stage = stageOf(data.stage || '');
    if (stage) setActivity(`${ACTIVITY[stage]}${stage === 'tasks' && planned ? ` (${finished + 1} of ${planned})` : ''} with ${data.model}`);
  });
  listen('draft_reset', () => { draft = ''; renderDraft(); });
  listen('token', data => {
    draft += data.text; renderDraft(); scrollToLatest();
    const code = streamingHtml(draft); if (code !== null) canvas.stream(code);
  });
  listen('plan', data => { logActivity(`Plan: ${data.text}`); planned = String(data.text || '').split('\n').filter(Boolean).length; setStage('plan', 'done', `${planned} task${planned === 1 ? '' : 's'}`); });
  listen('task', data => { finished += 1; logActivity(`Task ${data.id}${data.skill ? ` (${data.skill})` : ''} completed: ${data.instruction}`); });
  listen('warning', data => logActivity(data.message));
  listen('tool', (data: { stage: string; queries: string[]; sources: { n: number; title: string; url: string }[] }) => {
    const hosts = data.sources.map(s => { try { return new URL(s.url).hostname.replace(/^www\./, ''); } catch { return s.url; } });
    setStage('research', data.sources.length ? 'done' : 'issues', data.sources.map(s => `[${s.n}] ${s.title}`).join('\n') || 'No usable pages');
    setActivity(data.sources.length ? `Read ${data.sources.length} page${data.sources.length === 1 ? '' : 's'}: ${hosts.join(', ')}` : 'No usable pages found');
    if (data.queries.length) logActivity(`Searched: ${data.queries.join(' · ')}`);
    for (const source of data.sources) logActivity(`[${source.n}] ${source.title} — ${source.url}`);
  });
  listen('validation', data => {
    const checks: { language: string; passed: boolean; message: string }[] = data.checks || [];
    for (const check of checks) logActivity(`${check.language}: ${check.message}`);
    const failed = checks.filter(c => !c.passed).length;
    // In a chained Deep run the last subtask writes the answer, so there is no separate generate step.
    const generate = document.querySelector<HTMLElement>('#run-live .stage-track li[data-stage="generate"]');
    if (generate?.dataset.state === 'pending') setStage('generate', 'done', 'The last subtask wrote the answer');
    setStage('check', failed ? 'issues' : 'done', checks.length ? `${checks.length - failed} of ${checks.length} code blocks parse` : 'No Python or JSON code to check');
    if (mode === 'quick') setActivity('Finishing up');
  });
  listen('review', data => {
    logActivity(data.passed ? 'Review passed' : `Review findings: ${(data.issues || []).join('; ')}`);
    setStage('review', data.passed ? 'done' : 'issues', data.passed ? 'Passed' : (data.issues || []).join('; '));
    if (data.passed) { setStage('repair', 'skipped', 'Not needed'); setActivity('Review passed, finishing up'); }
  });
  listen('done', async data => {
    source.close(); activeRun = null; stream = null; updateSend(); clearInterval(liveClock);
    const page = data.status === 'completed' ? extractPage(data.output || '') : null;
    if (page) canvas.finish(page);
    logActivity(data.error || `Run ${data.status}`);
    const events = [...el('run-events').children].map(n => n.textContent || '');
    if (activeId) {
      try { await openChat(activeId); events.forEach(logActivity); await refreshHistory(); }
      catch (e) { toast(errorText(e)); }
    }
  });
  source.onerror = async () => {
    if (activeRun !== id) return;
    el('run-status').textContent = 'Reconnecting to run…';
    try {
      const run = await api<Run>(`/runs/${id}`);
      if (activeRun !== id) return;
      if (['completed', 'failed', 'cancelled'].includes(run.status)) { source.close(); if (activeId) await openChat(activeId); await refreshHistory(); }
    } catch { /* EventSource resumes from its last event ID automatically. */ }
  };
}
async function stopRun() {
  if (!activeRun) return;
  try { await send(`/runs/${activeRun}/cancel`, 'POST'); el('run-status').textContent = 'Stopping…'; }
  catch (e) { toast(errorText(e)); }
}
el('stop-run').addEventListener('click', stopRun);
el('chat-form').addEventListener('submit', async event => {
  event.preventDefault(); if (activeRun) { await stopRun(); return; }
  const text = prompt.value.trim(); if (!text || submitting || !ready) return;
  submitting = true; updateSend();
  try {
    const search = el('search-toggle').getAttribute('aria-pressed') === 'true' ? true : null;
    const run = await send<Run>('/runs', 'POST', { prompt: text, conversation_id: activeId, mode: select('composer-mode').value, search, request_id: crypto.randomUUID() });
    el('search-toggle').setAttribute('aria-pressed', 'false');
    prompt.value = ''; resizePrompt(); await refreshHistory(); await openChat(run.conversation_id);
  } catch (e) { toast(errorText(e)); }
  finally { submitting = false; updateSend(); }
});
prompt.addEventListener('input', resizePrompt);
prompt.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!activeRun) el<HTMLFormElement>('chat-form').requestSubmit(); } });

function refreshConnectionStatus() {
  const enabled = connections.filter(p => p.enabled);
  el('connection-status').textContent = ready ? `${enabled.length} model${enabled.length === 1 ? '' : 's'} connected` : 'Server offline';
  el('connect-models').hidden = enabled.length > 0;
  el('composer-note').textContent = enabled.length ? 'Auto picks the right model for each task. Answers can be wrong; check what matters.' : 'Connect a local model or API in Settings.';
}
function roleOptions(value: string | null) {
  return [new Option('Auto · follow fallback order', ''), ...connections.map(p => new Option(`${p.name} · ${p.model}${p.enabled ? '' : ' (disabled)'}`, p.id, false, p.id === value))];
}
// Fields the connection API accepts; used to flip one setting without resending the key.
const CONNECTION_FIELDS = ['name', 'protocol', 'base_url', 'model', 'local', 'free', 'enabled', 'priority', 'context_window', 'input_price', 'output_price', 'token_parameter', 'skills', 'tier', 'reasoning'] as const;
const openGroups = new Set<string>();
function groupLabel(list: Provider[]) {
  const preset = presets.find(x => x.base_url === list[0].base_url && x.id !== 'custom');
  if (preset) return preset.name;
  const prefix = list[0].name.split(' · ')[0];
  if (list.every(p => p.name.startsWith(prefix))) return prefix;
  try { return new URL(list[0].base_url).host; } catch { return list[0].base_url; }
}
function modelLabel(p: Provider) {
  // Discovered models are named "<provider> · <model>"; the group header already names the provider.
  const parts = p.name.split(' · ');
  return parts.length > 1 ? parts.slice(1).join(' · ') : p.name;
}
async function setEnabled(p: Provider, enabled: boolean) {
  const body = Object.fromEntries(CONNECTION_FIELDS.map(key => [key, p[key]]));
  try { await send(`/connections/${p.id}`, 'PUT', { ...body, enabled }); await loadSettings(); } catch (e) { toast(errorText(e)); await loadSettings(); }
}
function skillNames(ids: string[]) { return ids.map(id => skills.find(s => s.id === id)?.name || id).join(', '); }
function modelRow(p: Provider) {
  const row = document.createElement('div'); row.className = `model-row${p.enabled ? '' : ' off'}`;
  const toggle = Object.assign(document.createElement('input'), { type: 'checkbox', className: 'switch', checked: p.enabled });
  toggle.setAttribute('aria-label', `Use ${modelLabel(p)}`);
  toggle.addEventListener('change', () => void setEnabled(p, toggle.checked));
  const detail = document.createElement('div'); detail.className = 'model-detail'; detail.title = p.model;
  const name = document.createElement('strong'); name.textContent = modelLabel(p);
  const strength = p.tier[0].toUpperCase() + p.tier.slice(1);
  const profile = document.createElement('small');
  profile.textContent = `${strength}${p.skills.length ? `, ${skillNames(p.skills).toLowerCase()}` : ', any task'}${p.free ? '' : ', paid'}`;
  detail.append(name, profile);
  if (!p.has_key && !p.local) {
    // Cloud models almost always need a key; make a missing one impossible to miss.
    row.classList.add('needs-key');
    detail.append(Object.assign(document.createElement('small'), { className: 'key-warning', textContent: 'No API key saved. Click Edit and paste the key.' }));
  }
  const edit = document.createElement('button'); edit.className = 'text-button'; edit.textContent = 'Edit';
  edit.setAttribute('aria-label', `Edit ${modelLabel(p)}`);
  edit.addEventListener('click', () => editProvider(p, row));
  row.append(toggle, detail, edit);
  return row;
}
function renderProviders() {
  // Forms open inside the list, under the row being edited; move them out before the list is rebuilt.
  el('provider-list').before(el('provider-form')); el('provider-list').after(el('discover-panel'));
  el('provider-list').replaceChildren();
  if (!connections.length) { const p = document.createElement('p'); p.className = 'empty-providers'; p.textContent = 'No models connected. Start with Ollama or your preferred API.'; el('provider-list').append(p); }
  // One collapsible group per provider endpoint: a key added once shows up as one row, its models inside.
  const groups = new Map<string, Provider[]>();
  for (const p of connections) groups.set(p.base_url, [...(groups.get(p.base_url) || []), p]);
  for (const [url, list] of groups) {
    list.sort((a, b) => Number(b.enabled) - Number(a.enabled));
    const on = list.filter(p => p.enabled).length;
    const missingKey = list.some(p => !p.has_key && !p.local);
    const group = document.createElement('details'); group.className = 'provider-group';
    group.open = openGroups.has(url) || missingKey;
    group.addEventListener('toggle', () => { if (group.open) openGroups.add(url); else openGroups.delete(url); });
    const summary = document.createElement('summary');
    const chevron = document.createElement('span'); chevron.className = 'chevron'; chevron.setAttribute('aria-hidden', 'true');
    const title = document.createElement('div'); title.className = 'group-title';
    const name = document.createElement('strong'); name.textContent = groupLabel(list);
    const status = document.createElement('small');
    status.textContent = `${on} of ${list.length} in use${missingKey ? ', key missing' : ''}${list[0].local ? ', local' : list.every(p => p.free) ? ', free' : list.some(p => p.free) ? '' : ', paid'}`;
    if (missingKey) group.classList.add('needs-key');
    title.append(name, status);
    summary.append(chevron, title);
    const keyed = list.find(p => p.has_key || p.local);
    if (keyed) {
      const add = document.createElement('button'); add.type = 'button'; add.className = 'text-button'; add.textContent = 'Add models';
      add.addEventListener('click', event => { event.preventDefault(); group.after(el('discover-panel')); void discoverModels(keyed); });
      const actions = document.createElement('div'); actions.className = 'provider-actions'; actions.append(add); summary.append(actions);
    }
    const rows = document.createElement('div'); rows.className = 'model-rows'; rows.append(...list.map(modelRow));
    group.append(summary, rows); el('provider-list').append(group);
  }
  for (const role of ['generator', 'planner', 'reviewer'] as const) select(`${role}-id`).replaceChildren(...roleOptions(preferences?.[`${role}_id`] || null));
  refreshConnectionStatus();
}
const numericFields = ['max_cost', 'max_calls', 'max_repairs', 'max_output_tokens', 'context_tokens', 'token_budget', 'timeout_seconds', 'max_run_seconds'] as const;
async function loadSettings() {
  [connections, preferences, presets, skills] = await Promise.all([api<Provider[]>('/connections'), api<Preferences>('/settings'), api<Preset[]>('/presets'), api<Skill[]>('/skills')]);
  select('provider-preset').replaceChildren(...presets.map(p => new Option(p.name, p.id)));
  renderProviders(); renderSkills();
  select('routing').value = preferences.routing; select('mode-select').value = preferences.mode; select('composer-mode').value = preferences.mode;
  field('smart-routing').checked = preferences.smart_routing ?? true;
  select('web-search').value = preferences.web_search || 'auto'; field('search-url').value = preferences.search_url || ''; field('search-pages').value = String(preferences.search_pages ?? 3);
  field('allow-paid').checked = preferences.allow_paid; el<HTMLTextAreaElement>('system-prompt').value = preferences.system_prompt;
  for (const key of numericFields) field(key.replaceAll('_', '-')).value = String(preferences[key]);
}
function applyPreset() {
  const preset = presets.find(p => p.id === select('provider-preset').value); if (!preset) return;
  field('provider-name').value = preset.name; field('provider-url').value = preset.base_url; select('provider-protocol').value = preset.protocol;
  field('provider-local').checked = Boolean(preset.local); field('provider-free').checked = Boolean(preset.free); el('provider-hint').textContent = preset.hint;
  select('provider-tier').value = preset.tier || 'standard'; field('provider-reasoning').checked = Boolean(preset.reasoning); renderSkillPicker(preset.skills || []);
  renderKeySources();
}
// One credential can serve several models from the same provider host.
function renderKeySources() {
  let host = ''; try { host = new URL(field('provider-url').value).hostname; } catch {}
  const own = field('provider-id').value;
  const sources = connections.filter(c => c.has_key && c.id !== own && (() => { try { return new URL(c.base_url).hostname === host; } catch { return false; } })());
  select('provider-key-from').replaceChildren(new Option('No, use the key above', ''), ...sources.map(c => new Option(`${c.name} · ${c.model}`, c.id)));
  el('provider-key-from-row').hidden = !sources.length;
}
field('provider-url').addEventListener('input', renderKeySources);
// Discover every model a saved key can use (OpenRouter: free only), with suggested profiles, and add them in one go.
type Discovered = { model: string; name: string; tier: string; skills: string[]; reasoning: boolean; context_window: number; free: boolean | null; description: string; added?: boolean };
let discoverSource: Provider | null = null;
let discovered: Discovered[] = [];
async function discoverModels(source: Provider, message = '') {
  discoverSource = source; discovered = [];
  el('discover-panel').hidden = false; el('discover-list').replaceChildren(); el('discover-feedback').textContent = message;
  el('discover-title').textContent = `Models available with ${source.name.split(' · ')[0]}`;
  el('discover-hint').textContent = 'Reading the provider catalog…'; el('discover-panel').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  try {
    const result = await send<{ provider: string; models: Discovered[]; skipped: { model: string; reason: string }[] }>(`/connections/${source.id}/discover`, 'POST');
    discovered = result.models;
    el('discover-hint').textContent = `${result.models.length} ${result.provider === 'openrouter' ? 'free ' : ''}chat models found. Strength and skills are suggestions; change them before adding if you like.`;
    el('discover-list').replaceChildren(...discovered.map((m, index) => {
      const row = document.createElement('label'); row.className = `discover-row${m.added ? ' added' : ''}`; row.title = m.description;
      const box = document.createElement('input'); box.type = 'checkbox'; box.checked = !m.added; box.disabled = Boolean(m.added); box.dataset.index = String(index);
      const info = node('span', '', 'discover-info');
      info.append(node('strong', m.name.replace(/ \(free\)$/, '')), node('small', `${m.model} · ${Math.round(m.context_window / 1000)}K context${m.reasoning ? ' · thinks' : ''} · ${m.skills.length ? m.skills.join(', ') : 'generalist'}${m.added ? ' · already added' : ''}`));
      const tier = document.createElement('select'); tier.setAttribute('aria-label', `Strength of ${m.name}`);
      for (const value of ['light', 'standard', 'strong']) tier.append(new Option(value[0].toUpperCase() + value.slice(1), value, false, value === m.tier));
      tier.addEventListener('change', () => { discovered[index].tier = tier.value; });
      row.append(box, info, tier); return row;
    }));
  } catch (e) { el('discover-hint').textContent = errorText(e); }
}
el('discover-close').addEventListener('click', () => { el('discover-panel').hidden = true; });
el('discover-none').addEventListener('click', () => el('discover-list').querySelectorAll<HTMLInputElement>('input[type=checkbox]:not(:disabled)').forEach(box => { box.checked = false; }));
el('discover-add').addEventListener('click', async () => {
  if (!discoverSource) return;
  const models = [...el('discover-list').querySelectorAll<HTMLInputElement>('input[type=checkbox]:checked:not(:disabled)')].map(box => discovered[Number(box.dataset.index)]);
  if (!models.length) { el('discover-feedback').textContent = 'Select at least one model.'; return; }
  const check = field('discover-check').checked;
  el('discover-feedback').textContent = check ? `Checking access to ${models.length} model${models.length === 1 ? '' : 's'}…` : 'Adding…';
  try {
    const result = await send<{ created: Provider[]; skipped: { model: string; reason: string }[] }>(`/connections/${discoverSource.id}/adopt`, 'POST', { models, check });
    const message = `Added ${result.created.length}.` + (result.skipped.length ? ` Skipped: ${result.skipped.map(s => `${s.model} (${s.reason})`).join('; ')}` : '');
    await loadSettings(); void discoverModels(discoverSource, message);
  } catch (e) { el('discover-feedback').textContent = errorText(e); }
});
function renderSkillPicker(selected: string[]) {
  el('provider-skills').replaceChildren(...skills.map(skill => {
    const label = document.createElement('label'); label.className = 'skill-option'; label.title = skill.description;
    const box = document.createElement('input'); box.type = 'checkbox'; box.value = skill.id; box.checked = selected.includes(skill.id);
    label.append(box, ` ${skill.name}${skill.enabled ? '' : ' (disabled)'}`); return label;
  }));
}
function skillBody(skill: Skill, enabled = skill.enabled) {
  return { name: skill.name, description: skill.description, keywords: skill.keywords, instructions: skill.instructions, enabled };
}
function renderSkills() {
  el('skill-list').before(el('skill-form'));
  el('skill-list').replaceChildren(...skills.map(skill => {
    const row = document.createElement('div'); row.className = `model-row${skill.enabled ? '' : ' off'}`;
    const toggle = Object.assign(document.createElement('input'), { type: 'checkbox', className: 'switch', checked: skill.enabled });
    toggle.setAttribute('aria-label', `Use the ${skill.name} skill`);
    toggle.addEventListener('change', async () => {
      try { await send(`/skills/${skill.id}`, 'PUT', skillBody(skill, toggle.checked)); } catch (e) { toast(errorText(e)); }
      await loadSettings();
    });
    const detail = document.createElement('div'); detail.className = 'model-detail';
    const name = document.createElement('strong'); name.textContent = skill.name;
    const info = document.createElement('small'); info.className = 'one-line'; info.textContent = skill.description || 'No description';
    detail.append(name, info);
    const models = connections.filter(c => c.enabled && c.skills.includes(skill.id));
    const count = document.createElement('span'); count.className = `row-count${models.length ? '' : ' empty'}`;
    count.textContent = models.length ? `${models.length} model${models.length === 1 ? '' : 's'}` : 'No model';
    count.title = models.length ? models.map(modelLabel).join('\n') : 'Turn on a model with this skill, or tasks go to a generalist';
    const edit = document.createElement('button'); edit.className = 'text-button'; edit.textContent = 'Edit';
    edit.setAttribute('aria-label', `Edit the ${skill.name} skill`);
    edit.addEventListener('click', () => editSkill(skill, row));
    row.append(toggle, detail, count, edit); return row;
  }));
}
function editSkill(skill?: Skill, anchor?: Element) {
  el<HTMLFormElement>('skill-form').reset(); el('skill-form').hidden = false; el('skill-feedback').textContent = '';
  if (anchor) anchor.after(el('skill-form')); else el('skill-list').before(el('skill-form'));
  field('skill-id').value = skill?.id || ''; field('skill-name').value = skill?.name || ''; field('skill-description').value = skill?.description || '';
  field('skill-keywords').value = skill?.keywords.join(', ') || ''; el<HTMLTextAreaElement>('skill-instructions').value = skill?.instructions || '';
  field('skill-enabled').checked = skill?.enabled ?? true; el('remove-skill').hidden = !skill;
  el('skill-form').scrollIntoView({ block: 'nearest', behavior: 'smooth' }); field('skill-name').focus({ preventScroll: true });
}
el('remove-skill').addEventListener('click', async () => {
  const id = field('skill-id').value; if (!id) return;
  try { await send(`/skills/${id}`, 'DELETE'); el('skill-form').hidden = true; await loadSettings(); toast('Skill removed'); } catch (e) { el('skill-feedback').textContent = errorText(e); }
});
el('add-skill').addEventListener('click', () => editSkill());
el('cancel-skill').addEventListener('click', () => { el('skill-form').hidden = true; });
el('skill-form').addEventListener('submit', async event => {
  event.preventDefault();
  const id = field('skill-id').value;
  const body = { name: field('skill-name').value.trim(), description: field('skill-description').value.trim(), keywords: field('skill-keywords').value.split(','),
    instructions: el<HTMLTextAreaElement>('skill-instructions').value.trim(), enabled: field('skill-enabled').checked };
  try { await send(id ? `/skills/${id}` : '/skills', id ? 'PUT' : 'POST', body); el('skill-form').hidden = true; await loadSettings(); toast('Skill saved'); }
  catch (e) { el('skill-feedback').textContent = errorText(e); }
});
function editProvider(provider?: Provider, anchor?: Element) {
  el<HTMLFormElement>('provider-form').reset(); el('provider-form').hidden = false; el('provider-feedback').textContent = ''; el('model-catalog').replaceChildren();
  if (anchor) anchor.after(el('provider-form')); else el('provider-list').before(el('provider-form'));
  el('remove-provider').hidden = !provider; el('check-provider').hidden = !provider;
  field('provider-id').value = provider?.id || ''; field('provider-key').value = '';
  field('provider-key').placeholder = provider?.has_key ? 'Leave blank to keep the saved key' : 'Optional for local servers';
  if (!provider) { select('provider-preset').value = 'ollama'; applyPreset(); }
  else {
    select('provider-preset').value = presets.find(p => p.base_url === provider.base_url)?.id || 'custom';
    for (const [id, value] of Object.entries({ name: provider.name, url: provider.base_url, model: provider.model, context: provider.context_window, priority: provider.priority, 'input-price': provider.input_price ?? '', 'output-price': provider.output_price ?? '' })) field(`provider-${id}`).value = String(value);
    select('provider-protocol').value = provider.protocol; select('provider-token-param').value = provider.token_parameter;
    for (const key of ['local', 'free', 'enabled', 'reasoning'] as const) field(`provider-${key}`).checked = Boolean(provider[key]);
    select('provider-tier').value = provider.tier || 'standard'; renderSkillPicker(provider.skills || []); renderKeySources();
    el('provider-hint').textContent = presets.find(p => p.id === select('provider-preset').value)?.hint || '';
  }
  syncAdvanced();
  el('provider-form').scrollIntoView({ block: 'nearest', behavior: 'smooth' }); field('provider-name').focus({ preventScroll: true });
}
// Address and prices live under a fold; open it when they matter (custom servers, paid models).
function syncAdvanced() {
  const preset = select('provider-preset').value;
  if (preset === 'custom' || preset === 'vllm' || !field('provider-free').checked) el<HTMLDetailsElement>('provider-advanced').open = true;
}
el('provider-form').addEventListener('invalid', event => {
  if (el('provider-advanced').contains(event.target as Node)) el<HTMLDetailsElement>('provider-advanced').open = true;
}, true);
el('check-provider').addEventListener('click', async () => {
  const id = field('provider-id').value; if (!id) return;
  el('provider-feedback').textContent = 'Checking the model catalog…';
  try {
    const result = await send<{ models: string[]; model_found: boolean }>(`/connections/${id}/test`, 'POST');
    el('model-catalog').replaceChildren(...result.models.map(m => new Option(m, m)));
    el('provider-feedback').textContent = result.model_found ? 'Connected, and this model is listed.' : 'Connected, but this model is not in the catalog. Pick a listed ID or confirm it with your provider.';
  } catch (e) { el('provider-feedback').textContent = errorText(e); }
});
el('remove-provider').addEventListener('click', async () => {
  const id = field('provider-id').value; if (!id) return;
  try { await send(`/connections/${id}`, 'DELETE'); el('provider-form').hidden = true; await loadSettings(); toast('Model removed'); } catch (e) { el('provider-feedback').textContent = errorText(e); }
});
select('provider-preset').addEventListener('change', () => { applyPreset(); syncAdvanced(); });
el('add-connection').addEventListener('click', () => editProvider());
el('cancel-provider').addEventListener('click', () => { field('provider-key').value = ''; el('provider-form').hidden = true; });
el('provider-form').addEventListener('submit', async event => {
  event.preventDefault();
  const id = field('provider-id').value;
  const price = (name: string) => field(name).value === '' ? null : Number(field(name).value);
  const body = {
    name: field('provider-name').value.trim(), protocol: select('provider-protocol').value, base_url: field('provider-url').value.trim(), model: field('provider-model').value.trim(),
    api_key: field('provider-clear-key').checked ? '' : field('provider-key').value.trim() || null,
    key_from: select('provider-key-from').value || null,
    local: field('provider-local').checked, free: field('provider-free').checked, enabled: field('provider-enabled').checked,
    priority: Number(field('provider-priority').value), context_window: Number(field('provider-context').value), input_price: price('provider-input-price'), output_price: price('provider-output-price'), token_parameter: select('provider-token-param').value,
    tier: select('provider-tier').value, reasoning: field('provider-reasoning').checked,
    skills: [...el('provider-skills').querySelectorAll<HTMLInputElement>('input:checked')].map(box => box.value),
  };
  try {
    await send(id ? `/connections/${id}` : '/connections', id ? 'PUT' : 'POST', body);
    field('provider-key').value = ''; el('provider-form').hidden = true; await loadSettings(); toast('Model connection saved');
  } catch (e) { el('provider-feedback').textContent = errorText(e); }
});
el('orchestration-form').addEventListener('submit', async event => {
  event.preventDefault();
  const body: Preferences = { ...preferences, mode: select('mode-select').value as Mode, routing: select('routing').value, allow_paid: field('allow-paid').checked, smart_routing: field('smart-routing').checked,
    web_search: select('web-search').value, search_url: field('search-url').value.trim(), search_pages: Number(field('search-pages').value), system_prompt: el<HTMLTextAreaElement>('system-prompt').value };
  for (const key of numericFields) body[key] = Number(field(key.replaceAll('_', '-')).value);
  for (const role of ['generator', 'planner', 'reviewer'] as const) body[`${role}_id`] = select(`${role}-id`).value || null;
  try { preferences = await send<Preferences>('/settings', 'PUT', body); select('composer-mode').value = preferences.mode; el('settings-feedback').textContent = 'Saved. These settings apply to new runs.'; }
  catch (e) { el('settings-feedback').textContent = errorText(e); }
});
el('test-search').addEventListener('click', async () => {
  el('settings-feedback').textContent = 'Testing search… (save first if you changed the address)';
  try {
    const result = await send<{ results: number; titles: string[] }>('/search/test', 'POST');
    el('settings-feedback').textContent = `Search works: ${result.results} results, e.g. “${result.titles[0] ?? ''}”.`;
  } catch (e) { el('settings-feedback').textContent = errorText(e); }
});
// The globe forces a web search for the next message; otherwise HEAD decides from the request.
el('search-toggle').addEventListener('click', () => {
  const on = el('search-toggle').getAttribute('aria-pressed') !== 'true';
  el('search-toggle').setAttribute('aria-pressed', String(on));
});
const settingsTabs = [...document.querySelectorAll<HTMLButtonElement>('.settings-tabs [role=tab]')];
function showSettingsTab(tab: HTMLButtonElement, focus = false) {
  for (const other of settingsTabs) {
    const selected = other === tab;
    other.setAttribute('aria-selected', String(selected)); other.tabIndex = selected ? 0 : -1;
    el(other.getAttribute('aria-controls')!).hidden = !selected;
  }
  // Routing, search, and limits share one form and one Save button.
  el('settings-save').hidden = !['tab-routing', 'tab-search', 'tab-limits'].includes(tab.id);
  if (focus) tab.focus();
  try { localStorage.setItem('leo-settings-tab', tab.id); } catch { /* Remembering the tab is optional. */ }
}
settingsTabs.forEach((tab, index) => {
  tab.addEventListener('click', () => showSettingsTab(tab));
  tab.addEventListener('keydown', event => {
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[event.key];
    if (step) { event.preventDefault(); showSettingsTab(settingsTabs[(index + step + settingsTabs.length) % settingsTabs.length], true); }
    else if (event.key === 'Home' || event.key === 'End') { event.preventDefault(); showSettingsTab(settingsTabs[event.key === 'Home' ? 0 : settingsTabs.length - 1], true); }
  });
});
function openSettings(tabId?: string) {
  let saved: string | null = null;
  try { saved = localStorage.getItem('leo-settings-tab'); } catch { /* Fall back to the first tab. */ }
  showSettingsTab(settingsTabs.find(t => t.id === (tabId || saved)) || settingsTabs[0]);
  dialog('settings-dialog').showModal(); void loadSettings().catch(e => toast(errorText(e)));
}
el('open-settings').addEventListener('click', () => openSettings());
el('connect-models').addEventListener('click', () => { openSettings('tab-models'); editProvider(); });

function setTheme(theme: string) {
  document.documentElement.dataset.theme = theme; select('theme-select').value = theme;
  try { localStorage.setItem('leo-theme', theme); } catch {}
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'light' ? '#ffffff' : '#151515');
}
select('theme-select').addEventListener('change', () => setTheme(select('theme-select').value));
setTheme(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');
function syncSidebar() { const mobile = matchMedia('(max-width: 800px)').matches; const expanded = mobile ? document.body.classList.contains('sidebar-open') : !document.body.classList.contains('sidebar-collapsed'); el('sidebar').inert = !expanded; document.querySelector<HTMLElement>('.app-shell')!.inert = mobile && expanded; el('open-sidebar').setAttribute('aria-expanded', String(expanded)); }
function closeSidebar() { document.body.classList.remove('sidebar-open'); document.body.classList.add('sidebar-collapsed'); syncSidebar(); }
el('open-sidebar').addEventListener('click', () => { document.body.classList.toggle(matchMedia('(max-width: 800px)').matches ? 'sidebar-open' : 'sidebar-collapsed'); syncSidebar(); });
el('collapse-sidebar').addEventListener('click', closeSidebar); el('sidebar-scrim').addEventListener('click', closeSidebar); window.addEventListener('resize', syncSidebar);
el('new-chat').addEventListener('click', newChat); el('header-new-chat').addEventListener('click', newChat);
function renderSearch() {
  const query = field('search-input').value.toLowerCase(); el('search-results').replaceChildren();
  for (const chat of conversations.filter(c => c.title.toLowerCase().includes(query))) {
    const button = document.createElement('button'); button.className = 'search-result'; button.textContent = chat.title; button.addEventListener('click', () => void openChat(chat.id).catch(e => toast(errorText(e)))); el('search-results').append(button);
  }
  if (!el('search-results').children.length) el('search-results').textContent = 'No matching conversations.';
}
function openSearch() { field('search-input').value = ''; renderSearch(); dialog('search-dialog').showModal(); field('search-input').focus(); }
el('search-chats').addEventListener('click', openSearch); field('search-input').addEventListener('input', renderSearch);
document.querySelectorAll<HTMLButtonElement>('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog')?.close()));
dialog('settings-dialog').addEventListener('close', () => { field('provider-key').value = ''; el('clear-confirmation').hidden = true; });
el('clear-history').addEventListener('click', () => el('clear-confirmation').hidden = false);
el('cancel-clear').addEventListener('click', () => el('clear-confirmation').hidden = true);
el('confirm-clear').addEventListener('click', async () => {
  try { await refreshHistory(); if (conversations.some(c => c.active_run)) throw new Error('Stop active runs before clearing history'); for (const c of conversations) await send(`/conversations/${c.id}`, 'DELETE'); newChat(); await refreshHistory(); el('clear-confirmation').hidden = true; toast('History cleared'); }
  catch (e) { toast(errorText(e)); }
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') closeSidebar();
  if ((event.metaKey || event.ctrlKey) && ['k', '/'].includes(event.key.toLowerCase())) {
    event.preventDefault(); document.querySelectorAll<HTMLDialogElement>('dialog[open]').forEach(d => d.close()); event.key === '/' ? openSearch() : newChat();
  }
});
el('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await send('/session', 'POST', { token: field('access-token').value }); field('access-token').value = ''; dialog('login-dialog').close(); await initialize(); }
  catch (e) { el('login-feedback').textContent = errorText(e); }
});
async function initialize() {
  try {
    await loadSettings(); await refreshHistory(); ready = true; refreshConnectionStatus();
    let saved: string | null = null; try { saved = localStorage.getItem('leo-active-chat'); } catch {}
    if (saved && conversations.some(c => c.id === saved)) await openChat(saved);
  } catch (e) { ready = false; el('connection-status').textContent = 'Server unavailable'; el('composer-note').textContent = 'Start the L.E.O. backend, then reload this page.'; toast(errorText(e)); }
  updateSend();
}

// Keep the existing subtle pointer effect without idle animation work.
let ambientFrame = 0;
const ambient = document.querySelector<HTMLElement>('.ambient-background')!;
window.addEventListener('pointermove', event => {
  if (!matchMedia('(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)').matches || ambientFrame) return;
  ambientFrame = requestAnimationFrame(() => { ambient.style.setProperty('--pointer-x', `${event.clientX}px`); ambient.style.setProperty('--pointer-y', `${event.clientY}px`); ambient.style.setProperty('--pointer-active', '1'); ambientFrame = 0; });
}, { passive: true });
document.documentElement.addEventListener('pointerleave', () => ambient.style.setProperty('--pointer-active', '0'));
try { if (localStorage.getItem('leo-usage-panel') === 'open') setUsagePanel(true); } catch {}
syncSidebar(); resizePrompt(); void initialize();
