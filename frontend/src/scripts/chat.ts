import { canvas, extractPage, streamingHtml } from './canvas';
import { renderMarkdown } from './markdown';
import { breakdowns, compact, levelMeter, levelOf, money, node, percent, seconds, successRate, tokens, type Report } from './usage-ui';

type Mode = 'auto' | 'quick' | 'reviewed' | 'deep' | 'agent';
type Provider = { id: string; name: string; protocol: string; base_url: string; model: string; local: boolean; free: boolean; enabled: boolean; has_key: boolean; priority: number; context_window: number; input_price: number | null; output_price: number | null; token_parameter: string; skills: string[]; tier: string; reasoning: boolean; tools?: boolean | null; calls?: number; avg_input_tokens?: number | null; avg_output_tokens?: number | null };
type Skill = { id: string; name: string; description: string; keywords: string[]; instructions: string; enabled: boolean };
type Preset = { id: string; name: string; protocol: string; base_url: string; local?: boolean; free?: boolean; hint: string; skills?: string[]; tier?: string; reasoning?: boolean };
type Preferences = { generator_id: string | null; planner_id: string | null; reviewer_id: string | null; mode: Mode; routing: string; allow_paid: boolean; max_cost: number; max_calls: number; max_repairs: number; max_tool_calls: number; max_output_tokens: number; context_tokens: number; token_budget: number; timeout_seconds: number; max_run_seconds: number; system_prompt: string; smart_routing?: boolean; web_search?: string; search_url?: string; search_pages?: number; page_photos?: boolean;
  agent_max_steps: number; agent_max_calls: number; agent_token_budget: number; agent_max_cost: number; agent_max_run_seconds: number; agent_parallel: number; agent_command_timeout: number };
type Usage = { route?: RouteInfo; calls?: number; input_tokens?: number; output_tokens?: number; estimated_cost?: number; estimated?: boolean; uncertain_calls?: number; context_messages_omitted?: number; review_passed?: boolean | null };
type Run = { id: string; conversation_id: string; status: string; phase: string; output: string; error?: string; usage: Usage; mode: Mode; created_at?: number };
type Conversation = { id: string; title: string; updated_at: number; active_run: string | null; workspace?: boolean };
type Workspace = { id: string; name: string; source: string; repo_url: string | null; branch: string | null; has_token: boolean; status?: string; error?: string };
type Message = { role: 'user' | 'assistant'; content: string; run_id?: string };
type FullConversation = Conversation & { messages: Message[]; runs: Run[]; workspace: Workspace | null };

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
let currentUser: { id: string; username: string } | null = null;
let activeAgent = false;

