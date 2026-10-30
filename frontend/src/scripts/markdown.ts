// Minimal Markdown for chat answers, built as DOM nodes with textContent only: model output never becomes HTML.
// Supports headings, paragraphs, lists (one nested level), quotes, rules, tables, fenced code, and inline
// code, bold, italic, strikethrough, and http(s)/mailto links.

export type CodeBlock = (language: string, code: string) => HTMLElement;

const SAFE_LINK = /^(https?:\/\/|mailto:)/i;
const INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^*]+?)\*\*|__([^_]+?)__|~~([^~]+?)~~|\*([^*\s][^*]*?)\*|(?<![\w])_([^_\s][^_]*?)_(?![\w])|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

function element(tag: string, className = '') { const node = document.createElement(tag); if (className) node.className = className; return node; }

/** Inline formatting inside one block of text. */
export function inline(target: HTMLElement, text: string) {
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index! > last) target.append(text.slice(last, match.index));
    const [whole, , code, bold, bold2, strike, italic, italic2, label, href, bare] = match;
    if (code !== undefined) { const node = element('code', 'inline-code'); node.textContent = code.trim() ? code : whole; target.append(node); }
    else if (bold || bold2) { const node = element('strong'); inline(node, bold || bold2); target.append(node); }
    else if (strike) { const node = element('s'); inline(node, strike); target.append(node); }
    else if (italic || italic2) { const node = element('em'); inline(node, italic || italic2); target.append(node); }
    else if (label !== undefined || bare) {
      const url = bare || href;
      if (SAFE_LINK.test(url)) {
        const link = element('a') as HTMLAnchorElement; link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer nofollow';
        link.textContent = bare || label; target.append(link);
      } else target.append(whole);
    }
    last = match.index! + whole.length;
  }
  if (last < text.length) target.append(text.slice(last));
}

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const cells = (row: string) => row.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());

function list(lines: string[], start: number) {
  const first = LIST_ITEM.exec(lines[start])!;
  const ordered = /\d/.test(first[2]);
  const root = element(ordered ? 'ol' : 'ul');
  if (ordered && parseInt(first[2], 10) !== 1) (root as HTMLOListElement).start = parseInt(first[2], 10);
  const baseIndent = first[1].length;
  let i = start, item: HTMLElement | null = null;
  while (i < lines.length) {
    // Loose lists: blank lines between items keep the same list (and its numbering).
    if (!lines[i].trim()) {
      let next = i; while (next < lines.length && !lines[next].trim()) next++;
      const after = next < lines.length ? LIST_ITEM.exec(lines[next]) : null;
      if (after && after[1].length >= baseIndent && /\d/.test(after[2]) === ordered) { i = next; continue; }
      break;
    }
    const match = LIST_ITEM.exec(lines[i]);
    if (match && match[1].length <= baseIndent + 1) {
      if (/\d/.test(match[2]) !== ordered && match[1].length === baseIndent) break;
      item = element('li'); inline(item, match[3]); root.append(item); i++;
    } else if (match && item) {
      const [nested, next] = list(lines, i); item.append(nested); i = next;
    } else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && item) {
      item.append(' '); inline(item, lines[i].trim()); i++;
    } else break;
  }
  return [root, i] as const;
}

/** Block-level Markdown for text without code fences. */
function blocks(target: HTMLElement, text: string) {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const heading = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (heading) {
      const node = element(`h${Math.min(heading[1].length + 2, 6)}`, 'md-heading'); inline(node, heading[2]); target.append(node); i++; continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { target.append(element('hr')); i++; continue; }
    if (LIST_ITEM.test(line)) { const [node, next] = list(lines, i); target.append(node); i = next; continue; }
    if (/^\s*>/.test(line)) {
      const quote = element('blockquote'); const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ''));
      blocks(quote, body.join('\n')); target.append(quote); continue;
    }
    if (line.includes('|') && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      const wrap = element('div', 'md-table'); const table = element('table');
      const head = element('tr'); for (const cell of cells(line)) { const th = element('th'); inline(th, cell); head.append(th); }
      const thead = element('thead'), tbody = element('tbody'); thead.append(head); table.append(thead, tbody);
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        const row = element('tr'); for (const cell of cells(lines[i])) { const td = element('td'); inline(td, cell); row.append(td); }
        tbody.append(row); i++;
      }
      wrap.append(table); target.append(wrap); continue;
    }
    const paragraph = element('p');
    const body: string[] = [];
    while (i < lines.length && lines[i].trim() && !LIST_ITEM.test(lines[i]) && !/^(#{1,6})\s/.test(lines[i]) && !/^\s*>/.test(lines[i])
           && !(lines[i].includes('|') && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]))) body.push(lines[i++]);
    body.forEach((part, index) => { if (index) paragraph.append(element('br')); inline(paragraph, part); });
    target.append(paragraph);
  }
}

/** Render a full answer; an unterminated fence (while streaming) still shows as code. */
export function renderMarkdown(target: HTMLElement, text: string, codeBlock: CodeBlock) {
  target.replaceChildren();
  const fence = /```([^\n`]*)\n([\s\S]*?)(?:```|$)/g;
  let last = 0;
  for (const match of text.matchAll(fence)) {
    blocks(target, text.slice(last, match.index));
    target.append(codeBlock(match[1].trim().split(/\s+/)[0] || 'code', match[2].replace(/\n$/, '')));
    last = match.index! + match[0].length;
  }
  blocks(target, text.slice(last));
}
