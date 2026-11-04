// Sign-in and create-account page: OAuth provider buttons, the username/password form, and ?error= banners.
type Provider = { id: string; name: string };
type Providers = { providers: Provider[]; password_registration: boolean };
type Mode = 'signin' | 'register';

const el = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const page = document.querySelector<HTMLElement>('.auth-split')!;
const form = el<HTMLFormElement>('auth-form');
const username = el<HTMLInputElement>('auth-username');
const password = el<HTMLInputElement>('auth-password');
const submit = el<HTMLButtonElement>('auth-submit');
const feedback = el('auth-feedback');
const params = new URLSearchParams(location.search);
const LAST_PROVIDER = 'leo-last-provider';

let mode = page.dataset.mode as Mode;
let available: Providers = { providers: [], password_registration: false };

// Paths on this site only, matching the server's own check.
function safeNext(value: string | null) {
  return value && value.startsWith('/') && !value.startsWith('//') && !value.startsWith('/\\') ? value : '/';
}
const next = safeNext(params.get('next'));

const LOGOS: Record<string, string> = {
  google: '<svg viewBox="0 0 48 48" width="18" height="18" aria-hidden="true"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/><path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>',
  github: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M12 .3a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2c-3.3.7-4-1.6-4-1.6-.6-1.4-1.4-1.8-1.4-1.8-1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1 1.8 2.8 1.3 3.5 1 0-.8.4-1.3.7-1.6-2.7-.3-5.5-1.3-5.5-6 0-1.2.5-2.3 1.3-3.1-.2-.4-.6-1.6 0-3.2 0 0 1-.3 3.4 1.2a11.5 11.5 0 0 1 6 0c2.3-1.5 3.3-1.2 3.3-1.2.6 1.6.2 2.8 0 3.2.9.8 1.3 1.9 1.3 3.2 0 4.6-2.8 5.6-5.5 5.9.5.4.9 1 .9 2.2v3.3c0 .3.1.7.8.6A12 12 0 0 0 12 .3"/></svg>',
  oidc: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10.5" width="16" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5M12 14.5v2"/></svg>',
};

const ERRORS: Record<string, (email: string | null) => string> = {
  not_invited: (email) => email
    ? `${email} is not on this workspace's invite list. Ask your admin to add it, or use another account.`
    : 'That account has no verified email on the invite list. Ask your admin, or use another account.',
  cancelled: () => 'Sign-in was cancelled. Choose a method to try again.',
  failed: () => 'Sign-in did not complete. Please try again.',
};

function banner(message: string | null) {
  const box = el('auth-banner');
  box.textContent = message || '';
  box.hidden = !message;
}

function renderProviders() {
  const box = el('auth-providers');
  const last = (() => { try { return localStorage.getItem(LAST_PROVIDER); } catch { return null; } })();
  box.replaceChildren(...available.providers.map((provider) => {
    const link = document.createElement('a');
    link.className = 'auth-provider';
    link.href = `/api/auth/oauth/${encodeURIComponent(provider.id)}/start?next=${encodeURIComponent(next)}`;
    link.innerHTML = LOGOS[provider.id] || LOGOS.oidc;  // Fixed markup; names go in as text below.
    const label = document.createElement('span');
    label.textContent = `Continue with ${provider.name}`;
    link.append(label);
    if (provider.id === last) {
      const badge = document.createElement('small');
      badge.className = 'auth-badge';
      badge.textContent = 'Last used';
      link.append(badge);
    }
    link.addEventListener('click', () => {
      try { localStorage.setItem(LAST_PROVIDER, provider.id); } catch { /* Only a convenience. */ }
      link.classList.add('is-loading');
      link.setAttribute('aria-busy', 'true');
    });
    return link;
  }));
  const any = available.providers.length > 0;
  box.hidden = !any;
  el('auth-divider').hidden = !any || formClosed();
}

const formClosed = () => mode === 'register' && !available.password_registration;