function toast(text: string) {
  el('toast').textContent = text; el('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el('toast').hidden = true, 5000);
}
function errorText(error: unknown) { return error instanceof Error ? error.message : 'Something went wrong'; }
async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, { ...init, credentials: 'include', headers: { 'Content-Type': 'application/json', ...init.headers } });
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('Session expired');
  }
  const data = await response.json().catch(() => ({ detail: 'Server returned an unreadable response' }));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`);
  return data;
}
const send = <T>(path: string, method: string, body?: unknown) => api<T>(path, { method, credentials: 'include', ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
function rememberChat() { try { activeId ? localStorage.setItem('leo-active-chat', activeId) : localStorage.removeItem('leo-active-chat'); } catch {} }
function closeStream() { stream?.close(); stream = null; activeRun = null; activeAgent = false; draft = ''; clearInterval(liveClock); updateSend(); }
function steering() { return Boolean(activeRun && activeAgent && prompt.value.trim()); }
function updateSend() {
  const busy = Boolean(activeRun) && !steering();
  const button = el<HTMLButtonElement>('send-button');
  button.disabled = submitting || (!busy && (!ready || !prompt.value.trim()));
  const label = busy ? 'Stop response' : steering() ? 'Send to the agent while it works' : 'Send message';
  button.setAttribute('aria-label', label); button.title = label;
  button.querySelector<SVGElement>('svg')!.style.display = busy ? 'none' : '';
  button.querySelector<HTMLElement>('.stop-square')!.hidden = !busy;
  el('stop-run').hidden = !activeRun;
}
function resizePrompt() { prompt.style.height = 'auto'; prompt.style.height = `${Math.min(prompt.scrollHeight, 220)}px`; updateSend(); }
// The floating header lifts off the page once the conversation scrolls under it.
const syncScrolled = () => document.body.classList.toggle('scrolled', window.scrollY > 4);
window.addEventListener('scroll', syncScrolled, { passive: true }); syncScrolled();
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
  syncWorkspace();
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
  if (open && !el('workspace-panel').hidden) setWorkspacePanel(false);
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
// Chats are grouped by when they were last active, newest first.
function historyGroup(updated: number) {
  const day = 86_400_000, startOfToday = new Date().setHours(0, 0, 0, 0), time = updated * 1000;
  if (time >= startOfToday) return 'Today';
  if (time >= startOfToday - day) return 'Yesterday';
  if (time >= startOfToday - 6 * day) return 'Previous 7 days';
  if (time >= startOfToday - 29 * day) return 'Previous 30 days';
  return 'Older';
}
function historyTime(updated: number) {
  const date = new Date(updated * 1000), group = historyGroup(updated);
  if (group === 'Today' || group === 'Yesterday') return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (group === 'Previous 7 days') return date.toLocaleDateString([], { weekday: 'short' });
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
function renderHistory() {
  el('history-list').replaceChildren(); el('history-empty').hidden = conversations.length > 0;
  let group = '', list: HTMLElement | null = null;
  for (const chat of [...conversations].sort((a, b) => b.updated_at - a.updated_at)) {
    if (historyGroup(chat.updated_at) !== group) {
      group = historyGroup(chat.updated_at);
      const section = document.createElement('div'); section.className = 'history-group';
      const heading = document.createElement('h3'); heading.textContent = group;
      list = document.createElement('div'); list.className = 'history-items';
      section.append(heading, list); el('history-list').append(section);
    }
    const row = document.createElement('div'); row.className = `history-item${chat.id === activeId ? ' active' : ''}`;
    const button = document.createElement('button'); button.className = 'history-open'; button.title = chat.title;
    if (chat.id === activeId) button.setAttribute('aria-current', 'page');
    if (chat.active_run) button.append(Object.assign(document.createElement('span'), { className: 'status-dot', title: 'Running' }));
    button.dataset.status = chat.active_run ? 'running' : '';
    const title = Object.assign(document.createElement('span'), { className: 'history-title', textContent: chat.title });
    const time = Object.assign(document.createElement('span'), { className: 'history-time', textContent: historyTime(chat.updated_at) });
    button.append(title, time);
    button.querySelector('.status-dot')?.setAttribute('data-status', 'running');
    button.addEventListener('click', () => void openChat(chat.id).catch(e => toast(errorText(e))));
    const remove = document.createElement('button'); remove.className = 'history-delete'; remove.setAttribute('aria-label', `Delete ${chat.title}`);
    remove.append(el<HTMLTemplateElement>('icon-trash').content.cloneNode(true), Object.assign(document.createElement('span'), { textContent: 'Delete' }));
    // Deleting asks once: the first click arms the button, the second deletes.
    let disarm = 0;
    remove.addEventListener('click', async () => {
      if (!row.classList.contains('confirming')) {
        row.classList.add('confirming'); remove.setAttribute('aria-label', `Confirm deleting ${chat.title}`);
        disarm = window.setTimeout(() => { row.classList.remove('confirming'); remove.setAttribute('aria-label', `Delete ${chat.title}`); }, 3000);
        return;
      }
      clearTimeout(disarm);
      try { await send(`/conversations/${chat.id}`, 'DELETE'); if (activeId === chat.id) newChat(); await refreshHistory(); }
      catch (e) { toast(errorText(e)); }
    });
    row.append(button, remove); list!.append(row);
  }
}
async function openChat(id: string) {
  const nav = ++navigation;
  if (id !== activeId) canvas.hide();
  closeStream();
  const data = await api<FullConversation>(`/conversations/${id}`);
  if (nav !== navigation) return;
  activeId = id; current = data; rememberChat(); renderConversation(); renderConnectorMenu(); closeSidebar(); dialog('search-dialog').close();
  if (data.active_run) watchRun(data.active_run);
}
function newChat() {
  navigation++; canvas.hide(); closeStream(); activeId = null; current = null; rememberChat(); renderConversation(); renderConnectorMenu(); closeSidebar(); prompt.value = ''; resizePrompt(); prompt.focus(); window.scrollTo({ top: 0 });
}
// Live run view: the orchestration stages light up as the worker reports them.
const STAGES: Record<Mode, [string, string][]> = {
  auto: [['route', 'Route'], ['generate', 'Generate'], ['check', 'Check']],
  quick: [['generate', 'Generate'], ['check', 'Check']],
  reviewed: [['generate', 'Generate'], ['check', 'Check'], ['review', 'Review'], ['repair', 'Repair']],
  deep: [['plan', 'Plan'], ['tasks', 'Tasks'], ['generate', 'Generate'], ['check', 'Check'], ['review', 'Review'], ['repair', 'Repair']],
  agent: [['sandbox', 'Sandbox'], ['work', 'Work'], ['finish', 'Finish']],
};
const ACTIVITY: Record<string, string> = { sandbox: 'Starting the sandbox', work: 'Working', research: 'Searching the web', plan: 'Planning the steps', tasks: 'Working on the subtasks', generate: 'Writing the answer', review: 'Reviewing the answer', repair: 'Fixing review findings' };
let liveClock: ReturnType<typeof setInterval> | undefined;
function stageOf(phase: string) {
  return phase === 'sandbox' ? 'sandbox' : phase.startsWith('agent:') ? 'work' : phase.startsWith('research_') ? 'research' : phase === 'planning' ? 'plan' : phase.startsWith('task_') ? 'tasks' : phase === 'generating' ? 'generate'
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
  activeAgent = mode === 'agent';
  el('pending-response').insertBefore(liveNode(mode, run?.created_at ?? Date.now() / 1000), el('pending-content'));
  if (activeAgent) el('run-live').append(node('ul', '', 'agent-todos'), node('ol', '', 'agent-feed'));
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
  listenAgent(listen, id);
  listen('done', async data => {
    source.close(); activeRun = null; activeAgent = false; stream = null; updateSend(); clearInterval(liveClock);
    if (!el('workspace-panel').hidden) void loadFiles();
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
// Agent runs: a timeline of steps and tool calls, the todo list, sub-agents, and approval requests.
function agentName(agent: string) { return agent === 'main' ? 'Lead' : `Sub-agent ${agent.slice(4)}`; }
function feed(kind: string, text: string, id = '') {
  const list = document.querySelector('#run-live .agent-feed'); if (!list) { logActivity(text); return null; }
  const item = node('li', '', `feed-${kind}`); if (id) item.dataset.call = id;
  item.append(node('span', text, 'feed-text'));
  list.append(item); while (list.children.length > 200) list.firstChild?.remove();
  scrollToLatest(); return item;
}
function renderTodos(items: { content: string; status: string }[]) {
  const list = document.querySelector('#run-live .agent-todos'); if (!list) return;
  list.replaceChildren(...items.map(t => { const item = node('li', t.content); item.dataset.status = t.status; return item; }));
}
let filesTimer: ReturnType<typeof setTimeout> | undefined;
function listenAgent(listen: (kind: string, handler: (data: any) => void) => void, runId: string) {
  listen('agent_step', data => {
    setStage('work', 'active');
    setActivity(`${agentName(data.agent)} · step ${data.step}${data.model ? ` with ${data.model}` : ''}`);
    if (data.text && data.agent === 'main') feed('think', data.text);
    for (const call of data.tools || []) feed(data.agent === 'main' ? 'call' : 'subcall', `${data.agent === 'main' ? '' : agentName(data.agent) + ' · '}${call.name} ${call.summary}`, call.id);
  });
  listen('agent_tool', data => {
    const item = document.querySelector<HTMLElement>(`#run-live .agent-feed li[data-call="${CSS.escape(data.id)}"]`);
    if (item) {
      item.dataset.ok = String(data.ok);
      const detail = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = data.ok ? 'Output' : 'Error';
      detail.append(summary, node('pre', data.output)); item.append(detail);
    }
    if (['write_file', 'edit_file', 'bash'].includes(data.name) && !el('workspace-panel').hidden) { clearTimeout(filesTimer); filesTimer = setTimeout(() => void loadFiles(), 1200); }
  });
  listen('todos', data => renderTodos(data.items || []));
  listen('connector_tool', data => {
    // A tool call in a connected app: one row in the timeline, with its arguments and result one click away.
    if (!document.querySelector('#run-live .agent-feed')) el('run-live').append(node('ol', '', 'agent-feed'));
    const label = `${data.connector || 'Connector'} · ${data.tool} · ${({ done: 'done', denied: 'denied', failed: 'failed' } as Record<string, string>)[data.status] || data.status}`;
    const item = feed('call', label);
    if (item) {
      item.dataset.ok = String(data.status === 'done');
      const detail = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = 'Details';
      detail.append(summary, node('pre', `Arguments:\n${JSON.stringify(data.arguments ?? {}, null, 2)}\n\nResult:\n${data.result}`)); item.append(detail);
    }
    logActivity(label);
    setActivity(data.status === 'done' ? `Used ${data.connector} · ${data.tool}` : `${data.connector} · ${data.tool}: ${data.status}`);
  });
  listen('subagent', data => feed('sub', data.status === 'started' ? `${agentName(data.agent)} (${data.skill}) started: ${data.task}` : `${agentName(data.agent)} finished: ${data.report}`));
  listen('compacted', data => feed('note', `${agentName(data.agent)}: older steps summarized to free up context`));
  listen('steer', data => { if (!document.querySelector('#run-live .feed-you:last-child')) feed('you', data.message); });
  listen('approval', data => {
    setActivity('Waiting for your approval'); el('status-dot').dataset.status = 'waiting';
    const card = node('div', '', 'approval-card'); card.id = `approval-${data.id}`;
    const tool = data.kind === 'mcp_tool';
    card.append(node('strong', tool ? `${data.detail.connector} wants to run ${data.detail.tool}` : data.kind === 'git_push' ? 'Push to the repository?' : data.kind === 'budget' ? 'Allow more budget?' : 'Keep going?'),
                node('p', tool ? (data.detail.read_only ? 'This tool only reads data.' : 'This tool may change data in the connected app.') : data.message));
    if (data.detail?.message) card.append(node('small', `Commit message: ${data.detail.message}`));
    if (tool) {
      const args = JSON.stringify(data.detail.arguments ?? {}, null, 2);
      const detail = document.createElement('details'); detail.open = args.length < 400;
      const summary = document.createElement('summary'); summary.textContent = 'Arguments';
      detail.append(summary, node('pre', args)); card.append(detail);
    }
    const actions = node('div', '', 'form-actions start');
    const choices = tool
      ? [['Allow once', true, false, 'primary-button'], ['Always allow', true, true, 'secondary-button'], ['Deny', false, false, 'secondary-button']] as const
      : [['Approve', true, false, 'primary-button'], ['Decline', false, false, 'secondary-button']] as const;
    for (const [label, approved, remember, style] of choices) {
      const button = document.createElement('button'); button.type = 'button'; button.className = style; button.textContent = label;
      button.addEventListener('click', async () => {
        actions.querySelectorAll('button').forEach(b => (b as HTMLButtonElement).disabled = true);
        try { await send(`/runs/${runId}/approve`, 'POST', { approval_id: data.id, approved, remember }); }
        catch (e) { toast(errorText(e)); actions.querySelectorAll('button').forEach(b => (b as HTMLButtonElement).disabled = false); }
      });
      actions.append(button);
    }
    card.append(actions); el('run-live').append(card); scrollToLatest();
  });
  listen('approval_resolved', data => {
    document.getElementById(`approval-${data.id}`)?.remove();
    feed('note', data.kind === 'mcp_tool' ? (data.approved ? 'Allowed; continuing' : 'Denied; the model was told') : data.approved ? 'Approved; the agent continues' : 'Declined; the agent was told');
    setActivity(data.approved ? 'Continuing' : 'Continuing without it');
  });
}
// The Files panel: browse, upload to, and download the chat's sandbox workspace.
function syncWorkspace() {
  // Agent work moved to L.E.O.C.O.D.E; its controls appear only when this server still runs agents (LEO_SERVER_AGENT).
  const workspace = agentOn ? current?.workspace : null;
  el('toggle-workspace').hidden = !workspace;
  if (workspace) select('composer-mode').value = 'agent';
  if (!workspace) setWorkspacePanel(false, false);
  else if (!el('workspace-panel').hidden) void loadFiles();
}
function setWorkspacePanel(open: boolean, load = true) {
  if (open) { if (canvas.open) canvas.close(); setUsagePanel(false, false); }
  el('workspace-panel').hidden = !open; document.body.classList.toggle('usage-open', open || !el('usage-panel').hidden);
  el('toggle-workspace').setAttribute('aria-expanded', String(open));
  if (open && load) void loadFiles();
}
async function loadFiles() {
  const workspace = current?.workspace; if (!workspace) return;
  el('workspace-meta').textContent = workspace.repo_url ? `${workspace.repo_url.replace(/^https:\/\//, '')} · ${workspace.branch || 'default branch'}` : 'Sandbox workspace';
  el<HTMLAnchorElement>('workspace-download').href = `/api/workspaces/${workspace.id}/download`;
  try {
    const result = await api<{ entries: { path: string; type: string; size: number }[] }>(`/workspaces/${workspace.id}/files?depth=4`);
    if (current?.workspace?.id !== workspace.id) return;
    const items = result.entries.map(entry => {
      const item = document.createElement('li'); item.dataset.type = entry.type;
      item.style.setProperty('--depth', String(entry.path.split('/').length - 1));
      const button = document.createElement('button'); button.type = 'button'; button.textContent = entry.path.split('/').pop()!; button.title = entry.path;
      if (entry.type === 'file') button.addEventListener('click', () => void openFile(workspace.id, entry.path)); else button.disabled = true;
      item.append(button); return item;
    });
    el('workspace-files').replaceChildren(...(items.length ? items : [node('li', 'No files yet. Ask the agent to build something, or upload a .zip.', 'empty')]));
  } catch (e) { el('workspace-files').replaceChildren(node('li', errorText(e), 'empty')); }
}
async function openFile(workspaceId: string, path: string) {
  try {
    const file = await api<{ path: string; content: string; binary: boolean; size: number }>(`/workspaces/${workspaceId}/file?path=${encodeURIComponent(path)}`);
    el('workspace-file-name').textContent = path;
    el('workspace-file-content').textContent = file.binary ? `Binary file, ${file.size.toLocaleString()} bytes.` : file.content;
    el('workspace-viewer').hidden = false; el('workspace-files').hidden = true;
  } catch (e) { toast(errorText(e)); }
}
el('workspace-file-close').addEventListener('click', () => { el('workspace-viewer').hidden = true; el('workspace-files').hidden = false; });
el('toggle-workspace').addEventListener('click', () => setWorkspacePanel(el('workspace-panel').hidden));
el('close-workspace').addEventListener('click', () => setWorkspacePanel(false));
el('workspace-refresh').addEventListener('click', () => void loadFiles());
field('workspace-upload').addEventListener('change', async () => {
  const file = field('workspace-upload').files?.[0]; const workspace = current?.workspace;
  if (!file || !workspace) return;
  try {
    const response = await fetch(`/api/workspaces/${workspace.id}/upload`, { method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/zip' }, body: file });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Upload failed (${response.status})`);
    toast(`${file.name} unpacked into the workspace`); await loadFiles();
  } catch (e) { toast(errorText(e)); }
  field('workspace-upload').value = '';
});
function openWorkspaceDialog() {
  el<HTMLFormElement>('workspace-form').reset(); el('workspace-git').hidden = true; el('workspace-feedback').textContent = '';
  closeSidebar(); dialog('workspace-dialog').showModal(); field('workspace-name').focus();
}
el('new-workspace').addEventListener('click', openWorkspaceDialog);
document.querySelectorAll<HTMLInputElement>('input[name=workspace-source]').forEach(input => input.addEventListener('change', () => {
  el('workspace-git').hidden = input.value !== 'git' || !input.checked;
  field('workspace-repo').required = !el('workspace-git').hidden;
}));
el('workspace-form').addEventListener('submit', async event => {
  event.preventDefault();
  const source = document.querySelector<HTMLInputElement>('input[name=workspace-source]:checked')?.value || 'empty';
  const body = source === 'git'
    ? { name: field('workspace-name').value.trim(), source, repo_url: field('workspace-repo').value.trim(), branch: field('workspace-branch').value.trim() || null, token: field('workspace-token').value || null }
    : { name: field('workspace-name').value.trim(), source };
  el('workspace-feedback').textContent = source === 'git' ? 'Starting the sandbox and cloning…' : 'Starting the sandbox…';
  el<HTMLButtonElement>('workspace-create').disabled = true;
  try {
    const workspace = await send<Workspace>('/workspaces', 'POST', body);
    field('workspace-token').value = ''; dialog('workspace-dialog').close();
    await refreshHistory(); await openChat(workspace.id); setWorkspacePanel(true); prompt.focus();
  } catch (e) { el('workspace-feedback').textContent = errorText(e); }
  finally { el<HTMLButtonElement>('workspace-create').disabled = false; }
});
async function stopRun() {
  if (!activeRun) return;
  try { await send(`/runs/${activeRun}/cancel`, 'POST'); el('run-status').textContent = 'Stopping…'; }
  catch (e) { toast(errorText(e)); }
}
el('stop-run').addEventListener('click', stopRun);
el('chat-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (activeRun && steering()) {
    const message = prompt.value.trim();
    try { await send(`/runs/${activeRun}/steer`, 'POST', { message }); prompt.value = ''; resizePrompt(); feed('you', message); }
    catch (e) { toast(errorText(e)); }
    return;
  }
  if (activeRun) { await stopRun(); return; }
  const text = prompt.value.trim(); if (!text || submitting || !ready) return;
  submitting = true; updateSend();
  try {
    const search = el('search-toggle').getAttribute('aria-pressed') === 'true' ? true : null;
    const run = await send<Run>('/runs', 'POST', { prompt: text, conversation_id: activeId, mode: select('composer-mode').value, search,
      connectors: chatConnectors(), request_id: crypto.randomUUID() });
    if (!activeId) rememberConnectors(run.conversation_id);
    el('search-toggle').setAttribute('aria-pressed', 'false');
    prompt.value = ''; resizePrompt(); await refreshHistory(); await openChat(run.conversation_id);
  } catch (e) { toast(errorText(e)); }
  finally { submitting = false; updateSend(); }
});
prompt.addEventListener('input', resizePrompt);
prompt.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!activeRun || steering()) el<HTMLFormElement>('chat-form').requestSubmit(); } });

