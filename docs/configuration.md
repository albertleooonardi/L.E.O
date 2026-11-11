# Configuration and operations

[← Back to README](../README.md) · [CLI guide](../leo-cli/README.md)

Start with the README for installation and the connection flow. This guide covers deployment settings,
accounts, providers, tools, limits, and data protection.

## Security

- **Accounts and access.** Registration is off by default: only the first local user, or an admin, can create accounts
  (`LEO_ALLOW_REGISTRATION`). `LEO_ALLOWED_EMAILS` limits OAuth sign-in. `LEO_API_TOKEN` is an admin token for scripts.
  The API refuses to start when `LEO_ALLOWED_HOSTS` names a non-loopback host and no token is set. For remote access,
  set exact `LEO_ALLOWED_ORIGINS` and `LEO_ALLOWED_HOSTS` and put HTTPS in front.
- **Keys.** Saved model credentials, connector tokens, and managed router passwords are encrypted at rest (Fernet).
  Managed providers' upstream keys stay in 9Router; CLI requests use a device token. Direct-provider keys can sync to
  an authenticated CLI, which stores its encryption key in the OS keychain. Back up the database **and**
  `.leo/secret.key` (or `LEO_ENCRYPTION_KEY`) together. Preserve managed router volumes for their provider credentials.
- **Untrusted content.** Web pages, search results, photo titles, MCP results, and model output are passed to models as
  untrusted data in prompts. Pages are fetched only from public addresses, including after redirects.
- **Generated code.** In Chat the server only parses Python and JSON. A generated page runs in a sandboxed frame without
  same-origin access, with a content security policy that blocks network requests. Messages render model output as
  text, never as HTML.
- **Agent sandboxes (server).** One container per workspace, non-root, every capability dropped, `no-new-privileges`,
  CPU, memory, and process limits, on an internal network whose only exit is an egress proxy for package registries.
  Git tokens stay outside the sandbox, and a push needs your approval. Set `LEO_SANDBOX_RUNTIME=runsc` for gVisor.
- **L.E.O.C.O.D.E (your machine).** There is no sandbox: the agent works as you. Paths are confined to the project,
  edits and commands ask first, and `sudo`, `rm -rf /` or `~`, `curl | sh`, `git push`, and skill installs always ask,
  even with `--autorun`.

## Configuration reference

<details>
<summary><b>Accounts and sign-in</b></summary>

The sign-in page offers **Continue with Google**, **GitHub**, or any OpenID Connect provider (Keycloak, Authentik,
Microsoft Entra, Auth0, ...), plus username and password. A provider's button appears once its client ID and secret are
in `.env` (see `.env.example`). Register this callback URL with each provider:
`<LEO_PUBLIC_URL>/api/auth/oauth/<google|github|oidc>/callback`.

- **Who may join:** `LEO_ALLOWED_EMAILS` lists exact emails and `@domain` entries, checked on every OAuth sign-in. Left
  empty, OAuth signs in existing accounts and creates only the very first one.
- **Same person, several providers:** a verified email that matches an existing account lands on that account.
- **Settings → Account:** link or unlink providers, set or change a password, manage model accounts, and sign out lost
  CLI devices. You cannot unlink your last way to sign in.

</details>

<details>
<summary><b>Connect models</b></summary>

