// Canvas: preview generated web pages in a sandboxed frame. The frame has no same-origin access, so page
// scripts cannot read L.E.O.'s cookies, storage, or API; a CSP also blocks network calls from the page.

export type Page = { html: string; title: string };

const SANDBOX = 'allow-scripts allow-forms allow-modals';
const CSP = "default-src 'none'; img-src * data: blob:; media-src * data: blob:; style-src 'unsafe-inline' *; font-src * data:; script-src 'unsafe-inline' *; connect-src 'none'; form-action 'none'; base-uri 'none'";
const BLOCK = /```(html|css|js|javascript)[^\n]*\n([\s\S]*?)```/gi;

/** A runnable page from an answer: the first HTML block, with separate CSS/JS blocks merged in. */
export function extractPage(text: string): Page | null {
  let html = '', css = '', js = '';
  for (const [, language, code] of text.matchAll(BLOCK)) {
    const kind = language.toLowerCase();
    if (kind === 'html' && !html) html = code;
    else if (kind === 'css') css += `${code}\n`;
    else if (kind === 'js' || kind === 'javascript') js += `${code}\n`;
  }
  if (!html || !/<(html|body|div|main|section|header|h1|canvas|svg|button|p)\b/i.test(html)) return null;
  if (css) html = /<\/head>/i.test(html) ? html.replace(/<\/head>/i, `<style>\n${css}</style>\n</head>`) : `<style>\n${css}</style>\n${html}`;
  if (js) html = /<\/body>/i.test(html) ? html.replace(/<\/body>(?![\s\S]*<\/body>)/i, `<script>\n${js}</script>\n</body>`) : `${html}\n<script>\n${js}</script>`;
  const title = html.match(/<title[^>]*>([^<]{1,80})<\/title>/i)?.[1].trim() || 'Generated page';
  return { html, title };
}

/** The HTML block still being written, for live code streaming. */
export function streamingHtml(text: string) {
  return text.match(/```html[^\n]*\n([\s\S]*?)(?:```|$)/i)?.[1] ?? null;
}

function withPolicy(html: string) {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${CSP}">`;
  return /<head[^>]*>/i.test(html) ? html.replace(/<head[^>]*>/i, match => `${match}\n${meta}`) : `${meta}\n${html}`;
}
const escapeAttribute = (value: string) => value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

const el = (id: string) => document.getElementById(id) as HTMLElement;
let current: Page | null = null;
let dismissed = false;

function setTab(tab: 'preview' | 'code') {
  el('canvas-tab-preview').setAttribute('aria-selected', String(tab === 'preview'));
  el('canvas-tab-code').setAttribute('aria-selected', String(tab === 'code'));
  el('canvas-frame').hidden = tab !== 'preview'; el('canvas-code').hidden = tab !== 'code';
}
function setOpen(open: boolean) {
  el('canvas-panel').hidden = !open; document.body.classList.toggle('canvas-open', open);
  if (open) document.dispatchEvent(new CustomEvent('leo:canvas-open'));
}
function render() {
  if (!current) return;
  (el('canvas-frame') as HTMLIFrameElement).srcdoc = withPolicy(current.html);
}

export const canvas = {
  /** Show a finished page and run it. */
  show(page: Page, tab: 'preview' | 'code' = 'preview') {
    current = page; dismissed = false;
    el('canvas-title').textContent = page.title;
    el('canvas-code').querySelector('code')!.textContent = page.html;
    el('canvas-status').textContent = 'Running in a sandbox: no access to L.E.O., your keys, or the network.';
    setOpen(true); setTab(tab); render();
  },
  /** Follow an HTML block while it is generated; opens once per run unless the person closed it. */
  stream(code: string) {
    if (dismissed) return;
    if (el('canvas-panel').hidden) { setOpen(true); setTab('code'); el('canvas-title').textContent = 'Writing the page…'; }
    el('canvas-status').textContent = 'Writing code. The preview runs when the answer is complete.';
    const target = el('canvas-code');
    const atEnd = target.scrollTop + target.clientHeight >= target.scrollHeight - 40;
    target.querySelector('code')!.textContent = code;
    if (atEnd) target.scrollTop = target.scrollHeight;
  },
  /** The run finished: run the page unless the person closed the canvas during this run. */
  finish(page: Page) { if (!dismissed) this.show(page); },
  /** Switching chats: close without remembering a dismissal. */
  hide() { setOpen(false); },
  /** A new run starts: allow auto-open again. */
  reset() { dismissed = false; },
  close() { setOpen(false); dismissed = true; },
  get open() { return !el('canvas-panel').hidden; },
};

el('canvas-tab-preview').addEventListener('click', () => { setTab('preview'); render(); });
el('canvas-tab-code').addEventListener('click', () => setTab('code'));
el('canvas-reload').addEventListener('click', () => { setTab('preview'); render(); });
el('canvas-close').addEventListener('click', () => canvas.close());
el('canvas-open').addEventListener('click', () => {
  if (!current) return;
  // The new tab holds only a sandboxed frame, so the page still runs without L.E.O.'s origin.
  const wrapper = `<!doctype html><meta charset="utf-8"><title>${escapeAttribute(current.title)}</title><style>html,body,iframe{margin:0;width:100%;height:100%;border:0;display:block}</style><iframe sandbox="${SANDBOX}" srcdoc="${escapeAttribute(withPolicy(current.html))}"></iframe>`;
  const url = URL.createObjectURL(new Blob([wrapper], { type: 'text/html' }));
  window.open(url, '_blank', 'noopener'); setTimeout(() => URL.revokeObjectURL(url), 60_000);
});
el('canvas-download').addEventListener('click', () => {
  if (!current) return;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([current.html], { type: 'text/html' }));
  link.download = `${current.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'page'}.html`;
  link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
});
