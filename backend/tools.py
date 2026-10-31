"""Tools HEAD can plan into a run: web search through SearXNG, and reading web pages as clean text.

L.E.O. runs these itself and hands the text to a model, so research works with any model, including ones
without tool-calling support. Model-chosen URLs are untrusted: only public http(s) addresses are fetched,
every redirect is re-checked, and size, time, and text length are capped.
"""
import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

MAX_PAGE_BYTES = 2_000_000
PAGE_CHARS = 8000  # About 2–3K tokens per page: reading pages is the most expensive part of research.
MAX_REDIRECTS = 3
USER_AGENT = "Mozilla/5.0 (compatible; LEO-research/0.2; +https://github.com/albertleooonardi/L.E.O)"
SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe", "template", "button"}
BLOCK = {"p", "div", "li", "br", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "pre", "blockquote"}


class ToolError(Exception):
    pass


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.skip, self.in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag == "title" and not self.skip and not self.title:
            self.in_title = True  # Only the document title: SVG icons carry their own <title> tags.
        elif tag in BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP and self.skip:
            self.skip -= 1
        elif tag == "title" and self.in_title:
            self.in_title = False
        elif tag in BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def readable_text(html):
    """Title and main text of a page, with scripts, menus, and footers removed and whitespace collapsed."""
    parser = _Text()
    parser.feed(html)
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    text = "\n".join(line for line in lines if len(line) > 1)
    return " ".join(parser.title.split())[:200], text


def _public_host(host, resolver):
    """True only if every address the host resolves to is public (blocks local network and metadata services)."""
    try:
        infos = resolver(host, None)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(addresses)


async def fetch_page(url, transport=None, resolver=None, timeout=20):
    resolver = resolver or socket.getaddrinfo
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), transport=transport, follow_redirects=False,
                                 trust_env=False, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ToolError(f"Only public http(s) pages can be read: {url[:120]}")
            if not await asyncio.to_thread(_public_host, parts.hostname, resolver):
                raise ToolError(f"Blocked a private or unresolvable address: {parts.hostname}")
            try:
                async with client.stream("GET", url) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    if not response.is_success:
                        raise ToolError(f"Page returned HTTP {response.status_code}: {url[:120]}")
                    kind = response.headers.get("content-type", "").lower()
                    if kind and not any(t in kind for t in ("text/html", "text/plain", "application/xhtml")):
                        raise ToolError(f"Not a readable page ({kind.split(';')[0]}): {url[:120]}")
                    body = b""
                    async for piece in response.aiter_bytes():
                        body += piece
                        if len(body) > MAX_PAGE_BYTES:
                            break
                    text = body.decode(response.encoding or "utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise ToolError(f"Could not read {url[:120]}: {type(exc).__name__}") from exc
            title, content = readable_text(text) if "html" in kind or "<html" in text[:500].lower() else ("", text)
            if len(content) < 200:
                raise ToolError(f"Page has too little readable text: {url[:120]}")
            return {"url": url, "title": title or parts.hostname, "text": content[:PAGE_CHARS]}
    raise ToolError(f"Too many redirects: {url[:120]}")


async def web_search(query, search_url, transport=None, limit=5):
    """Results from a SearXNG instance's JSON API (enable `json` under search.formats in its settings.yml)."""
    if not search_url:
        raise ToolError("Web search is not set up. Add your SearXNG address in Settings → Web search.")
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport, trust_env=False, follow_redirects=False) as client:
            response = await client.get(search_url.rstrip("/") + "/search", params={"q": query, "format": "json", "safesearch": 1})
    except httpx.HTTPError as exc:
        raise ToolError(f"Cannot reach the search service at {search_url}; is SearXNG running?") from exc
    if response.status_code == 403:
        raise ToolError("SearXNG refused JSON output; enable `json` under search.formats in its settings.yml")
    if not response.is_success:
        raise ToolError(f"Search service returned HTTP {response.status_code}")
    try:
        results = response.json().get("results", [])
    except ValueError as exc:
        raise ToolError("Search service did not return JSON") from exc
    seen, found = set(), []
    for item in results:
        link = item.get("url") or ""
        if urlsplit(link).scheme in {"http", "https"} and link not in seen:
            seen.add(link)
            found.append({"title": (item.get("title") or link)[:200], "url": link, "snippet": (item.get("content") or "")[:300]})
        if len(found) >= limit:
            break
    return found