**Sign in with an account (Docker Compose).** **Settings → Models → Connect provider → Sign in** offers account sign-in: ChatGPT (Codex),
GitHub Copilot, Kiro, Kimi, Kilo Code, Qoder, Grok CLI, and Z.ai GLM Coding. Behind it, each user gets their own
[9Router](https://github.com/decolua/9router) container (pinned, no host port), which keeps and refreshes the tokens
and serves the models. Routers are per user because a 9Router key reaches every account signed in on that router.
9Router warns that ChatGPT (Codex), GitHub Copilot, and Kiro do not license subscription sign-in for router use and may
restrict such accounts; L.E.O. asks you to accept that first. Claude subscription sign-in is not offered (Anthropic
only allows those tokens in its own apps), so use a Claude API key.

**Or use an API key (Docker Compose):** **Connect provider → API key**, choose your provider, and paste the key.
9Router validates and stores the upstream credential; L.E.O. uses a separate router key for model calls. Choose models
from the searchable catalog; OpenRouter starts with **Free only** selected. Providers appear once, with their enabled
models inside. **Refresh** checks router connectivity; it does not make a generation request. Existing direct providers
can use **Move to 9Router**, preserving their model settings and usage history. Connections using different keys for
the same provider stay direct until connected individually. **Local or custom server** keeps manual setup available.
Use a model's **Edit** action to adjust its routing profile or prices. Paid models also need **Routing → Allow paid API usage**.

| Provider | Preset / protocol | Suggested profile |
| --- | --- | --- |
| Ollama | `http://localhost:11434/v1`, Chat Completions | General · light |
| vLLM / LM Studio | Your server URL (LM Studio is usually port 1234) | General · standard |
| 9Router | Managed per user by Compose; outside Docker `http://localhost:20128/v1` | Set per model |
| OpenRouter | `https://openrouter.ai/api/v1` | Set per model |
| OpenAI / GPT | `https://api.openai.com/v1`; Responses | Coding, Backend, Reasoning · strong |
| Anthropic / Claude | `https://api.anthropic.com/v1`; Anthropic Messages | Coding, Backend, Frontend, Reasoning · strong |
| Qwen / Alibaba Cloud | DashScope OpenAI-compatible API | Coding, General · standard |
| OpenCode | OpenCode inference API | Coding · standard |
| Google Gemini (AI Studio) | Gemini OpenAI-compatible API | Frontend, Design, General · standard |

A ChatGPT or Claude chat subscription is not API access; an API key uses API billing. **No API charge** means you have
verified the endpoint is free for this usage.

</details>

<details>
<summary><b>Discover models</b></summary>

**Add models** reads a provider's whole catalog and suggests a profile for each chat model: OpenRouter's free models,
Gemini's text chat models, Claude 4.5 and later, or whatever an OpenAI-compatible `/models` lists. Strength comes from
size and family (550B, Pro, Opus → strong; Mini, Haiku, small sizes → light). Thinking comes from what the provider
reports, and coding skills only from models the provider describes as coding models. **Check access first** sends one
tiny request per model and skips those your key cannot use, with the reason.

</details>

<details>
<summary><b>Skills</b></summary>

**Settings → Skills** manages what HEAD matches on. Each skill has a name, a description (shown to the Deep-mode
planner), keywords (whole words), instructions added to the prompt whenever a model works on it, and a switch. Seven
starter skills are created on first start: General, Frontend, Backend, Coding, Design, Research, and Reasoning. Edit,
disable, or delete them, or add your own, such as "Data analysis" with `pandas, dataframe, chart`. A model with no skills is a **generalist**: it
can take any task but loses ties to a specialist. Turn off **Match tasks to skills** to go back to fixed roles.

</details>

<details>
<summary><b>Web research and photos</b></summary>

- **Simple research:** L.E.O. searches your SearXNG with your words, reads the top pages (3 by default, about 8,000
  characters each), and one model answers citing them as [n]. The search costs no model tokens.
- **Deep runs:** the planner can give a task its own search with up to three queries; only that task gets the text.
- **The 🌐 Search toggle** forces research for one message.
- **Photos for pages:** for a page build, L.E.O. searches Wikimedia Commons for the page's topic. It keeps JPEG photos
  at least 1,000 px wide, landscape, under a CC or public-domain licence, from Wikimedia's own host, that load when
  checked. Up to four go to the model with their author and licence; it must credit what it uses and may not invent
  other image URLs.

SearXNG needs JSON output (`deploy/searxng/settings.yml`). `./start.sh` starts it from `<workspace>/searxng` when
installed there, or uses one on port 8888; with Docker, `docker compose up searxng`. Then set **Settings → Web search**
to `http://localhost:8888` and click **Test search**.

</details>

<details>
<summary><b>Limits and defaults</b></summary>

| Setting | Chat runs | Agent runs |
| --- | --- | --- |
| Model calls | 8 | 200 |
| Tokens | 200,000 | 4,000,000 |
| Estimated cost | $0.10 | $1.00 |
| Time | 10 min | 2 h (from the last approval) |
| Repairs | 1 | — |
| Steps | — | 80 (lead), 40 (sub-agent) |
| Sub-agents at once | — | 3 |

Every call counts, including planning, review, repair, and failed attempts. Context is capped at 64,000 tokens and
output at 8,192 per call by default (Settings → Limits). Agent runs that reach a limit ask for another allowance of the
same size instead of failing.

</details>

<details>
<summary><b>Canvas, usage, and context</b></summary>

- **Canvas:** an HTML answer streams into the canvas while it is written, then runs as a live preview with **Preview /
  Code**, **Desktop width** (1,400 px), **Run again**, **Open in a new tab**, and **Download**.
- **Context meter** next to the message box, e.g. `Comfortable · 3% · 3.8K/128K`. Levels: Comfortable under 50%,
  Moderate 50–69%, High 70–89%, Near limit 90% and above (older turns are being trimmed).
- **Per-chat Usage panel** and the **Usage page** (`/usage/`): tokens, success rate, cost, and shares by model, role,
  and outcome, with every call and its model.

</details>

<details>
<summary><b>Use L.E.O. from Claude Code (MCP)</b></summary>

```bash
claude mcp add leo -- uv run --directory /path/to/L.E.O python -m backend.mcp_server
```

| Tool | What it does |
| --- | --- |
| `leo_ask` | Send a request (mode `auto` by default); returns the answer, the route, the models per stage, usage, and a `conversation_id` to continue |
| `leo_route` | Preview the routing decision without calling a model |
| `leo_usage` | Tokens, calls, cost, and context per model, overall or for one conversation |
| `leo_models` | List connections with their skills, tier, and reasoning flag |

Set `LEO_API_URL` if the API is not at `http://127.0.0.1:8000`, and `LEO_API_TOKEN` when the server requires a token.

</details>

<details>
<summary><b>Connectors (remote MCP servers in Chat)</b></summary>

1. **Settings → Connectors:** add an MCP server by URL (Streamable HTTP), then sign in (OAuth with automatic client
   registration) or paste a token.
2. **Per chat:** the composer's **Connectors** menu chooses which ones a chat may use.
3. **Who calls tools:** only the sub-agent writing the answer, or in Deep mode the tasks the planner assigned a connector
   to. Planner and reviewer never call tools.
4. **Permissions:** every tool starts at **Ask** (Allow once, Always allow, Deny). Completed tool results are reused on resume; provider requests interrupted mid-call can be retried.
5. **Addresses** must be public HTTPS unless `LEO_MCP_ALLOW_PRIVATE=1`.

</details>

<details>
<summary><b>Agent mode on the server</b></summary>

Agent work has moved to L.E.O.C.O.D.E, so the web app hides it unless the server sets `LEO_SERVER_AGENT=1` (with
Docker Compose). Then **Agent** in the composer gives the chat a Linux sandbox: start empty, upload a `.zip`, or clone a
repository. Browse files in the **Files** panel, download a `.zip`, and approve every `git_push`. The server's agent is
flat (a lead agent with sub-agents); the tree is used by L.E.O.C.O.D.E.

</details>

<details>
<summary><b>Run the processes separately, and Docker</b></summary>

```bash
uv run uvicorn backend.api:create_app --factory --host 127.0.0.1 --port 8000
uv run python -m backend.worker      # in another terminal, after the API has created the database
```

Both must use the same database and encryption key. Compose runs the API, the worker, PostgreSQL, SearXNG, `sandboxd`,
and the egress proxy, with ports bound to loopback (8000 for L.E.O., 8888 for SearXNG, 1455 for the ChatGPT sign-in
redirect). In **Settings → Web search** use `http://searxng:8080`, and reach host Ollama at
`http://host.docker.internal:11434/v1`. Every setting is documented in [`.env.example`](../.env.example).

</details>
