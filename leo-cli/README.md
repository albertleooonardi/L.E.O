# L.E.O.C.O.D.E

The multi-agent LLM Efficiency Orchestrator in your terminal. `leocode` (or just `leo`) works on the folder you start it
in: it plans with a todo list, reads and edits files, runs commands and tests, and splits big tasks down a tree of
agents, where your strongest model directs and reviews, and your cheaper models do the volume.

It signs in to your L.E.O. server and syncs your models, then runs the agent on your own machine.
**Providers connected through 9Router**, including API-key providers and subscription accounts, run through your
L.E.O. server. The upstream credentials and router key stay on the server; the CLI signs requests with its device token.
Prompts for these models pass through the server, which does not store them. `/models` labels these providers **9Router**.
Local or custom endpoints and older direct connections call their provider directly; their synced keys are encrypted
locally with a key in the OS keychain. Transcripts stay on this computer. Usage totals go back to the server's Usage page.

## Install

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). On Windows you also need
[Git for Windows](https://git-scm.com/download/win), because the agent runs its commands in Git Bash.

```bash
npm install -g leo-orchestrator    # or try it first: npx leo-orchestrator
# or with uv directly:
uv tool install "git+https://github.com/albertleooonardi/L.E.O#subdirectory=leo-cli"
uv tool install ./leo-cli          # from a checkout of this repository
```

The npm package is a small launcher: it installs [uv](https://docs.astral.sh/uv/) if you do not have it, installs
L.E.O.C.O.D.E with uv on first use (uv brings Python too), and runs it. **`leocode update`** upgrades L.E.O.C.O.D.E in place,
however you installed it.

## Sign in

Run `leocode`. The first time, it shows the L.E.O welcome screen:

1. **Choose Sign in.** It uses `http://localhost:8000`; choose **Use another L.E.O. server** or pass `--server` to change it.
   Arrow keys move, Enter selects, and Esc goes back. **Appearance** is optional; `/theme` changes it later.
2. **Sign in on the L.E.O. website**, which opens in your browser. Sign in or create an account (with a password or
   with Google or GitHub, as on the website), then click **Allow** for this device.
3. The browser comes back to `leocode`. A compact welcome shows your project, model readiness, and approval mode.
   Provider keys for 9Router models stay on the server.

Choose Dark, Light, High contrast, Terminal colours, or No colour with `/theme`. `--no-color` and the `NO_COLOR`
environment variable select monochrome output without changing your saved theme.

If no browser can open (for example over SSH), `leocode` prints the link; open it on any device, and `leocode` carries on once
you click Allow.

In a session, `/login [server]` signs in again (another account or server) and `/logout` signs out. From a script,
use `leocode login [--server URL]`, `leocode sync` (leocode also syncs on its own when the models are more than an hour old),
`leocode models`, and `leocode logout`. You can sign a lost device out under
**Settings → Account → CLI devices**. Keys that were already downloaded still work until you rotate them, so rotate
them for a lost laptop.

## Use

```bash
cd my-project
leocode                                  # interactive session on this folder
leocode ~/code/my-app                    # ...or on another folder
leocode -p "fix the failing tests"       # one task; the answer goes to stdout, exit code 1 if it failed
leocode -p "..." --autorun               # unattended: no questions, except for risky actions
leocode --worktree                       # work in a new git worktree and branch, leaving your folder alone
leocode resume                           # continue the last session on this folder (leocode resume 2: the one before)
```

**Attach files with @.** In a message, `@path` attaches a file's current text (`fix the bug in @src/app.py`) or a
folder's listing (`@src/`), so the agent starts with it. Tab completes paths as you type after `@`. Paths are relative to
the folder leocode works on; `~/...` and absolute paths work too, and quote a path with spaces: `@"my notes.md"`. A file
outside the folder is attached for reading only: the agent's own tools stay inside the folder.

**While the agent works:**
- Its answer appears as it is written, a paragraph at a time; the line being written shows in the status bar.
- The status bar shows a moving wave, what the agent is doing (thinking, running a tool, or writing), and for how long.
- Each tool call is one line, followed by a short result.
- Typing a message steers the agent.
- Esc stops the current task.
- Ctrl-C twice quits. Unfinished work keeps its progress, and `leocode resume` continues it.

**Approvals.** By default, leocode asks before it edits a file or runs a command. The question is a menu: choose with
the arrow keys and Enter, or press the option's number. Esc (or Ctrl-C) says no.

| Option | Meaning |
|---|---|
| 1. Yes | Yes, once. |
| 2. Yes, and allow … for the rest of this session | For commands, it remembers a prefix such as `npm test`; for edits, all edits. |
| 3. Yes, and always allow … in this project | The same, saved to `.leo-cli/settings.json`. |
| 4. No | The agent is told to do it differently. |

Keys pressed in the first moment after a question appears are ignored, so a message you were typing to steer the agent
cannot answer it. A remembered prefix such as `npm test` never covers a chained command such as
`npm test && rm -rf src`: every part has to be allowed. With `leocode -p`, type the option's number and Enter.

Reading files and searching never ask. `--autorun` (or `/autorun on`) stops the questions, but these always ask, even
with autorun, and are never remembered:
- `sudo`;
- `rm -rf` of `/` or your home folder;
- piping `curl` or `wget` into a shell;
- `git push`;
- installing skills.

**Commands in a session.** Type `/` to see them as you type; Tab completes a command, your skills, your command
files, and `@` paths. The status bar at the bottom shows every session, what the current one is doing, and the keys that work now.

| Command | What it does |
|---|---|
| `/new [--worktree]` | Start another session. Several sessions can run at once. |
| `/sessions` | List the sessions in this window. |
| `/switch N` | Show session N. |
| `/resume [N]` | List earlier sessions on this folder, or reopen one. |
| `/cd PATH` | Work on another folder in this window (its own rules, skills, and sessions). |
| `/clear` | Start over in this session. |
| `/autorun on\|off [--save]` | Turn the approval questions off or on. |
| `/models [add]` | List models by provider, or connect one. `/model` works too. `/models gemini` filters the list. |
| `/sync` | Fetch model changes made on the website. |
| `/cost` | Show tokens and cost for this session. |
| `/tree` | Draw the agents of the latest task as an ASCII tree. |
| `/usage` | Calls, tokens, cost, and share of tokens for every model on this device; totals for this session, folder, and device. |
| `/context` | How full the agent's context window is: system prompt, your messages, replies, tool results, tool definitions. |
| `/mcp` | MCP servers: the connectors added in L.E.O Chat, and the ones set up for the CLI, with their tools. |
| `/login [server]` | Sign in in the browser, to another account or server. |
| `/logout` | Sign out and remove the synced models and keys. |
| `/theme [name]` | Change the colours: `dark`, `light`, `contrast`, `terminal`, or `mono`. |
| `/help [search]` | List commands, or filter them, for example `/help model`. |
| `/exit` | Quit. |

**Skills and your own commands:**
- **Skills.** A skill is a folder with a `SKILL.md` (front matter `name` and `description`). Put it in
  `~/.config/leo-cli/skills/` (`%APPDATA%\leo-cli\skills\` on Windows) or in the project's `.leo-cli/skills/`.
  The agent loads a skill when the work needs it, and `/name args` asks for it directly.
- **Command files.** `.leo-cli/commands/<name>.md` holds a prompt template for `/name`. `$ARGUMENTS` is replaced with
  what you type after the command.

**Adding models.** `/models add` (or `leocode models add`) shares the connections in Settings → Models:
- **Sign in with an account:** ChatGPT, GitHub Copilot, Kiro, Kimi, Kilo Code, Qoder, Grok CLI, or Z.ai GLM Coding. Your
  browser opens the sign-in page, and leocode shows the code when the page asks for one. The account's models then come down.
  This needs a server started with Docker Compose, which runs the sign-ins; leocode says so otherwise.
- **Connected providers:** choose more models without entering a key again.
- **Connect with an API key:** choose OpenRouter, Gemini, OpenAI, Anthropic, DeepSeek, Groq, Mistral, xAI, or Cohere.
  9Router validates and stores the key. OpenRouter offers free models first; paid models are available under **All models**.
- **Local or custom server:** Ollama, vLLM / LM Studio, or a custom endpoint. Servers without 9Router can still use these.
  Older L.E.O. servers keep the previous direct-provider setup flow.

The model list is a checklist: type to filter, Space selects, and Enter adds your selection (or the highlighted model).
Ctrl-A selects or clears visible models, Ctrl-U clears the search, and Esc goes back. Selections survive filtering;
add up to 60 models at a time. Models known to call tools come first. `/models` shows tool support as **yes**, **no**,
or **untested**, and switches to a stacked layout in narrow terminals. Use `/models <search>` or `leocode models <search>`
to filter by model, provider, or skill. To remove a model or turn it off, use the website.

**MCP servers.** `/mcp` lists two kinds:
- **From L.E.O Chat:** the connectors under Settings → Connectors on the website. Sync brings their names, state, and
  tools down; their sign-ins and tokens stay on the server, which makes each call for leocode. Tools denied there stay off.
- **Set up for the CLI:** servers in `~/.config/leo-cli/mcp.json` (yours everywhere), `.leo-cli/mcp.json`, or
  `.mcp.json` (this project; it wins on a name clash). Local servers start in the project folder when the first task
  begins and run until leocode closes; `/mcp` checks each one and lists its tools.

```json
{"mcpServers": {
  "files":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
  "linear": {"url": "https://mcp.linear.app/mcp", "headers": {"Authorization": "Bearer ..."}}
}}
```

The agent gets the tools of every working server, named `<server>__<tool>`. A tool that only reads (or that L.E.O Chat
allows) runs freely; the others ask first, with the menu above, and "this session" or "always in this project"
remembers that tool. A server that fails to start is left out; `/mcp` says why.

## How the tree of agents works

Your models have a strength: strong, standard, or light (Settings → Models). Each level of the tree uses one strength, so
a tree has at most 3 levels. A strength you have no model for is skipped.

| Agent | Model | What it does |
|---|---|---|
| Root | strongest | Reads the project and decides: answer, give an easy task to one small worker, or split the task. Reviews, and writes the final answer. |
| Manager | middle | Only on 3 levels, for a subtask the root thinks is too large for one worker. Splits it and reviews its workers. |
| Worker | weakest on its branch, or the cheapest level for small work | The only agents that edit files and run commands. |

The root works alone only if you have one strength of model, or too few requests are left today for a tree. Strengths are
set per model (Settings → Models); `/models rerank` ranks the models of signed-in accounts again with the current rules.

**Review.** Each parent reviews each child, on evidence leocode collects itself (files changed, commands run, and their exit
codes), not only on the child's report. Rejected work gets one more try with the feedback, then one try on a stronger
model. After that it counts as failed, and the final answer ends with a **Not finished** list.

**Limits.** A run has at most 12 agents below the root, and at most 3 workers run at once (`agent_parallel`). Your budgets
apply to the whole tree. When a provider says an account's limit is used up (such as OpenRouter's free requests a day, or a
ChatGPT plan), leocode skips all that account's models until it lifts, and counts its requests from then on, so the tree
stays small when few are left. If no model can write the last message, the work is kept and the answer says what was done. An approval from any worker pauses the whole tree, and answering resumes it.

**Seeing it.** While the agent works, the tree shows above the prompt, cut down to fit your terminal. When a task that split
ends, it is drawn as an ASCII graph, with each agent's state, model, tokens, calls, and cost. `/tree` draws the latest one
at any time, and `/usage` starts with it; if the root did the task alone, they say so. `leocode -p` prints it to stderr.

```
                +--------------------------+
                | ROOT done                |
                | Add login and tests      |
                | dots-3-note-preview      |
                | 4.2K 4 calls free        |
                +--------------------------+
                             |
              +--------------+--------------+
              |                             |
+--------------------------+ +--------------------------+
| WORKER accepted          | | WORKER retrying try 2/3  |
| Login route              | | Tests for login          |
| north-mini-code          | | north-mini-code          |
| 1.1K 2 calls free        | | 900 2 calls free         |
+--------------------------+ +--------------------------+
```

## Where things are stored

| What | Where |
|---|---|
| Settings, sessions, and synced models | `~/.config/leo-cli/` (`%APPDATA%\leo-cli\` on Windows). Set `LEO_CLI_HOME` to change it. |
| Per-project rules, skills, commands, and worktrees | `.leo-cli/` in the project; add it to `.gitignore` if you like. |

**Safety.** There is no sandbox: the agent works as you, on your files. Paths stay inside the project folder, and links
that point out of it are refused. The approval questions are the safety layer, so keep autorun for projects you trust.

## Development

From the repository root, `uv run --frozen python -m pytest leo-cli/tests` runs the CLI's tests. The CLI reuses the
server's engine (`backend/`) in-process: routing, the agent loop, budgets, and checkpoints. `leo_cli/workspace.py`
replaces the Docker sandbox with your folder.

Windows notes:
- Commands run in Git Bash, found next to `git.exe`. Set `LEO_CLI_BASH` to use another `bash.exe`.
- Use Windows Terminal or PowerShell for the interactive prompt; the mintty window of Git Bash is not a full console.