async def research(queries, urls, search_url, pages=3, transport=None, resolver=None):
    """Search, pick the top pages (explicit URLs first, then results from each query in turn), and read them."""
    results, errors = {}, []
    for query in queries[:3]:
        try:
            results[query] = await web_search(query, search_url, transport)
        except ToolError as exc:
            errors.append(str(exc))
            break  # Same service for every query: one failure explains them all.
    picked = list(dict.fromkeys(urls))
    for rank in range(5):
        for found in results.values():
            if rank < len(found) and found[rank]["url"] not in picked:
                picked.append(found[rank]["url"])
    sources = []
    # Read a few extra candidates so a blocked or empty page does not shrink the source set.
    candidates = picked[:pages + 2]
    fetched = await asyncio.gather(*(fetch_page(u, transport, resolver) for u in candidates), return_exceptions=True)
    for link, page in zip(candidates, fetched):
        if isinstance(page, Exception):
            errors.append(str(page) if isinstance(page, ToolError) else f"Could not read {link[:120]}")
        elif len(sources) < pages:
            sources.append({"n": len(sources) + 1, **page})
    return {"queries": list(results), "results": [r for found in results.values() for r in found], "sources": sources, "errors": errors[:6]}


def sources_block(sources):
    return "\n\n".join(f"[{s['n']}] {s['title']} — {s['url']}\n{s['text']}" for s in sources)


# Photos for generated pages: real, openly licensed images from Wikimedia Commons (free, no key), each checked to load.
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PHOTO_HOST = "upload.wikimedia.org"
STYLE_WORDS = re.compile(r"\b(minimalist|minimal|clean|modern|simple|elegant|sleek|beautiful|informative|informatic|responsive|"
                         r"one[- ]page|single[- ]page|landing|page|website|site|web|html|dan|and|with|design|layout)\b", re.I)


def photo_topic(prompt):
    """What a page is about, for a photo search: "a landing page about coffee" → "coffee"."""
    text = prompt[:300]
    found = re.search(r"\b(?:about|for|on|of)\s+(?:an?\s+|the\s+|my\s+|our\s+)?([a-z][\w' -]{2,50}?)(?=[.,;:!?]|\s+(?:with|that|which|using|as|in|to)\b|$)",
                      text, re.I) or re.search(r"\b(?:an?|the|my|our)\s+([\w' -]{3,40}?)\s+(?:website|landing page|web ?page|site|page)\b", text, re.I)
    topic = STYLE_WORDS.sub(" ", found.group(1) if found else text)
    return " ".join(topic.split()[:4]).strip(" -'") or None


def _plain(html_text, limit):
    return " ".join(re.sub(r"<[^>]+>", " ", html_text or "").split())[:limit]


async def find_photos(topic, transport=None, limit=4, timeout=15):
    """Up to `limit` wide, openly licensed photos for `topic`, with author and licence; never an unchecked URL."""
    params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": f"{topic} filetype:bitmap", "gsrnamespace": 6,
              "gsrlimit": 40, "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": 1600}
    photos, errors = [], []
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False, follow_redirects=False,
                                     headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(COMMONS_API, params=params)
            response.raise_for_status()
            pages = sorted((response.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
            for page in pages:
                info = (page.get("imageinfo") or [{}])[0]
                meta = info.get("extmetadata") or {}
                licence = _plain((meta.get("LicenseShortName") or {}).get("value"), 40)
                url = (info.get("thumburl") or info.get("url") or "").split("?")[0]  # Drop Wikimedia's tracking parameters.
                width, height = info.get("width") or 0, info.get("height") or 0
                # Photographs (JPEG), landscape, large enough for a hero, under an open licence, from Wikimedia's own image host.
                if (info.get("mime") != "image/jpeg" or width < 1000 or width < height * 1.15 or urlsplit(url).hostname != PHOTO_HOST
                        or not re.search(r"\bCC\b|CC0|CC-BY|Public domain|PD", licence, re.I)):
                    continue
                check = await client.head(url)
                if check.status_code != 200 or not check.headers.get("content-type", "").startswith("image/"):
                    continue
                title = re.sub(r"\.\w+$", "", page.get("title", "").removeprefix("File:")).replace("_", " ")
                photos.append({"n": len(photos) + 1, "url": url, "title": _plain(title, 90), "author": _plain((meta.get("Artist") or {}).get("value"), 60) or "Unknown",
                               "licence": licence, "source": info.get("descriptionurl", "")})
                if len(photos) >= limit:
                    break
    except (httpx.HTTPError, ValueError) as exc:
        errors.append(f"Photo search failed: {type(exc).__name__}")
    return {"topic": topic, "photos": photos, "errors": errors}


def photos_block(photos):
    return "\n".join(f"[P{p['n']}] {p['url']} — \"{p['title']}\" — by {p['author']}, {p['licence']} — source page: {p['source']}" for p in photos)