function setMode(value: Mode, remember = true) {
  mode = value;
  page.dataset.mode = value;
  const signin = value === 'signin';
  for (const tab of document.querySelectorAll<HTMLButtonElement>('[data-tab]')) {
    const selected = tab.dataset.tab === value;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  el('auth-title').textContent = signin ? 'Welcome back' : 'Create your account';
  el('auth-caption').textContent = signin ? 'Your models, chats and keys, on any device.'
    : !formClosed() ? 'Use your work account, or pick a username and password.'
    : available.providers.length ? 'Join with your work account if your email is invited.' : 'Accounts on this server are invite-only.';
  document.title = `${signin ? 'Sign in' : 'Create account'} — L.E.O`;
  submit.textContent = signin ? 'Sign in' : 'Create account';
  password.autocomplete = signin ? 'current-password' : 'new-password';
  el('auth-rules').hidden = signin;
  feedback.textContent = '';
  const closed = formClosed();
  form.hidden = closed;
  const note = el('auth-closed');
  note.hidden = !closed;
  note.textContent = available.providers.length
    ? 'New accounts join with a button above when their email is on the invite list. Otherwise, ask your workspace admin to create one for you.'
    : 'Ask your workspace admin to create an account for you.';
  renderProviders();
  checkRules();
  if (remember) history.replaceState(null, '', `${signin ? '/login' : '/register'}${location.search}`);
}

function checkRules() {
  const rules = { username: username.value.trim().length >= 3, length: password.value.length >= 8 };
  for (const item of document.querySelectorAll<HTMLElement>('#auth-rules [data-rule]')) {
    item.classList.toggle('met', rules[item.dataset.rule as keyof typeof rules]);
  }
  return rules.username && rules.length;
}

function busy(on: boolean) {
  submit.disabled = on;
  submit.setAttribute('aria-busy', String(on));
  submit.textContent = on ? (mode === 'signin' ? 'Signing in…' : 'Creating account…') : (mode === 'signin' ? 'Sign in' : 'Create account');
}

async function post(path: string, body: unknown) {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), credentials: 'include' });
  if (response.ok) return null;
  const data = await response.json().catch(() => ({}));
  const detail = data?.detail;
  // Validation errors arrive as a list; show the first one in plain words.
  return typeof detail === 'string' ? detail : Array.isArray(detail) && detail[0]?.msg ? String(detail[0].msg) : 'Something went wrong. Please try again.';
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  feedback.textContent = '';
  const credentials = { username: username.value.trim(), password: password.value };
  if (!credentials.username || !credentials.password) {
    feedback.textContent = 'Enter your username and password.';
    (credentials.username ? password : username).focus();
    return;
  }
  if (mode === 'register' && !checkRules()) {
    feedback.textContent = 'Check the requirements above.';
    return;
  }
  busy(true);
  try {
    const failed = mode === 'register'
      ? await post('/api/auth/register', credentials) ?? await post('/api/auth/login', credentials)
      : await post('/api/auth/login', credentials);
    if (failed) {
      feedback.textContent = failed;
      busy(false);
      password.select();
      return;
    }
    location.href = next;
  } catch {
    feedback.textContent = 'Unable to reach the server. Is it running?';
    busy(false);
  }
});

el('auth-reveal').addEventListener('click', (event) => {
  const button = event.currentTarget as HTMLButtonElement;
  const show = password.type === 'password';
  password.type = show ? 'text' : 'password';
  button.textContent = show ? 'Hide' : 'Show';
  button.setAttribute('aria-pressed', String(show));
  password.focus();
});

for (const type of ['keydown', 'keyup'] as const) {
  password.addEventListener(type, (event) => { el('auth-caps').hidden = !event.getModifierState('CapsLock'); });
}
password.addEventListener('blur', () => { el('auth-caps').hidden = true; });
username.addEventListener('input', checkRules);
password.addEventListener('input', checkRules);

const tabs = [...document.querySelectorAll<HTMLButtonElement>('[data-tab]')];
for (const tab of tabs) {
  tab.addEventListener('click', () => setMode(tab.dataset.tab as Mode));
  tab.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const other = tabs.find((t) => t !== tab)!;
    other.focus();
    setMode(other.dataset.tab as Mode);
  });
}

const error = params.get('error');
if (error) banner((ERRORS[error] || ERRORS.failed)(params.get('email')));

setMode(mode, false);
(async () => {
  try {
    const response = await fetch('/api/auth/providers', { credentials: 'include' });
    if (response.ok) available = await response.json();
  } catch {
    feedback.textContent = 'Unable to reach the server. Is it running?';
  }
  setMode(mode, false);
  if (!form.hidden && !available.providers.length) username.focus();
})();