function refreshConnectionStatus() {
  const enabled = connections.filter(p => p.enabled);
  el('connection-status').textContent = ready ? `${enabled.length} model${enabled.length === 1 ? '' : 's'} connected` : 'Server offline';
  el('sidebar-status').textContent = el('connection-status').textContent; el('sidebar-dot').dataset.status = ready && enabled.length ? 'completed' : '';
  el('connect-models').hidden = enabled.length > 0;
  el('composer-note').textContent = enabled.length ? 'Auto picks the right model for each task. Answers can be wrong; check what matters.' : 'Connect a local model or API in Settings.';
}
function roleOptions(value: string | null) {
  return [new Option('Auto · follow fallback order', ''), ...connections.map(p => new Option(`${p.name} · ${p.model}${p.enabled ? '' : ' (disabled)'}`, p.id, false, p.id === value))];
}
// Fields the connection API accepts; used to flip one setting without resending the key.
const CONNECTION_FIELDS = ['name', 'protocol', 'base_url', 'model', 'local', 'free', 'enabled', 'priority', 'context_window', 'input_price', 'output_price', 'token_parameter', 'skills', 'tier', 'reasoning', 'tools'] as const;
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
function compactTokens(n: number) { return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K` : String(n); }
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
  profile.textContent = `${strength}${p.skills.length ? `, ${skillNames(p.skills).toLowerCase()}` : ', any task'}${p.free ? '' : ', paid'}${p.tools === true ? ', calls tools' : p.tools === false ? ', no tool calling' : ''}`;
  detail.append(name, profile);
  if (!p.has_key && !p.local) {
    // Cloud models almost always need a key; make a missing one impossible to miss.
    row.classList.add('needs-key');
    detail.append(Object.assign(document.createElement('small'), { className: 'key-warning', textContent: 'No API key saved. Click Edit and paste the key.' }));
  }
  const meta = document.createElement('div'); meta.className = 'row-meta';
  const price = document.createElement('span');
  price.textContent = p.free || p.local ? 'Free' : p.input_price == null ? 'Price not set' : `$${p.input_price} / $${p.output_price} per 1M`;
  const tokens = document.createElement('span');
  tokens.textContent = p.calls ? `~${compactTokens((p.avg_input_tokens || 0) + (p.avg_output_tokens || 0))} tokens a call` : 'Not used yet';
  if (p.calls) tokens.title = `${p.calls} call${p.calls === 1 ? '' : 's'}: about ${p.avg_input_tokens} in and ${p.avg_output_tokens} out each`;
  meta.append(price, tokens);
  const edit = document.createElement('button'); edit.className = 'text-button'; edit.textContent = 'Edit';
  edit.setAttribute('aria-label', `Edit ${modelLabel(p)}`);
  edit.addEventListener('click', () => editProvider(p, row));
  row.append(toggle, detail, meta, edit);
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
function routingInputs() { return [...document.querySelectorAll<HTMLInputElement>('input[name=routing]')]; }
function setRouting(value: string) { for (const input of routingInputs()) input.checked = input.value === value; if (!routingInputs().some(i => i.checked)) routingInputs()[0].checked = true; }
function routingChoice() { return routingInputs().find(i => i.checked)?.value || 'auto'; }
// The cost limit only matters when paid models are allowed.
function syncPaidLimit() { el('paid-limit').classList.toggle('open', field('allow-paid').checked); }
field('allow-paid').addEventListener('change', syncPaidLimit);
const numericFields = ['max_cost', 'max_calls', 'max_repairs', 'max_tool_calls', 'max_output_tokens', 'context_tokens', 'token_budget', 'timeout_seconds', 'max_run_seconds',
  'agent_max_steps', 'agent_max_calls', 'agent_token_budget', 'agent_max_cost', 'agent_max_run_seconds', 'agent_parallel', 'agent_command_timeout'] as const;
async function loadSettings() {
  [connections, preferences, presets, skills] = await Promise.all([api<Provider[]>('/connections'), api<Preferences>('/settings'), api<Preset[]>('/presets'), api<Skill[]>('/skills')]);
  await loadUser();
  select('provider-preset').replaceChildren(...presets.map(p => new Option(p.name, p.id)));
  renderProviders(); renderSkills();
  setRouting(preferences.routing); select('mode-select').value = preferences.mode; select('composer-mode').value = preferences.mode;
  field('smart-routing').checked = preferences.smart_routing ?? true;
  field('page-photos').checked = preferences.page_photos ?? true;
  select('web-search').value = preferences.web_search || 'auto'; field('search-url').value = preferences.search_url || ''; field('search-pages').value = String(preferences.search_pages ?? 3);
  field('allow-paid').checked = preferences.allow_paid; syncPaidLimit(); el<HTMLTextAreaElement>('system-prompt').value = preferences.system_prompt;
  for (const key of numericFields) field(key.replaceAll('_', '-')).value = String(preferences[key]);
  void loadAccounts(); void loadConnectors();
}

// Connectors: the user's remote MCP servers. Settings manages them; the composer picks which ones a chat may use.
type ConnectorTool = { name: string; description: string; read_only: boolean };
type Connector = { id: string; name: string; url: string; enabled: boolean; auth: 'none' | 'token' | 'oauth'; status: string; error: string | null;
  tools: ConnectorTool[]; permissions: Record<string, 'ask' | 'allow' | 'deny'>; signed_in: boolean; needs_client: boolean };
let connectorList: Connector[] = [];
let agentOn = false;
const chatChoices = new Map<string, string[]>();  // Conversation id → connectors switched on for that chat.
const usable = () => connectorList.filter(c => c.enabled && c.tools.length);
function chatConnectors() {
  const known = new Set(usable().map(c => c.id));
  return (chatChoices.get(activeId || 'new') ?? [...known]).filter(id => known.has(id));
}
function rememberConnectors(conversationId: string) { chatChoices.set(conversationId, chatConnectors()); chatChoices.delete('new'); }
async function loadConnectors() {
  try { connectorList = await api<Connector[]>('/connectors'); } catch { connectorList = []; }
  renderConnectors(); renderConnectorMenu();
}
function renderConnectorMenu() {
  const on = new Set(chatConnectors());
  const count = el('connector-count'); count.textContent = String(on.size); count.hidden = !on.size;
  const rows = usable().map(c => {
    const row = node('label', '', 'connector-menu-row');
    const box = Object.assign(document.createElement('input'), { type: 'checkbox', checked: on.has(c.id) });
    box.addEventListener('change', () => {
      const chosen = new Set(chatConnectors()); box.checked ? chosen.add(c.id) : chosen.delete(c.id);
      chatChoices.set(activeId || 'new', [...chosen]); renderConnectorMenu();
    });
    row.append(box, node('span', c.name), node('small', `${c.tools.length} tool${c.tools.length === 1 ? '' : 's'}`));
    return row;
  });
  el('connector-menu-list').replaceChildren(...(rows.length ? rows : [node('p', 'No connectors yet. Add your MCP servers in Settings.', 'connector-menu-empty')]));
}
function setConnectorMenu(open: boolean) {
  el('connector-menu').hidden = !open; el('connector-toggle').setAttribute('aria-expanded', String(open));
  if (open) renderConnectorMenu();
}
el('connector-toggle').addEventListener('click', () => setConnectorMenu(el('connector-menu').hidden));
document.addEventListener('click', event => { if (!(event.target as HTMLElement).closest('.connector-picker')) setConnectorMenu(false); });
el('manage-connectors').addEventListener('click', () => { setConnectorMenu(false); openSettings('tab-connectors'); });
const STATUS_TEXT: Record<string, string> = { connected: 'Connected', sign_in: 'Sign in needed', error: 'Error', new: 'Not checked yet' };
function renderConnectors() {
  el('connector-list').replaceChildren(...(connectorList.length ? connectorList.map(connectorCard) : [node('p', 'No connectors yet. Add an MCP server by its URL, such as Linear, Notion, or your own.', 'account-empty')]));
}
function connectorCard(c: Connector) {
  const card = node('section', '', 'connector-card'); card.dataset.status = c.status;
  const head = node('div', '', 'connector-head');
  const title = node('div', '');
  title.append(node('strong', c.name), node('small', c.url));
  const status = node('span', STATUS_TEXT[c.status] || c.status, 'connector-status');
  const toggle = node('label', '', 'connector-switch');
  const box = Object.assign(document.createElement('input'), { type: 'checkbox', checked: c.enabled, title: 'Use this connector in chats' });
  box.addEventListener('change', () => void editConnector(c, { enabled: box.checked }));
  toggle.append(box, node('span', 'On'));
  head.append(title, status, toggle);
  card.append(head);
  if (c.error) card.append(node('p', c.error, 'form-feedback'));
  if (c.auth === 'oauth' && c.needs_client) {
    const form = node('form', '', 'connector-client');
    const id = Object.assign(document.createElement('input'), { placeholder: 'Client ID', required: true, maxLength: 500 });
    const secret = Object.assign(document.createElement('input'), { placeholder: 'Client secret (if any)', type: 'password', autocomplete: 'off' });
    const save = Object.assign(document.createElement('button'), { type: 'submit', className: 'secondary-button', textContent: 'Save client' });
    form.append(node('p', 'This service does not register apps automatically. Create an OAuth app there with this callback URL, then paste its client ID:', 'field-hint'),
      node('code', `${location.origin}/api/connectors/oauth/callback`, 'account-code'), id, secret, save);
    form.addEventListener('submit', event => { event.preventDefault(); void editConnector(c, { client_id: id.value.trim(), client_secret: secret.value || null }); });
    card.append(form);
  }
  const actions = node('div', '', 'form-actions start');
  const button = (label: string, handler: () => void, style = 'secondary-button') => {
    const b = Object.assign(document.createElement('button'), { type: 'button', className: style, textContent: label }); b.addEventListener('click', handler); actions.append(b); return b;
  };
  if (c.auth === 'oauth') {
    const link = Object.assign(document.createElement('a'), { className: c.signed_in ? 'secondary-button' : 'primary-button', textContent: c.signed_in ? 'Sign in again' : 'Sign in',
      href: `/api/connectors/${encodeURIComponent(c.id)}/oauth/start` });
    actions.append(link);
  }
  button('Refresh tools', () => void refreshConnector(c));
  const readOnly = c.tools.filter(t => t.read_only && c.permissions[t.name] !== 'allow');
  if (readOnly.length) button(`Allow all read-only (${readOnly.length})`, () => void editConnector(c, { permissions: Object.fromEntries(readOnly.map(t => [t.name, 'allow'])) }));
  button('Remove', () => void removeConnector(c), 'text-button danger');
  card.append(actions);
  if (c.tools.length) {
    const tools = document.createElement('details'); tools.className = 'connector-tools';
    const summary = document.createElement('summary'); summary.textContent = `${c.tools.length} tool${c.tools.length === 1 ? '' : 's'}`;
    tools.append(summary, ...c.tools.map(t => {
      const row = node('div', '', 'connector-tool');
      const text = node('div', '');
      const name = node('strong', t.name); if (t.read_only) name.append(node('em', 'read-only'));
      text.append(name, node('small', t.description));
      const choice = document.createElement('select'); choice.setAttribute('aria-label', `Permission for ${t.name}`);
      for (const [value, label] of [['ask', 'Ask'], ['allow', 'Allow'], ['deny', 'Deny']]) choice.append(new Option(label, value, false, (c.permissions[t.name] || 'ask') === value));
      choice.addEventListener('change', () => void editConnector(c, { permissions: { [t.name]: choice.value } }));
      row.append(text, choice); return row;
    }));
    card.append(tools);
  }
  return card;
}
async function editConnector(c: Connector, body: Record<string, unknown>) {
  try { await send(`/connectors/${c.id}`, 'PUT', body); } catch (e) { toast(errorText(e)); }
  await loadConnectors();
}
async function refreshConnector(c: Connector) {
  try { await send(`/connectors/${c.id}/refresh`, 'POST'); toast(`${c.name}: tools refreshed`); } catch (e) { toast(errorText(e)); }
  await loadConnectors();
}
async function removeConnector(c: Connector) {
  if (!window.confirm(`Remove ${c.name}? Chats can no longer use its tools.`)) return;
  try { await send(`/connectors/${c.id}`, 'DELETE'); toast(`${c.name} removed`); } catch (e) { toast(errorText(e)); }
  await loadConnectors();
}
function syncConnectorForm() {
  const auth = select('connector-auth').value;
  el('connector-token-field').hidden = auth !== 'token';
  el('connector-client').hidden = auth !== 'oauth';
  el('save-connector').textContent = auth === 'oauth' ? 'Add and sign in' : 'Add connector';
}
select('connector-auth').addEventListener('change', syncConnectorForm);
el('add-connector').addEventListener('click', () => {
  el<HTMLFormElement>('connector-form').reset(); el('connector-feedback').textContent = ''; syncConnectorForm();
  el('connector-form').hidden = false; field('connector-name').focus();
});
el('cancel-connector').addEventListener('click', () => { field('connector-token').value = ''; field('connector-client-secret').value = ''; el('connector-form').hidden = true; });
el('connector-form').addEventListener('submit', async event => {
  event.preventDefault();
  const auth = select('connector-auth').value;
  el('connector-feedback').textContent = auth === 'oauth' ? 'Adding…' : 'Connecting and reading its tools…';
  try {
    const made = await send<Connector>('/connectors', 'POST', { name: field('connector-name').value.trim(), url: field('connector-url').value.trim(), auth,
      token: auth === 'token' ? field('connector-token').value.trim() || null : null,
      client_id: auth === 'oauth' ? field('connector-client-id').value.trim() || null : null,
      client_secret: auth === 'oauth' ? field('connector-client-secret').value || null : null });
    field('connector-token').value = ''; field('connector-client-secret').value = '';
    if (auth === 'oauth') { location.href = `/api/connectors/${encodeURIComponent(made.id)}/oauth/start`; return; }
    el('connector-form').hidden = true; el('connector-feedback').textContent = '';
    toast(made.status === 'connected' ? `${made.name} connected: ${made.tools.length} tools` : `${made.name} saved, but: ${made.error || 'it could not be reached'}`);
    await loadConnectors();
  } catch (e) { el('connector-feedback').textContent = errorText(e); }
});

type Me = { id: string; username: string; created_at: number; email: string | null; display_name: string | null; avatar_url: string | null; identities: string[]; has_password: boolean };
let signInProviders: { id: string; name: string }[] = [];
async function loadUser() {
  try {
    const [user, offered] = await Promise.all([api<Me>('/auth/me'), signInProviders.length ? null : api<{ providers: { id: string; name: string }[] }>('/auth/providers').catch(() => null)]);
    if (offered) signInProviders = offered.providers;
    currentUser = user;
    const badge = el('user-badge');
    const name = el('user-badge-name');
    if (badge && name) { badge.hidden = false; name.textContent = user.username; el('user-badge-initial').textContent = user.username.slice(0, 1).toUpperCase(); }
    renderProfile(user);
  } catch {
    currentUser = null;
    const badge = el('user-badge');
    if (badge) badge.hidden = true;
  }
}
// Settings → Account: who you are, how you sign in, and your password.
function renderProfile(user: Me) {
  el('profile-name').textContent = user.display_name || user.username;
  el('profile-detail').textContent = [user.email, `@${user.username}`].filter(Boolean).join(' · ');
  el('profile-initial').textContent = (user.display_name || user.username).slice(0, 1).toUpperCase();
  const avatar = el<HTMLImageElement>('profile-avatar');
  avatar.hidden = !user.avatar_url; el('profile-initial').hidden = Boolean(user.avatar_url);
  if (user.avatar_url) avatar.src = user.avatar_url;
  const ways = user.identities.length + (user.has_password ? 1 : 0);
  const row = (title: string, detail: string, action: HTMLElement) => {
    const item = document.createElement('div'); item.className = 'identity-row';
    const text = document.createElement('span'); const words = document.createElement('div');
    words.append(Object.assign(document.createElement('strong'), { textContent: title }), Object.assign(document.createElement('small'), { textContent: detail }));
    text.append(words); item.append(text, action); return item;
  };
  const passwordButton = Object.assign(document.createElement('button'), { type: 'button', className: 'text-button', textContent: user.has_password ? 'Change' : 'Set a password' });
  passwordButton.addEventListener('click', () => {
    el('current-password-field').hidden = !user.has_password;
    el('password-form').hidden = false; el('identity-feedback').textContent = '';
    (user.has_password ? field('current-password') : field('new-password')).focus();
  });
  const rows = [row('Password', user.has_password ? `Sign in as ${user.username}` : 'Not set: you sign in with a provider below', passwordButton)];
  const known = new Map(signInProviders.map(p => [p.id, p.name]));
  for (const id of new Set([...signInProviders.map(p => p.id), ...user.identities])) {
    const linked = user.identities.includes(id);
    let action: HTMLElement;
    if (linked) {
      action = Object.assign(document.createElement('button'), { type: 'button', className: 'text-button danger', textContent: 'Unlink', disabled: ways <= 1,
        title: ways <= 1 ? 'Set a password or link another account first, so you can still sign in' : '' });
      action.addEventListener('click', () => void unlinkIdentity(id, known.get(id) || id));
    } else {
      action = Object.assign(document.createElement('a'), { className: 'text-button', textContent: 'Link',
        href: `/api/auth/oauth/${encodeURIComponent(id)}/start?link=1&next=${encodeURIComponent('/?settings=account')}` });
    }
    rows.push(row(known.get(id) || id, linked ? 'Linked: you can sign in with it' : 'Not linked', action));
  }
  el('identity-list').replaceChildren(...rows);
  void loadDevices();
}
async function loadDevices() {
  let devices: { id: string; name: string; created_at: number; last_used_at: number | null }[] = [];
  try { devices = await api('/cli/devices'); } catch { /* Shown as none. */ }
  const day = (t: number | null) => t ? new Date(t * 1000).toLocaleDateString() : 'never';
  el('cli-device-list').replaceChildren(...(devices.length ? devices.map(d => {
    const item = node('div', '', 'account-row'); const text = node('div', '');
    text.append(node('strong', d.name), node('small', `Added ${day(d.created_at)} · last synced ${day(d.last_used_at)}`));
    const revoke = Object.assign(document.createElement('button'), { type: 'button', className: 'text-button danger', textContent: 'Revoke' });
    revoke.addEventListener('click', async () => {
      if (!window.confirm(`Revoke ${d.name}? It can no longer sync. Rotate your provider keys if the device is lost.`)) return;
      try { await send(`/cli/devices/${d.id}`, 'DELETE'); toast(`${d.name} revoked`); } catch (e) { toast(errorText(e)); }
      void loadDevices();
    });
    item.append(text, revoke); return item;
  }) : [node('p', 'No devices yet. Install L.E.O.C.O.D.E and run leocode login.', 'account-empty')]));
}
async function unlinkIdentity(id: string, name: string) {
  if (!window.confirm(`Unlink ${name}? You will no longer be able to sign in with it.`)) return;
  try { await send(`/auth/identities/${encodeURIComponent(id)}`, 'DELETE'); toast(`${name} unlinked`); await loadUser(); }
  catch (e) { el('identity-feedback').textContent = errorText(e); }
}
el('password-form').addEventListener('submit', async event => {
  event.preventDefault();
  const body = { current_password: el('current-password-field').hidden ? null : field('current-password').value, new_password: field('new-password').value };
  if (body.new_password.length < 8) { el('identity-feedback').textContent = 'Use at least 8 characters.'; return; }
  try {
    await send('/auth/password', 'PUT', body);
    field('current-password').value = ''; field('new-password').value = ''; el('password-form').hidden = true;
    el('identity-feedback').textContent = ''; toast('Password saved'); await loadUser();
  } catch (e) { el('identity-feedback').textContent = errorText(e); }
});
el('cancel-password').addEventListener('click', () => { field('current-password').value = ''; field('new-password').value = ''; el('password-form').hidden = true; });

async function doLogout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch {}
  window.location.href = '/login';
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
    select('provider-tools').value = provider.tools == null ? '' : String(provider.tools);
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
// Account sign-in through 9Router: one click per provider; the API-key form stays one step away.
type AccountProvider = { id: string; name: string; flow: 'device' | 'redirect'; risk: string | null };
type Account = { id: string; provider: string; name: string; label: string };
type SignIn = { id: string; provider: string; status: 'waiting' | 'finishing' | 'done' | 'error'; error: string | null; url: string; user_code: string | null; result: { account: string; added: number } | null };
let accountInfo: { available: boolean; error: string | null; providers: AccountProvider[]; accounts: Account[] } | null = null;
let signIn: SignIn | null = null;
let signInTimer: ReturnType<typeof setTimeout> | undefined;
async function loadAccounts() {
  try { accountInfo = await api('/accounts'); } catch { accountInfo = null; }
  renderAccounts();
}
function renderAccounts() {
  const accounts = accountInfo?.accounts || [];
  const empty = Object.assign(document.createElement('p'), { className: 'account-empty',
    textContent: accountInfo && !accountInfo.available ? accountInfo.error || 'Model-account sign-in is unavailable' : 'No model accounts yet.' });
  el('account-list').replaceChildren(...(accounts.length ? [] : [empty]), ...accounts.map(a => {
    const row = document.createElement('div'); row.className = 'account-row';
    const text = document.createElement('div');
    text.append(Object.assign(document.createElement('strong'), { textContent: a.name }), Object.assign(document.createElement('small'), { textContent: a.label || 'Signed in' }));
    const remove = Object.assign(document.createElement('button'), { type: 'button', className: 'text-button danger', textContent: 'Disconnect' });
    remove.addEventListener('click', () => void disconnectAccount(a));
    row.append(text, remove); return row;
  }));
  el('account-options').replaceChildren(...(accountInfo?.providers || []).map(p => {
    const button = Object.assign(document.createElement('button'), { type: 'button', className: 'account-option', disabled: !accountInfo?.available || Boolean(signIn) });
    button.append(Object.assign(document.createElement('strong'), { textContent: p.name }), Object.assign(document.createElement('small'), { textContent: p.flow === 'device' ? 'Sign in with a code' : 'Sign in in the browser' }));
    button.addEventListener('click', () => void startSignIn(p));
    return button;
  }));
  if (accountInfo && !accountInfo.available && !signIn) el('account-feedback').textContent = accountInfo.error || 'Account sign-in is unavailable';
}
function showChooser() {
  el('provider-form').hidden = true; el('account-chooser').hidden = false; el('account-feedback').textContent = '';
  el('account-chooser').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  void loadAccounts();
}
function stopSignIn() {
  clearTimeout(signInTimer);
  if (signIn?.status === 'waiting') void send(`/accounts/signin/${signIn.id}`, 'DELETE').catch(() => {});
  signIn = null; el('account-flow').hidden = true; renderAccounts();
}
async function startSignIn(p: AccountProvider) {
  if (p.risk && !window.confirm(`${p.name}\n\n${p.risk}`)) return;
  el('account-feedback').textContent = `Starting ${p.name} sign-in…`;
  try { signIn = await send<SignIn>('/accounts/signin', 'POST', { provider: p.id, accept_risk: Boolean(p.risk) }); }
  catch (e) { el('account-feedback').textContent = errorText(e); return; }
  el('account-feedback').textContent = '';
  el('account-code').hidden = !signIn.user_code; el('account-code').textContent = signIn.user_code || '';
  el('account-flow-text').textContent = signIn.user_code
    ? `Copy this code, open the ${p.name} sign-in page, and enter it there. L.E.O. notices when you're done.`
    : `Open the ${p.name} sign-in page and sign in. L.E.O. notices when you're done.`;
  el<HTMLAnchorElement>('account-open').href = signIn.url; el('account-flow').hidden = false;
  renderAccounts(); signInTimer = setTimeout(() => void pollSignIn(p), 5000);
}
async function pollSignIn(p: AccountProvider) {
  if (!signIn) return;
  try { signIn = await api<SignIn>(`/accounts/signin/${signIn.id}`); }
  catch (e) { el('account-feedback').textContent = errorText(e); stopSignIn(); return; }
  if (signIn.status === 'done' || signIn.status === 'error') {
    const finished = signIn; signIn = null; el('account-flow').hidden = true;
    if (finished.status === 'done') {
      const added = finished.result?.added || 0;
      el('account-chooser').hidden = true;
      toast(`${p.name} connected${finished.result?.account ? ` as ${finished.result.account}` : ''}: ${added ? `${added} model${added === 1 ? '' : 's'} added` : 'no new models'}`);
    } else el('account-feedback').textContent = finished.error || 'Sign-in failed';
    await loadSettings(); return;
  }
  signInTimer = setTimeout(() => void pollSignIn(p), 5000);
}
async function disconnectAccount(a: Account) {
  if (!window.confirm(`Disconnect ${a.name}${a.label ? ` (${a.label})` : ''}? Its models are removed from L.E.O.`)) return;
  try {
    const result = await send<{ removed_models: number }>(`/accounts/${a.id}`, 'DELETE');
    toast(`${a.name} disconnected${result.removed_models ? `, ${result.removed_models} models removed` : ''}`);
  } catch (e) { toast(errorText(e)); }
  await loadSettings();
}
el('account-close').addEventListener('click', () => { stopSignIn(); el('account-chooser').hidden = true; });
el('account-cancel').addEventListener('click', stopSignIn);
el('use-api-key').addEventListener('click', () => { stopSignIn(); el('account-chooser').hidden = true; editProvider(); });
el('add-connection').addEventListener('click', showChooser);
el('add-model-account').addEventListener('click', () => { showSettingsTab(settingsTabs.find(t => t.id === 'tab-models')!); showChooser(); });
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
    tools: select('provider-tools').value === '' ? null : select('provider-tools').value === 'true',
    skills: [...el('provider-skills').querySelectorAll<HTMLInputElement>('input:checked')].map(box => box.value),
  };
  try {
    await send(id ? `/connections/${id}` : '/connections', id ? 'PUT' : 'POST', body);
    field('provider-key').value = ''; el('provider-form').hidden = true; await loadSettings(); toast('Model connection saved');
  } catch (e) { el('provider-feedback').textContent = errorText(e); }
});
el('orchestration-form').addEventListener('submit', async event => {
  event.preventDefault();
  const body: Preferences = { ...preferences, mode: select('mode-select').value as Mode, routing: routingChoice(), allow_paid: field('allow-paid').checked, smart_routing: field('smart-routing').checked,
    web_search: select('web-search').value, search_url: field('search-url').value.trim(), search_pages: Number(field('search-pages').value), page_photos: field('page-photos').checked, system_prompt: el<HTMLTextAreaElement>('system-prompt').value };
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
  moveTabMarker(tab);
  // Routing, search, and limits share one form and one Save button.
  el('settings-save').hidden = !['tab-routing', 'tab-search', 'tab-limits'].includes(tab.id);
  if (focus) tab.focus();
  try { localStorage.setItem('leo-settings-tab', tab.id); } catch { /* Remembering the tab is optional. */ }
}
function moveTabMarker(tab: HTMLButtonElement) {
  const marker = document.querySelector<HTMLElement>('.tab-marker'); if (!marker || !tab.offsetWidth) return;
  marker.style.transform = `translate(${tab.offsetLeft}px, ${tab.offsetTop}px)`;
  marker.style.width = `${tab.offsetWidth}px`; marker.style.height = `${tab.offsetHeight}px`;
}
window.addEventListener('resize', () => { const tab = settingsTabs.find(t => t.getAttribute('aria-selected') === 'true'); if (tab) moveTabMarker(tab); });
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
  // Place the tab marker without animating it from the corner, then let later tab changes slide it.
  const marker = document.querySelector<HTMLElement>('.tab-marker')!; marker.classList.remove('animate');
  moveTabMarker(settingsTabs.find(t => t.getAttribute('aria-selected') === 'true')!);
  requestAnimationFrame(() => requestAnimationFrame(() => marker.classList.add('animate')));
}
el('open-settings').addEventListener('click', () => openSettings());
// Back from linking a sign-in provider (/?settings=account) or signing in to a connector (/?settings=connectors),
// with error= when it did not work.
{
  const params = new URLSearchParams(location.search);
  if (params.get('settings') === 'connectors') {
    const failed = params.get('error');
    history.replaceState(null, '', location.pathname);
    openSettings('tab-connectors');
    if (failed) toast(failed === 'needs_client' ? 'This service needs a client ID: see the connector below' : failed === 'cancelled' ? 'Sign-in was cancelled' : 'Sign-in did not complete; see the connector below');
    else if (params.get('connected')) toast('Connector signed in');
  }
  if (params.get('settings') === 'models') {  // From L.E.O.C.O.D.E: "add a model on the website".
    history.replaceState(null, '', location.pathname);
    openSettings('tab-models');
  }
  if (params.get('settings') === 'account') {
    const failed = params.get('error');
    history.replaceState(null, '', location.pathname);
    openSettings('tab-account');
    if (failed) toast(failed === 'linked_elsewhere' ? 'That account is already linked to another L.E.O. user' : failed === 'cancelled' ? 'Linking was cancelled' : 'Linking did not complete; try again');
  }
}
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
el('new-chat').addEventListener('click', newChat); el('sidebar-settings').addEventListener('click', () => { if (matchMedia('(max-width: 800px)').matches) closeSidebar(); openSettings(); });
document.querySelectorAll<HTMLElement>('kbd.shortcut').forEach(k => { k.textContent = `${/Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl '}${k.dataset.key}`; }); el('header-new-chat').addEventListener('click', newChat);
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
    const health = await api<{ agent: boolean }>('/health').catch(() => ({ agent: false }));
    agentOn = health.agent;
    el('new-workspace').hidden = !agentOn;
    if (agentOn && !select('composer-mode').querySelector('option[value=agent]')) select('composer-mode').append(new Option('Agent · works in a sandbox', 'agent'));
    await loadSettings(); await refreshHistory(); ready = true; refreshConnectionStatus();
    let saved: string | null = null; try { saved = localStorage.getItem('leo-active-chat'); } catch {}
    if (saved && conversations.some(c => c.id === saved)) await openChat(saved);
  } catch (e) { ready = false; el('connection-status').textContent = 'Server unavailable'; el('sidebar-status').textContent = 'Server unavailable'; el('composer-note').textContent = 'Start the L.E.O. backend, then reload this page.'; toast(errorText(e)); }
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
el('sidebar-logout')?.addEventListener('click', doLogout);
el('header-logout')?.addEventListener('click', doLogout);
syncSidebar(); resizePrompt(); void initialize();
