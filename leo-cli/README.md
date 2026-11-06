# L.E.O CLI

The multi-agent LLM Efficiency Orchestrator in your terminal. `leo` works on the folder you start it in: it plans
with a todo list, reads and edits files, runs commands and tests, and hands focused subtasks to sub-agents. Every step
is routed to the cheapest capable model you have connected.

It signs in to your L.E.O. server and downloads your models and API keys. After that it runs on your own machine: it
calls API-key models directly, and your code, prompts, and transcripts stay on your computer. Only usage totals (calls,
tokens, cost per model) go back to the server, for the Usage page.

**Subscription models** (ChatGPT, Copilot, and others you signed in to on the website) live on your own 9Router, which
only the L.E.O. server can reach. leo calls them **through your L.E.O. server**, so prompts for those models pass
through it on the way (it stores nothing). `/models` marks them "via your L.E.O. server". The router's key never leaves
the server; leo signs those calls with its device token.

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
L.E.O CLI with uv on first use (uv brings Python too), and runs it. **`leo update`** upgrades L.E.O CLI in place,
however you installed it.

## Sign in

Run `leo`. The first time, it shows the L.E.O welcome screen:

1. **Choose a theme** with the arrow keys and Enter: Dark, Light, High contrast, Terminal colours (your terminal's own
   16 colours), or No colour. A preview shows how the agent's output looks. Change it any time with `/theme`.
2. **Choose Sign in, Use another L.E.O. server, or Quit.** Sign in uses `http://localhost:8000` unless you pick
   another server (or pass `--server`).
3. **Sign in on the L.E.O. website**, which opens in your browser. Sign in or create an account (with a password or
   with Google or GitHub, as on the website), then click **Allow** for this device.
4. The browser comes back to `leo` and the session starts. Your models download to this computer, and API keys go in
   the OS keychain (Windows Credential Manager, macOS Keychain, or Secret Service).

If no browser can open (for example over SSH), `leo` prints the link; open it on any device, and `leo` carries on once
you click Allow.

In a session, `/login [server]` signs in again (another account or server) and `/logout` signs out. From a script,
use `leo login [--server URL]`, `leo sync` (leo also syncs on its own when the models are more than an hour old),
`leo models`, and `leo logout`. You can sign a lost device out under
**Settings → Account → CLI devices**. Keys that were already downloaded still work until you rotate them, so rotate
them for a lost laptop.

## Use

```bash
cd my-project
leo                                  # interactive session on this folder
leo ~/code/my-app                    # ...or on another folder
leo -p "fix the failing tests"       # one task; the answer goes to stdout, exit code 1 if it failed
leo -p "..." --autorun               # unattended: no questions, except for risky actions
leo --worktree                       # work in a new git worktree and branch, leaving your folder alone
leo resume                           # continue the last session on this folder (leo resume 2: the one before)
```

**Attach files with @.** In a message, `@path` attaches a file's current text (`fix the bug in @src/app.py`) or a
folder's listing (`@src/`), so the agent starts with it. Tab completes paths as you type after `@`. Paths are relative to
the folder leo works on; `~/...` and absolute paths work too, and quote a path with spaces: `@"my notes.md"`. A file
outside the folder is attached for reading only: the agent's own tools stay inside the folder.

**While the agent works:**
- Its answer appears as it is written, a paragraph at a time; the line being written shows in the status bar.
- The status bar shows a moving wave, what the agent is doing (thinking, running a tool, or writing), and for how long.
- Each tool call is one line, followed by a short result.
- Typing a message steers the agent.
- Esc stops the current task.
- Ctrl-C twice quits. Unfinished work keeps its progress, and `leo resume` continues it.

**Approvals.** By default, leo asks before it edits a file or runs a command. The question is a menu: choose with
the arrow keys and Enter, or press the option's number. Esc (or Ctrl-C) says no.

| Option | Meaning |
|---|---|
| 1. Yes | Yes, once. |
| 2. Yes, and allow … for the rest of this session | For commands, it remembers a prefix such as `npm test`; for edits, all edits. |
| 3. Yes, and always allow … in this project | The same, saved to `.leo-cli/settings.json`. |
| 4. No | The agent is told to do it differently. |

Keys pressed in the first moment after a question appears are ignored, so a message you were typing to steer the agent
cannot answer it. A remembered prefix such as `npm test` never covers a chained command such as
`npm test && rm -rf src`: every part has to be allowed. With `leo -p`, type the option's number and Enter.

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
| `/models [add]` | List your models, or add some (saved on your L.E.O. account). `/model` works too. |
| `/sync` | Fetch model changes made on the website. |
| `/cost` | Show tokens and cost for this session. |
| `/usage` | Calls, tokens, cost, and share of tokens for every model on this device; totals for this session, folder, and device. |
| `/context` | How full the agent's context window is: system prompt, your messages, replies, tool results, tool definitions. |
| `/mcp` | MCP servers: the connectors added in L.E.O Chat, and the ones set up for the CLI, with their tools. |
| `/login [server]` | Sign in in the browser, to another account or server. |
| `/logout` | Sign out and remove the synced models and keys. |
| `/theme [name]` | Change the colours: `dark`, `light`, `contrast`, `terminal`, or `mono`. |
| `/help` | List all commands. |
| `/exit` | Quit. |

**Skills and your own commands:**
- **Skills.** A skill is a folder with a `SKILL.md` (front matter `name` and `description`). Put it in
  `~/.config/leo-cli/skills/` (`%APPDATA%\leo-cli\skills\` on Windows) or in the project's `.leo-cli/skills/`.
  The agent loads a skill when the work needs it, and `/name args` asks for it directly.
- **Command files.** `.leo-cli/commands/<name>.md` holds a prompt template for `/name`. `$ARGUMENTS` is replaced with
  what you type after the command.

**Adding models.** `/models add` (or `leo models add`) has the ways in of Settings → Models:
- **Sign in with an account:** ChatGPT, GitHub Copilot, Kiro, Kimi, Kilo Code, Qoder, Grok CLI, or Z.ai GLM Coding. Your
  browser opens the sign-in page, and leo shows the code when the page asks for one. The account's models then come down.
  This needs a server started with Docker Compose, which runs the sign-ins; leo says so otherwise.
- **More models from a provider you already use:** picks up its saved key, so you type nothing again.
- **A provider with an API key:** OpenRouter, Gemini, OpenAI, Anthropic, Ollama, and others.

The model list is a checklist: type to filter, Space ticks, Enter adds the ticked ones (or the highlighted one). Models
known to call tools come first, because the agent needs them. To remove a model or turn it off, use the website.

**MCP servers.** `/mcp` lists two kinds:
- **From L.E.O Chat:** the connectors under Settings → Connectors on the website. Sync brings their names, state, and
  tools down; their sign-ins and tokens stay on the server, which makes each call for leo. Tools denied there stay off.
- **Set up for the CLI:** servers in `~/.config/leo-cli/mcp.json` (yours everywhere), `.leo-cli/mcp.json`, or
  `.mcp.json` (this project; it wins on a name clash). Local servers start in the project folder when the first task
  begins and run until leo closes; `/mcp` checks each one and lists its tools.

```json
{"mcpServers": {
  "files":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
  "linear": {"url": "https://mcp.linear.app/mcp", "headers": {"Authorization": "Bearer ..."}}
}}
```

The agent gets the tools of every working server, named `<server>__<tool>`. A tool that only reads (or that L.E.O Chat
allows) runs freely; the others ask first, with the menu above, and "this session" or "always in this project"
remembers that tool. A server that fails to start is left out; `/mcp` says why.

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
