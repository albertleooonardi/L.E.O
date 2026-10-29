import { canvas, extractPage, streamingHtml } from './canvas';
import { breakdowns, compact, levelMeter, levelOf, money, node, percent, seconds, successRate, tokens, type Report } from './usage-ui';

type Mode = 'auto' | 'quick' | 'reviewed' | 'deep';
type Provider = { id: string; name: string; protocol: string; base_url: string; model: string; local: boolean; free: boolean; enabled: boolean; has_key: boolean; priority: number; context_window: number; input_price: number | null; output_price: number | null; token_parameter: string; skills: string[]; tier: string; reasoning: boolean };
type Skill = { id: string; name: string; description: string; keywords: string[]; instructions: string; enabled: boolean };
type Preset = { id: string; name: string; protocol: string; base_url: string; local?: boolean; free?: boolean; hint: string; skills?: string[]; tier?: string; reasoning?: boolean };
type Preferences = { generator_id: string | null; planner_id: string | null; reviewer_id: string | null; mode: Mode; routing: string; allow_paid: boolean; max_cost: number; max_calls: number; max_repairs: number; max_output_tokens: number; context_tokens: number; token_budget: number; timeout_seconds: number; max_run_seconds: number; system_prompt: string; smart_routing?: boolean };
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
function renderContent(target: HTMLElement, text: string) {
  target.replaceChildren();
  const parts = text.split(/```([^\n]*)\n([\s\S]*?)(?:```|$)/g);
  for (let i = 0; i < parts.length; i += 3) {
    if (parts[i]) { const p = document.createElement('div'); p.className = 'prose-text'; p.textContent = parts[i]; target.append(p); }
    if (parts[i + 2] !== undefined) {
      const wrapper = document.createElement('div'); wrapper.className = 'code-block';
      const heading = document.createElement('div'); heading.className = 'code-heading';
      const language = document.createElement('span'); language.textContent = parts[i + 1] || 'code';
      const button = document.createElement('button'); button.textContent = 'Copy'; const codeText = parts[i + 2];
      button.addEventListener('click', () => copy(codeText)); heading.append(language, button);
      const pre = document.createElement('pre'); const code = document.createElement('code'); code.textContent = codeText; pre.append(code); wrapper.append(heading, pre); target.append(wrapper);
    }
  }
}
async function copy(text: string) { try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { toast('Select the text to copy it'); } }
function messageNode(message: Message, pending = false) {
  const article = document.createElement('article'); article.className = `message ${message.role}`;
  const content = document.createElement('div'); content.className = 'message-content';
  if (pending) { article.id = 'pending-response'; content.id = 'pending-content'; }
  if (message.role === 'assistant') {
    const name = document.createElement('div'); name.className = 'message-name'; name.textContent = 'L.E.O'; if (pending) name.append(node('span', '', 'working-dot')); article.append(name);
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
const ACTIVITY: Record<string, string> = { plan: 'Planning the steps', tasks: 'Working on the subtasks', generate: 'Writing the answer', review: 'Reviewing the answer', repair: 'Fixing review findings' };
let liveClock: ReturnType<typeof setInterval> | undefined;
function stageOf(phase: string) {
  return phase === 'planning' ? 'plan' : phase.startsWith('task_') ? 'tasks' : phase === 'generating' ? 'generate'
    : phase.startsWith('reviewing') ? 'review' : phase.startsWith('repairing') ? 'repair' : null;
}
function stageTrack(stages: [string, string][]) {
  const track = node('ol', '', 'stage-track'); track.setAttribute('aria-label', 'Run stages');
  for (const [key, label] of stages) { const step = node('li', label); step.dataset.stage = key; step.dataset.state = 'pending'; track.append(step); }
  return track;
}
type RouteInfo = { skill: string; difficulty: string; mode: Mode; auto: boolean; signals: string[] };
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
  const last = target.lastElementChild;
  (last?.classList.contains('prose-text') ? last : last?.querySelector('code') ?? target).append(caret);
}
function watchRun(id: string) {
  closeStream(); activeRun = id; draft = ''; updateSend(); canvas.reset();
  el('run-panel').hidden = false; el('run-status').textContent = 'Queued'; el('run-usage').textContent = ''; el('status-dot').dataset.status = 'running'; el('run-events').replaceChildren();
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
    const track = stageTrack([['route', routeLabel(data)], ...STAGES[data.mode]]);
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
  listen('validation', data => {
    const checks: { language: string; passed: boolean; message: string }[] = data.checks || [];
    for (const check of checks) logActivity(`${check.language}: ${check.message}`);
    const failed = checks.filter(c => !c.passed).length;
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
    const run = await send<Run>('/runs', 'POST', { prompt: text, conversation_id: activeId, mode: select('composer-mode').value, request_id: crypto.randomUUID() });
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
  el('composer-note').textContent = enabled.length ? 'Quick saves calls. Reviewed adds a second opinion. Deep adds a plan.' : 'Connect a local model or API in Settings.';
}
function roleOptions(value: string | null) {
  return [new Option('Auto · follow fallback order', ''), ...connections.map(p => new Option(`${p.name} · ${p.model}${p.enabled ? '' : ' (disabled)'}`, p.id, false, p.id === value))];
}
function renderProviders() {
  el('provider-list').replaceChildren();
  if (!connections.length) { const p = document.createElement('p'); p.className = 'empty-providers'; p.textContent = 'No models connected. Start with Ollama or your preferred API.'; el('provider-list').append(p); }
  for (const p of connections) {
    const card = document.createElement('div'); card.className = 'provider-card';
    const detail = document.createElement('div'); const name = document.createElement('strong'); name.textContent = p.name;
    const subtitle = document.createElement('small'); subtitle.textContent = `${p.model} · ${p.local ? 'local' : 'cloud'} · ${p.free ? 'no API charge' : 'paid'}${p.enabled ? '' : ' · disabled'}${p.has_key ? ' · key saved' : ''}`;
    const profile = document.createElement('small'); profile.textContent = `${p.tier} tier${p.reasoning ? ' · reasoning' : ''} · ${p.skills.length ? p.skills.map(id => skills.find(s => s.id === id)?.name || id).join(', ') : 'generalist'}`;
    detail.append(name, subtitle, profile);
    const actions = document.createElement('div'); actions.className = 'provider-actions';
    for (const [label, handler] of [
      ['Edit', () => editProvider(p)],
      ['Check', async () => {
        try {
          toast('Checking model catalog…');
          const result = await send<{ models: string[]; model_found: boolean }>(`/connections/${p.id}/test`, 'POST');
          editProvider(p); el('model-catalog').replaceChildren(...result.models.map(id => new Option(id, id)));
          el('provider-feedback').textContent = result.model_found ? 'Connected. Model is listed. Inference will be checked on your first message.' : 'Connected. This model was not in the catalog; choose a listed ID or confirm it with your provider.';
        } catch (e) { toast(errorText(e)); }
      }],
      ['Remove', async () => { try { await send(`/connections/${p.id}`, 'DELETE'); await loadSettings(); } catch (e) { toast(errorText(e)); } }],
    ] as [string, () => void | Promise<void>][]) {
      const b = document.createElement('button'); b.className = 'secondary-button'; b.textContent = label; b.addEventListener('click', handler); actions.append(b);
    }
    card.append(detail, actions); el('provider-list').append(card);
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
  field('allow-paid').checked = preferences.allow_paid; el<HTMLTextAreaElement>('system-prompt').value = preferences.system_prompt;
  for (const key of numericFields) field(key.replaceAll('_', '-')).value = String(preferences[key]);
}
function applyPreset() {
  const preset = presets.find(p => p.id === select('provider-preset').value); if (!preset) return;
  field('provider-name').value = preset.name; field('provider-url').value = preset.base_url; select('provider-protocol').value = preset.protocol;
  field('provider-local').checked = Boolean(preset.local); field('provider-free').checked = Boolean(preset.free); el('provider-hint').textContent = preset.hint;
  select('provider-tier').value = preset.tier || 'standard'; field('provider-reasoning').checked = Boolean(preset.reasoning); renderSkillPicker(preset.skills || []);
}
function renderSkillPicker(selected: string[]) {
  el('provider-skills').replaceChildren(...skills.map(skill => {
    const label = document.createElement('label'); label.className = 'skill-option'; label.title = skill.description;
    const box = document.createElement('input'); box.type = 'checkbox'; box.value = skill.id; box.checked = selected.includes(skill.id);
    label.append(box, ` ${skill.name}${skill.enabled ? '' : ' (disabled)'}`); return label;
  }));
}
function renderSkills() {
  el('skill-list').replaceChildren(...skills.map(skill => {
    const card = document.createElement('div'); card.className = 'provider-card';
    const detail = document.createElement('div'); const name = document.createElement('strong'); name.textContent = `${skill.name}${skill.enabled ? '' : ' (disabled)'}`;
    const models = connections.filter(c => c.skills.includes(skill.id)).map(c => c.model);
    const info = document.createElement('small'); info.textContent = `${skill.description || 'No description'} · ${models.length ? `models: ${models.join(', ')}` : 'no model has this skill yet'}`;
    detail.append(name, info);
    const actions = document.createElement('div'); actions.className = 'provider-actions';
    const edit = document.createElement('button'); edit.className = 'secondary-button'; edit.textContent = 'Edit'; edit.addEventListener('click', () => editSkill(skill));
    const remove = document.createElement('button'); remove.className = 'secondary-button'; remove.textContent = 'Remove';
    remove.addEventListener('click', async () => { try { await send(`/skills/${skill.id}`, 'DELETE'); await loadSettings(); toast('Skill removed'); } catch (e) { toast(errorText(e)); } });
    actions.append(edit, remove); card.append(detail, actions); return card;
  }));
}
function editSkill(skill?: Skill) {
  el<HTMLFormElement>('skill-form').reset(); el('skill-form').hidden = false; el('skill-feedback').textContent = '';
  field('skill-id').value = skill?.id || ''; field('skill-name').value = skill?.name || ''; field('skill-description').value = skill?.description || '';
  field('skill-keywords').value = skill?.keywords.join(', ') || ''; el<HTMLTextAreaElement>('skill-instructions').value = skill?.instructions || '';
  field('skill-enabled').checked = skill?.enabled ?? true; field('skill-name').focus();
}
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
function editProvider(provider?: Provider) {
  el<HTMLFormElement>('provider-form').reset(); el('provider-form').hidden = false; el('provider-feedback').textContent = ''; el('model-catalog').replaceChildren();
  field('provider-id').value = provider?.id || ''; field('provider-key').value = '';
  field('provider-key').placeholder = provider?.has_key ? 'Leave blank to keep the saved key' : 'Optional for local servers';
  if (!provider) { select('provider-preset').value = 'ollama'; applyPreset(); }
  else {
    select('provider-preset').value = presets.find(p => p.base_url === provider.base_url)?.id || 'custom';
    for (const [id, value] of Object.entries({ name: provider.name, url: provider.base_url, model: provider.model, context: provider.context_window, priority: provider.priority, 'input-price': provider.input_price ?? '', 'output-price': provider.output_price ?? '' })) field(`provider-${id}`).value = String(value);
    select('provider-protocol').value = provider.protocol; select('provider-token-param').value = provider.token_parameter;
    for (const key of ['local', 'free', 'enabled', 'reasoning'] as const) field(`provider-${key}`).checked = Boolean(provider[key]);
    select('provider-tier').value = provider.tier || 'standard'; renderSkillPicker(provider.skills || []);
    el('provider-hint').textContent = presets.find(p => p.id === select('provider-preset').value)?.hint || '';
  }
  field('provider-name').focus();
}
select('provider-preset').addEventListener('change', applyPreset);
el('add-connection').addEventListener('click', () => editProvider());
el('cancel-provider').addEventListener('click', () => { field('provider-key').value = ''; el('provider-form').hidden = true; });
el('provider-form').addEventListener('submit', async event => {
  event.preventDefault();
  const id = field('provider-id').value;
  const price = (name: string) => field(name).value === '' ? null : Number(field(name).value);
  const body = {
    name: field('provider-name').value.trim(), protocol: select('provider-protocol').value, base_url: field('provider-url').value.trim(), model: field('provider-model').value.trim(),
    api_key: field('provider-clear-key').checked ? '' : field('provider-key').value.trim() || null,
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
  const body: Preferences = { ...preferences, mode: select('mode-select').value as Mode, routing: select('routing').value, allow_paid: field('allow-paid').checked, smart_routing: field('smart-routing').checked, system_prompt: el<HTMLTextAreaElement>('system-prompt').value };
  for (const key of numericFields) body[key] = Number(field(key.replaceAll('_', '-')).value);
  for (const role of ['generator', 'planner', 'reviewer'] as const) body[`${role}_id`] = select(`${role}-id`).value || null;
  try { preferences = await send<Preferences>('/settings', 'PUT', body); select('composer-mode').value = preferences.mode; el('settings-feedback').textContent = 'Saved. These settings apply to new runs.'; }
  catch (e) { el('settings-feedback').textContent = errorText(e); }
});
function openSettings() { dialog('settings-dialog').showModal(); void loadSettings().catch(e => toast(errorText(e))); }
el('open-settings').addEventListener('click', openSettings);
el('connect-models').addEventListener('click', () => { openSettings(); editProvider(); });

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
