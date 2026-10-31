<div align="center">

# L.E.O.

**Language Model Efficiency Orchestrator**

Use your free and local models like a pro subscription.

![Status: work in progress](https://img.shields.io/badge/status-work%20in%20progress-orange)
![Version 0.2](https://img.shields.io/badge/version-0.2-blue)
![Tests: 96 passing](https://img.shields.io/badge/tests-96%20passing-brightgreen)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB)
![Runs locally](https://img.shields.io/badge/runs-locally-555)

</div>

![L.E.O. with a generated coffee landing page in the live preview](docs/screenshots/hero.png)

<sub>"Create me a minimalist, clean dan informatic landing page about coffee" in Auto mode. HEAD saw one self-contained page, so it made one call to the best free frontend model (Gemini 2.5 Flash): 38 seconds, 9.2K tokens, three real Wikimedia photos with their photographers credited, no broken links. Shown in the preview at desktop width. *(Real run)*</sub>

L.E.O. connects the models you already have (Ollama, LM Studio, vLLM, every free model on OpenRouter, Gemini AI Studio, and paid APIs such as Claude if you choose) and puts a **routing head (HEAD)** in front of them. HEAD reads each task, decides which skill it needs and how hard it is, and sends it to the cheapest model that can do it well, at a matching thinking effort. A quick question goes to a small model, code to a coding model with an independent reviewer, a web page to your best design model with real photos, and paid models step in only when they are the better or cheaper fit.

> **🚧 Work in progress.** L.E.O. runs end to end with an automated test suite, but routing rules, defaults, and the database schema may still change without migrations. Use it with your own models and keys, and do not expose it to the internet without the [access settings](#security-and-execution-boundaries).

## At a glance

| | |
| --- | --- |
| **Matches the big web apps on correctness** | 4/4 coding and reasoning tasks passed, like the Gemini and ChatGPT web apps, using free models only |
| **Fewer defects on pages** | No broken links or placeholder text, self-contained files, and real credited photos instead of invented image links |
| **Costs nothing to run on free models** | Routes across 12 free models; a rate-limited model is waited for or skipped, so runs keep going |
| **Shows its work** | Every call's model, reason, tokens, and context level, per chat and overall |
| **Honest trade-off** | Reviewed coding answers take 1–3 minutes instead of seconds |

## Benchmark: L.E.O. against the Gemini and ChatGPT web apps

The same prompts went to **gemini.google.com** (Gemini 3.5 Flash), **chatgpt.com** (free, logged out), and L.E.O. Auto with its free models. `benchmarks/qa.py` graded every reply the same way: hidden tests for code, the known answer for the puzzle, and for the page a 14-point checklist, visitor-facing defects, and content measured in the browser.

| Task | Gemini web | ChatGPT web | L.E.O. (free models) | L.E.O. calls · tokens · time |
| --- | :---: | :---: | :---: | --- |
| IPv4 validator (hidden tests) | ✅ | ✅ | ✅ | 2 · 8.3K · 107 s |
| Duration parser (hidden tests) | ✅ | ✅ | ✅ | 3 · 17.7K · 161 s |
| Roman numerals (hidden tests) | ✅ | ✅ | ✅ | 2 · 7.5K · 54 s |
| Stairs puzzle (answer 927) | ✅ | ✅ | ✅ | 1 · 1.7K · 38 s |
| Coffee landing page: checklist | 79% | 79% | **79–93%** (4 runs) | 1 · 4.0–9.2K · 10–38 s |
| … broken or empty links | 4 | 1 | **0** | |
| … photos | none | 5 hot-linked | **3 verified, credited** | |
| … external scripts | Tailwind CDN | none | **none** | |

![The coffee landing pages from Gemini, ChatGPT, and L.E.O. side by side](docs/screenshots/qa-coffee-pages.jpg)

<sub>Each page rendered at desktop width and shown whole. ChatGPT's page is the most polished visually; L.E.O.'s has real, credited photos, the most content, and no broken links.</sub>

**How L.E.O. got there.** In round 1, L.E.O. passed only 1 of 4: a cheap coding model used `str.isdigit()` (which accepts non-ASCII digits) and left a broken draft before a "let me revise" version, and the page was thin with a broken link. The QA loop then added:

- **An independent reviewer where rules are stated.** Coding requests with explicit requirements are reviewed by the strongest available model that did not write the answer, tracing the code on the request's own examples.
- **Static checks that trigger a repair:** several versions of one function, links to missing sections, placeholder text.
- **Real photos for pages** from Wikimedia Commons, each checked to load and credited.
- **Self-correcting profiles:** a model that burns its whole output limit on hidden thinking is marked as a reasoning model, so later calls cap its effort.
- **Continuation:** an answer cut off inside a code block is continued, not lost.

Claude.ai needs a sign-in, so Claude is not in this table yet.

<details>
<summary><b>Earlier benchmarks and how to run them</b></summary>

`benchmarks/run.py` sends the same prompts to single models directly ("one app, one model", like using the Gemini app) and to L.E.O. It checks coding answers against hidden acceptance tests (run in a macOS sandbox with network and writes blocked), scores websites with a 14-point rubric (complete page, mobile viewport, menu with prices, call to action, hours or contact, responsive layout, interactivity, accessibility…), and checks the long app task for its required features. `benchmarks/report.py` merges result files and recomputes L.E.O.'s **actual** tokens from each run's call ledger; unbilled reservations of failed calls are shown separately.

```sh
LEO_DATA_DIR=.leo-bench uv run python -m benchmarks.run --single gemini-3.5-flash --single <other model> --leo auto
uv run python -m benchmarks.report benchmarks/results/<file>.json [...]
```

Tasks: six coding functions with edge cases, "Create me a coffee shop website", "Create a landing page with a clean, minimalist design for a note-taking app", and a long task: a complete personal finance tracker in one HTML file.

#### 30 Oct 2026: L.E.O. Auto with a team of free models

L.E.O.'s team: Gemini 2.5 Flash, Laguna S (coding), Nemotron 3.5 Lightning (reasoning), and Nemotron 3 Ultra 550B (strong), all free. Baselines: Gemini 3.5 Flash alone (closest to the free Gemini app) and Nemotron 3 Ultra alone. Thinking tokens are counted.

| Approach | Correct | Blocked by provider | Tokens per correct task | Mean time |
| --- | --- | --- | --- | --- |
| Gemini 3.5 Flash alone | 2/2 | 7 of 9 (free quota exhausted) | 2,746 | 17 s |
| **L.E.O. Auto** | **8/9** | **0** | 14,822 | 116 s |
| Nemotron 3 Ultra alone (pages and app only) | 2/3 | 0 | 20,143 | 347 s |

| Task | Gemini 3.5 Flash | L.E.O. Auto | Nemotron 3 Ultra |
| --- | --- | --- | --- |
| roman | ✅ 3,446 | ✅ **1,803** | — |
| intervals | ✅ 2,047 | ✅ **1,601** | — |
| duration, top-k, LRU | rate-limited | ✅ 1,430 · 537 · 640 | — |
| ipv4 | rate-limited | ❌ accepted Arabic-Indic digits | — |
| coffee shop website | rate-limited | ✅ rubric 100%, 21,707 | ✅ rubric 100%, **11,481** |
| minimalist landing page | rate-limited | ✅ rubric 93%, 36,599 | ❌ rubric 71%, 16,418 |
| finance tracker app | rate-limited | ✅ all features, 52,830 | ✅ all features, **12,387** |

**What this shows**

- **Everyday coding: L.E.O. used fewer tokens** than Gemini 3.5 Flash alone on every task both answered (−48% and −22%), by routing to a coding model and giving thinking models low effort.
- **Resilience: L.E.O. answered every task**, while the single free model was blocked by its quota after two. Routing across many free models is L.E.O.'s clearest advantage today.
- **Builds: L.E.O. cost 2–4× the tokens of one strong model.** Splitting a page across models, plus a review-and-repair loop, costs more than one capable model writing it once. Two fixes since then (no review loop for design work in Auto; the last subtask's result reused as the answer) target this; a re-run is pending.
- **Quality on pages:** L.E.O. passed all three page/app checks; Nemotron Ultra alone missed the landing page's footer, contact details, interactivity, and a complete document.
- **Shared limits:** most models, including L.E.O.'s, failed the IPv4 edge case. Routing cannot add knowledge the models lack.

#### 29 Oct 2026: before smart routing

Nemotron 3.5 Lightning (cheap) and Gemini 2.5 Flash (strong); L.E.O. used a fixed generator (Nemotron). Gemini's thinking tokens were not yet counted, so its numbers are understated.

| Approach | Passed | Tokens per passed task | Mean calls |
| --- | --- | --- | --- |
| Gemini 2.5 Flash alone | 5/6 (1 rate limit) | 1,406 | 1.3 |
| Nemotron alone | 5/6 | 5,337 | 1.0 |
| L.E.O. Quick (fixed generator) | 4/6 | 4,814 | 1.2 |
| L.E.O. Reviewed | 4/6 | 9,132 | 2.3 |
| L.E.O. Deep | 3/3 | 18,813 | 6.3 |

This run exposed the problems fixed in v0.2: a fixed generator, a reasoning model running out of output before answering, uncounted thinking tokens, a context bug in Deep mode, and review approving code that fails on import.

Full results are in `benchmarks/results/`. Samples are small; treat differences as indicative, not statistically significant. 
</details>

## How it works

```mermaid
flowchart LR
    R[Your request] --> H{HEAD<br/>skill · difficulty · tools}
    H -->|needs current info| W[Web search<br/>SearXNG]
    H -->|page build| P[Real photos<br/>Wikimedia Commons]
    H -->|light / standard / one page| Q[Quick: one call]
    H -->|stated rules · strong work| V[Reviewed: answer + independent review]
    H -->|app build · several skills| D[Deep: plan → subtasks by skill]
    W & P --> Q & D
    Q & V & D --> C[Static checks · repair · canvas]
```

1. **Classify, with no tokens spent.** Keyword rules match the request to a skill (General, Frontend, Backend, Coding, Design, Research, Reasoning, or your own). Signals such as a build request, scope words ("complete", "production"), several skills, several parts, or length set the difficulty: light, standard, or strong.
2. **Pick a mode.** Light and standard work, and a single web page, get **Quick** (one call). Coding with explicit rules and strong single-skill work get **Reviewed**. App builds and multi-skill work get **Deep**: a planner splits the work into subtasks, each routed by its own skill.
3. **Gather what the task needs.** Web pages for research questions (cited as [n]), and verified, openly licensed photos for page builds.
4. **Pick a model and an effort.** Candidates are ranked: strong enough → has the skill → generalist → then, with **Auto**, the cheapest per call (free and local models cost nothing; paid ones are priced from their own past token use) → the lightest tier that is enough. Thinking models get low effort for light and standard work and medium for strong work and pages.
5. **Survive the free tier.** A model asking for a short wait (≤ 20 s) is retried once rather than replaced by a weaker one; otherwise it backs off from 30 seconds up to an hour while the next model takes over.
6. **Check and explain.** Static checks can trigger a repair; reviews return a pass or a list of issues; every decision is logged with its reason, e.g. `Auto → reviewed · coding · standard · 4 calls`.

`POST /api/route` (and the MCP tool `leo_route`) previews the decision for any prompt without calling a model.

## Features

- **Routing head:** skills you can edit, strength tiers, thinking-effort control, Auto/Quick/Reviewed/Deep modes, per-subtask routing, and a preview of every decision.
- **Models:** OpenAI-compatible, OpenAI Responses, and Anthropic APIs with streaming; one-click discovery of every model a key can use, with suggested profiles; one row per provider with on/off switches, prices, and average tokens per call.
- **Quality:** an independent reviewer for code with stated rules, static checks with automatic repair, real photos for pages, and continuation of cut-off answers.
- **Research:** web search through your own SearXNG, safe page reading, cited answers.
- **Control:** per-run limits on calls, tokens, time, and cost; paid models only with your opt-in and within a cost cap.
- **Interface:** chats grouped by day, rendered Markdown, a live run view, a sandboxed live preview with a desktop-width mode, per-chat and overall usage, tabbed Settings, light and dark themes.
- **Reliability:** a durable background worker with checkpoints, crash recovery, Stop, and resumable streams.
- **Integration:** an MCP server so Claude Code and other agents can hand work to your routed models.

## Screenshots

| Chats and a reviewed coding answer | Settings → Models |
| --- | --- |
| ![Sidebar with chats grouped by day, and a reviewed answer](docs/screenshots/sidebar-chat.jpg) | ![Models grouped by provider with switches, prices, and tokens per call](docs/screenshots/settings-models.jpg) |
| HEAD's choice under the answer: Auto → reviewed, coding, standard. Two free models were rate-limited, Nemotron 3 Ultra wrote the code, Nemotron Super VL reviewed it. *(Real run)* | One row per provider; each model has a switch, its strength and skills, its price, and its average tokens per call. *(Real data)* |
| **Settings → Routing** | **Usage across all chats** |
| ![Routing tab with Auto, Free first, Local first, By priority](docs/screenshots/settings-routing.jpg) | ![Usage page with tokens by model, role, and outcome](docs/screenshots/usage-page.jpg) |
| **Auto** weighs every model you turned on: fit first, then cost per call. | Tokens, success rate, context, and shares by model, role, and outcome. *(Real data)* |
| **Web research with cited sources** | **Discover every model a key can use** |
| ![Answer citing three recipe pages](docs/screenshots/research-answer.jpg) | ![Model discovery list](docs/screenshots/discover-models.jpg) |
| SearXNG search, three pages read, one free model answered with [n] citations: 1 call, 6,761 tokens. *(Real run)* | OpenRouter's free chat models with suggested strength, thinking, and skills. *(Real data)* |

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Node.js 22.12+ (Node 24 recommended).

```sh
./start.sh
```

One command installs dependencies on first run, rebuilds the UI when it changed, starts web search (SearXNG) if it is installed, runs the API and a durable worker, and opens **http://127.0.0.1:8000**. Ctrl+C stops everything. Your models, keys, chats, and settings live in `.leo/`; pass another folder to use a different workspace, e.g. `./start.sh .leo-bench`.

Then open **Settings → Models → Add provider**, paste a key (a free OpenRouter or Gemini AI Studio key is enough), and use **Add models** to pick the models L.E.O. may use. No model is downloaded and no provider is contacted until you do.

## Status

v0.2 is a working prototype, not a finished product.

**Working and tested** (96 automated tests plus browser checks): the routing head and all modes; OpenRouter (12 free models) and Gemini AI Studio live; web research; real photos; durable runs; limits and paid opt-in; the interface above; the MCP server over stdio.

**Not yet proven or built:**

- **Speed on code.** Reviewed answers take 1–3 minutes; the web apps answer in seconds.
- **Free-tier limits dominate in practice.** Rate limits and outages hit Gemini, Laguna, and Gemma throughout testing; L.E.O. routes around them, but daily quotas still run out.
- **Review is a model's opinion.** Generated code is never executed by the server, so reviews cannot run tests.
- **Not run yet:** Docker Compose / PostgreSQL, and live use of OpenAI, Qwen, OpenCode, Ollama, LM Studio, vLLM, and Claude generation (the Claude account had no credits).
- **Single owner only:** no multi-user accounts, and no schema migrations yet.

## Real photos for pages

When HEAD sees a page build, it takes the page's topic ("a landing page about **coffee**") and searches **Wikimedia Commons** (free, no key) for wide, openly licensed photographs. Each candidate must be a JPEG photo at least 1,000 px wide, landscape, from Wikimedia's own image host, under a CC or public-domain licence, and must load when checked. Up to four go to the model with their title, photographer, and licence; the model must credit each photo it uses and may not invent other image URLs. If nothing suitable is found, the model is told to draw simple SVG or CSS visuals instead of leaving placeholders. Turn it off in **Settings → Web search → Real photos for pages**.

## Reference

<details>
<summary><b>Connect models</b></summary>

1. Open **Settings → Add provider** and choose a provider preset.
2. Set the API base URL and exact model ID. Enter an API key if the endpoint requires one; keys are encrypted on the server and never shown again.
3. Set the **routing profile**: skills, strength (light, standard, strong), and **Reasoning model** if it spends hidden thinking tokens. Presets suggest one.
4. **Save**, then **Check** to test the model catalog. This does not send a generation request; manual model IDs work when a provider has no compatible catalog.
5. Click **Add models** on the provider to add every other model the same key can use (see **Discover models** below), or add models by hand. A new connection can also **reuse a saved key** for the same host. Settings lists one collapsible row per provider with its models inside; each model's switch turns it on or off. Optionally assign planner and reviewer roles.
6. For a paid model, enter input/output prices per million tokens and enable **Allow paid API usage**. Prices are your estimates, not live billing data. Cloud connections without a key are flagged in Settings.

| Provider | Preset / protocol | Suggested profile |
| --- | --- | --- |
| Ollama | `http://localhost:11434/v1`, Chat Completions; pull a model yourself | General · light |
| vLLM / LM Studio | Your server URL (LM Studio is usually port 1234); Chat Completions | General · standard |
| OpenRouter | `https://openrouter.ai/api/v1`; Chat Completions | Set per model |
| OpenAI / GPT | `https://api.openai.com/v1`; Responses | Coding, Backend, Reasoning · strong |
| Anthropic / Claude | `https://api.anthropic.com/v1`; Anthropic Messages | Coding, Backend, Frontend, Reasoning · strong |
| Qwen / Alibaba Cloud | DashScope OpenAI-compatible API for your region | Coding, General · standard |
| OpenCode | OpenCode inference API; use a model available to external clients | Coding · standard |
| Google Gemini (AI Studio) | Gemini OpenAI-compatible API | Frontend, Design, General · standard |
| Other models | Any endpoint speaking one of the three protocols | Set per model |

Presets are editable. Provider eligibility, model names, free quotas, and prices change often. A ChatGPT or Claude chat subscription is not API access; connecting an API key uses API billing. **No API charge** means you have verified the endpoint is free for this usage. It does not stop a provider from billing you if it is set incorrectly.

</details>

<details>
<summary><b>Discover models</b></summary>

A key usually unlocks many models. **Add models** on a provider reads the provider's whole catalog and suggests a routing profile for each chat model:

- **OpenRouter:** free models only (price 0 or a `:free` ID). Audio models, safety filters, and router aliases are left out.
- **Gemini AI Studio:** text chat models from the native catalog. Image, speech, audio, live, embedding, robotics, and agent products and `-latest` aliases are left out.
- **Anthropic:** Claude 4.5 and later, with suggested list prices you can edit.
- **Other OpenAI-compatible servers:** whatever their `/models` lists.

Profiles come from the catalog itself: strength from size and family (e.g. 550B, Pro, Opus → strong; Mini, Haiku, small sizes, and Gemini 2.x Flash-Lite → light; Gemini 3 and later Flash-Lite → standard, since it writes at the level of earlier Flash models), thinking from what the provider reports, and coding skills only for models the provider describes as coding models. With **Check access first** (on by default), L.E.O. sends one tiny request per model and skips the ones your key cannot use, with the reason (not on your plan, rate-limited, timed out); a model that rejects an effort setting is kept with effort off. Selected models are added in one click, reusing the saved key.

</details>

<details>
<summary><b>Skills and routing profiles</b></summary>

**Settings → Skills** manages the skills the router matches on. Each skill has a name, a description (shown to the Deep-mode planner), keywords (matched as whole words), instructions added to the prompt whenever a model works on that skill, and an on/off switch. Six starter skills are created on first start; edit, disable, delete, or add your own, such as "Data analysis" with keywords `pandas, dataframe, chart`.

A connection with no skills is a **generalist**: it can take any task but loses ties to a specialist. Give strong-but-expensive models the **strong** tier so they are used only for strong work, and mark reasoning models so they are not used for simple questions.

Turn off **Match tasks to skills** in Settings → Routing to go back to fixed roles: the generator assignment and model choice setting decide.

</details>

<details>
<summary><b>Web research</b></summary>

HEAD searches only when a request needs fresh or outside information: explicit intent such as "search for", "latest", "news", "price of", "how much does", "sources", "cite", "according to", "weather", or a link in the message. Plain questions ("Make me a recipe for an egg sandwich") are answered from model knowledge, because reading pages is the most expensive part of a run. The 🌐 **Search** toggle in the message box forces research for one message.

- **Simple research:** L.E.O. searches with your words, reads the top pages (3 by default), and one model answers citing them as [n]. The search costs no model tokens.
- **Deep runs:** the planner can plan its own search tasks with up to three queries; only those tasks receive the page text.
- **Safety:** pages are fetched only from public addresses (never your local network or cloud metadata services, including after redirects), capped in size and time, stripped to readable text (about 8,000 characters per page), and passed to models as information, never as instructions.

Search uses your own [SearXNG](https://github.com/searxng/searxng) instance with JSON output enabled (`deploy/searxng/settings.yml`). `./start.sh` starts it automatically when it is installed from source in `<workspace>/searxng` (a `venv` with SearXNG's requirements plus its `src` checkout), or uses one already running on port 8888. With Docker: `docker compose up searxng`. Then set **Settings → Web search** to `http://localhost:8888` and click **Test search**.

</details>

<details>
<summary><b>Paid models as a backup (Claude)</b></summary>

Connect a paid API (for example Anthropic Claude: preset **Anthropic / Claude**, then **Models** to add Haiku, Sonnet, and Opus), enable **Allow paid API usage**, and set a **Max cost per run** (Settings → Routing). With **Auto**, a paid model is chosen when it fits the task better than the free ones, or when it is the cheapest fit (Haiku for light, Sonnet for standard, Opus for strong work). Choose **Free first** to treat paid models strictly as a backup: each step then tries up to three free models, then the paid model that fits, then the remaining free models. L.E.O. reserves the worst-case cost before every paid call and stops before the cap. Claude receives a thinking-effort setting by difficulty, refusals hand the step to the next model, and an empty provider balance is reported as such.

</details>

<details>
<summary><b>Modes, limits, and context</b></summary>

- **Auto** (default): the routing head picks one of the modes below.
- **Quick:** one generation call plus local syntax checks.
- **Reviewed:** generation → syntax checks → a reviewer's JSON verdict → bounded repair and re-review. An invalid verdict counts as unverified, never as a pass.
- **Deep:** a validated plan of 1–3 subtasks, run in dependency order (each routed by its own skill) → synthesis → review and repair.

Every call counts toward the per-run limits on calls, tokens, time, and estimated cost, including planning, review, repair, and failed attempts. Before each request L.E.O. reserves a conservative input estimate plus the output cap. Provider-reported usage replaces the estimate afterwards. Failed or interrupted calls keep their reservation because they may have been billed.

Older conversation turns are dropped as whole turns when a request would exceed the context allowance; the current request and system instructions are always kept. Oversized requests fail with a clear error.

</details>

<details>
<summary><b>Canvas (live preview)</b></summary>

When an answer contains an HTML page, its code streams into the canvas while it is written, then runs as a live preview. Separate CSS and JavaScript blocks are merged in. Use **Preview / Code**, **Desktop width** (widens the preview to 1,400 px, since most pages switch to a tablet layout below 900 px), **Run again**, **Open in a new tab**, or **Download**; **Open preview** on any earlier answer reopens it.

The page runs in a sandboxed frame without same-origin access, so its scripts cannot read L.E.O.'s cookies, storage, or API. A content security policy blocks network requests from the page. Only web pages run in the canvas; nothing generated runs on the server.

</details>

<details>
<summary><b>Usage and context tracking</b></summary>

- **Context meter** (next to the message box): the active chat's level and percentage, e.g. `Comfortable · 15.7% · 2.5K/16K`.
- **Per-chat panel** (header **Usage** button): context level meter, tokens, success rate, cost, and percentage breakdowns: input vs output, tokens by model, tokens by role, call outcomes (completed, in progress, uncertain), plus every call with its model.
- **Usage page** (`/usage/`): the same across all chats, plus chats by context level, a per-model table with share of tokens and success rate, and a log of recent calls showing routing and fallback.

Context levels: **Comfortable** under 50%, **Moderate** 50–69%, **High** 70–89%, **Near limit** 90% and above (older turns are being trimmed). Context is the latest completed call's input + output against the lower of the model's window and Settings → Context tokens.

</details>

<details>
<summary><b>Use L.E.O. from Claude Code (MCP)</b></summary>

L.E.O. includes an MCP server (stdio) that calls your running L.E.O. API, so other agents can hand work to your routed models:

```sh
claude mcp add leo -- uv run --directory /path/to/L.E.O python -m backend.mcp_server
```

| Tool | What it does |
| --- | --- |
| `leo_ask` | Send a request (mode `auto` by default); returns the answer, the route, the models used per stage, token usage, and a `conversation_id` to continue the chat |
| `leo_route` | Preview the routing decision without calling a model |
| `leo_usage` | Tokens, calls, cost, and context per model, overall or for one conversation |
| `leo_models` | List connections with their skills, tier, and reasoning flag |

Set `LEO_API_URL` if the API is not at `http://127.0.0.1:8000`, and `LEO_API_TOKEN` when the server requires a token. The MCP server adds no capability beyond the API.

</details>

<details>
<summary><b>Persistence and recovery</b></summary>

Local development uses SQLite with WAL in `.leo/`. PostgreSQL uses the same SQLAlchemy schema and is configured in Compose. The database holds connections, settings, skills, conversations, messages, runs, events, and per-call usage records.

The API enqueues runs; a separate worker claims one at a time with a renewable 30-second lease. After a crash, expired runs are reclaimed, completed stages are reused, interrupted calls are marked as such (their reservation still counts), and remaining stages continue. Stream event IDs support reconnect and replay. Closing the browser does not cancel a run; **Stop** does.

Provider calls are at-least-once: a crash during a request can lead to the request being sent again. This is not an exactly-once billing guarantee.

To run the processes separately:

```sh
uv run uvicorn backend.api:create_app --factory --host 127.0.0.1 --port 8000
# In another terminal, after the API has initialized the database:
uv run python -m backend.worker
```

Both must use the same database and encryption key. Back up the database **and** `.leo/secret.key` together (or keep `LEO_ENCRYPTION_KEY` in a secret manager); without the key, saved API keys cannot be read.

</details>

<details>
<summary><b>Docker / PostgreSQL</b></summary>

```sh
cp .env.example .env
# Set a strong LEO_DB_PASSWORD and, when needed, LEO_API_TOKEN.
docker compose up --build
```

Compose runs the API, the worker, and PostgreSQL, with port 8000 bound to the host's loopback interface. **This setup has not been run yet** (no Docker host was available); expect to smoke-test it. Inside containers, reach host Ollama at `http://host.docker.internal:11434/v1`. `.env` is read by Compose only; export variables in your shell for local Python commands. Settings are documented in `.env.example`.

</details>


## Security and execution boundaries

- **Single-owner workspace.** The dev launcher binds to loopback. The API refuses to start when `LEO_ALLOWED_HOSTS` names a non-loopback host and `LEO_API_TOKEN` is empty. For remote access, set `LEO_API_TOKEN`, exact `LEO_ALLOWED_ORIGINS` and `LEO_ALLOWED_HOSTS`, and put HTTPS in front. The token grants the whole workspace; there are no per-user accounts.
- **Keys.** Provider API keys are encrypted at rest (Fernet) and never returned by the API. Browser storage holds only the theme, panel state, and the selected conversation ID.
- **Origins.** Unexpected browser origins and hostnames are rejected. Arbitrary provider URLs, including local and private addresses, are an owner-only setting by design.
- **Generated code.** The server only parses Python and JSON for syntax; it never runs generated code, edits your files, runs shell commands, or deploys anything. The only thing that runs is a generated web page, in the browser canvas sandbox. Chat messages render model output as text, never as HTML in the app.

## Development

```sh
make check   # backend tests + frontend type check
make build   # build the frontend served by the API
```

The backend suite (96 tests) covers routing classification and ranking, web research and page-reading safety, model discovery, per-subtask routing, Auto mode, skills, the MCP server over stdio, protocol streaming and usage, fallback and cooldown, paid opt-in and limits, key encryption, origin/host/token checks, idempotent submission, cancellation, context trimming, review and repair, task dependencies, checkpoint recovery, and usage reporting. Tests use deterministic HTTP transports and never call real providers.

For manual browser testing without real models, run the QA fixture and connect it with model `leo-test-model` (local, no API charge), using a separate `LEO_DATA_DIR`:

```sh
uv run uvicorn tests.mock_provider:app --host 127.0.0.1 --port 9011
```

It returns clearly labeled fixture output, including a skill-labeled plan and a sample web page for the canvas. Set `LEO_MOCK_DELAY=0.35` to slow streaming down and watch the live view.

Interactive API docs are at `/api/docs` while the backend runs. The product requirements live in [`spec-driven.md`](spec-driven.md).

<details>
<summary><b>Project structure</b></summary>

| Path | Purpose |
| --- | --- |
| `backend/router.py` | HEAD: starter skills, classification, search cues, Auto mode, effort, model ranking |
| `backend/tools.py` | Web search (SearXNG), safe page reading, and verified photos from Wikimedia Commons |
| `backend/discovery.py` | Model discovery and automatic routing profiles |
| `backend/orchestrator.py` | Per-task routing, budgets, planning, subtasks, review, and repair |
| `backend/providers.py` | Chat Completions, Responses, and Anthropic streaming adapters |
| `backend/store.py` | Persistence, encrypted keys, skills, job leases, usage ledger and reports |
| `backend/api.py` | HTTP API, owner access, resumable event stream |
| `backend/worker.py` | Durable background worker and cancellation |
| `backend/context.py` | Context selection, plan validation, syntax checks |
| `backend/mcp_server.py` | MCP server for Claude Code and other clients |
| `frontend/src/scripts/chat.ts` | Chat, settings, skills, model discovery, live run view |
| `frontend/src/scripts/markdown.ts` | Safe Markdown rendering for answers (DOM only, no HTML) |
| `frontend/src/scripts/canvas.ts` | Sandboxed live preview of generated pages |
| `frontend/src/scripts/usage-ui.ts`, `usage.ts` | Context levels and percentage breakdowns |
| `benchmarks/` | Tasks with hidden tests, the comparison runner, the QA grader (`qa.py`), results |
| `tests/` | Backend test suite and the QA mock provider |
| `spec-driven.md` | Product requirements and technical specification |

</details>


## Roadmap

- Run the same QA loop against Claude (claude.ai) and on more task types: apps, research, writing.
- Make reviewed coding answers faster: a cheaper first review, and skipping review when static checks and the generator agree.
- Run acceptance tests in an isolated execution service, so review can rely on real tests instead of a model's reading.
- Learn routing from results: tune skills, tiers, effort, and Auto thresholds from benchmark outcomes instead of hand-set rules.
- More tools for HEAD to plan (code execution in a sandbox, files) and MCP client support, with explicit per-tool permissions.
- Database migrations, a Docker smoke test, and multi-user accounts before any shared hosting.

---

L.E.O. is a work in progress. Issues and suggestions are welcome. No license has been chosen yet, so all rights are reserved by the author until one is added.
